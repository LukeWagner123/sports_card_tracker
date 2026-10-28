"""Compare cards on current price-guide values: grade multiples, liquidity, and price vs. performance.

Uses the latest price_snapshots day from one source. "Cheap vs. performance" fits
log(PSA 10 price) against a standardized performance score across players of one sport
(analytics.valuation.fit_residuals); a negative residual means cheaper than peers who
performed similarly. It ignores card age, position scarcity and hype, so treat it as a
shortlist to research, not a buy signal.
"""

import sqlite3
from dataclasses import dataclass

from cardtracker.analytics.valuation import Valuation, fit_residuals, zscores


@dataclass
class CardRow:
    card_id: int
    player_id: int
    player: str
    sport: str
    position: str | None
    year: int
    set_name: str
    card_number: str
    raw: float | None
    grade9: float | None
    psa10: float | None
    volume: int | None
    psa10_change_pct: float | None       # vs. the earliest snapshot, when there are several days

    @property
    def psa10_over_raw(self) -> float | None:
        return self.psa10 / self.raw if self.psa10 and self.raw else None

    @property
    def psa10_over_9(self) -> float | None:
        return self.psa10 / self.grade9 if self.psa10 and self.grade9 else None


def _price(conn, card_id, grader, grade, day, source) -> float | None:
    row = conn.execute(
        "SELECT price_usd FROM price_snapshots WHERE card_id = ? AND grader = ? AND grade IS ? "
        "AND snapshot_date = ? AND source = ?", (card_id, grader, grade, day, source)).fetchone()
    return row["price_usd"] if row else None


def card_rows(conn: sqlite3.Connection, source: str = "sportscardspro") -> tuple[str | None, list[CardRow]]:
    """(latest snapshot date, one row per card with a snapshot that day)."""
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT snapshot_date FROM price_snapshots WHERE source = ? ORDER BY 1", (source,))]
    if not days:
        return None, []
    first, latest = days[0], days[-1]
    cards = conn.execute(
        "SELECT DISTINCT c.id, p.id AS pid, p.name, p.sport, p.position, c.year, c.set_name, c.card_number, "
        "s.sales_volume FROM price_snapshots s JOIN cards c ON c.id = s.card_id "
        "JOIN players p ON p.id = c.player_id WHERE s.snapshot_date = ? AND s.source = ?",
        (latest, source)).fetchall()
    rows = []
    for c in cards:
        psa10 = _price(conn, c["id"], "PSA", 10.0, latest, source)
        old = _price(conn, c["id"], "PSA", 10.0, first, source) if first != latest else None
        rows.append(CardRow(
            c["id"], c["pid"], c["name"], c["sport"], c["position"], c["year"], c["set_name"], c["card_number"],
            _price(conn, c["id"], "RAW", None, latest, source),
            _price(conn, c["id"], "ANY", 9.0, latest, source),
            psa10, c["sales_volume"],
            (psa10 / old - 1) * 100 if psa10 and old else None))
    return latest, rows


def nfl_performance(conn: sqlite3.Connection, season: int) -> dict[int, float]:
    """PPR fantasy points per game, standardized within each position (QB vs QB, WR vs WR...)."""
    rows = conn.execute(
        "SELECT p.id, p.position, "
        "MAX(CASE WHEN s.metric = 'fantasy_points_ppr' THEN s.value END) AS pts, "
        "MAX(CASE WHEN s.metric = 'games' THEN s.value END) AS games "
        "FROM players p JOIN player_stats s ON s.player_id = p.id "
        "WHERE p.sport = 'football' AND s.season = ? GROUP BY p.id, p.position", (season,)).fetchall()
    by_pos: dict[str, dict[int, float]] = {}
    for r in rows:
        if r["pts"] is not None and r["games"] and r["games"] >= 4:
            by_pos.setdefault(r["position"], {})[r["id"]] = r["pts"] / r["games"]
    out: dict[int, float] = {}
    for group in by_pos.values():
        if len(group) >= 3:
            out.update(zscores(group))
    return out


def cheap_vs_performance(rows: list[CardRow], performance: dict[int, float]) -> list[tuple[CardRow, Valuation]]:
    """Residuals of PSA 10 price vs. performance, most undervalued first."""
    by_player = {r.player_id: r for r in rows if r.psa10 and r.player_id in performance}
    if len(by_player) < 3:
        return []
    vals = fit_residuals({pid: r.psa10 for pid, r in by_player.items()},
                         {pid: performance[pid] for pid in by_player})
    return [(by_player[v.player_id], v) for v in vals]
