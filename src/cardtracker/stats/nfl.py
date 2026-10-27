"""NFL player season stats from nflverse (free, openly licensed, published as GitHub release CSVs).

https://github.com/nflverse/nflverse-data/releases/tag/stats_player
"""

import csv
import io
import sqlite3

from cardtracker.db import now_iso, store_raw
from cardtracker.fetcher import Fetcher
from cardtracker.runs import track_run
from cardtracker.stats.common import to_float, upsert_player, write_stats

SOURCE = "nflverse"
URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_{kind}_{season}.csv"

# Card markets are driven by skill positions; defense/special teams are skipped.
POSITIONS = {"QB", "RB", "WR", "TE"}
METRICS = [
    "games", "completions", "attempts", "passing_yards", "passing_tds", "passing_interceptions",
    "passing_epa", "passing_cpoe", "carries", "rushing_yards", "rushing_tds", "rushing_epa",
    "receptions", "targets", "receiving_yards", "receiving_tds", "receiving_epa",
    "target_share", "wopr", "fantasy_points_ppr",
]


def parse(csv_text: str) -> list[dict]:
    """Rows for skill-position players: {external_id, name, position, season, metrics}."""
    out = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        if row.get("position") not in POSITIONS:
            continue
        metrics = {m: v for m in METRICS if (v := to_float(row.get(m))) is not None}
        out.append({
            "external_id": row["player_id"],
            "name": row["player_display_name"],
            "position": row["position"],
            "season": int(row["season"]),
            "metrics": metrics,
        })
    return out


def ingest_season(conn: sqlite3.Connection, season: int, fetcher: Fetcher | None = None,
                  kind: str = "reg") -> int:
    fetcher = fetcher or Fetcher(min_interval=1.0)
    with track_run(conn, f"{SOURCE}:{kind}:{season}") as run:
        text = fetcher.get(URL.format(kind=kind, season=season)).text
        store_raw(conn, SOURCE, f"{kind}_{season}", {"csv": text})
        as_of = now_iso()[:10]
        for rec in parse(text):
            pid = upsert_player(conn, SOURCE, rec["external_id"], rec["name"], "football", rec["position"])
            run.rows_written += write_stats(conn, pid, rec["season"], as_of, rec["metrics"], SOURCE)
        conn.commit()
    return run.rows_written
