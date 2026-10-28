"""Import Google Trends CSV exports (trends.google.com -> Explore -> download icon).

Each file holds one or more terms over time:

    Category: All categories

    Week,Luka Doncic: (United States),Josh Allen: (United States)
    2021-10-31,45,12
    2021-11-07,<1,13

Values are 0-100 relative to the peak within that download, so a file is only internally
comparable. Each file is stored as its own batch and analyses never compare across batches.
For per-player seasonality, download one player per file: with several terms in one file,
quieter players get squashed into a few low integers. Ranges over 5 years export monthly;
5 years or less export weekly.
"""

import csv
import hashlib
import io
import re
import sqlite3
import unicodedata
from datetime import date

SOURCE = "google_trends"
DATE_HEADERS = {"week", "month", "day", "time"}
LESS_THAN_ONE = 0.5          # "<1" means non-zero but below 1; keep it rather than dropping it


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower().strip()


def _parse_date(cell: str) -> date | None:
    """'2021-10-31', '2021-10', or a range like '2021-10-31 - 2021-11-06' (first date wins)."""
    m = re.search(r"(\d{4})-(\d{2})(?:-(\d{2}))?", cell)
    if not m:
        return None
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))


def _parse_value(cell: str) -> float | None:
    cell = cell.strip()
    if cell == "<1":
        return LESS_THAN_ONE
    try:
        return float(cell)
    except ValueError:
        return None


def _split_term(header: str) -> tuple[str, str]:
    """'Luka Doncic: (United States)' -> ('Luka Doncic', 'United States')."""
    m = re.match(r"^(.*?):\s*\((.+)\)\s*$", header)
    return (m.group(1).strip(), m.group(2).strip()) if m else (header.strip(), "Worldwide")


def parse(text: str) -> list[tuple[str, str, list[tuple[date, float]]]]:
    """[(term, geo, [(date, value), ...])] for each term column in an export."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    start = next((i for i, r in enumerate(rows) if r and r[0].strip().lower() in DATE_HEADERS), None)
    if start is None:
        raise ValueError("no Week/Month/Day/Time header row found; is this a Google Trends export?")
    header = rows[start]
    series = []
    for col in range(1, len(header)):
        term, geo = _split_term(header[col])
        points = []
        for r in rows[start + 1:]:
            if len(r) <= col:
                continue
            d, v = _parse_date(r[0]), _parse_value(r[col])
            if d is not None and v is not None:
                points.append((d, v))
        series.append((term, geo, points))
    return series


def _player_id(conn: sqlite3.Connection, term: str, sport: str) -> int | None:
    matches = [r["id"] for r in conn.execute("SELECT id, name FROM players WHERE sport = ?", (sport,))
               if _fold(r["name"]) == _fold(term)]
    return matches[0] if len(matches) == 1 else None


def import_csv(conn: sqlite3.Connection, path: str, sport: str) -> tuple[int, list[str]]:
    """Load one export. Terms that aren't a known player of `sport` are kept with player_id NULL.

    Returns (points written, terms not matched to a player). Re-importing a file is a no-op."""
    raw = open(path, encoding="utf-8-sig").read()
    batch = hashlib.sha1(raw.encode()).hexdigest()[:16]
    written, unmatched = 0, []
    for term, geo, points in parse(raw):
        pid = _player_id(conn, term, sport)
        if pid is None:
            unmatched.append(term)
        for d, v in points:
            cur = conn.execute(
                "INSERT OR IGNORE INTO search_interest (term, player_id, geo, date, value, batch, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (term, pid, geo, d.isoformat(), v, batch, SOURCE))
            written += cur.rowcount
    conn.commit()
    return written, unmatched
