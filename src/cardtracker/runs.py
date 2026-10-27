"""Ingestion run tracking: every run records its outcome in source_runs."""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from cardtracker.db import now_iso
from cardtracker.fetcher import BlockedError

# A run writing less than this fraction of the previous successful run is flagged 'suspect':
# the usual symptom of a source changing its format and the parser silently matching nothing.
SUSPECT_RATIO = 0.5


@dataclass
class Run:
    id: int
    rows_written: int = 0
    idle: bool = False       # set when the run had nothing to do; skips the health checks


def previous_rows(conn: sqlite3.Connection, source: str, before_id: int) -> int | None:
    row = conn.execute(
        "SELECT rows_written FROM source_runs WHERE source = ? AND status = 'ok' AND id < ? "
        "ORDER BY id DESC LIMIT 1",
        (source, before_id),
    ).fetchone()
    return row["rows_written"] if row else None


@contextmanager
def track_run(conn: sqlite3.Connection, source: str, compare_previous: bool = True):
    """Record a run in source_runs. Zero rows is always suspect; with compare_previous, so is a
    sharp drop from the previous ok run (only meaningful when runs are similar in size)."""
    cur = conn.execute(
        "INSERT INTO source_runs (source, started_at, status) VALUES (?, ?, 'running')",
        (source, now_iso()),
    )
    conn.commit()
    run = Run(cur.lastrowid)
    status, detail = "ok", None
    try:
        yield run
    except BlockedError as e:
        conn.rollback()
        status, detail = "blocked", str(e)
        raise
    except Exception as e:
        conn.rollback()
        status, detail = "error", f"{type(e).__name__}: {e}"
        raise
    else:
        prev = previous_rows(conn, source, run.id) if compare_previous else None
        if run.idle:
            status = "idle"
        elif run.rows_written == 0 or (prev and run.rows_written < prev * SUSPECT_RATIO):
            status = "suspect"
            detail = f"wrote {run.rows_written} rows; previous ok run wrote {prev}"
    finally:
        conn.execute(
            "UPDATE source_runs SET finished_at = ?, status = ?, rows_written = ?, detail = ? WHERE id = ?",
            (now_iso(), status, run.rows_written, detail, run.id),
        )
        conn.commit()
