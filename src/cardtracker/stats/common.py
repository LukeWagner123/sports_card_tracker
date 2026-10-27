import sqlite3


def upsert_player(conn: sqlite3.Connection, source: str, external_id: str, name: str,
                  sport: str, position: str | None = None) -> int:
    """Return the player id for a source's external id, creating the player if new."""
    row = conn.execute(
        "SELECT player_id FROM player_external_ids WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    if row:
        if position:
            conn.execute("UPDATE players SET position = ? WHERE id = ?", (position, row["player_id"]))
        return row["player_id"]
    cur = conn.execute(
        "INSERT INTO players (name, sport, position) VALUES (?, ?, ?)", (name, sport, position)
    )
    conn.execute(
        "INSERT INTO player_external_ids (player_id, source, external_id) VALUES (?, ?, ?)",
        (cur.lastrowid, source, external_id),
    )
    return cur.lastrowid


def write_stats(conn: sqlite3.Connection, player_id: int, season: int, as_of: str,
                metrics: dict[str, float], source: str) -> int:
    conn.executemany(
        "INSERT INTO player_stats (player_id, season, as_of, metric, value, source) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (player_id, season, as_of, metric, source) DO UPDATE SET value = excluded.value",
        [(player_id, season, as_of, m, v, source) for m, v in metrics.items()],
    )
    return len(metrics)


def to_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
