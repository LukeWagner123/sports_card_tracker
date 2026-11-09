"""Trading-market views of the card data, for the Studio.

  risk()          per card and grade: yearly volatility, correlation with its market, worst drop
                  in the last year, and liquidity (how many days a year it actually sells)
  spread_series() the PSA 10 / PSA 9 price ratio of one card over time; cheap_grade fires when
                  one grade is unusually cheap next to the other (1.5 standard deviations off the
                  card's own one-year norm)
  production()    NFL on-field output: PPR fantasy points per game over a player's last 17 games
  valuation()     NFL PSA 10 prices against output: a cross-section regression of log price on
                  log points per game, position and career year. A card priced far under the
                  line is cheap for what the player produces; cheap_vs_output fires on the bottom
                  fifth. It fits only on games and prices known on the day, so it is a fair
                  out-of-sample test when run through signals.backtest().
  paper_step()    a forward paper portfolio: each month each strategy buys up to 10 card-grades
                  at fair value, sells them 60 days later at fair value less ~15% costs. Only a
                  forward record can't be fooled by hindsight.
  brief()         plain-English headlines for the top of the Studio

Card Hedge records a price on days a card sells, often from one sale, so every measure here uses
cleaned series (bad prints dropped) and medians where it can.
"""

import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import mean, median, pstdev

from cardtracker.analytics import signals as sg
from cardtracker.analytics.chain_index import chain

WEEK = timedelta(days=7)
SPREAD_Z = 1.5
POSITIONS = ("QB", "RB", "WR", "TE")
PAPER_SLOTS = 10
PAPER_STAKE = 100.0
PAPER_MIN_PRICE = 10.0
STRATEGIES = {
    "market": "Every liquid card-grade, equal weight: the benchmark",
    "cheap_vs_output": "The 10 NFL PSA 10s priced furthest under their output",
    "cheap_grade": "The 10 grades cheapest next to the same card's other grade",
    "near_low": "The 10 liquid cards nearest their 1-year low",
}


# ------------------------------------------------------------------------------------ helpers

def clean(s: sg.Series) -> sg.Series:
    """The series without bad prints (see signals._outlier)."""
    return sg.Series([(d, p) for i, (d, p) in enumerate(zip(s.dates, s.prices)) if not sg._outlier(s.prices, i)])


def typical(s: sg.Series, day: date, days: int = 30) -> float | None:
    v = s.between(day - timedelta(days=days), day)
    return median(v) if len(v) >= 2 else None


def liquidity(priced_days_365: int) -> str:
    """A: sells most weeks several times; D: a handful of times a year."""
    return "A" if priced_days_365 >= 120 else "B" if priced_days_365 >= 48 else "C" if priced_days_365 >= 12 else "D"


LIQUIDITY_WORDS = {"A": "trades often", "B": "trades weekly-ish", "C": "trades a few times a month", "D": "rarely trades"}


def market_levels(cards: dict, series: dict) -> dict[tuple[str, str], dict[date, float]]:
    """(sport, grade) -> date -> chained index level."""
    groups: dict[tuple, list] = defaultdict(list)
    for (cid, grade), s in series.items():
        groups[(cards[cid]["sport"], grade)].append(list(zip(s.dates, s.prices)))
    return {k: {d: lvl for d, lvl, _ in chain(v, min_series=3)} for k, v in groups.items()}


def _level_at(levels: dict[date, float], day: date) -> float | None:
    for back in range(8):
        v = levels.get(day - timedelta(days=back))
        if v:
            return v
    return None


def _level_avg(levels: dict[date, float], day: date, days: int) -> float | None:
    """Mean market level over the `days` before `day`, to match a card's median over the same days."""
    v = [levels[d] for d in (day - timedelta(days=i) for i in range(days)) if d in levels]
    return mean(v) if len(v) >= days // 2 else None


# --------------------------------------------------------------------------------------- risk

