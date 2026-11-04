"""Card Hedge API: card price history (daily, back to ~spring 2020) and player index history.

Docs: https://api.cardhedger.com/docs  (OpenAPI: https://api.cardhedger.com/openapi.json)
- Auth: X-API-Key header. Requests per minute/day depend on the subscription tier
  (API: 10/min, 5,000/day); set CARDHEDGE_REQUESTS_PER_MINUTE to match yours.
- prices-by-card returns at most 366 days per call; older history is paged with end_date.
  Windows that end in the past never change, so each is fetched once (tracked in raw_records).
- Daily prices are Card Hedge's computed price per day, not individual sales.
- The daily request allowance is shared by every run and key on the account, so calls are
  counted per UTC day in api_usage, and a 429 that says to wait for the next day ends the run
  cleanly (QuotaExhausted) instead of sleeping.

`daily()` is the scheduled entry point: discover cards, then backfill and refresh prices grade by
grade (PSA 10 first), then player indexes, stopping at the call budget or a time limit.
"""

import os
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

from cardtracker.db import now_iso, store_raw
from cardtracker.fetcher import Fetcher, RateLimitedError
from cardtracker.names import last_name
from cardtracker.runs import track_run

SOURCE = "cardhedge"
API = "https://api.cardhedger.com/v1/cards"
HISTORY_FLOOR = date(2020, 1, 1)          # price coverage begins around spring 2020
INDEX_FLOOR = date(2024, 1, 1)            # player index coverage begins October 2024
DEFAULT_GRADES = ["PSA 10", "PSA 9", "Raw"]
DAILY_LIMIT = int(os.environ.get("CARDHEDGE_DAILY_LIMIT", "5000"))


class QuotaExhausted(RuntimeError):
    """Card Hedge's daily request allowance is used up; resume after 00:00 UTC."""


