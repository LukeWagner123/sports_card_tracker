"""NBA player season stats from stats.nba.com (the source behind the nba_api package).

stats.nba.com drops requests from most cloud/datacenter IPs (they time out rather than error),
so run this from a home connection.
"""

import sqlite3

from cardtracker.db import now_iso, store_raw
from cardtracker.fetcher import Fetcher
from cardtracker.runs import track_run
from cardtracker.stats.common import to_float, upsert_player, write_stats

SOURCE = "nba_stats"
URL = "https://stats.nba.com/stats/leaguedashplayerstats"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Accept": "application/json",
}
METRICS = {
    "GP": "games", "MIN": "min", "PTS": "pts", "REB": "reb", "AST": "ast", "STL": "stl",
    "BLK": "blk", "TOV": "tov", "FG_PCT": "fg_pct", "FG3_PCT": "fg3_pct", "FT_PCT": "ft_pct",
    "PLUS_MINUS": "plus_minus", "NBA_FANTASY_PTS": "fantasy_pts",
}


def season_label(season: int) -> str:
    """2025 -> '2025-26' (season keyed by the year it starts)."""
    return f"{season}-{(season + 1) % 100:02d}"


def params(season: int) -> dict:
    return {
        "Season": season_label(season), "SeasonType": "Regular Season", "PerMode": "PerGame",
        "MeasureType": "Base", "LeagueID": "00", "LastNGames": 0, "Month": 0,
        "OpponentTeamID": 0, "PaceAdjust": "N", "Period": 0, "PlusMinus": "N",
        "Rank": "N", "TeamID": 0,
    }


def parse(payload: dict) -> list[dict]:
    result = payload["resultSets"][0]
    headers = result["headers"]
    out = []
    for values in result["rowSet"]:
        row = dict(zip(headers, values))
        metrics = {name: v for key, name in METRICS.items() if (v := to_float(row.get(key))) is not None}
        out.append({"external_id": str(row["PLAYER_ID"]), "name": row["PLAYER_NAME"], "metrics": metrics})
    return out


def ingest_season(conn: sqlite3.Connection, season: int, fetcher: Fetcher | None = None) -> int:
    if fetcher is None:
        fetcher = Fetcher(min_interval=3.0)
        fetcher.session.headers.update(HEADERS)
    with track_run(conn, f"{SOURCE}:{season}") as run:
        payload = fetcher.get(URL, params=params(season)).json()
        store_raw(conn, SOURCE, season_label(season), payload)
        as_of = now_iso()[:10]
        for rec in parse(payload):
            pid = upsert_player(conn, SOURCE, rec["external_id"], rec["name"], "basketball")
            run.rows_written += write_stats(conn, pid, season, as_of, rec["metrics"], SOURCE)
        conn.commit()
    return run.rows_written
