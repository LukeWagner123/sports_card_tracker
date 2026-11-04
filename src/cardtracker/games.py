"""Game results and player game logs, for game-level price studies.

- NFL games: nflverse schedules (games.csv, the file `calendar fetch nfl` uses), completed only.
- NFL player games: nflverse weekly player stats (stats_player_week_<season>.csv), for players
  with tracked cards only. A player is matched by nflverse id when linked, else by exact name;
  a name that belongs to two players that season is skipped.
- EPL games: openfootball fixtures with full-time scores. There are no rosters, so Premier League
  players are not tied to club results yet.
"""

import csv
import io
import sqlite3

import requests

from cardtracker.calendar import EPL_FIXTURES_URL, NFL_SCHEDULE_URL
from cardtracker.db import store_raw
from cardtracker.fetcher import Fetcher
from cardtracker.ingest.cardhedge import _fold
from cardtracker.runs import track_run

WEEK_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"


def nfl_games_from_schedule(csv_text: str) -> list[tuple]:
    rows = []
    for g in csv.DictReader(io.StringIO(csv_text)):
        if g.get("home_score", "") == "" or g.get("away_score", "") == "":
            continue                                  # not played yet
        rows.append((g["game_id"], "NFL", int(g["season"]), int(g["week"]), g["game_type"], g["gameday"],
                     g["home_team"], g["away_team"], int(float(g["home_score"])), int(float(g["away_score"])),
                     "nflverse"))
    return rows


def _save_games(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    conn.executemany(
        "INSERT INTO games (game_id, league, season, week, game_type, date, home, away, home_score, away_score, "
        "source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (game_id) DO UPDATE SET "
        "home_score = excluded.home_score, away_score = excluded.away_score, date = excluded.date", rows)
    return len(rows)


def ingest_nfl_games(conn: sqlite3.Connection, fetcher: Fetcher | None = None) -> int:
    fetcher = fetcher or Fetcher(min_interval=1.0)
    with track_run(conn, "games:nfl") as run:
        run.rows_written = _save_games(conn, nfl_games_from_schedule(fetcher.get(NFL_SCHEDULE_URL).text))
        conn.commit()
    return run.rows_written


def nfl_player_games(csv_text: str, tracked: dict[str, int], linked: dict[str, int]) -> list[tuple]:
    """(player_id, game_id, team, fantasy_ppr) for tracked players. `tracked` maps folded names
    to player ids, `linked` maps nflverse ids to player ids."""
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    ids_by_name: dict[str, set[str]] = {}
    for r in rows:
        ids_by_name.setdefault(_fold(r["player_display_name"]), set()).add(r["player_id"])
    out = []
    for r in rows:
        pid = linked.get(r["player_id"])
        if pid is None:
            name = _fold(r["player_display_name"])
            if len(ids_by_name[name]) == 1:
                pid = tracked.get(name)
        if pid is None or not r.get("game_id"):
            continue
        ppr = r.get("fantasy_points_ppr")
        out.append((pid, r["game_id"], r["team"], float(ppr) if ppr not in (None, "", "NA") else None, "nflverse"))
    return out


def ingest_nfl_player_games(conn: sqlite3.Connection, seasons: list[int], fetcher: Fetcher | None = None) -> int:
    fetcher = fetcher or Fetcher(min_interval=1.0)
    tracked = {_fold(r["name"]): r["id"] for r in conn.execute(
        "SELECT DISTINCT p.id, p.name FROM players p JOIN cards c ON c.player_id = p.id "
        "JOIN watchlist w ON w.card_id = c.id WHERE p.sport = 'football'")}
    linked = {r["external_id"]: r["player_id"] for r in conn.execute(
        "SELECT player_id, external_id FROM player_external_ids WHERE source = 'nflverse'")}
    games = {r[0] for r in conn.execute("SELECT game_id FROM games WHERE league = 'NFL'")}
    with track_run(conn, "games:nfl_players", compare_previous=False) as run:
        for season in seasons:
            try:
                text = fetcher.get(WEEK_URL.format(season=season)).text
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    continue                              # season not published yet
                raise
            rows = [r for r in nfl_player_games(text, tracked, linked) if r[1] in games]
            conn.executemany(
                "INSERT INTO player_games (player_id, game_id, team, fantasy_ppr, source) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (player_id, game_id) DO UPDATE SET team = excluded.team, "
                "fantasy_ppr = excluded.fantasy_ppr", rows)
            store_raw(conn, "nflverse", f"week_{season}", {"rows": len(rows)})
            run.rows_written += len(rows)
            conn.commit()
    return run.rows_written


def epl_games_from_fixtures(payload: dict, season: int) -> list[tuple]:
    rows = []
    for m in payload.get("matches", []):
        score = m.get("score")
        ft = score.get("ft") if isinstance(score, dict) else score     # older files: "score": [2, 1]
        if not ft or len(ft) != 2 or not m.get("date"):
            continue
        if m["date"] < f"{season}-08-01":
            continue                                  # misdated (openfootball 2019-20 has July 2019 for July 2020)
        gid = f"EPL_{m['date']}_{m['team1']}_{m['team2']}"
        rows.append((gid, "EPL", season, None, "REG", m["date"], m["team1"], m["team2"], int(ft[0]), int(ft[1]),
                     "openfootball"))
    return rows


def ingest_epl_games(conn: sqlite3.Connection, first: int, last: int, fetcher: Fetcher | None = None) -> int:
    fetcher = fetcher or Fetcher(min_interval=1.0)
    with track_run(conn, "games:epl") as run:
        for season in range(first, last + 1):
            label = f"{season}-{(season + 1) % 100:02d}"
            try:
                payload = fetcher.get(EPL_FIXTURES_URL.format(label=label)).json()
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    continue
                raise
            run.rows_written += _save_games(conn, epl_games_from_fixtures(payload, season))
        conn.commit()
    return run.rows_written
