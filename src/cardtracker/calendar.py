"""League calendars: key dates per season, and the phases derived from them.

Events (all optional except season_start / regular_season_end / playoffs_start / championship):
  season_start, regular_season_end, playoffs_start, championship, plus anything imported
  from CSV (draft, trade_deadline, ...), which event studies can use directly.

Phases, for season S:
  early_season   season_start -> first third of the regular season
  mid_season     middle third
  late_season    final third -> playoffs_start
  playoffs       playoffs_start -> championship
  offseason      championship -> season_start of S+1
"""

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from cardtracker.fetcher import Fetcher
from cardtracker.runs import track_run

NFL_SCHEDULE_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
NBA_GAMELOG_URL = "https://stats.nba.com/stats/leaguegamelog"
NFL_PLAYOFF_TYPES = {"WC", "DIV", "CON", "SB"}

PHASES = ["early_season", "mid_season", "late_season", "playoffs", "offseason"]
SPORT_LEAGUE = {"football": "NFL", "basketball": "NBA"}


@dataclass(frozen=True)
class Phase:
    league: str
    season: int
    name: str
    start: date
    end: date


def save_events(conn: sqlite3.Connection, league: str, season: int, events: dict[str, str], source: str) -> int:
    conn.executemany(
        "INSERT INTO season_events (league, season, event, date, source) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (league, season, event) DO UPDATE SET date = excluded.date, source = excluded.source",
        [(league, season, e, d, source) for e, d in events.items()],
    )
    return len(events)


def nfl_events_from_schedule(csv_text: str) -> dict[int, dict[str, str]]:
    """Derive per-season key dates from the nflverse games.csv (completed and scheduled games)."""
    seasons: dict[int, dict[str, str]] = {}
    for g in csv.DictReader(io.StringIO(csv_text)):
        season, kind, day = int(g["season"]), g["game_type"], g["gameday"]
        ev = seasons.setdefault(season, {})
        if kind == "REG":
            ev["season_start"] = min(ev.get("season_start", day), day)
            ev["regular_season_end"] = max(ev.get("regular_season_end", day), day)
        elif kind in NFL_PLAYOFF_TYPES:
            ev["playoffs_start"] = min(ev.get("playoffs_start", day), day)
            if kind == "SB":
                ev["championship"] = day
    return seasons


def ingest_nfl(conn: sqlite3.Connection, fetcher: Fetcher | None = None) -> int:
    fetcher = fetcher or Fetcher(min_interval=1.0)
    with track_run(conn, "calendar:nfl") as run:
        seasons = nfl_events_from_schedule(fetcher.get(NFL_SCHEDULE_URL).text)
        for season, events in seasons.items():
            run.rows_written += save_events(conn, "NFL", season, events, "nflverse")
        conn.commit()
    return run.rows_written


def nba_game_dates(payload: dict) -> list[str]:
    result = payload["resultSets"][0]
    idx = result["headers"].index("GAME_DATE")
    return sorted({row[idx] for row in result["rowSet"]})


def ingest_nba(conn: sqlite3.Connection, first: int, last: int, fetcher: Fetcher | None = None) -> int:
    """NBA key dates from stats.nba.com game logs. Run from a home connection (cloud IPs time out)."""
    from cardtracker.stats.nba import HEADERS, season_label

    if fetcher is None:
        fetcher = Fetcher(min_interval=3.0)
        fetcher.session.headers.update(HEADERS)
    with track_run(conn, "calendar:nba") as run:
        for season in range(first, last + 1):
            events = {}
            for season_type in ("Regular Season", "Playoffs"):
                payload = fetcher.get(NBA_GAMELOG_URL, params={
                    "Counter": 0, "Direction": "ASC", "LeagueID": "00", "PlayerOrTeam": "T",
                    "Season": season_label(season), "SeasonType": season_type, "Sorter": "DATE",
                }).json()
                dates = nba_game_dates(payload)
                if not dates:
                    continue
                if season_type == "Regular Season":
                    events["season_start"], events["regular_season_end"] = dates[0], dates[-1]
                else:
                    events["playoffs_start"], events["championship"] = dates[0], dates[-1]
            run.rows_written += save_events(conn, "NBA", season, events, "nba_stats")
            conn.commit()
    return run.rows_written


def import_csv(conn: sqlite3.Connection, path: str) -> int:
    """Load events from a CSV with columns league, season, event, date (YYYY-MM-DD), source."""
    count = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            date.fromisoformat(row["date"])          # reject malformed dates
            count += save_events(conn, row["league"].upper(), int(row["season"]),
                                 {row["event"]: row["date"]}, row.get("source") or "manual")
    conn.commit()
    return count


def events(conn: sqlite3.Connection, league: str) -> dict[int, dict[str, date]]:
    out: dict[int, dict[str, date]] = {}
    for r in conn.execute("SELECT season, event, date FROM season_events WHERE league = ?", (league,)):
        out.setdefault(r["season"], {})[r["event"]] = date.fromisoformat(r["date"])
    return out


def phases(conn: sqlite3.Connection, league: str) -> list[Phase]:
    """All complete phases for a league. Seasons missing a required date are skipped."""
    ev = events(conn, league)
    out = []
    for season in sorted(ev):
        e = ev[season]
        if not {"season_start", "playoffs_start", "championship"} <= e.keys():
            continue
        start, po = e["season_start"], e["playoffs_start"]
        third = (po - start) / 3
        out += [
            Phase(league, season, "early_season", start, start + third),
            Phase(league, season, "mid_season", start + third, start + 2 * third),
            Phase(league, season, "late_season", start + 2 * third, po),
            Phase(league, season, "playoffs", po, e["championship"]),
        ]
        nxt = ev.get(season + 1, {}).get("season_start")
        if nxt:
            out.append(Phase(league, season, "offseason", e["championship"] + timedelta(days=1), nxt))
    return out
