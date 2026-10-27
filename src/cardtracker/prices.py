"""Import historical sales from a CSV, whatever the source (an export, manual lookups, ...).

CSV columns: sport, player, year, set_name, card_number, parallel, grader, grade, date
(YYYY-MM-DD), price_usd, source. Cards must already exist (via a watchlist import); rows for
unknown cards are reported, not guessed.
"""

import csv
import hashlib
import sqlite3


def import_csv(conn: sqlite3.Connection, path: str) -> tuple[int, list[str]]:
    """Returns (rows imported, descriptions of rows whose card was not found)."""
    imported, missing = 0, []
    seen: dict[str, int] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            card = conn.execute(
                "SELECT c.id FROM cards c JOIN players p ON p.id = c.player_id WHERE p.name = ? "
                "AND p.sport = ? AND c.year = ? AND c.set_name = ? AND c.card_number = ? AND c.parallel = ?",
                (row["player"], row["sport"], int(row["year"]), row["set_name"], row["card_number"],
                 row.get("parallel") or "Base"),
            ).fetchone()
            if not card:
                missing.append(f"{row['player']} {row['year']} {row['set_name']} #{row['card_number']}")
                continue
            grader = (row.get("grader") or "RAW").upper()
            grade = float(row["grade"]) if row.get("grade") else None
            # Content hash + occurrence number as external id: re-importing the same file is a no-op,
            # while two genuine identical sales on the same day both survive.
            content = "|".join([str(card["id"]), grader, str(grade), row["date"], row["price_usd"]])
            seen[content] = seen.get(content, 0) + 1
            ext = hashlib.sha1(f"{content}|{seen[content]}".encode()).hexdigest()
            cur = conn.execute(
                "INSERT OR IGNORE INTO sales (card_id, grader, grade, price_usd, sold_at, source, external_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (card["id"], grader, grade, float(row["price_usd"]), row["date"],
                 row.get("source") or "import", ext),
            )
            imported += cur.rowcount
    conn.commit()
    return imported, missing
