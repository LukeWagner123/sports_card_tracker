import json
import os
import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

DEFAULT_DB = "data/cards.db"


def db_path() -> str:
    return os.environ.get("CARDTRACKER_DB", DEFAULT_DB)


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS never alters an existing
# table, so these are applied to older databases. Append only; never edit or reorder.
ADDED_COLUMNS = [
    ("sales", "price_lag_s", "INTEGER"),
    ("listings", "bid_count", "INTEGER"),
    ("watchlist", "exclude_terms", "TEXT"),
    ("watchlist", "last_searched", "TEXT"),
]


def init_db(conn: sqlite3.Connection) -> None:
    schema = resources.files("cardtracker").joinpath("schema.sql").read_text()
    # Add missing columns before the schema script, whose indexes may reference them.
    for table, column, ddl in ADDED_COLUMNS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    conn.executescript(schema)
    conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store_raw(conn: sqlite3.Connection, source: str, external_id: str | None, payload: dict) -> int:
    """Persist a source payload before parsing, so parsers can be re-run later."""
    cur = conn.execute(
        "INSERT INTO raw_records (source, external_id, fetched_at, payload) VALUES (?, ?, ?, ?)",
        (source, external_id, now_iso(), json.dumps(payload)),
    )
    return cur.lastrowid
