"""Deal signals per card and grade, and a backtest of each on the price history.

Signals at a date use only prices up to that date (except the seasonal one, see below):

  near_low        price in the bottom 20% of its 52-week range
  demand_lag      the player's sales in the last 3 closed months are up 30%+ on the 3 before
                  (at least 1,000 sales) while this card's price is flat or down over 90 days
  seasonal        the league's month-of-year pattern points up 5%+ over the next 2 months
  falling         down 20%+ in 30 days (information: realistically tested, falling cards did no
                  worse than the market afterwards)
  thin            fewer than 6 priced days in 90 (a warning: the price is a guess)

The backtest takes the first of every month from 2021, finds the cards each signal fired on, and
measures their next-60-day change against the median of every priced card on that date
("excess"). The 2021-24 crash made nearly everything fall, so the raw change would say
everything is a bad buy; the excess says whether the signal picked better than the market.

It runs two ways. "Naive" buys at the signal day's price and sells at the price 60 days later.
"Realistic" buys at the median price 1-3 weeks after the signal and sells at the median two weeks
around day 60. Card Hedge's daily price is often a single sale, so a cheap one-off sale looks like
a dip that "recovers" in the naive test; nobody could buy at that price after the fact. Only the
realistic result decides whether a signal counts. In November 2026 no signal beat the market
there: the naive gains were that one-off-sale noise. The deal that exists is the cheap sale
itself, so fair_value() says what a card normally sells for, to judge a listing against.

The seasonal signal uses month effects estimated from the whole history, including the months it
is tested on, so its backtest is in-sample and flatters it. Read it as "the pattern held on
average", not as an out-of-sample result.
"""

import math
import sqlite3
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median

SOURCE = "cardhedge"
GRADE_LABEL = {("PSA", 10.0): "PSA 10", ("PSA", 9.0): "PSA 9", ("RAW", None): "Raw"}
STALE_DAYS = 45
HORIZON = 60
FEES = 0.15            # round trip: eBay final value fee (~13%) plus shipping and supplies

BUY_SIGNALS = ("near_low", "demand_lag", "seasonal")
WARNINGS = ("falling", "thin")
DESCRIPTIONS = {
    "near_low": "In the bottom 20% of its 52-week range",
    "demand_lag": "Player's sales up 30%+ over 3 months while this card's price hasn't moved up",
    "seasonal": "The league's calendar has pointed up 5%+ over the next two months",
    "falling": "Down 20%+ in the last 30 days",
    "thin": "Fewer than 6 priced days in 90: the price is a guess",
    "all": "Every priced card (the market)",
    "combo": "Two or more buy signals at once",
}


class Series:
    def __init__(self, points: list[tuple[date, float]]):
        self.dates = [d for d, _ in points]
        self.prices = [p for _, p in points]

    def at(self, day: date) -> float | None:
        """Last price on or before `day`, if no older than STALE_DAYS."""
        i = bisect_right(self.dates, day) - 1
        if i < 0 or (day - self.dates[i]).days > STALE_DAYS:
            return None
        return self.prices[i]

    def between(self, lo: date, hi: date) -> list[float]:
        return self.prices[bisect_right(self.dates, lo):bisect_right(self.dates, hi)]

    def count(self, lo: date, hi: date) -> int:
        return bisect_right(self.dates, hi) - bisect_right(self.dates, lo)

    def last_day(self, day: date) -> date | None:
        i = bisect_right(self.dates, day) - 1
        return self.dates[i] if i >= 0 else None


@dataclass
class Snapshot:
    price: float
    chg_30d: float | None
    chg_90d: float | None
    chg_365d: float | None
    low_1y: float
    high_1y: float
    median_1y: float
    range_pos: float | None          # 0 at the 52-week low, 1 at the high
    priced_days_90: int
    last_priced: date
    signals: list[str] = field(default_factory=list)


def _chg(now: float, then: float | None) -> float | None:
    return (now / then - 1) * 100 if then else None


def snapshot(s: Series, day: date, demand: float | None, seasonal: float | None) -> Snapshot | None:
    p = s.at(day)
    if p is None:
        return None
    year = s.between(day - timedelta(days=365), day)
    if len(year) < 3:
        return None
    lo, hi = min(year), max(year)
    snap = Snapshot(
        price=p, chg_30d=_chg(p, s.at(day - timedelta(days=30))), chg_90d=_chg(p, s.at(day - timedelta(days=90))),
        chg_365d=_chg(p, s.at(day - timedelta(days=365))), low_1y=lo, high_1y=hi, median_1y=median(year),
        range_pos=(p - lo) / (hi - lo) if hi > lo else None, priced_days_90=s.count(day - timedelta(days=90), day),
        last_priced=s.last_day(day))
    if snap.range_pos is not None and snap.range_pos <= 0.2:
        snap.signals.append("near_low")
    if demand is not None and demand >= 30 and snap.chg_90d is not None and snap.chg_90d <= 0:
        snap.signals.append("demand_lag")
    if seasonal is not None and seasonal >= 5:
        snap.signals.append("seasonal")
    if snap.chg_30d is not None and snap.chg_30d <= -20:
        snap.signals.append("falling")
    if snap.priced_days_90 < 6:
        snap.signals.append("thin")
    return snap


