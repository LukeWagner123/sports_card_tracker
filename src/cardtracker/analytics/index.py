"""Price indices: robust per-card prices and per-player baskets."""

import sqlite3
from statistics import median


def card_price(conn: sqlite3.Connection, card_id: int, grader: str, grade: float,
               since: str, until: str) -> tuple[float | None, int]:
    """Median sale price for a card+grade in [since, until). Returns (price, sale_count).

    Median rather than mean: single sales are noisy and outliers (shill bids,
    mislabeled lots) are common. sale_count doubles as a confidence measure.
    """
    rows = conn.execute(
        "SELECT price_usd FROM sales WHERE card_id = ? AND grader = ? AND grade = ? "
        "AND sold_at >= ? AND sold_at < ?",
        (card_id, grader, grade, since, until),
    ).fetchall()
    prices = [r["price_usd"] for r in rows]
    return (median(prices) if prices else None), len(prices)


def player_index(conn: sqlite3.Connection, player_id: int, grader: str, grade: float,
                 since: str, until: str, min_sales: int = 3) -> float | None:
    """Sales-weighted average of median card prices across a player's cards.

    Cards with fewer than min_sales in the window are excluded as too illiquid to price.
    """
    card_ids = [r["id"] for r in conn.execute("SELECT id FROM cards WHERE player_id = ?", (player_id,))]
    weighted, total = 0.0, 0
    for card_id in card_ids:
        price, n = card_price(conn, card_id, grader, grade, since, until)
        if price is None or n < min_sales:
            continue
        weighted += price * n
        total += n
    return weighted / total if total else None
