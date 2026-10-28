"""Seasonal price behavior: how card prices move through a league's calendar.

Three views, built from one league's price series: individual sales, daily price-guide values
(price_snapshots), or player indexes (see DATA_SOURCES):

- phase_returns:   log return from the start to the end of each phase (early/mid/late season,
                   playoffs, offseason), aggregated across cards and seasons.
- event_study:     average price path in the weeks around an event (e.g. playoffs_start),
                   relative to the price at the event.
- month_of_year:   each month's median price relative to that card's median for the year.

Prices are noisy and sparse, so a "price level" at a date is the median of sales within
± window_days, and requires min_sales of them. Returns are log returns (symmetric, additive).
Market-wide booms and crashes hit every card at once; check per-season breakdowns before
trusting a pooled number driven by one year.
"""

import math
import sqlite3
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean, median

from cardtracker.calendar import PHASES, SPORT_LEAGUE, events, phases

Series = list[tuple[date, float]]          # sorted (sale date, price)


@dataclass
class Summary:
    key: str
    n: int
    median: float
    mean: float
    hit_rate: float                        # share of observations > 0
    by_season: dict[int, float]            # median per season

    @property
    def median_pct(self) -> float:
        return math.expm1(self.median) * 100


# Where series come from: individual sales, daily price-guide values, player indexes, or Google
# Trends search interest (attention, not price).
DATA_SOURCES = ("sales", "snapshots", "player_index", "trends")

# Trends series are weekly or monthly points, not many sales a day: one point near a date is
# enough, and the window must reach the nearest monthly point.
SPARSE_LEVEL = {"window_days": 16, "min_sales": 1}


def load_series(conn: sqlite3.Connection, league: str, grader: str | None = None,
                grade: float | None = None, data: str = "sales") -> dict[tuple, Series]:
    """Series for one league, grouped by (card_id, grader, grade); by (player_id,) for player
    indexes; by (player_id, batch) for trends, since each Trends download has its own scale.
    grader/grade apply only to card prices."""
    sport = {v: k for k, v in SPORT_LEAGUE.items()}[league]
    series: dict[tuple, Series] = defaultdict(list)
    if data == "trends":
        rows = conn.execute(
            "SELECT t.player_id, t.batch, t.date AS d, t.value FROM search_interest t "
            "JOIN players p ON p.id = t.player_id WHERE p.sport = ? AND t.value > 0 ORDER BY d", (sport,))
        for r in rows:
            series[(r["player_id"], r["batch"])].append((date.fromisoformat(r["d"]), r["value"]))
        return series
    if data == "player_index":
        rows = conn.execute(
            "SELECT i.player_id, i.date AS d, i.value FROM player_index i "
            "JOIN players p ON p.id = i.player_id WHERE p.sport = ? AND i.value > 0 ORDER BY d", (sport,))
        for r in rows:
            series[(r["player_id"],)].append((date.fromisoformat(r["d"]), r["value"]))
        return series
    if data == "snapshots":
        sql = ("SELECT s.card_id, s.grader, s.grade, s.snapshot_date AS d, s.price_usd "
               "FROM price_snapshots s JOIN cards c ON c.id = s.card_id JOIN players p ON p.id = c.player_id "
               "WHERE p.sport = ? AND s.price_usd > 0")
    elif data == "sales":
        sql = ("SELECT s.card_id, s.grader, s.grade, substr(s.sold_at, 1, 10) AS d, s.price_usd "
               "FROM sales s JOIN cards c ON c.id = s.card_id JOIN players p ON p.id = c.player_id "
               "WHERE p.sport = ? AND s.price_usd > 0")
    else:
        raise ValueError(f"data must be one of {DATA_SOURCES}")
    params: list = [sport]
    if grader:
        sql += " AND s.grader = ?"
        params.append(grader)
    if grade is not None:
        sql += " AND s.grade = ?"
        params.append(grade)
    for r in conn.execute(sql + " ORDER BY d", params):
        series[(r["card_id"], r["grader"], r["grade"])].append((date.fromisoformat(r["d"]), r["price_usd"]))
    return series


def level_at(series: Series, when: date, window_days: int = 14, min_sales: int = 2) -> float | None:
    """Median price of sales within ± window_days of `when`, or None if too few."""
    dates = [d for d, _ in series]
    lo = bisect_left(dates, when - timedelta(days=window_days))
    hi = bisect_right(dates, when + timedelta(days=window_days))
    prices = [p for _, p in series[lo:hi]]
    return median(prices) if len(prices) >= min_sales else None


def _summarize(key: str, obs: list[tuple[int, float]]) -> Summary | None:
    if not obs:
        return None
    vals = [v for _, v in obs]
    seasons: dict[int, list[float]] = defaultdict(list)
    for s, v in obs:
        seasons[s].append(v)
    return Summary(key, len(vals), median(vals), mean(vals),
                   sum(v > 0 for v in vals) / len(vals),
                   {s: median(v) for s, v in sorted(seasons.items())})


def phase_returns(conn: sqlite3.Connection, league: str, grader: str | None = None,
                  grade: float | None = None, window_days: int = 14, min_sales: int = 2,
                  data: str = "sales") -> list[Summary]:
    series = load_series(conn, league, grader, grade, data)
    obs: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for ph in phases(conn, league):
        for s in series.values():
            a = level_at(s, ph.start, window_days, min_sales)
            b = level_at(s, ph.end, window_days, min_sales)
            if a and b:
                obs[ph.name].append((ph.season, math.log(b / a)))
    return [summ for name in PHASES if (summ := _summarize(name, obs[name]))]


def event_study(conn: sqlite3.Connection, league: str, event: str, weeks_before: int = 8,
                weeks_after: int = 12, grader: str | None = None, grade: float | None = None,
                window_days: int = 7, min_sales: int = 2, data: str = "sales") -> list[Summary]:
    """Average log price relative to the event date, at each weekly offset."""
    series = load_series(conn, league, grader, grade, data)
    obs: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for season, ev in events(conn, league).items():
        if event not in ev:
            continue
        for s in series.values():
            base = level_at(s, ev[event], window_days, min_sales)
            if not base:
                continue
            for w in range(-weeks_before, weeks_after + 1):
                lvl = level_at(s, ev[event] + timedelta(weeks=w), window_days, min_sales)
                if lvl:
                    obs[w].append((season, math.log(lvl / base)))
    return [summ for w in sorted(obs) if (summ := _summarize(f"{w:+d}w", obs[w]))]


def month_of_year(conn: sqlite3.Connection, league: str, grader: str | None = None,
                  grade: float | None = None, min_sales: int = 2, data: str = "sales") -> list[Summary]:
    """Each calendar month's median price vs. the same card's median for that year."""
    series = load_series(conn, league, grader, grade, data)
    obs: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for s in series.values():
        by_year: dict[int, list[float]] = defaultdict(list)
        by_month: dict[tuple[int, int], list[float]] = defaultdict(list)
        for d, p in s:
            by_year[d.year].append(p)
            by_month[(d.year, d.month)].append(p)
        for (year, month), prices in by_month.items():
            if len(prices) >= min_sales and len(by_year[year]) >= 6 * min_sales:
                obs[month].append((year, math.log(median(prices) / median(by_year[year]))))
    names = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return [summ for m in range(1, 13) if (summ := _summarize(names[m - 1], obs[m]))]