def load(conn: sqlite3.Connection):
    """Card info, price series per (card, grade), player monthly sales, and month effects."""
    cards = {r["id"]: dict(r) for r in conn.execute(
        "SELECT c.id, c.player_id, p.name AS player, p.sport, c.year, c.manufacturer, c.set_name, c.card_number, "
        "c.is_rookie FROM cards c JOIN players p ON p.id = c.player_id "
        "WHERE c.id IN (SELECT card_id FROM card_external_ids WHERE source = ?)", (SOURCE,))}
    raw: dict[tuple, list] = defaultdict(list)
    for r in conn.execute("SELECT card_id, grader, grade, snapshot_date, price_usd FROM price_snapshots "
                          "WHERE source = ? AND price_usd > 0 ORDER BY snapshot_date", (SOURCE,)):
        if (r["grader"], r["grade"]) in GRADE_LABEL and r["card_id"] in cards:
            raw[(r["card_id"], GRADE_LABEL[(r["grader"], r["grade"])])].append(
                (date.fromisoformat(r["snapshot_date"]), r["price_usd"]))
    series = {k: Series(v) for k, v in raw.items()}
    sales: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for r in conn.execute("SELECT player_id, period_start, sales FROM player_sales WHERE interval = 'month' "
                          "ORDER BY period_start"):
        sales[r["player_id"]].append((r["period_start"][:7], r["sales"]))
    return cards, series, sales


def demand_change(months: list[tuple[str, int]], day: date, min_sales: int = 1000) -> float | None:
    """Sales in the 3 closed months before `day` vs the 3 before those, in percent."""
    closed = [m for m in months if m[0] < day.strftime("%Y-%m")]
    if len(closed) < 6:
        return None
    last3, prev3 = sum(m[1] for m in closed[-3:]), sum(m[1] for m in closed[-6:-3])
    return (last3 / prev3 - 1) * 100 if prev3 and last3 >= min_sales else None


def month_effects(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[int, float]]:
    """(league, grade) -> month -> detrended log level effect, from the seasonality analysis."""
    from cardtracker.analytics import seasonality as sz
    out = {}
    for league in ("NFL", "NBA", "EPL"):
        for (grader, grade), label in GRADE_LABEL.items():
            rows = sz.month_of_year(conn, league, grader, grade, data="snapshots", source=SOURCE, detrend=True)
            names = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
            out[(league, label)] = {names.index(r.key) + 1: r.median for r in rows if r.n >= 5}
    return out


def seasonal_outlook(effects: dict[int, float], day: date, months_ahead: int = 2) -> float | None:
    """Expected percent move from this month to `months_ahead` later, from month effects."""
    m0, m1 = day.month, (day.month - 1 + months_ahead) % 12 + 1
    if m0 not in effects or m1 not in effects:
        return None
    return math.expm1(effects[m1] - effects[m0]) * 100


LEAGUE = {"football": "NFL", "basketball": "NBA", "soccer": "EPL"}


def _typical(s: Series, day: date, days: int = 14) -> float | None:
    v = s.between(day - timedelta(days=days), day)
    return median(v) if len(v) >= 2 else None


def backtest(conn: sqlite3.Connection, start: date = date(2021, 1, 1), today: date | None = None,
             cache=None, realistic: bool = True) -> list[dict]:
    """Per signal: how the cards it fired on did over the next HORIZON days, against the market."""
    cards, series, sales = cache or load(conn)
    effects = month_effects(conn)
    today = today or date.today()
    excess: dict[str, list[float]] = defaultdict(list)
    raw: dict[str, list[float]] = defaultdict(list)
    day = start
    while day <= today - timedelta(days=HORIZON + 7):
        moves = []
        for (cid, grade), s in series.items():
            card = cards[cid]
            snap = snapshot(s, day, demand_change(sales.get(card["player_id"], []), day),
                            seasonal_outlook(effects.get((LEAGUE[card["sport"]], grade), {}), day))
            if realistic:
                entry = _typical(s, day + timedelta(days=21))
                later = _typical(s, day + timedelta(days=HORIZON + 7))
            else:
                entry, later = (snap.price if snap else None), s.at(day + timedelta(days=HORIZON))
            if snap and entry and later:
                moves.append((snap.signals, math.log(later / entry)))
        if len(moves) >= 10:
            base = median(m for _, m in moves)
            for sigs, m in moves:
                for sig in sigs + ["all"] + (["combo"] if sum(x in BUY_SIGNALS for x in sigs) >= 2 else []):
                    excess[sig].append(m - base)
                    raw[sig].append(m)
        day = (day.replace(day=1) + timedelta(days=32)).replace(day=1)
    out = []
    for sig in ("all",) + BUY_SIGNALS + ("combo",) + WARNINGS:
        vals = excess.get(sig, [])
        if not vals:
            continue
        out.append({"signal": sig, "description": DESCRIPTIONS[sig],
                    "n": len(vals), "median_excess_pct": round(math.expm1(median(vals)) * 100, 2),
                    "hit_rate": round(sum(v > 0 for v in vals) / len(vals), 3),
                    "median_raw_pct": round(math.expm1(median(raw[sig])) * 100, 2),
                    "in_sample": sig == "seasonal", "method": "realistic" if realistic else "naive"})
    return out


