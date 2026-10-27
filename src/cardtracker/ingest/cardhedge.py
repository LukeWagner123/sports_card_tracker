"""Card Hedge API: card price history (daily, back to ~spring 2020) and player index history.

Docs: https://api.cardhedger.com/docs  (OpenAPI: https://api.cardhedger.com/openapi.json)
- Auth: X-API-Key header. Requests per minute/day depend on the subscription tier
  (API: 10/min, 5,000/day); set CARDHEDGE_REQUESTS_PER_MINUTE to match yours.
- prices-by-card returns at most 366 days per call; older history is paged with end_date.
  Windows that end in the past never change, so each is fetched once (tracked in raw_records).
- Daily prices are Card Hedge's computed price per day, not individual sales.
"""

import os
import re
import sqlite3
import unicodedata
from datetime import date, timedelta

from cardtracker.db import store_raw
from cardtracker.fetcher import Fetcher
from cardtracker.names import last_name
from cardtracker.runs import track_run

SOURCE = "cardhedge"
API = "https://api.cardhedger.com/v1/cards"
HISTORY_FLOOR = date(2019, 1, 1)          # coverage generally begins around spring 2020
DEFAULT_GRADES = ["PSA 10", "PSA 9", "Raw"]


class CardHedgeClient:
    def __init__(self, api_key: str | None = None, fetcher: Fetcher | None = None):
        self.api_key = api_key or os.environ.get("CARDHEDGE_API_KEY")
        if not self.api_key:
            raise RuntimeError("CARDHEDGE_API_KEY is not set")
        per_minute = float(os.environ.get("CARDHEDGE_REQUESTS_PER_MINUTE", "10"))
        self.fetcher = fetcher or Fetcher(min_interval=60 / per_minute + 0.1, check_robots=False)
        self.calls = 0

    def _post(self, path: str, body: dict) -> dict:
        self.calls += 1
        return self.fetcher.post(f"{API}/{path}", json=body, headers={"X-API-Key": self.api_key}).json()

    def card_search(self, search: str, number: str | None = None, page_size: int = 50) -> list[dict]:
        body = {"search": search, "page": 1, "page_size": page_size}
        if number:
            body["number"] = number
        return self._post("card-search", body).get("cards", [])

    def prices_by_card(self, card_id: str, grade: str, end_date: date, days: int) -> list[dict]:
        return self._post("prices-by-card", {"card_id": card_id, "grade": grade, "days": days,
                                             "end_date": end_date.isoformat()}).get("prices", [])

    def player_search(self, name: str, category: str) -> list[dict]:
        return self._post("player-search", {"search": name, "category": category, "count": 10}).get("players", [])

    def player_index_history(self, player_id: str, start: date, end: date) -> list[dict]:
        return self._post("player-index-history", {
            "player_id": player_id, "start_date": start.isoformat(), "end_date": end.isoformat(),
            "interval": "daily"}).get("points", [])


def parse_grade(label: str) -> tuple[str, float | None]:
    """'PSA 10' -> ('PSA', 10.0); 'Raw' -> ('RAW', None)."""
    parts = label.split()
    if len(parts) == 2 and re.fullmatch(r"\d+(\.\d+)?", parts[1]):
        return parts[0].upper(), float(parts[1])
    if label.strip().lower() == "raw":
        return "RAW", None
    raise ValueError(f"unsupported grade label {label!r}")