def risk(s: sg.Series, c: sg.Series, mkt: dict[date, float], day: date) -> dict:
    """Volatility (yearly), correlation with the market and worst drop, from 4-weekly steps of the card's
    28-day median price over the last 52 weeks, plus liquidity. Medians, not single sales: one
    sale can be a damaged copy or a shill bid, and single prints made raw cards look ±200% a year.
    So these measure how the typical price moves, which is what you'd trade at."""
    steps = [day - timedelta(days=28 * i) for i in range(13, -1, -1)]
    pts = [(typical(c, w, 28), _level_avg(mkt, w, 28)) for w in steps]
    rets, pairs = [], []
    for (p0, m0), (p1, m1) in zip(pts, pts[1:]):
        if p0 and p1:
            r = math.log(p1 / p0)
            rets.append(r)
            if m0 and m1:
                pairs.append((r, math.log(m1 / m0)))
    out: dict = {"priced_days_365": s.count(day - timedelta(days=365), day)}
    out["liquidity"] = liquidity(out["priced_days_365"])
    if len(rets) >= 8:
        out["vol_pct"] = round(pstdev(rets) * math.sqrt(13) * 100, 1)
    if len(pairs) >= 8:
        # Correlation, not beta: with 13 monthly points a beta swings wildly (10x one month, -1x the
        # next); a correlation stays between -1 and 1 and says how much the card moves with its market.
        mr, cr = [m for _, m in pairs], [r for r, _ in pairs]
        sm, sc = pstdev(mr), pstdev(cr)
        if sm > 0 and sc > 0:
            mc, rc = mean(mr), mean(cr)
            out["corr"] = round(sum((r - rc) * (m - mc) for r, m in pairs) / len(pairs) / (sm * sc), 2)
    prices = [p for p, _ in pts if p]
    if len(prices) >= 6:
        peak, worst = prices[0], 0.0
        for p in prices:
            peak = max(peak, p)
            worst = max(worst, 1 - p / peak)
        out["max_drop_pct"] = round(-worst * 100, 1)
    now, then = typical(c, day, 30), typical(c, day - timedelta(days=30), 30)
    if now and then:
        out["trend_30d"] = round((now / then - 1) * 100, 1)
    return out


# ------------------------------------------------------------------------------ grade spreads

def spread_series(s10: sg.Series, s9: sg.Series, start: date, end: date) -> list[tuple[date, float]]:
    """Weekly log(PSA 10 / PSA 9) from 30-day medians of the cleaned series."""
    a, b = clean(s10), clean(s9)
    out, w = [], start
    while w <= end:
        p10, p9 = typical(a, w), typical(b, w)
        if p10 and p9:
            out.append((w, math.log(p10 / p9)))
        w += WEEK
    return out


def spread_at(ratios: list[tuple[date, float]], day: date) -> dict | None:
    """The ratio now and how unusual it is against the card's own last year (z-score)."""
    year = [r for d, r in ratios if day - timedelta(days=365) < d <= day]
    if len(year) < 20:
        return None
    hist = year[:-1]
    sd = pstdev(hist)
    if sd < 0.03:
        return None
    return {"ratio": round(math.exp(year[-1]), 2), "ratio_norm": round(math.exp(median(hist)), 2),
            "z": round((year[-1] - mean(hist)) / sd, 2)}


def spread_cache(cards: dict, series: dict) -> dict[int, list[tuple[date, float]]]:
    out = {}
    for cid in cards:
        s10, s9 = series.get((cid, "PSA 10")), series.get((cid, "PSA 9"))
        if s10 and s9 and len(s10.dates) > 20 and len(s9.dates) > 20:
            start = max(s10.dates[0], s9.dates[0])
            # Align to Sundays so every card's weeks line up with the monthly test dates' weeks.
            start += timedelta(days=(6 - start.weekday()) % 7)
            out[cid] = spread_series(s10, s9, start, max(s10.dates[-1], s9.dates[-1]))
    return out


def cheap_grade(spread: dict | None, grade: str) -> bool:
    """PSA 9 is cheap when PSA 10s are unusually dear next to it, and the other way round."""
    if not spread:
        return False
    return (grade == "PSA 9" and spread["z"] >= SPREAD_Z) or (grade == "PSA 10" and spread["z"] <= -SPREAD_Z)


