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


def init_db(conn: sqlite3.Connection) -> None:
    schema = resources.files("cardtracker").joinpath("schema.sql").read_text()
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