def fair_value(s: Series, day: date) -> dict | None:
    """What the card normally sells for now: the median of the last 30 days' prices (90 if fewer
    than 3), the 90-day middle range, and the price under which a listing is a good deal: below
    the 90-day 25th percentile and at least 15% under fair value, which is about where a resale at
    fair value covers selling costs."""
    recent = s.between(day - timedelta(days=30), day)
    window = 30
    if len(recent) < 3:
        recent, window = s.between(day - timedelta(days=90), day), 90
    if len(recent) < 3:
        return None
    q = sorted(s.between(day - timedelta(days=90), day))
    fair = median(recent)
    p25, p75 = q[int(0.25 * (len(q) - 1))], q[int(0.75 * (len(q) - 1))]
    return {"fair": round(fair, 2), "fair_window_days": window, "p25_90": round(p25, 2), "p75_90": round(p75, 2),
            "deal_under": round(min(p25, fair * (1 - FEES)), 2), "breakeven_buy": round(fair * (1 - FEES), 2)}


def passes(t: dict) -> bool:
    """A signal counts only if, tested realistically and out of sample, it beat the market by 3%+
    with a 53%+ hit rate over 100+ cases. 'falling' never counts as a buy; it is information."""
    return (t["signal"] in BUY_SIGNALS and not t.get("in_sample") and t.get("method", "realistic") == "realistic"
            and t["n"] >= 100 and t["median_excess_pct"] >= 3 and t["hit_rate"] >= 0.53)


def current(conn: sqlite3.Connection, today: date | None = None, cache=None, tested: list[dict] | None = None) -> list[dict]:
    """Every card-grade priced now, with its numbers, signals, a play and a buy-under price.

    A signal counts toward a play only if it passes() its realistic backtest. The estimated edge
    adds those signals' median excess; a play is a 'Buy watch' when that edge clears round-trip
    fees, 'Watch' below that. A thin price vetoes both. With no signal passing, there are no plays:
    the page then rests on fair value, which is where a deal shows up (a listing under it)."""
    cards, series, sales = cache or load(conn)
    effects = month_effects(conn)
    today = today or date.today()
    tested = tested if tested is not None else backtest(conn, today=today, cache=(cards, series, sales))
    tested = [t for t in tested if t.get("method", "realistic") == "realistic"]
    worked = {t["signal"]: t["median_excess_pct"] for t in tested if passes(t)}
    rows = []
    for (cid, grade), s in series.items():
        card = cards[cid]
        demand = demand_change(sales.get(card["player_id"], []), today)
        outlook = seasonal_outlook(effects.get((LEAGUE[card["sport"]], grade), {}), today)
        snap = snapshot(s, today, demand, outlook)
        if not snap:
            continue
        good = [x for x in snap.signals if x in worked]
        warn = [x for x in snap.signals if x in WARNINGS]
        edge = sum(worked[x] for x in good)
        if "thin" in warn:
            play = "Too thin to judge"
        elif good and edge >= FEES * 100:
            play = "Buy watch"
        elif good:
            play = "Watch"
        else:
            play = ""
        year_prices = sorted(s.between(today - timedelta(days=365), today))
        # Buy under: the cheaper of today's price and the card's 25th-percentile price this year.
        buy_under = min(snap.price, year_prices[int(0.25 * (len(year_prices) - 1))])
        fv = fair_value(s, today) or {}
        rows.append({
            **fv,
            "card_id": cid, "player": card["player"], "sport": card["sport"], "league": LEAGUE[card["sport"]],
            "year": card["year"], "set": f"{card['manufacturer'] or ''} {card['set_name']}".strip(),
            "number": card["card_number"], "rookie": bool(card["is_rookie"]), "grade": grade,
            "price": round(snap.price, 2), "chg_30d": _r(snap.chg_30d), "chg_90d": _r(snap.chg_90d),
            "chg_365d": _r(snap.chg_365d), "low_1y": round(snap.low_1y, 2), "high_1y": round(snap.high_1y, 2),
            "median_1y": round(snap.median_1y, 2), "range_pos": None if snap.range_pos is None else round(snap.range_pos, 3),
            "priced_days_90": snap.priced_days_90, "last_priced": snap.last_priced.isoformat(),
            "player_sales_chg": _r(demand), "seasonal_60d": _r(outlook),
            "signals": snap.signals, "edge_pct": round(edge, 1), "play": play, "buy_under": round(buy_under, 2),
        })
    return rows


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


def weekly_history(series: Series, start: date | None = None) -> list[list]:
    """[date, price] for the last observation in each week (for charts)."""
    out, week = [], None
    for d, p in zip(series.dates, series.prices):
        if start and d < start:
            continue
        key = d - timedelta(days=d.weekday())
        if key == week:
            out[-1] = [d.isoformat(), round(p, 2)]
        else:
            out.append([d.isoformat(), round(p, 2)])
            week = key
    return out
