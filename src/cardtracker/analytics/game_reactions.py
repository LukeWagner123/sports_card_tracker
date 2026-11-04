"""How card prices react to games: wins, losses, streaks, and a player's own big or bad games.

For every game a tracked player played, the card's price in the week before the game is compared
with the week after (days 1-7) and the second week after (days 8-14). Card Hedge prices exist
only on sale days, so each window's price is the median of whatever prices fall in it, and a
game is used only when both windows have one. With detrend, the card's own trend over the
surrounding year is removed, as in the seasonality views.

Caveats: windows of consecutive weekly games overlap (one game's "after" is the next game's
"before"), so observations are not independent; and a team's results, a player's output and
league-wide news on the same weekend are tangled together. Read differences between groups,
with their counts, rather than any single number.
"""

import math
import sqlite3
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean, median, pstdev

from cardtracker.analytics.seasonality import drift_per_day

SOURCE = "cardhedge"
HORIZONS = {7: (1, 7), 14: (8, 14)}       # days after the game: (first, last)
PRE = (-7, -1)
# The two weeks leading into the game, as a check: if cards already moved before a result they
# couldn't know, a difference after the game is momentum, not a reaction to it.
BEFORE_PRE, BEFORE = (-21, -15), {-14: (-7, -1)}


def team_results(conn: sqlite3.Connection, league: str) -> dict[tuple[str, str], tuple[str, int, int]]:
    """(game_id, team) -> (result 'W'/'L'/'T', streak, margin). The streak is signed: +3 is the
    third straight win including this game, -2 the second straight loss. Resets each season."""
    by_team: dict[tuple[int, str], list] = defaultdict(list)
    for g in conn.execute("SELECT game_id, season, date, home, away, home_score, away_score FROM games "
                          "WHERE league = ? ORDER BY date", (league,)):
        by_team[(g["season"], g["home"])].append((g["game_id"], g["home_score"] - g["away_score"]))
        by_team[(g["season"], g["away"])].append((g["game_id"], g["away_score"] - g["home_score"]))
    out = {}
    for (_, team), games in by_team.items():
        streak = 0
        for gid, margin in games:
            if margin > 0:
                streak = streak + 1 if streak > 0 else 1
            elif margin < 0:
                streak = streak - 1 if streak < 0 else -1
            else:
                streak = 0
            out[(gid, team)] = ("W" if margin > 0 else "L" if margin < 0 else "T", streak, margin)
    return out


def streak_bucket(streak: int) -> str:
    if streak >= 3:
        return "W3+"
    if streak <= -3:
        return "L3+"
    return {2: "W2", 1: "W1", -1: "L1", -2: "L2"}.get(streak, "T")


def performance_bucket(z: float | None) -> str | None:
    if z is None:
        return None
    return "Big game" if z > 1 else "Bad game" if z < -1 else "Typical game"


@dataclass
class Reaction:
    player_id: int
    card_id: int
    grade: str
    game_id: str
    day: date
    game_type: str
    result: str
    streak: int
    margin: int
    ppr_z: float | None
    returns: dict[int, float]             # horizon days -> log change (detrended if asked)


def _window_median(dates: list[date], prices: list[float], day: date, lo: int, hi: int) -> float | None:
    a = bisect_left(dates, day + timedelta(days=lo))
    b = bisect_right(dates, day + timedelta(days=hi))
    return median(prices[a:b]) if b > a else None


GRADES = {"PSA 10": ("PSA", 10.0), "PSA 9": ("PSA", 9.0), "Raw": ("RAW", None)}


def reactions(conn: sqlite3.Connection, league: str = "NFL", detrend: bool = True,
              pre: tuple[int, int] = PRE, horizons: dict[int, tuple[int, int]] = HORIZONS) -> list[Reaction]:
    results = team_results(conn, league)
    games = defaultdict(list)                       # player_id -> [(game_id, date, type, team, ppr)]
    for r in conn.execute(
            "SELECT pg.player_id, pg.game_id, pg.team, pg.fantasy_ppr, g.date, g.game_type FROM player_games pg "
            "JOIN games g ON g.game_id = pg.game_id WHERE g.league = ? ORDER BY g.date", (league,)):
        games[r["player_id"]].append((r["game_id"], date.fromisoformat(r["date"]), r["game_type"], r["team"],
                                      r["fantasy_ppr"]))
    zs: dict[tuple[int, str], float] = {}
    for pid, gl in games.items():
        pts = [g[4] for g in gl if g[4] is not None]
        if len(pts) >= 5 and pstdev(pts) > 0:
            mu, sd = mean(pts), pstdev(pts)
            for g in gl:
                if g[4] is not None:
                    zs[(pid, g[0])] = (g[4] - mu) / sd
    series: dict[tuple, list] = defaultdict(list)
    for r in conn.execute(
            "SELECT s.card_id, c.player_id, s.grader, s.grade, s.snapshot_date, s.price_usd FROM price_snapshots s "
            "JOIN cards c ON c.id = s.card_id WHERE s.source = ? AND s.price_usd > 0 AND c.player_id IN "
            f"({','.join('?' * len(games)) or 'NULL'}) ORDER BY s.snapshot_date", (SOURCE, *games)):
        series[(r["card_id"], r["player_id"], r["grader"], r["grade"])].append(
            (date.fromisoformat(r["snapshot_date"]), r["price_usd"]))
    label = {v: k for k, v in GRADES.items()}
    out = []
    for (card_id, pid, grader, grade), s in series.items():
        if (grader, grade) not in label:
            continue
        dates, prices = [d for d, _ in s], [p for _, p in s]
        for gid, day, gtype, team, _ in games[pid]:
            if (gid, team) not in results:
                continue
            base = _window_median(dates, prices, day, *pre)
            if not base:
                continue
            drift = drift_per_day(s, day, 14, 1) if detrend else 0.0
            if drift is None:
                continue
            rets = {}
            for h, (lo, hi) in horizons.items():
                post = _window_median(dates, prices, day, lo, hi)
                if post:
                    days_between = (lo + hi) / 2 - sum(pre) / 2
                    rets[h] = math.log(post / base) - drift * days_between
            if rets:
                res, streak, margin = results[(gid, team)]
                out.append(Reaction(pid, card_id, label[(grader, grade)], gid, day, gtype, res, streak, margin,
                                    zs.get((pid, gid)), rets))
    return out


def summarize(rs: list[Reaction]) -> list[dict]:
    """Median, mean and share positive per grade, horizon and grouping (result, streak,
    performance, regular season vs playoffs)."""
    groups: dict[tuple, list[float]] = defaultdict(list)
    for r in rs:
        keys = [("result", r.result), ("streak", streak_bucket(r.streak)),
                ("game_type", "Regular season" if r.game_type == "REG" else "Playoffs")]
        if (pb := performance_bucket(r.ppr_z)) is not None:
            keys.append(("performance", pb))
        for h, v in r.returns.items():
            for grade in (r.grade, "All grades"):
                for gt, g in keys:
                    groups[(grade, h, gt, g)].append(v)
    rows = []
    for (grade, h, gt, g), vals in sorted(groups.items()):
        rows.append({"grade": grade, "horizon_days": h, "group_type": gt, "group": g, "n": len(vals),
                     "median_pct": round(math.expm1(median(vals)) * 100, 2),
                     "mean_pct": round(math.expm1(mean(vals)) * 100, 2),
                     "hit_rate": round(sum(v > 0 for v in vals) / len(vals), 3)})
    return rows
