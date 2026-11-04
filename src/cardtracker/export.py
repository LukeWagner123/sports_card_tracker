"""Export analysis tables as CSV files for charts (e.g. the dashboard).

    cardtracker export dashboard --out DIR

Writes, from Card Hedge daily prices:
  market_index.csv   weekly chained index per sport and grade (100 at the start)
  player_index.csv   weekly chained index per player, all their cards and grades combined
  players.csv        one row per player: 90-day and 1-year index change, history, top PSA 10 price,
                     sales in the last 3 closed months and their change from the 3 before
  sales_monthly.csv  sales count and dollars per sport per month (Card Hedge sales-stats)
  phases.csv         detrended median return per league, grade and season phase
  phase_seasons.csv  the same per season
  months.csv         detrended month-of-year effect per league and grade
  events.csv         detrended price path around season start and playoffs start, by week
  game_reactions.csv NFL card price change 1-7 and 8-14 days after games, by result, team
                     streak, the player's own output and regular season vs playoffs; horizon -14
                     is the two weeks before the game (a check against momentum)
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
    sales: dict[int, list] = defaultdict(list)
    for r in conn.execute("SELECT player_id, period_start, sales, total_usd FROM player_sales "
                          "WHERE interval = 'month' AND source = ? ORDER BY period_start", (SOURCE,)):
        sales[r["player_id"]].append((r["period_start"], r["sales"], r["total_usd"]))
    names = {r["id"]: r for r in conn.execute("SELECT id, name, sport FROM players")}

    idx_rows, player_rows = [], []
    for pid in sorted(set(by_player) | set(sales)):
        members = by_player.get(pid, [])
        idx = chain([s for _, s in members]) if members else []
        long_enough = bool(idx) and (idx[-1][0] - idx[0][0]).days >= min_history_days
        if long_enough:
            for d, level, _ in _weekly(idx):
                idx_rows.append((d.isoformat(), names[pid]["name"], names[pid]["sport"], round(level, 2)))
        c90 = change(idx, 90) if long_enough else None
        c365 = change(idx, 365) if long_enough else None
        months = sales.get(pid, [])
        last3 = sum(m[1] for m in months[-3:]) if len(months) >= 3 else None
        prev3 = sum(m[1] for m in months[-6:-3]) if len(months) >= 6 else None
        usd3 = sum(m[2] for m in months[-3:]) if len(months) >= 3 else None
        player_rows.append((names[pid]["name"], names[pid]["sport"],
                            None if c90 is None else round(c90, 1), None if c365 is None else round(c365, 1),
                            len({k[0] for k, _ in members}), idx[0][0].isoformat() if idx else None,
                            round(top_psa10[pid], 2) if pid in top_psa10 else None,
                            last3, round((last3 / prev3 - 1) * 100, 1) if last3 is not None and prev3 else None,
                            round(usd3 / last3, 2) if last3 else None))
    counts["player_index"] = _write(out / "player_index.csv", ["date", "player", "sport", "level"], idx_rows)
    counts["players"] = _write(out / "players.csv", ["player", "sport", "chg_90d", "chg_1y", "cards", "since",
                                                     "psa10_price", "sales_3m", "sales_chg", "avg_sale_3m"],
                               player_rows)
    monthly = conn.execute(
        "SELECT substr(s.period_start, 1, 7) AS month, p.sport, SUM(s.sales) AS sales, ROUND(SUM(s.total_usd)) AS usd, "
        "COUNT(DISTINCT s.player_id) AS players FROM player_sales s JOIN players p ON p.id = s.player_id "
        "WHERE s.interval = 'month' AND s.source = ? GROUP BY 1, 2 ORDER BY 1, 2", (SOURCE,)).fetchall()
    counts["sales_monthly"] = _write(out / "sales_monthly.csv", ["month", "sport", "sales", "usd", "players"],
                                     [tuple(r) for r in monthly])

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
    from cardtracker.analytics import game_reactions as gr
    cols = ["grade", "horizon_days", "group_type", "group", "median_pct", "mean_pct", "hit_rate", "n"]
    rows = gr.summarize(gr.reactions(conn, "NFL")) + gr.summarize(
        gr.reactions(conn, "NFL", pre=gr.BEFORE_PRE, horizons=gr.BEFORE))       # horizon -14: the 2 weeks before
    counts["game_reactions"] = _write(out / "game_reactions.csv", ["league"] + cols,
                                      [["NFL"] + [r[c] for c in cols] for r in rows])
    return counts


def _num(v: str):
    if v in ("", None):
        return None
    try:
        f = float(v)
    except ValueError:
        return v
    return int(f) if f.is_integer() and "." not in v else f


def _rows(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return [{k: _num(v) for k, v in r.items()} for r in csv.DictReader(f)]


def export_studio(conn: sqlite3.Connection, out: Path, as_of: str | None = None) -> dict[str, int]:
    """The dashboard tables as compact JSON for the personal site's private Studio (cards/ in the
    private studio repo). Per-player histories are split by sport so every file stays under
    GitHub's 1 MB limit for reading a file through its contents API."""
    import json
    import tempfile
    from datetime import datetime, timezone

    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        counts = export_dashboard(conn, tmp)
        t = {name: _rows(tmp / f"{name}.csv") for name in counts}
    files: dict[str, object] = {}
    market: dict = defaultdict(lambda: defaultdict(list))
    for r in t["market_index"]:
        market[r["sport"]][r["grade"]].append([r["date"], r["level"], r["series"]])
    files["market.json"] = market
    files["players.json"] = t["players"]
    files["seasonality.json"] = {k: t[k] for k in ("phases", "phase_seasons", "months", "events")}
    files["games.json"] = t["game_reactions"]
    files["sales.json"] = t["sales_monthly"]
    per_sport: dict = defaultdict(lambda: defaultdict(list))
    for r in t["player_index"]:
        per_sport[r["sport"]][r["player"]].append([r["date"], round(r["level"], 1)])
    for sport, players in per_sport.items():
        files[f"player-index-{sport}.json"] = players
    sales: dict = defaultdict(list)
    for r in conn.execute(
            "SELECT p.name, s.period_start, s.sales, s.total_usd FROM player_sales s JOIN players p ON p.id = s.player_id "
            "WHERE s.interval = 'month' ORDER BY s.period_start"):
        sales[r["name"]].append([r["period_start"][:7], r["sales"], round(r["total_usd"])])
    files["player-sales.json"] = sales
    files["meta.json"] = {
        "updated": as_of or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Card Hedge daily prices and sales counts; nflverse games; openfootball results",
        "rows": counts,
        "files": sorted(files),
    }
    sizes = {}
    for name, data in files.items():
        text = json.dumps(data, separators=(",", ":"))
        (out / name).write_text(text)
        sizes[name] = len(text)
    return sizes