# --------------------------------------------------------------------------------- production

def load_games(conn: sqlite3.Connection) -> dict[int, list[tuple[date, int, float]]]:
    """player_id -> [(game date, season, PPR points)] for NFL games, oldest first."""
    out: dict[int, list] = defaultdict(list)
    for r in conn.execute(
            "SELECT pg.player_id, g.date, g.season, pg.fantasy_ppr FROM player_games pg JOIN games g USING (game_id) "
            "WHERE g.league = 'NFL' AND pg.fantasy_ppr IS NOT NULL ORDER BY g.date"):
        out[r[0]].append((date.fromisoformat(r[1]), r[2], r[3]))
    return out


def production(games: list[tuple[date, int, float]], day: date, n: int = 17) -> tuple[float, int] | None:
    """PPR points per game over the last n games before `day` within 15 months; None under 6."""
    recent = [p for d, _, p in games if d <= day and (day - d).days <= 455][-n:]
    return (mean(recent), len(recent)) if len(recent) >= 6 else None


def by_season(games: list[tuple[date, int, float]]) -> list[list]:
    seasons: dict[int, list[float]] = defaultdict(list)
    for _, season, p in games:
        seasons[season].append(p)
    return [[s, len(v), round(mean(v), 1)] for s, v in sorted(seasons.items())]


def nfl_season(day: date) -> int:
    return day.year if day.month >= 9 else day.year - 1


def _ols(x: list[list[float]], y: list[float], ridge: float = 1e-3) -> list[float]:
    """Least squares by the normal equations (a handful of columns), solved by Gauss-Jordan."""
    k = len(x[0])
    a = [[sum(r[i] * r[j] for r in x) + (ridge if i == j and i else 0) for j in range(k)] for i in range(k)]
    b = [sum(r[i] * yy for r, yy in zip(x, y)) for i in range(k)]
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(a[r][col]))
        a[col], a[piv], b[col], b[piv] = a[piv], a[col], b[piv], b[col]
        if abs(a[col][col]) < 1e-12:
            continue
        for r in range(k):
            if r != col:
                f = a[r][col] / a[col][col]
                a[r] = [v - f * w for v, w in zip(a[r], a[col])]
                b[r] -= f * b[col]
    return [b[i] / a[i][i] if abs(a[i][i]) > 1e-12 else 0.0 for i in range(k)]


def _features(ppg: float, position: str, career: int, product: str = "", products: tuple[str, ...] = ()) -> list[float]:
    return ([1.0, math.log(max(ppg, 1.0))] + [1.0 if position == p else 0.0 for p in POSITIONS[:-1]]
            + [1.0 if min(career, 3) == c else 0.0 for c in (0, 1, 2)]
            + [1.0 if product == p else 0.0 for p in products])


def _product(card: dict) -> str:
    """'Panini Prizm', 'Donruss Optic': base cards of different products price at different levels."""
    return f"{card.get('manufacturer') or ''} {card.get('set_name') or ''}".strip()


