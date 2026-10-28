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
import unicodedata
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


def _fold(text: str) -> str:
    """Lowercase and strip accents: 'Dončić' -> 'doncic'."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower().strip()


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


PRIZM_CONSOLE = re.compile(r"^(Football|Basketball) Cards (\d{4}) Panini Prizm$")
FIRST_PRIZM_YEAR = 2012            # Panini Prizm football and basketball both began in 2012
BASE_EXCLUDES = {
    "basketball": "silver;holo;red;blue;green;orange;purple;gold;mojo;pink;white;black;cracked ice;choice;fast break",
    "football": "silver;holo;red;blue;green;orange;purple;gold;mojo;pink;white;black;camo;choice;no huddle",
}


def _product_player(name: str) -> str:
    """'Puka Nacua #357' -> 'Puka Nacua'."""
    return name.split("#")[0].strip()


def _name_key(name: str) -> str:
    """Comparable player name: 'Patrick Mahomes II', 'Amon Ra St. Brown', 'Travis Etienne Jr.'
    -> 'patrick mahomes', 'amon ra st brown', 'travis etienne'."""
    tokens = re.sub(r"[-.'’]", " ", _fold(name)).split()
    return " ".join(t for t in tokens if t not in {"jr", "sr", "ii", "iii", "iv"})


def _base_prizm(products: list[dict], player: str, sport: str, year: int | None = None) -> list[tuple]:
    """(year, card_number, product_id) for the player's unbracketed Prizm base cards in `products`."""
    out = []
    for p in products:
        m = PRIZM_CONSOLE.match(p.get("console-name", ""))
        name = p.get("product-name", "")
        if not m or m.group(1) != sport.title() or "[" in name or "#" not in name:
            continue
        if _name_key(_product_player(name)) != _name_key(player) or (year and int(m.group(2)) != year):
            continue
        out.append((int(m.group(2)), name.split("#")[-1].strip(), p["id"]))
    return out


def find_first_prizm(client: SportsCardsProClient, player: str, sport: str,
                     debut: int | None = None, last_year: int | None = None) -> dict | None:
    """The player's earliest Panini Prizm base card: their rookie Prizm if they debuted in 2012+.

    With a known debut season, only that year is searched (or 2012 for earlier debuts), and a
    miss returns None rather than substituting a later card. Without one, years are searched one
    at a time from 2012: a single broad search is not enough, because results are capped at 100
    and a veteran's older cards get crowded out, making a recent card look like the first.
    Returns {year, card_number, product_id, is_rookie} or None."""
    if debut:
        years = [max(debut, FIRST_PRIZM_YEAR)]
    else:
        years = range(FIRST_PRIZM_YEAR, (last_year or date.today().year) + 1)
    for year in years:
        found = _base_prizm(client.search(f"{player} {year} prizm"), player, sport, year)
        if found:
            y, number, pid = min(found)
            rookie = (debut >= FIRST_PRIZM_YEAR) if debut else (y > FIRST_PRIZM_YEAR)
            return {"year": y, "card_number": number, "product_id": pid, "is_rookie": rookie}
    return None


def build_watchlist(client: SportsCardsProClient, players: list[tuple[str, str, int | None]],
                    path: str) -> list[str]:
    """Write a watchlist CSV of each player's first Prizm base card from (name, sport, debut season
    or None). Returns players not found."""
    import csv

    missing = []
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sport", "player", "year", "manufacturer", "set_name", "card_number", "parallel",
                    "is_rookie", "search_query", "exclude_terms", "notes", "sportscardspro_id"])
        for player, sport, debut in players:
            card = find_first_prizm(client, player, sport, debut)
            if not card:
                missing.append(player)
                continue
            if card["is_rookie"]:
                note = "" if debut else "earliest Prizm found; rookie year not verified"
            else:
                note = "earliest Prizm; player debuted before Prizm existed (not a rookie card)"
            w.writerow([sport, player, card["year"], "Panini", "Prizm", card["card_number"], "Base",
                        int(card["is_rookie"]), f"{card['year']} Prizm {player} {card['card_number']}",
                        BASE_EXCLUDES[sport], note, card["product_id"]])
    return missing


def link_from_csv(conn: sqlite3.Connection, path: str) -> int:
    """Link cards using the sportscardspro_id column written by build_watchlist (no re-search)."""
    import csv

    linked = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("sportscardspro_id"):
                continue
            card = conn.execute(
                "SELECT c.id FROM cards c JOIN players p ON p.id = c.player_id WHERE p.name = ? AND p.sport = ? "
                "AND c.year = ? AND c.set_name = ? AND c.card_number = ? AND c.parallel = ?",
                (row["player"], row["sport"], int(row["year"]), row["set_name"], row["card_number"],
                 row["parallel"] or "Base")).fetchone()
            if card:
                cur = conn.execute("INSERT OR IGNORE INTO card_external_ids (card_id, source, external_id) "
                                   "VALUES (?, ?, ?)", (card["id"], SOURCE, row["sportscardspro_id"]))
                linked += cur.rowcount
    conn.commit()
    return linked


def drop_superseded(conn: sqlite3.Connection, path: str) -> int:
    """For each player in a discovery CSV, drop their other watchlist cards (and those cards'
    SportsCardsPro links and snapshots): earlier discovery runs could pick the wrong card."""
    import csv

    removed = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            stale = conn.execute(
                "SELECT c.id FROM watchlist w JOIN cards c ON c.id = w.card_id JOIN players p ON p.id = c.player_id "
                "WHERE p.name = ? AND p.sport = ? AND NOT (c.year = ? AND c.set_name = ? AND c.card_number = ? "
                "AND c.parallel = ?)",
                (row["player"], row["sport"], int(row["year"]), row["set_name"], row["card_number"],
                 row["parallel"] or "Base")).fetchall()
            for (card_id,) in stale:
                conn.execute("DELETE FROM watchlist WHERE card_id = ?", (card_id,))
                conn.execute("DELETE FROM card_external_ids WHERE card_id = ? AND source = ?", (card_id, SOURCE))
                conn.execute("DELETE FROM price_snapshots WHERE card_id = ? AND source = ?", (card_id, SOURCE))
                removed += 1
    conn.commit()
    return removed


def drop_wrong_year_prizm(conn: sqlite3.Connection) -> list[str]:
    """Drop watchlist Prizm base cards whose year contradicts the player's known debut season
    (the first Prizm year is max(debut, 2012)). Catches wrong picks for players a later
    discovery run could not find, which drop_superseded never sees."""
    stale = conn.execute(
        "SELECT c.id, p.name, c.year FROM watchlist w JOIN cards c ON c.id = w.card_id "
        "JOIN players p ON p.id = c.player_id WHERE c.set_name = 'Prizm' AND c.parallel = 'Base' "
        "AND p.debut_season IS NOT NULL AND c.year != MAX(p.debut_season, ?)", (FIRST_PRIZM_YEAR,)).fetchall()
    for card_id, _, _ in stale:
        conn.execute("DELETE FROM watchlist WHERE card_id = ?", (card_id,))
        conn.execute("DELETE FROM card_external_ids WHERE card_id = ? AND source = ?", (card_id, SOURCE))
        conn.execute("DELETE FROM price_snapshots WHERE card_id = ? AND source = ?", (card_id, SOURCE))
    conn.commit()
    return [f"{name} {year}" for _, name, year in stale]