def _fold(text: str) -> str:
    """Lowercase and strip accents: 'Dončić' -> 'doncic'."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower().strip()


def best_card(cards: list[dict], year: int, set_name: str, sport: str, number: str,
              parallel: str, player: str) -> dict | None:
    """The one result for an exact card, or None if zero or several fit.

    Card Hedge set names look like '2018 Panini Prizm Basketball'; requiring the set name to end
    with '<set> <Sport>' keeps 'Prizm' from matching 'Prizm Mosaic' or 'Prizm Draft Picks'.
    """
    want_end = _fold(f"{set_name} {sport}")
    surname = _fold(last_name(player))
    fits = [
        c for c in cards
        if _fold(c.get("set", "")).startswith(str(year))
        and _fold(c.get("set", "")).endswith(want_end)
        and _fold(c.get("number", "")).lstrip("#") == _fold(number).lstrip("#")
        and _fold(c.get("variant", "") or "base") == _fold(parallel)
        and surname in _fold(c.get("player", ""))
    ]
    return fits[0] if len(fits) == 1 else None


def link_cards(conn: sqlite3.Connection, client: CardHedgeClient) -> tuple[int, list[str]]:
    """Find the Card Hedge card_id for each unlinked watchlist card."""
    rows = conn.execute(
        "SELECT c.id, c.year, c.manufacturer, c.set_name, c.card_number, c.parallel, "
        "p.name AS player, p.sport FROM watchlist w JOIN cards c ON c.id = w.card_id "
        "JOIN players p ON p.id = c.player_id WHERE NOT EXISTS ("
        "  SELECT 1 FROM card_external_ids x WHERE x.card_id = c.id AND x.source = ?)",
        (SOURCE,),
    ).fetchall()
    linked, unmatched = 0, []
    for r in rows:
        query = " ".join(str(v) for v in (r["year"], r["manufacturer"] or "", r["set_name"], r["player"]) if v)
        match = best_card(client.card_search(query, number=r["card_number"]), r["year"], r["set_name"],
                          r["sport"], r["card_number"], r["parallel"], r["player"])
        if not match:
            unmatched.append(f"{query} #{r['card_number']}")
            continue
        conn.execute("INSERT INTO card_external_ids (card_id, source, external_id) VALUES (?, ?, ?)",
                     (r["id"], SOURCE, match["card_id"]))
        linked += 1
    conn.commit()
    return linked, unmatched


def _windows(today: date, floor: date):
    """(end_date, days) calendar-year windows paging back from today: year-to-date first, then each
    full prior year. Anchored to Dec 31 so a past window's key never changes and is fetched once.
    Each covers (end - days, end], so they are contiguous and never overlap."""
    ytd = (today - date(today.year - 1, 12, 31)).days
    yield today, ytd
    for year in range(today.year - 1, floor.year - 1, -1):
        end = date(year, 12, 31)
        yield end, (end - date(year - 1, 12, 31)).days


def _window_done(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("SELECT 1 FROM raw_records WHERE source = ? AND external_id = ? LIMIT 1",
                        (SOURCE, key)).fetchone() is not None


def fetch_price_history(conn: sqlite3.Connection, client: CardHedgeClient, grades: list[str] | None = None,
                        today: date | None = None, max_calls: int = 4500) -> dict:
    """Page each linked card's daily price history back to its start, for each grade.

    Past windows are fetched once; the window ending today is refreshed every run.
    Stops cleanly at max_calls (re-run to continue)."""
    today = today or date.today()
    grades = grades or DEFAULT_GRADES
    links = conn.execute("SELECT card_id, external_id FROM card_external_ids WHERE source = ?",
                         (SOURCE,)).fetchall()
    points = 0
    with track_run(conn, f"{SOURCE}:prices", compare_previous=False) as run:
        for link in links:
            for label in grades:
                grader, grade = parse_grade(label)
                for end, days in _windows(today, HISTORY_FLOOR):
                    key = f"prices|{link['external_id']}|{label}|{end.isoformat()}"
                    if end < today and _window_done(conn, key):
                        continue
                    if client.calls >= max_calls:
                        run.rows_written, run.idle = points, points == 0
                        return {"calls": client.calls, "points": points, "complete": False}
                    prices = client.prices_by_card(link["external_id"], label, end, days)
                    store_raw(conn, SOURCE, key, {"prices": prices})
                    start = end - timedelta(days=days)
                    conn.execute(
                        "DELETE FROM price_snapshots WHERE card_id = ? AND grader = ? AND grade IS ? "
                        "AND source = ? AND snapshot_date > ? AND snapshot_date <= ?",
                        (link["card_id"], grader, grade, SOURCE, start.isoformat(), end.isoformat()))
                    for p in prices:
                        conn.execute(
                            "INSERT INTO price_snapshots (card_id, grader, grade, price_usd, snapshot_date, source) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (link["card_id"], grader, grade, float(p["price"]), p["closing_date"][:10], SOURCE))
                    points += len(prices)
                    conn.commit()
                    if not prices:
                        break               # reached the start of this card's history
        run.rows_written = points
    return {"calls": client.calls, "points": points, "complete": True}


def link_players(conn: sqlite3.Connection, client: CardHedgeClient, sport: str) -> tuple[int, list[str]]:
    """Link players who have watchlist cards to Card Hedge player IDs (exact name match only)."""
    category = sport.title()
    rows = conn.execute(
        "SELECT DISTINCT p.id, p.name FROM players p JOIN cards c ON c.player_id = p.id "
        "JOIN watchlist w ON w.card_id = c.id WHERE p.sport = ? AND NOT EXISTS ("
        "  SELECT 1 FROM player_external_ids x WHERE x.player_id = p.id AND x.source = ?)",
        (sport, SOURCE),
    ).fetchall()
    linked, unmatched = 0, []
    for r in rows:
        exact = [p for p in client.player_search(r["name"], category) if _fold(p["name"]) == _fold(r["name"])]
        if len(exact) != 1:
            unmatched.append(r["name"])
            continue
        conn.execute("INSERT INTO player_external_ids (player_id, source, external_id) VALUES (?, ?, ?)",
                     (r["id"], SOURCE, exact[0]["player_id"]))
        linked += 1
    conn.commit()
    return linked, unmatched


def fetch_player_index(conn: sqlite3.Connection, client: CardHedgeClient, today: date | None = None) -> int:
    """Daily player index history for every linked player, back to the start of coverage."""
    today = today or date.today()
    links = conn.execute("SELECT player_id, external_id FROM player_external_ids WHERE source = ?",
                         (SOURCE,)).fetchall()
    with track_run(conn, f"{SOURCE}:player_index", compare_previous=False) as run:
        for link in links:
            for end, days in _windows(today, HISTORY_FLOOR):
                key = f"index|{link['external_id']}|{end.isoformat()}"
                if end < today and _window_done(conn, key):
                    continue
                start = end - timedelta(days=days - 1)       # start_date is inclusive here
                points = client.player_index_history(link["external_id"], start, end)
                store_raw(conn, SOURCE, key, {"points": points})
                usable = [p for p in points if p.get("value") is not None and not p.get("withheld")]
                for p in usable:
                    conn.execute(
                        "INSERT INTO player_index (player_id, date, value, volume_30d, card_count, source) "
                        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (player_id, date, source) DO UPDATE SET "
                        "value = excluded.value, volume_30d = excluded.volume_30d, card_count = excluded.card_count",
                        (link["player_id"], p["date"][:10], float(p["value"]), p.get("volume_30d"),
                         p.get("card_count"), SOURCE))
                run.rows_written += len(usable)
                conn.commit()
                if not points:
                    break
    return run.rows_written