def valuation(cards: dict, series: dict, games: dict, positions: dict, day: date, min_n: int = 25) -> dict[int, dict]:
    """card_id -> output, expected and actual PSA 10 price, and how far under or over the line.
    `series` should be cleaned (Context.cleaned)."""
    rows = []
    for (cid, grade), s in series.items():
        card = cards[cid]
        if grade != "PSA 10" or card["sport"] != "football" or positions.get(card["player_id"]) not in POSITIONS:
            continue
        prod = production(games.get(card["player_id"], []), day)
        price = typical(s, day, 45)
        if not prod or not price:
            continue
        career = max(0, nfl_season(day) - card["year"])
        rows.append((cid, prod, price, positions[card["player_id"]], career))
    if len(rows) < min_n:
        return {}
    # Each product gets its own level (the most common is the base), so a Mosaic card is compared
    # with Mosaic cards, not with Prizm. Products with few cards share the base level.
    counts = Counter(_product(cards[r[0]]) for r in rows)
    products = tuple(sorted(p for p, n in counts.items() if n >= 5 and p != counts.most_common(1)[0][0]))
    x = [_features(p[0], pos, car, _product(cards[cid]), products) for cid, p, _, pos, car in rows]
    y = [math.log(price) for _, _, price, _, _ in rows]
    beta = _ols(x, y)
    out = {}
    for (cid, prod, price, pos, car), xi, yi in zip(rows, x, y):
        fit = sum(b * v for b, v in zip(beta, xi))
        out[cid] = {"ppg": round(prod[0], 1), "games": prod[1], "position": pos, "career_year": car,
                    "expected": round(math.exp(fit), 2), "actual": round(price, 2),
                    "vs_output_pct": round(math.expm1(yi - fit) * 100, 1)}
    cut = sorted(v["vs_output_pct"] for v in out.values())[len(out) // 5]
    for v in out.values():
        v["cheap"] = v["vs_output_pct"] <= min(cut, -20)
    return out


# ----------------------------------------------------------------------- signals for backtest

class Context:
    """Data the extra signals need, loaded once."""

    def __init__(self, conn: sqlite3.Connection, cards: dict, series: dict):
        self.cards, self.series = cards, series
        self.cleaned = {k: clean(s) for k, s in series.items()}
        self._mkt = None
        self.spreads = spread_cache(cards, series)
        self.games = load_games(conn)
        self.positions = {r[0]: r[1] for r in conn.execute("SELECT id, position FROM players")}

    @property
    def market(self) -> dict[tuple[str, str], dict[date, float]]:
        if self._mkt is None:
            self._mkt = market_levels(self.cards, self.series)
        return self._mkt

    def signals(self, day: date) -> dict[tuple[int, str], list[str]]:
        out: dict[tuple[int, str], list[str]] = defaultdict(list)
        for cid, ratios in self.spreads.items():
            sp = spread_at(ratios, day)
            for grade in ("PSA 10", "PSA 9"):
                if cheap_grade(sp, grade):
                    out[(cid, grade)].append("cheap_grade")
        for cid, v in valuation(self.cards, self.cleaned, self.games, self.positions, day).items():
            if v["cheap"]:
                out[(cid, "PSA 10")].append("cheap_vs_output")
        return out


# ------------------------------------------------------------------------------------ details

def card_details(ctx: Context, day: date) -> dict[tuple[int, str], dict]:
    """(card, grade) -> risk, spread and valuation fields for deals.json."""
    mkt = ctx.market
    vals = valuation(ctx.cards, ctx.cleaned, ctx.games, ctx.positions, day)
    out = {}
    for (cid, grade), s in ctx.series.items():
        d = risk(s, ctx.cleaned[(cid, grade)], mkt.get((ctx.cards[cid]["sport"], grade), {}), day)
        sp = spread_at(ctx.spreads.get(cid, []), day)
        if sp:
            d.update({"spread_ratio": sp["ratio"], "spread_norm": sp["ratio_norm"], "spread_z": sp["z"]})
        if grade == "PSA 10" and cid in vals:
            v = vals[cid]
            d.update({"ppg": v["ppg"], "expected": v["expected"], "vs_output_pct": v["vs_output_pct"]})
        out[(cid, grade)] = d
    return out


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def player_details(conn: sqlite3.Connection, ctx: Context, deals: list[dict], players: list[dict],
                   day: date) -> dict[str, dict]:
    """slug -> a tear sheet: position, output by season, valuation, risk and a short verdict."""
    by_player: dict[str, list[dict]] = defaultdict(list)
    for d in deals:
        by_player[d["player"]].append(d)
    ids = {r[1]: r[0] for r in conn.execute("SELECT id, name FROM players")}
    rows = {p["player"]: p for p in players}
    mkt_chg = _market_changes(ctx, day)
    out = {}
    for name, p in rows.items():
        pid = ids.get(name)
        mine = by_player.get(name, [])
        games = ctx.games.get(pid, []) if p["sport"] == "football" else []
        prod = production(games, day) if games else None
        # The player's read across all their valued PSA 10s (each against its own product), and the
        # most-traded one as the card to open.
        valued = [d for d in mine if d.get("vs_output_pct") is not None]
        val = max(valued, key=lambda d: d["priced_days_90"], default=None)
        liq = [d for d in mine if d.get("liquidity")]
        best_liq = min((d["liquidity"] for d in liq), default=None)
        vols = [d["vol_pct"] for d in mine if d.get("vol_pct") is not None and d["grade"] == "PSA 10"]
        corrs = [d["corr"] for d in mine if d.get("corr") is not None and d["grade"] == "PSA 10"]
        entry = {
            "name": name, "slug": slug(name), "sport": p["sport"], "position": ctx.positions.get(pid),
            "chg_90d": p.get("chg_90d"), "chg_1y": p.get("chg_1y"), "market_90d": mkt_chg.get(p["sport"]),
            "sales_3m": p.get("sales_3m"), "sales_chg": p.get("sales_chg"), "avg_sale_3m": p.get("avg_sale_3m"),
            "seasons": by_season(games), "ppg": round(prod[0], 1) if prod else None,
            "vs_output_pct": round(median(d["vs_output_pct"] for d in valued), 1) if valued else None,
            "vs_output_cards": len(valued), "expected": val["expected"] if val else None,
            "valued_card": val["card_id"] if val else None,
            "vol_pct": round(median(vols), 1) if vols else None, "corr": round(median(corrs), 2) if corrs else None,
            "liquidity": best_liq, "cards": sorted({d["card_id"] for d in mine}),
        }
        entry["verdict"], entry["points"] = verdict(entry)
        out[entry["slug"]] = entry
    return out


def _market_changes(ctx: Context, day: date) -> dict[str, float]:
    mkt = ctx.market
    out = {}
    for sport in ("football", "basketball", "soccer"):
        lv = mkt.get((sport, "PSA 10"), {})
        now, then = _level_at(lv, max(lv) if lv else day), _level_at(lv, (max(lv) if lv else day) - timedelta(days=90))
        if now and then:
            out[sport] = round((now / then - 1) * 100, 1)
    return out


def verdict(e: dict) -> tuple[str, list[str]]:
    """A one-line read and the points behind it, in plain English."""
    pts, tags = [], []
    if e["chg_90d"] is not None:
        rel = e["chg_90d"] - (e["market_90d"] or 0)
        pts.append(f"Cards {_signed(e['chg_90d'])} over 90 days, against {_signed(e['market_90d'] or 0)} for the market.")
        if rel >= 10:
            tags.append("outrunning the market")
        elif rel <= -10:
            tags.append("lagging the market")
    if e["vs_output_pct"] is not None:
        v = e["vs_output_pct"]
        n = e.get("vs_output_cards") or 1
        what = "PSA 10 priced" if n == 1 else "PSA 10s priced a median"
        pts.append(f"{what} {abs(v):.0f}% {'under' if v < 0 else 'over'} what similar output at the same "
                   f"position and career stage sells for in the same set ({e['ppg']} PPR points a game"
                   f"{'' if n == 1 else f', {n} cards'}).")
        if v <= -25:
            tags.append("cheap for the output")
        elif v >= 50:
            tags.append("priced for more than current output")
    if e["sales_chg"] is not None and (e["sales_3m"] or 0) >= 300:
        pts.append(f"Sales {_signed(e['sales_chg'])} over the last 3 months ({e['sales_3m']:,} sales).")
        if e["sales_chg"] >= 30:
            tags.append("demand rising")
        elif e["sales_chg"] <= -30:
            tags.append("demand fading")
    if e["vol_pct"] is not None:
        together = ("" if e["corr"] is None else " and mostly move with the market" if e["corr"] >= 0.5
                    else " and mostly move on their own news, not the market's" if e["corr"] < 0.2 else
                    " and partly move with the market")
        pts.append(f"PSA 10s swing about ±{e['vol_pct']:.0f}% a year{together}.")
        if e["vol_pct"] >= 80:
            tags.append("volatile")
    if e["liquidity"]:
        pts.append(f"Most liquid card {LIQUIDITY_WORDS[e['liquidity']]} (liquidity {e['liquidity']}).")
        if e["liquidity"] == "D":
            tags.append("hard to sell")
    if not tags:
        return ("Nothing stands out: moving with the market." if pts else "Not enough data yet."), pts
    return (tags[0][0].upper() + tags[0][1:] + (", " + ", ".join(tags[1:]) if tags[1:] else "") + "."), pts


def _signed(v: float) -> str:
    return f"{'+' if v > 0 else '−' if v < 0 else ''}{abs(v):.0f}%"


# ------------------------------------------------------------------------------ paper trading

PAPER_SQL = """CREATE TABLE IF NOT EXISTS paper_trades (
    id         INTEGER PRIMARY KEY,
    strategy   TEXT NOT NULL,
    card_id    INTEGER NOT NULL REFERENCES cards(id),
    grade      TEXT NOT NULL,
    opened     TEXT NOT NULL,
    entry_usd  REAL NOT NULL,
    closed     TEXT,
    exit_usd   REAL,
    UNIQUE (strategy, card_id, grade, opened)
)"""


def picks(rows: list[dict], strategy: str) -> list[dict]:
    """The card-grades a strategy buys today, from the deals rows (with details merged in)."""
    # $10+: under that, shipping and fees eat any gain, so a real trade wouldn't happen.
    liquid = [r for r in rows if "thin" not in r["signals"] and r.get("liquidity") in ("A", "B", "C")
              and (r.get("fair") or 0) >= PAPER_MIN_PRICE]
    if strategy == "market":
        return liquid
    if strategy == "cheap_vs_output":
        c = [r for r in liquid if r.get("vs_output_pct") is not None and r["vs_output_pct"] <= -20]
        return sorted(c, key=lambda r: r["vs_output_pct"])[:PAPER_SLOTS]
    if strategy == "cheap_grade":
        c = [r for r in liquid if "cheap_grade" in r["signals"]]
        return sorted(c, key=lambda r: -abs(r.get("spread_z") or 0))[:PAPER_SLOTS]
    if strategy == "near_low":
        c = [r for r in liquid if "near_low" in r["signals"]]
        return sorted(c, key=lambda r: r["range_pos"] if r["range_pos"] is not None else 1)[:PAPER_SLOTS]
    raise ValueError(strategy)


def paper_step(conn: sqlite3.Connection, rows: list[dict], today: date) -> dict[str, int]:
    """Close positions held HORIZON days at today's fair value; open this month's picks once."""
    conn.execute(PAPER_SQL)
    fair = {(r["card_id"], r["grade"]): r["fair"] for r in rows if r.get("fair")}
    closed = 0
    for t in conn.execute("SELECT id, card_id, grade FROM paper_trades WHERE closed IS NULL AND opened <= ?",
                          ((today - timedelta(days=sg.HORIZON)).isoformat(),)).fetchall():
        price = fair.get((t[1], t[2]))
        if price:
            conn.execute("UPDATE paper_trades SET closed = ?, exit_usd = ? WHERE id = ?", (today.isoformat(), price, t[0]))
            closed += 1
    opened = 0
    month = today.strftime("%Y-%m")
    for strategy in STRATEGIES:
        if conn.execute("SELECT 1 FROM paper_trades WHERE strategy = ? AND substr(opened, 1, 7) = ?",
                        (strategy, month)).fetchone():
            continue
        for r in picks(rows, strategy):
            conn.execute("INSERT OR IGNORE INTO paper_trades (strategy, card_id, grade, opened, entry_usd) "
                         "VALUES (?, ?, ?, ?, ?)", (strategy, r["card_id"], r["grade"], today.isoformat(), r["fair"]))
            opened += 1
    conn.commit()
    return {"opened": opened, "closed": closed}


def paper_summary(conn: sqlite3.Connection, rows: list[dict], titles: dict[int, str]) -> dict:
    """Per strategy: positions marked at fair value, return after costs, and against the market."""
    conn.execute(PAPER_SQL)
    fair = {(r["card_id"], r["grade"]): r["fair"] for r in rows if r.get("fair")}
    out: dict = {"fees": sg.FEES, "stake": PAPER_STAKE, "horizon_days": sg.HORIZON, "strategies": []}
    trades = conn.execute("SELECT strategy, card_id, grade, opened, entry_usd, closed, exit_usd FROM paper_trades "
                          "ORDER BY opened, strategy").fetchall()
    out["started"] = trades[0][3] if trades else None
    for strategy, desc in STRATEGIES.items():
        pos, rets = [], []
        for t in trades:
            if t[0] != strategy:
                continue
            now = t[6] if t[5] else fair.get((t[1], t[2]))
            gross = (now / t[4] - 1) * 100 if now else None
            net = ((now * (1 - sg.FEES)) / t[4] - 1) * 100 if now else None
            if net is not None:
                rets.append((gross, net))
            pos.append({"card_id": t[1], "grade": t[2], "title": titles.get(t[1], f"Card {t[1]}"), "opened": t[3],
                        "entry": round(t[4], 2), "now": round(now, 2) if now else None, "closed": t[5],
                        "gross_pct": None if gross is None else round(gross, 1),
                        "net_pct": None if net is None else round(net, 1)})
        out["strategies"].append({
            "key": strategy, "description": desc, "positions": pos if strategy != "market" else [],
            "n": len(pos), "open": sum(1 for p in pos if not p["closed"]),
            "gross_pct": round(mean(g for g, _ in rets), 2) if rets else None,
            "net_pct": round(mean(n for _, n in rets), 2) if rets else None})
    base = next((s["gross_pct"] for s in out["strategies"] if s["key"] == "market"), None)
    for s in out["strategies"]:
        s["vs_market_pct"] = None if s["gross_pct"] is None or base is None else round(s["gross_pct"] - base, 2)
    return out


# -------------------------------------------------------------------------------------- brief

SIGNAL_SHORT = {"near_low": "near a 1-year low", "demand_lag": "demand up, price lagging",
                "seasonal": "seasonal tailwind", "cheap_grade": "cheap grade next to the other grade",
                "cheap_vs_output": "NFL cards cheap for the player's output"}
STRATEGY_SHORT = {"market": "market", "cheap_vs_output": "cheap for output", "cheap_grade": "cheap grade",
                  "near_low": "near 1-year low"}
SPORT_NAMES = {"football": "Football", "basketball": "Basketball", "soccer": "Premier League"}


def brief(ctx: Context, deals: list[dict], players: dict[str, dict], tests: list[dict], paper: dict,
          effects: dict, day: date) -> list[dict]:
    """Five to eight headlines, each a sentence or two with a link to the detail."""
    items = []
    mkt = ctx.market
    for sport, league in (("football", "NFL"), ("basketball", "NBA"), ("soccer", "EPL")):
        lv = mkt.get((sport, "PSA 10"), {})
        if not lv:
            continue
        last = max(lv)
        now, then = _level_at(lv, last), _level_at(lv, last - timedelta(days=30))
        if not (now and then):
            continue
        chg = (now / then - 1) * 100
        eff = effects.get((league, "PSA 10"), {})
        prev_m = (day.month - 2) % 12 + 1
        norm = math.expm1(eff[day.month] - eff[prev_m]) * 100 if day.month in eff and prev_m in eff else None
        text = f"{SPORT_NAMES[sport]} PSA 10s {_signed(chg)} over the last 30 days."
        if norm is not None:
            gap = chg - norm
            text += (f" This time of year is usually {_signed(norm)}, so "
                     + ("about normal." if abs(gap) < 3 else "stronger than usual." if gap > 0 else "weaker than usual."))
        items.append({"kind": "market", "tone": "up" if chg > 0 else "down", "text": text, "href": "/studio/cards/market"})
    liquid = [d for d in deals if d.get("liquidity") in ("A", "B") and d.get("trend_30d") is not None
              and d["fair"] >= 25]
    if liquid:
        up = max(liquid, key=lambda d: d["trend_30d"])
        down = min(liquid, key=lambda d: d["trend_30d"])
        if up["trend_30d"] >= 10:
            items.append({"kind": "mover", "tone": "up", "href": f"/studio/cards/card/{up['card_id']}",
                          "text": f"Biggest riser among cards that trade often: {_title(up)} {up['grade']}, "
                                  f"{_signed(up['trend_30d'])} in 30 days to about ${up['fair']:,.0f}."})
        if down["trend_30d"] <= -10:
            items.append({"kind": "mover", "tone": "down", "href": f"/studio/cards/card/{down['card_id']}",
                          "text": f"Biggest faller: {_title(down)} {down['grade']}, {_signed(down['trend_30d'])} in 30 days "
                                  f"to about ${down['fair']:,.0f}."})
    cheap = sorted((d for d in deals if d.get("vs_output_pct") is not None and d.get("liquidity") in ("A", "B", "C")),
                   key=lambda d: d["vs_output_pct"])[:3]
    if cheap and cheap[0]["vs_output_pct"] <= -25:
        names = ", ".join(f"{d['player']} ({_signed(d['vs_output_pct'])})" for d in cheap)
        items.append({"kind": "value", "tone": "neutral", "href": "/studio/cards/value",
                      "text": f"Cheapest NFL PSA 10s for what the player produces: {names}. Cheap can mean the market "
                              f"expects decline, so check the player before buying."})
    spread = sorted((d for d in deals if "cheap_grade" in d["signals"]), key=lambda d: -abs(d.get("spread_z") or 0))
    if spread:
        d = spread[0]
        items.append({"kind": "spread", "tone": "neutral", "href": f"/studio/cards/card/{d['card_id']}",
                      "text": f"Widest grade gap: {_title(d)} PSA 10s cost {d['spread_ratio']}× the PSA 9 "
                              f"(usually {d['spread_norm']}×), so the {d['grade']} is cheap next to the other grade."})
    surge = sorted((p for p in players.values() if (p["sales_3m"] or 0) >= 1000 and p["sales_chg"] is not None),
                   key=lambda p: -p["sales_chg"])
    if surge and surge[0]["sales_chg"] >= 30:
        p = surge[0]
        items.append({"kind": "demand", "tone": "up", "href": f"/studio/cards/player/{p['slug']}",
                      "text": f"Demand surge: {p['name']} sales {_signed(p['sales_chg'])} over 3 months "
                              f"({p['sales_3m']:,} sales); cards {_signed(p['chg_90d'] or 0)} over 90 days."})
    best = max((t for t in tests if t["signal"] in sg.BUY_SIGNALS and not t.get("in_sample")),
               key=lambda t: t["median_excess_pct"], default=None)
    passing = [t for t in tests if sg.passes(t)]
    if passing:
        items.append({"kind": "signals", "tone": "up", "href": "/studio/cards/signals",
                      "text": "Signals that beat the market in realistic tests: "
                              + ", ".join(SIGNAL_SHORT.get(t["signal"], t["signal"]) for t in passing) + "."})
    elif best:
        items.append({"kind": "signals", "tone": "neutral", "href": "/studio/cards/signals",
                      "text": f"No buy signal beats the market in realistic testing yet. Closest: "
                              f"{SIGNAL_SHORT.get(best['signal'], best['signal'])}, {_signed(best['median_excess_pct'])} "
                              f"a trade against the market after a realistic entry (it needs +3%)."})
    if paper.get("started"):
        ranked = [s for s in paper["strategies"] if s["key"] != "market" and s["vs_market_pct"] is not None]
        if ranked:
            top = max(ranked, key=lambda s: s["vs_market_pct"])
            items.append({"kind": "paper", "tone": "neutral", "href": "/studio/cards/paper",
                          "text": f"Paper portfolio since {paper['started']}: best strategy so far is "
                                  f"“{STRATEGY_SHORT[top['key']]}”, {_signed(top['vs_market_pct'])} "
                                  f"against the market. Too early to mean much before a few months."})
    return items


def _title(d: dict) -> str:
    return f"{d['player']} {d['year']} {d['set']} #{d['number']}"
