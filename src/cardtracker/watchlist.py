"""Watchlist: the cards we actively collect data on, loaded from a curated CSV.

CSV columns: sport, player, year, manufacturer, set_name, card_number, parallel, is_rookie,
search_query, exclude_terms (';'-separated), notes.
"""

import csv
import sqlite3
from pathlib import Path

from cardtracker.db import now_iso


def _player_id(conn: sqlite3.Connection, name: str, sport: str) -> int:
    # Watchlist rows are hand-curated, so exact-name lookup is acceptable here; ambiguous
    # names (two players, same name and sport) are rejected rather than guessed.
    rows = conn.execute("SELECT id FROM players WHERE name = ? AND sport = ?", (name, sport)).fetchall()
    if len(rows) > 1:
        raise ValueError(f"ambiguous player {name!r} ({sport}); link the card manually")
    if rows:
        return rows[0]["id"]
    return conn.execute("INSERT INTO players (name, sport) VALUES (?, ?)", (name, sport)).lastrowid


def import_csv(conn: sqlite3.Connection, path: str | Path) -> int:
    """Upsert cards and watchlist entries from a CSV. Returns rows imported."""
    count = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            pid = _player_id(conn, row["player"].strip(), row["sport"].strip())
            key = (int(row["year"]), row["set_name"].strip(), row["card_number"].strip(),
                   row.get("parallel", "").strip() or "Base", pid)
            conn.execute(
                "INSERT OR IGNORE INTO cards (year, set_name, card_number, parallel, player_id, "
                "manufacturer, is_rookie) VALUES (?, ?, ?, ?, ?, ?, ?)",
                key + (row.get("manufacturer", "").strip() or None, int(row.get("is_rookie") or 0)),
            )
            card_id = conn.execute(
                "SELECT id FROM cards WHERE year = ? AND set_name = ? AND card_number = ? "
                "AND parallel = ? AND player_id = ?", key,
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO watchlist (card_id, search_query, exclude_terms, added_at, notes) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (card_id) DO UPDATE SET "
                "search_query = excluded.search_query, exclude_terms = excluded.exclude_terms, "
                "notes = excluded.notes",
                (card_id, row["search_query"].strip(), row.get("exclude_terms", "").strip(),
                 now_iso(), row.get("notes", "").strip() or None),
            )
            count += 1
    conn.commit()
    return count
