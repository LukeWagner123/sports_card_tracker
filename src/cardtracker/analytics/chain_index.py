"""Chained price indexes: a market, sport or player level built from many card price series.

Each day's index move is the average daily log change of the series priced on both that day and
the day before, chained from 100. Series can start and stop (new cards, cards with no recent
sales) without their price levels causing jumps, which a plain average of prices would do: adding
a $2,000 card to a basket of $50 cards would look like a rally.

Card Hedge records a price only on days a card sells (about one day in four for a typical card),
so series are forward-filled between observations (a missing day is no change; the next
observation carries the whole move), and past a series' last sale for up to `stale_days`, never
beyond the latest date in the data. A card that has not sold for longer drops out. Daily moves
are clipped at +/- `clip` in log terms, so one bad print in a thinly traded card can't swing the
index.
"""

import math
from collections import defaultdict
from datetime import date, timedelta

Series = list[tuple[date, float]]


def _filled(series: Series, until: date) -> dict[date, float]:
    out: dict[date, float] = {}
    pts = sorted(series)
    for (d, p), nxt in zip(pts, pts[1:] + [None]):
        end = nxt[0] if nxt else until + timedelta(days=1)
        while d < end:
            out[d] = p
            d += timedelta(days=1)
    return out


def chain(series: list[Series], min_series: int = 1, clip: float = 0.4,
          stale_days: int = 45) -> list[tuple[date, float, int]]:
    """(date, index level, series priced that day) from the first observation to the last day with
    at least min_series priced; a thinner day in between carries the level forward unchanged."""
    series = [s for s in series if s]
    if not series:
        return []
    latest = max(max(d for d, _ in s) for s in series)
    filled = [_filled(s, min(max(d for d, _ in s) + timedelta(days=stale_days), latest)) for s in series]
    first = min(min(f) for f in filled)
    last = max(max(f) for f in filled)
    by_day: dict[date, list[float]] = defaultdict(list)
    for f in filled:
        for d, p in f.items():
            prev = f.get(d - timedelta(days=1))
            if prev and p > 0 and prev > 0:
                by_day[d].append(max(-clip, min(clip, math.log(p / prev))))
    level, out = 0.0, [(first, 100.0, sum(1 for f in filled if first in f))]
    d = first + timedelta(days=1)
    while d <= last:
        moves = by_day.get(d, [])
        if len(moves) >= min_series:
            level += sum(moves) / len(moves)
        out.append((d, 100 * math.exp(level), len(moves)))
        d += timedelta(days=1)
    while len(out) > 1 and out[-1][2] < min_series:
        out.pop()
    return out


def change(index: list[tuple[date, float, int]], days: int) -> float | None:
    """Percent change over the last `days` of an index, or None if it is shorter than that."""
    if not index:
        return None
    end_day, end_level, _ = index[-1]
    start = end_day - timedelta(days=days)
    past = [lvl for d, lvl, _ in index if d <= start]
    return (end_level / past[-1] - 1) * 100 if past and index[0][0] <= start else None
