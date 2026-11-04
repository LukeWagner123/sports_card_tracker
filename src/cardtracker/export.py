"""Export analysis tables as CSV files for charts (e.g. the dashboard).

    cardtracker export dashboard --out DIR

Writes, from Card Hedge daily prices:
  market_index.csv   weekly chained index per sport and grade (100 at the start)
  player_index.csv   weekly chained index per player, all their cards and grades combined
  players.csv        one row per player: 90-day and 1-year index change, history, top PSA 10 price
  phases.csv         detrended median return per league, grade and season phase
  phase_seasons.csv  the same per season
  months.csv         detrended month-of-year effect per league and grade
  events.csv         detrended price path around season start and playoffs start, by week
"""

import csv
import math
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

from cardtracker.analytics import seasonality as sz
from cardtracker.analytics.chain_index import chain, change

GRADES = [("PSA 10", "PSA", 10.0), ("PSA 9", "PSA", 9.0), ("Raw", "RAW", None)]
LEAGUES = {"NFL": "football", "NBA": "basketball", "EPL": "soccer"}
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
SOURCE = "cardhedge"


def _series(conn: sqlite3.Connection) -> dict[tuple, list[tuple[date, float]]]:
    """(card_id, grader, grade) -> daily prices, with each card's player and sport."""
    out: dict[tuple, list[tuple[date, float]]] = defaultdict(list)
    for r in conn.execute(
            "SELECT s.card_id, s.grader, s.grade, s.snapshot_date, s.price_usd FROM price_snapshots s "
            "WHERE s.source = ? AND s.price_usd > 0 ORDER BY s.snapshot_date", (SOURCE,)):
        out[(r["card_id"], r["grader"], r["grade"])].append((date.fromisoformat(r["snapshot_date"]), r["price_usd"]))
    return out


def _weekly(index):
    """Every 7th day from the start, plus the last day."""
    if not index:
        return []
    first = index[0][0]
    rows = [p for p in index if (p[0] - first).days % 7 == 0]
    return rows if rows[-1] is index[-1] else rows + [index[-1]]


def _write(path: Path, header: list[str], rows) -> int:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        n = 0
        for row in rows:
            w.writerow(row)
            n += 1
    return n


def _pct(log_value: float) -> float:
    return round(math.expm1(log_value) * 100, 2)


def export_dashboard(conn: sqlite3.Connection, out: Path, min_history_days: int = 180) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    series = _series(conn)
    cards = {r["id"]: r for r in conn.execute(
        "SELECT c.id, p.id AS player_id, p.name, p.sport FROM cards c JOIN players p ON p.id = c.player_id")}
    counts: dict[str, int] = {}

    market = []
    for sport in LEAGUES.values():
        for label, grader, grade in GRADES:
            picked = [s for (cid, g, gr), s in series.items()
                      if cards[cid]["sport"] == sport and g == grader and gr == grade]
            for d, level, n in _weekly(chain(picked, min_series=3)):
                market.append((d.isoformat(), sport, label, round(level, 2), n))
    counts["market_index"] = _write(out / "market_index.csv", ["date", "sport", "grade", "level", "series"], market)

    by_player: dict[int, list] = defaultdict(list)
    for (cid, g, gr), s in series.items():
        by_player[cards[cid]["player_id"]].append(((cid, g, gr), s))
    top_psa10: dict[int, float] = {}
    for (cid, g, gr), s in series.items():
        if g == "PSA" and gr == 10.0:
            pid = cards[cid]["player_id"]
            top_psa10[pid] = max(top_psa10.get(pid, 0), s[-1][1])
    idx_rows, player_rows = [], []
    for pid, members in by_player.items():
        idx = chain([s for _, s in members])
        if not idx or (idx[-1][0] - idx[0][0]).days < min_history_days:
            continue
        info = cards[members[0][0][0]]
        for d, level, _ in _weekly(idx):
            idx_rows.append((d.isoformat(), info["name"], info["sport"], round(level, 2)))
        c90, c365 = change(idx, 90), change(idx, 365)
        player_rows.append((info["name"], info["sport"],
                            None if c90 is None else round(c90, 1), None if c365 is None else round(c365, 1),
                            len({k[0] for k, _ in members}), idx[0][0].isoformat(),
                            round(top_psa10[pid], 2) if pid in top_psa10 else None))
    counts["player_index"] = _write(out / "player_index.csv", ["date", "player", "sport", "level"], idx_rows)
    counts["players"] = _write(out / "players.csv", ["player", "sport", "chg_90d", "chg_1y", "cards", "since",
                                                     "psa10_price"], player_rows)

    phases, seasons, months, events = [], [], [], []
    for league in LEAGUES:
        for label, grader, grade in GRADES:
            kw = dict(data="snapshots", source=SOURCE, detrend=True)
            for r in sz.phase_returns(conn, league, grader, grade, **kw):
                phases.append((league, label, r.key, _pct(r.median), round(r.hit_rate, 3), r.n))
                seasons += [(league, label, r.key, s, _pct(v)) for s, v in r.by_season.items()]
            for r in sz.month_of_year(conn, league, grader, grade, **kw):
                months.append((league, label, MONTHS.index(r.key) + 1, r.key, _pct(r.median),
                               round(r.hit_rate, 3), r.n))
        for event in ("season_start", "playoffs_start"):
            for r in sz.event_study(conn, league, event, weeks_before=8, weeks_after=12, grader="PSA", grade=10.0,
                                    data="snapshots", source=SOURCE, detrend=True):
                events.append((league, event, int(r.key.rstrip("w")), _pct(r.median), r.n))
    counts["phases"] = _write(out / "phases.csv", ["league", "grade", "phase", "median_pct", "hit_rate", "n"], phases)
    counts["phase_seasons"] = _write(out / "phase_seasons.csv", ["league", "grade", "phase", "season", "pct"], seasons)
    counts["months"] = _write(out / "months.csv", ["league", "grade", "month", "month_name", "median_pct",
                                                   "hit_rate", "n"], months)
    counts["events"] = _write(out / "events.csv", ["league", "event", "week", "median_pct", "n"], events)
    return counts
