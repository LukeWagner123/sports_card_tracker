"""SportsCardsPro price guide API (same API as PriceCharting).

Docs: https://www.pricecharting.com/api-documentation
- Current values only: "Historic prices and historic sales are not supported." Daily snapshots
  are how history accumulates.
- Storing results is allowed, but "All data gathered from the API or CSV must be purged after
  your subscription has ended" -> `purge()`.
- Internal use only; sharing the data with third parties needs a commercial license.
- Max 1 call/second.
"""

import os
import re
import sqlite3
from datetime import date

from cardtracker.db import store_raw
from cardtracker.fetcher import Fetcher
from cardtracker.names import last_name
from cardtracker.runs import track_run

SOURCE = "sportscardspro"
API = "https://www.sportscardspro.com/api"

# Price fields for cards -> (grader, grade). "ANY" = graded by any company. Prices are in cents.
FIELDS = {
    "loose-price": ("RAW", None),
    "cib-price": ("ANY", 7.0),
    "new-price": ("ANY", 8.0),
    "graded-price": ("ANY", 9.0),
    "box-only-price": ("ANY", 9.5),
    "manual-only-price": ("PSA", 10.0),
    "bgs-10-price": ("BGS", 10.0),
    "condition-17-price": ("CGC", 10.0),
    "condition-18-price": ("SGC", 10.0),
}


class SportsCardsProClient:
    def __init__(self, token: str | None = None, fetcher: Fetcher | None = None):
        self.token = token or os.environ.get("PRICECHARTING_TOKEN")
        if not self.token:
            raise RuntimeError("PRICECHARTING_TOKEN is not set")
        self.fetcher = fetcher or Fetcher(min_interval=1.1, check_robots=False)

    def _get(self, path: str, **params) -> dict:
        body = self.fetcher.get(f"{API}/{path}", params={"t": self.token, **params}).json()
        if body.get("status") != "success":
            raise RuntimeError(f"sportscardspro {path}: {body.get('error-message', body)}")
        return body

    def product(self, product_id: str) -> dict:
        return self._get("product", id=product_id)

    def search(self, query: str) -> list[dict]:
        return self._get("products", q=query)["products"]


def parse_prices(product: dict) -> list[tuple[str, float | None, float]]:
    """[(grader, grade, price_usd)] for every non-zero card price field."""
    out = []
    for field, (grader, grade) in FIELDS.items():
        cents = product.get(field)
        if cents:
            out.append((grader, grade, cents / 100))
    return out


def _bracket(name: str) -> str | None:
    """Parallel name from 'Player [Silver Prizm] #280'; tolerates a missing ']' in source data."""
    m = re.search(r"\[([^\]#]*)", name)
    return m.group(1).strip().lower() if m else None


def best_match(products: list[dict], year: int, set_name: str, number: str,
               parallel: str, player: str) -> dict | None:
    """Pick the product for an exact card; None unless exactly one candidate fits.

    Product names look like 'Luka Doncic #280' or 'Luka Doncic [Silver Prizm] #280';
    console names like 'Basketball Cards 2018 Panini Prizm'.
    """
    number = number.lstrip("#")
    surname = last_name(player).lower()
    want_parallel = None if parallel.lower() == "base" else parallel.lower()
    fits = []
    for p in products:
        name, console = p.get("product-name", ""), p.get("console-name", "")
        if not re.search(rf"#{re.escape(number)}\b", name):
            continue
        # Console names are '<Sport> Cards <year> <manufacturer> <set>'; requiring the set at the
        # end keeps 'Prizm' from matching 'Prizm Draft Picks' or '... Prizm Redemption'.
        if surname not in name.lower() or str(year) not in console:
            continue
        if not console.lower().endswith(" " + set_name.lower()):
            continue
        bracket = _bracket(name)
        if want_parallel is None and bracket is not None:
            continue
        if want_parallel is not None and (bracket is None or want_parallel not in bracket):
            continue
        fits.append(p)
    return fits[0] if len(fits) == 1 else None


def link_watchlist(conn: sqlite3.Connection, client: SportsCardsProClient) -> tuple[int, list[str]]:
    """Find the SportsCardsPro product for each unlinked watchlist card.

    Returns (linked count, cards that could not be matched unambiguously)."""
    rows = conn.execute(
        "SELECT c.id, c.year, c.set_name, c.card_number, c.parallel, p.name AS player "
        "FROM watchlist w JOIN cards c ON c.id = w.card_id JOIN players p ON p.id = c.player_id "
        "WHERE NOT EXISTS (SELECT 1 FROM card_external_ids x WHERE x.card_id = c.id AND x.source = ?)",
        (SOURCE,),
    ).fetchall()
    linked, unmatched = 0, []
    for r in rows:
        query = f"{r['player']} {r['year']} {r['set_name']} {r['card_number']}"
        match = best_match(client.search(query), r["year"], r["set_name"], r["card_number"],
                           r["parallel"], r["player"])
        if not match:
            unmatched.append(query)
            continue
        conn.execute("INSERT INTO card_external_ids (card_id, source, external_id) VALUES (?, ?, ?)",
                     (r["id"], SOURCE, match["id"]))
        linked += 1
    conn.commit()
    return linked, unmatched


def snapshot(conn: sqlite3.Connection, client: SportsCardsProClient, today: date | None = None) -> int:
    """Record today's values for every linked card. Safe to re-run the same day."""
    day = (today or date.today()).isoformat()
    links = conn.execute("SELECT card_id, external_id FROM card_external_ids WHERE source = ?",
                         (SOURCE,)).fetchall()
    with track_run(conn, f"{SOURCE}:snapshot") as run:
        for link in links:
            product = client.product(link["external_id"])
            store_raw(conn, SOURCE, link["external_id"], product)
            volume = int(product["sales-volume"]) if str(product.get("sales-volume", "")).isdigit() else None
            # Replace the card's snapshot for the day. (An upsert would miss RAW rows: their grade
            # is NULL, and SQLite never treats NULLs as conflicting in a UNIQUE constraint.)
            conn.execute("DELETE FROM price_snapshots WHERE card_id = ? AND snapshot_date = ? AND source = ?",
                         (link["card_id"], day, SOURCE))
            for grader, grade, price in parse_prices(product):
                conn.execute(
                    "INSERT INTO price_snapshots (card_id, grader, grade, price_usd, sales_volume, "
                    "snapshot_date, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (link["card_id"], grader, grade, price, volume, day, SOURCE),
                )
                run.rows_written += 1
            conn.commit()
    return run.rows_written


def purge(conn: sqlite3.Connection) -> dict[str, int]:
    """Delete everything obtained from SportsCardsPro. Required when the subscription ends."""
    counts = {
        "price_snapshots": conn.execute("DELETE FROM price_snapshots WHERE source = ?", (SOURCE,)).rowcount,
        "raw_records": conn.execute("DELETE FROM raw_records WHERE source = ?", (SOURCE,)).rowcount,
        "card_external_ids": conn.execute("DELETE FROM card_external_ids WHERE source = ?", (SOURCE,)).rowcount,
    }
    conn.commit()
    return counts
