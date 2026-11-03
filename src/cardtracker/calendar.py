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

Leagues without playoffs (the Premier League) end at regular_season_end: their thirds span
season_start -> regular_season_end, and the offseason runs from there to the next season_start.
International tournaments (World Cup, Euros; mostly in the soccer offseason, but Qatar 2022 was
mid-season) are imported as tournament_start / tournament_final events, for event studies.
"""

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from cardtracker.fetcher import Fetcher
from cardtracker.runs import track_run

NFL_SCHEDULE_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
NBA_GAMELOG_URL = "https://stats.nba.com/stats/leaguegamelog"
NFL_PLAYOFF_TYPES = {"WC", "DIV", "CON", "SB"}
# openfootball (CC0 public domain): one JSON file of fixtures per Premier League season.
EPL_FIXTURES_URL = "https://raw.githubusercontent.com/openfootball/football.json/master/{label}/en.1.json"

PHASES = ["early_season", "mid_season", "late_season", "playoffs", "offseason"]
SPORT_LEAGUE = {"football": "NFL", "basketball": "NBA", "soccer": "EPL"}
NO_PLAYOFFS = {"EPL"}


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


def epl_events_from_fixtures(payload: dict, season: int) -> dict[str, str]:
    """First and last matchday. The league never plays before August of its start year, so a
    fixture dated earlier means the file is wrong (openfootball's 2019-20 file dates the July 2020
    restart matches as July 2019): such a season returns nothing, and its dates belong in
    calendars/epl_overrides.csv instead."""
    dates = sorted(m["date"] for m in payload.get("matches", []) if m.get("date"))
    if not dates or dates[0] < f"{season}-08-01":
        return {}
    return {"season_start": dates[0], "regular_season_end": dates[-1]}


def ingest_epl(conn: sqlite3.Connection, first: int, last: int, fetcher: Fetcher | None = None) -> int:
    """Premier League season dates (first and last matchday) from openfootball fixtures. For the
    season in progress the last date is the final scheduled fixture."""
    fetcher = fetcher or Fetcher(min_interval=1.0)
    with track_run(conn, "calendar:epl") as run:
        for season in range(first, last + 1):
            label = f"{season}-{(season + 1) % 100:02d}"
            try:
                payload = fetcher.get(EPL_FIXTURES_URL.format(label=label)).json()
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    continue            # season not published yet
                raise
            events = epl_events_from_fixtures(payload, season)
            if not events:
                print(f"EPL {label}: fixture dates look wrong; skipped (see calendars/epl_overrides.csv)")
            run.rows_written += save_events(conn, "EPL", season, events, "openfootball")
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
    playoffs = league not in NO_PLAYOFFS
    for season in sorted(ev):
        e = ev[season]
        needed = {"season_start", "playoffs_start", "championship"} if playoffs else {"season_start", "regular_season_end"}
        if not needed <= e.keys():
            continue
        start = e["season_start"]
        regular_end = e["playoffs_start"] if playoffs else e["regular_season_end"]
        season_end = e["championship"] if playoffs else e["regular_season_end"]
        nxt = ev.get(season + 1, {}).get("season_start")
        if not playoffs and not nxt and season_end >= date.today():
            continue                    # season in progress: its last date is only scheduled
        third = (regular_end - start) / 3
        out += [
            Phase(league, season, "early_season", start, start + third),
            Phase(league, season, "mid_season", start + third, start + 2 * third),
            Phase(league, season, "late_season", start + 2 * third, regular_end),
        ]
        if playoffs:
            out.append(Phase(league, season, "playoffs", regular_end, season_end))
        if nxt:
            out.append(Phase(league, season, "offseason", season_end + timedelta(days=1), nxt))
    return out