def _utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def calls_today(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT calls FROM api_usage WHERE source = ? AND day = ?", (SOURCE, _utc_day())).fetchone()
    return row["calls"] if row else 0


class CardHedgeClient:
    def __init__(self, api_key: str | None = None, fetcher: Fetcher | None = None,
                 conn: sqlite3.Connection | None = None):
        self.api_key = api_key or os.environ.get("CARDHEDGE_API_KEY")
        if not self.api_key:
            raise RuntimeError("CARDHEDGE_API_KEY is not set")
        per_minute = float(os.environ.get("CARDHEDGE_REQUESTS_PER_MINUTE", "10"))
        self.fetcher = fetcher or Fetcher(min_interval=60 / per_minute + 0.1, check_robots=False, max_wait=120)
        self.conn = conn                   # when set, every call is counted in api_usage
        self.calls = 0
        self.max_calls: int | None = None
        self.deadline: float | None = None

    def set_budget(self, calls: int | None = None, minutes: float | None = None) -> None:
        """Allow `calls` more calls and/or `minutes` more minutes; see out_of_budget."""
        self.max_calls = None if calls is None else self.calls + calls
        self.deadline = None if minutes is None else time.monotonic() + minutes * 60

    @property
    def out_of_budget(self) -> bool:
        return ((self.max_calls is not None and self.calls >= self.max_calls)
                or (self.deadline is not None and time.monotonic() >= self.deadline))

    def _post(self, path: str, body: dict) -> dict:
        self.calls += 1
        if self.conn is not None:
            self.conn.execute(
                "INSERT INTO api_usage (source, day, calls) VALUES (?, ?, 1) "
                "ON CONFLICT (source, day) DO UPDATE SET calls = calls + 1", (SOURCE, _utc_day()))
            self.conn.commit()
        try:
            resp = self.fetcher.post(f"{API}/{path}", json=body, headers={"X-API-Key": self.api_key})
        except RateLimitedError as e:
            raise QuotaExhausted(str(e)) from e
        return resp.json()

    def search_cards_sorted(self, set_name: str, page: int, page_size: int = 100) -> dict:
        return self._post("search-cards-wsort", {"set": set_name, "sort_by": "sales_30day",
                                                  "sort_order": "desc", "page": page, "page_size": page_size})

    def card_search(self, search: str | None = None, number: str | None = None, set_name: str | None = None,
                    page_size: int = 100) -> list[dict]:
        body = {"page": 1, "page_size": page_size}
        for key, value in (("search", search), ("number", number), ("set", set_name)):
            if value:
                body[key] = value
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
    """The result for an exact card, or None if none fits.

    Card Hedge set names look like '2018 Panini Prizm Basketball'; requiring the set name to end
    with '<set> <Sport>' keeps 'Prizm' from matching 'Prizm Mosaic' or 'Prizm Draft Picks'.
    The catalog has duplicate records for some cards (e.g. 'Lebron James' and 'LeBron James');
    the one with the most recent sales is the record that carries the price data.
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
    return max(fits, key=lambda c: c.get("30 Day Sales") or 0) if fits else None


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
        args = (r["year"], r["set_name"], r["sport"], r["card_number"], r["parallel"], r["player"])
        match = None
        if r["manufacturer"]:
            # Exact set + number returns every version of this one card, so a base card can't be
            # crowded off the page by other cards' parallels.
            exact_set = f"{r['year']} {r['manufacturer']} {r['set_name']} {r['sport'].title()}"
            match = best_card(client.card_search(number=r["card_number"], set_name=exact_set), *args)
        if not match:
            match = best_card(client.card_search(query, number=r["card_number"]), *args)
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


def _recently_fetched(conn: sqlite3.Connection, prefix: str, today: date, refresh_days: int) -> bool:
    """Whether the year-to-date window (key `prefix` + its fetch date) was fetched within
    refresh_days this year. Keys from last year's windows sort before Jan 1 and never count."""
    if refresh_days <= 0:
        return False
    lo = max(today - timedelta(days=refresh_days - 1), date(today.year, 1, 1))
    return conn.execute(
        "SELECT 1 FROM raw_records WHERE source = ? AND external_id >= ? AND external_id <= ? LIMIT 1",
        (SOURCE, prefix + lo.isoformat(), prefix + today.isoformat())).fetchone() is not None


def _stop(client, max_calls: int | None) -> bool:
    return (max_calls is not None and client.calls >= max_calls) or getattr(client, "out_of_budget", False)


def _links_in_turn(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Linked cards, alternating between sports, so a run cut short covers every sport."""
    return conn.execute(
        "SELECT x.card_id, x.external_id, c.year FROM card_external_ids x JOIN cards c ON c.id = x.card_id "
        "JOIN players p ON p.id = c.player_id WHERE x.source = ? "
        "ORDER BY ROW_NUMBER() OVER (PARTITION BY p.sport ORDER BY x.rowid), p.sport", (SOURCE,)).fetchall()


def fetch_price_history(conn: sqlite3.Connection, client: CardHedgeClient, grades: list[str] | None = None,
                        today: date | None = None, max_calls: int | None = 4500, refresh_days: int = 0) -> dict:
    """Page each linked card's daily price history back to its start, for each grade.

    Past windows are fetched once. The window ending today is re-fetched unless it was fetched
    within refresh_days. Windows before the card's set year are skipped, and paging stops at the
    first empty past window. Stops cleanly at max_calls, the client's budget, or the daily quota
    (re-run to continue)."""
    today = today or date.today()
    grades = grades or DEFAULT_GRADES
    points, complete = 0, True
    with track_run(conn, f"{SOURCE}:prices", compare_previous=False) as run:
        try:
            for link in _links_in_turn(conn):
                for label in grades:
                    grader, grade = parse_grade(label)
                    for end, days in _windows(today, HISTORY_FLOOR):
                        if end.year < link["year"]:
                            break           # before the card existed
                        key = f"prices|{link['external_id']}|{label}|{end.isoformat()}"
                        if end < today and _window_done(conn, key):
                            continue
                        if end == today and _recently_fetched(conn, key[:-10], today, refresh_days):
                            continue
                        if _stop(client, max_calls):
                            raise _Budget
                        prices = client.prices_by_card(link["external_id"], label, end, days)
                        # Rows below hold everything a payload has (date, price), so only the
                        # count is kept: full payloads would be most of the database.
                        store_raw(conn, SOURCE, key, {"points": len(prices)})
                        start = end - timedelta(days=days)
                        conn.execute(
                            "DELETE FROM price_snapshots WHERE card_id = ? AND grader = ? AND grade IS ? "
                            "AND source = ? AND snapshot_date > ? AND snapshot_date <= ?",
                            (link["card_id"], grader, grade, SOURCE, start.isoformat(), end.isoformat()))
                        conn.executemany(
                            "INSERT INTO price_snapshots (card_id, grader, grade, price_usd, snapshot_date, source) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            [(link["card_id"], grader, grade, float(p["price"]), p["closing_date"][:10], SOURCE)
                             for p in prices])
                        points += len(prices)
                        conn.commit()
                        if not prices and end < today:
                            break           # reached the start of this card's history
        except (_Budget, QuotaExhausted):
            complete = False
        run.rows_written, run.idle = points, points == 0
    return {"calls": client.calls, "points": points, "complete": complete}


class _Budget(Exception):
    """Internal: the call budget or time limit was reached."""


def link_players(conn: sqlite3.Connection, client: CardHedgeClient, sport: str,
                 recheck_days: int = 30) -> tuple[int, list[str]]:
    """Link players who have watchlist cards to Card Hedge player IDs (exact name match only).
    A player with no match is searched again only after recheck_days."""
    category = sport.title()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=recheck_days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT DISTINCT p.id, p.name FROM players p JOIN cards c ON c.player_id = p.id "
        "JOIN watchlist w ON w.card_id = c.id WHERE p.sport = ? AND NOT EXISTS ("
        "  SELECT 1 FROM player_external_ids x WHERE x.player_id = p.id AND x.source = ?)",
        (sport, SOURCE),
    ).fetchall()
    linked, unmatched = 0, []
    for r in rows:
        key = f"player-search|{sport}|{r['name']}"
        if conn.execute("SELECT 1 FROM raw_records WHERE source = ? AND external_id = ? AND fetched_at >= ?",
                        (SOURCE, key, cutoff)).fetchone():
            unmatched.append(r["name"])
            continue
        if _stop(client, None):
            break
        found = client.player_search(r["name"], category)
        store_raw(conn, SOURCE, key, {"players": found})
        exact = [p for p in found if _fold(p["name"]) == _fold(r["name"])]
        if len(exact) != 1:
            unmatched.append(r["name"])
            conn.commit()
            continue
        conn.execute("INSERT OR IGNORE INTO player_external_ids (player_id, source, external_id) VALUES (?, ?, ?)",
                     (r["id"], SOURCE, exact[0]["player_id"]))
        linked += 1
        conn.commit()
    return linked, unmatched


def fetch_player_index(conn: sqlite3.Connection, client: CardHedgeClient, today: date | None = None,
                       refresh_days: int = 0, max_calls: int | None = None) -> int:
    """Daily player index history for every linked player, from the start of coverage, in one
    call per player (the endpoint serves up to 1,830 days). Re-fetched every refresh_days."""
    today = today or date.today()
    links = conn.execute("SELECT player_id, external_id FROM player_external_ids WHERE source = ?",
                         (SOURCE,)).fetchall()
    with track_run(conn, f"{SOURCE}:player_index", compare_previous=False) as run:
        try:
            for link in links:
                prefix = f"index|{link['external_id']}|all|"
                if _recently_fetched(conn, prefix, today, refresh_days):
                    continue
                if _stop(client, max_calls):
                    raise _Budget
                try:
                    points = client.player_index_history(link["external_id"], INDEX_FLOOR, today)
                except requests.HTTPError as e:
                    # Card Hedge answers 404 for a player without an index.
                    if e.response is None or e.response.status_code != 404:
                        raise
                    points = []
                store_raw(conn, SOURCE, prefix + today.isoformat(), {"points": len(points)})
                usable = [p for p in points if p.get("value") is not None and not p.get("withheld")]
                conn.executemany(
                    "INSERT INTO player_index (player_id, date, value, volume_30d, card_count, source) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (player_id, date, source) DO UPDATE SET "
                    "value = excluded.value, volume_30d = excluded.volume_30d, card_count = excluded.card_count",
                    [(link["player_id"], p["date"][:10], float(p["value"]), p.get("volume_30d"),
                      p.get("card_count"), SOURCE) for p in usable])
                run.rows_written += len(usable)
                conn.commit()
        except (_Budget, QuotaExhausted):
            pass
        run.idle = run.rows_written == 0
    return run.rows_written


# ---------------------------------------------------------------- discovery

SPORT_WORD = {"basketball": "Basketball", "football": "Football", "soccer": "Soccer"}


@dataclass(frozen=True)
class SetSpec:
    sport: str
    set_name: str              # Card Hedge set name, e.g. '2018 Panini Prizm Basketball'
    rookies_only: bool
    min_sales_30d: int         # skip cards that barely trade: their daily prices are mostly noise
    max_cards: int
    max_pages: int = 5         # results are sorted by sales and mostly parallels and inserts


def default_sets(last_year: int = 2025) -> list[SetSpec]:
    """Flagship sets: Panini Prizm rookies (NBA, NFL) since 2012, and the Premier League's
    flagship chrome sets (Panini Prizm 2019-2024, Topps from 2025, when Topps took the licence).
    Premier League cards trade on established stars, not rookies, so soccer keeps every player;
    its sales are mostly inserts and parallels, so its base cards get a lower floor and deeper paging."""
    specs = []
    for y in range(2012, last_year + 1):
        specs.append(SetSpec("basketball", f"{y} Panini Prizm Basketball", True, 5, 30))
        specs.append(SetSpec("football", f"{y} Panini Prizm Football", True, 5, 30))
    for y in range(2019, 2025):
        specs.append(SetSpec("soccer", f"{y} Panini Prizm Premier League Soccer", False, 1, 40, 10))
    specs.append(SetSpec("soccer", "2025 Topps Premier League Soccer", False, 1, 40, 10))
    specs.append(SetSpec("soccer", "2026 Topps Chrome Premier League Soccer", False, 1, 40, 10))
    return specs


BASE_SUBSETS = {"", "base", "base set", "rookie", "rookies"}


def _is_base(card: dict) -> bool:
    """The base-set card itself. Card Hedge labels a Prizm base card's variant 'Base' or 'Prizm'
    (Derrick Henry's 2016 #298 is 'Prizm'), and also gives inserts the variant 'Base' (subset
    'Prizmatic', 'Instant Impact', 'Variation', ...), so the subset must be the base set too."""
    return (_fold(card.get("variant") or "base") in {"base", "prizm"}
            and _fold(card.get("subset") or "") in BASE_SUBSETS
            and str(card.get("number", "")).lstrip("#").isdigit())      # 'PJ-MNS' is a memorabilia card


NOT_PLAYERS = {"team", "badge", "checklist", "logo", "crest", "trophy", "stadium"}


def _is_player(card: dict) -> bool:
    """Base sets include club badges and checklists ('Team Badge')."""
    return bool(card.get("player")) and not (set(_fold(card["player"]).split()) & NOT_PLAYERS)


def _is_rookie(card: dict) -> bool:
    """The rookie flag is missing on some rookie cards (Tyreek Hill's 2016 #296 is subset 'Rookie',
    rookie false); 'Rookie Introduction' and similar subsets are inserts, not rookie cards."""
    return bool(card.get("rookie")) or _fold(card.get("subset") or "") == "rookie"


def _split_set(card: dict, sport: str) -> tuple[int, str, str]:
    """'2018 Panini Prizm Basketball' -> (2018, 'Panini', 'Prizm')."""
    year = int(card["set"].split()[0])
    words = (card.get("set_type") or card["set"].split(" ", 1)[1]).split()
    if words and words[-1].lower() == SPORT_WORD[sport].lower():
        words = words[:-1]
    return year, words[0], " ".join(words[1:])


def _upsert_card(conn: sqlite3.Connection, card: dict, sport: str, players: dict[str, int]) -> int:
    year, manufacturer, set_name = _split_set(card, sport)
    key = _fold(card["player"])
    if key not in players:
        players[key] = conn.execute("INSERT INTO players (name, sport) VALUES (?, ?)",
                                    (card["player"], sport)).lastrowid
    pid = players[key]
    number = str(card["number"]).lstrip("#")
    row = conn.execute("SELECT id FROM cards WHERE player_id = ? AND year = ? AND set_name = ? AND card_number = ? "
                       "AND parallel = 'Base'", (pid, year, set_name, number)).fetchone()
    if row:
        card_id = row["id"]
    else:
        card_id = conn.execute(
            "INSERT INTO cards (player_id, year, manufacturer, set_name, card_number, parallel, is_rookie) "
            "VALUES (?, ?, ?, ?, ?, 'Base', ?)",
            (pid, year, manufacturer, set_name, number, int(_is_rookie(card)))).lastrowid
    conn.execute("INSERT OR IGNORE INTO card_external_ids (card_id, source, external_id) VALUES (?, ?, ?)",
                 (card_id, SOURCE, card["card_id"]))
    conn.execute("INSERT OR IGNORE INTO watchlist (card_id, added_at, notes) VALUES (?, ?, 'cardhedge discover')",
                 (card_id, now_iso()))
    return card_id


def discover(conn: sqlite3.Connection, client: CardHedgeClient, specs: list[SetSpec] | None = None,
             rescan_days: int = 30,
             rookie_seasons: dict[str, dict[str, set[int]]] | None = None) -> dict[str, int]:
    """Add each set's most-traded base cards (by Card Hedge 30-day sales) to the watchlist, linked.
    Results are sorted by sales, so paging stops once cards fall below the sales floor.
    A set is rescanned after rescan_days, which picks up players whose market has grown.

    Card Hedge doesn't flag rookies in its newest sets. There, a card is a rookie if
    rookie_seasons[sport] (e.g. nflverse) says the player's rookie season is the set year, or,
    for a sport without that list, if the player has no card in an earlier year of the product
    (which can let a legend's card through). Accepted Card Hedge ids are recorded per set, so
    prune_discovered() can remove cards an earlier, looser scan added."""
    specs = specs if specs is not None else default_sets()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=rescan_days)).isoformat(timespec="seconds")
    added: dict[str, int] = {}
    for spec in specs:
        key = f"discover|{spec.set_name}"
        if conn.execute("SELECT 1 FROM raw_records WHERE source = ? AND external_id = ? AND fetched_at >= ?",
                        (SOURCE, key, cutoff)).fetchone():
            continue
        players = {_fold(r["name"]): r["id"] for r in
                   conn.execute("SELECT id, name FROM players WHERE sport = ?", (spec.sport,))}
        # Players with a card in an earlier year of this product: their card here isn't a rookie.
        # Used only when Card Hedge hasn't flagged a set's rookies (its newest sets have no flags).
        product = spec.set_name.split(" ", 1)[1]
        year = int(spec.set_name.split()[0])
        earlier = {_fold(r["name"]) for r in conn.execute(
            "SELECT DISTINCT p.name FROM cards c JOIN players p ON p.id = c.player_id "
            "WHERE p.sport = ? AND c.year < ? AND c.manufacturer || ' ' || c.set_name || ' ' || ? = ?",
            (spec.sport, year, SPORT_WORD[spec.sport], product))}
        kept: set[int] = set()
        ids: list[str] = []
        seen_players: set[str] = set()      # one card per player per set: the most-sold one
        for page in range(1, spec.max_pages + 1):
            if _stop(client, None):
                conn.commit()
                return added
            cards = client.search_cards_sorted(spec.set_name, page).get("cards", [])
            flagged = any(_is_rookie(c) for c in cards)
            for c in cards:
                if len(kept) >= spec.max_cards:
                    break
                name = _fold(c.get("player") or "")
                known = (rookie_seasons or {}).get(spec.sport)
                if flagged:
                    rookie = _is_rookie(c)
                elif known is not None:
                    rookie = year in known.get(name, set())
                else:
                    rookie = name not in earlier
                if (c.get("set") == spec.set_name and _is_base(c) and _is_player(c) and name not in seen_players
                        and (rookie or not spec.rookies_only)
                        and (c.get("30 Day Sales") or 0) >= spec.min_sales_30d):
                    kept.add(_upsert_card(conn, c, spec.sport, players))
                    ids.append(c["card_id"])
                    seen_players.add(name)
            if (len(kept) >= spec.max_cards or len(cards) < 100
                    or (cards[-1].get("30 Day Sales") or 0) < spec.min_sales_30d):
                break
        store_raw(conn, SOURCE, key, {"cards": len(kept), "ids": ids})
        conn.commit()
        added[spec.sport] = added.get(spec.sport, 0) + len(kept)
    return added


def prune_discovered(conn: sqlite3.Connection) -> int:
    """Delete discovered cards that the latest scan of their set no longer accepts, with their
    prices. Only cards added by discover() are touched, and only for sets scanned with id lists."""
    import json
    latest: dict[str, set[str]] = {}
    for r in conn.execute("SELECT external_id, payload FROM raw_records WHERE source = ? AND external_id LIKE "
                          "'discover|%' ORDER BY id", (SOURCE,)):
        payload = json.loads(r["payload"])
        if "ids" in payload:
            latest[r["external_id"][len("discover|"):]] = set(payload["ids"])
    accepted = set().union(*latest.values()) if latest else set()
    doomed = []
    for r in conn.execute(
            "SELECT c.id, c.year, c.manufacturer, c.set_name, p.sport, x.external_id FROM watchlist w "
            "JOIN cards c ON c.id = w.card_id JOIN players p ON p.id = c.player_id "
            "JOIN card_external_ids x ON x.card_id = c.id AND x.source = ? WHERE w.notes = 'cardhedge discover'",
            (SOURCE,)):
        set_name = f"{r['year']} {r['manufacturer']} {r['set_name']} {SPORT_WORD[r['sport']]}"
        if set_name in latest and r["external_id"] not in accepted:
            doomed.append(r["id"])
    for card_id in doomed:
        for table in ("price_snapshots", "card_external_ids", "watchlist", "sales", "listings", "pop_snapshots"):
            conn.execute(f"DELETE FROM {table} WHERE card_id = ?", (card_id,))
        conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    conn.commit()
    return len(doomed)


def daily(conn: sqlite3.Connection, client: CardHedgeClient, budget: int = DAILY_LIMIT - 200,
          minutes: float | None = None, grades: list[str] | None = None, refresh_days: int = 14,
          today: date | None = None, specs: list[SetSpec] | None = None,
          rookie_seasons: dict[str, dict[str, set[int]]] | None = None) -> dict:
    """One scheduled run: discovery, then prices grade by grade (PSA 10 first), then player
    indexes. `budget` is the day's total across all runs (api_usage), leaving headroom for ad-hoc
    use; `minutes` caps wall time (e.g. a CI job limit)."""
    used = calls_today(conn)
    client.set_budget(max(0, budget - used), minutes)
    report: dict = {"calls_used_today_before": used}
    try:
        report["discovered"] = discover(conn, client, specs, rookie_seasons=rookie_seasons)
        report["pruned"] = prune_discovered(conn)
        for label in grades or DEFAULT_GRADES:
            res = fetch_price_history(conn, client, [label], today=today, max_calls=None, refresh_days=refresh_days)
            report[label] = res["points"]
            if not res["complete"]:
                break
        else:
            for sport in SPORT_WORD:
                link_players(conn, client, sport)
            report["player_index"] = fetch_player_index(conn, client, today=today, refresh_days=refresh_days)
    except QuotaExhausted:
        report["quota_exhausted"] = True
    report["calls"] = client.calls
    report["out_of_budget"] = client.out_of_budget
    return report
