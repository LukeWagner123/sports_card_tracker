"""QB card prices against wins, playoffs, fantasy output and defense, season by season.

The card for every QB is his Panini Prizm base rookie, graded PSA 10 and PSA 9: the standard
issue, so QBs compare like for like. QBs whose rookie year predates Prizm (2012) have none and are
left out. A QB-season counts when he started 6+ regular-season games.

- Price change for a season: median price Feb 15-Mar 31 after it over median Aug 1-Sep 10 before
  it, in logs, minus the same change for the football market (median across all tracked football
  cards in that grade). "Regular season" stops at Dec 26-Jan 14, before most playoff games.
- Expected wins: each start's win probability from the closing spread (normal, sd 13.5 points).
  Surprise wins = actual - expected. Preseason expectation: the spreads of weeks 1-3 only, which
  are set before the season says much.
- Defense: team points allowed per regular-season game, ranked 1 (fewest) to 32.

Hindsight: everything but the August test uses results known only after the season.
"""

import math
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import date

from cardtracker.ingest.cardhedge import _fold_name

MIN_STARTS = 10           # over the study's seasons, to count as a starting QB
SEASON_STARTS = 6         # in a season, for that season to count
SD_POINTS = 13.5


def win_prob(points_favoured: float) -> float:
    return 0.5 * (1 + math.erf(points_favoured / SD_POINTS / math.sqrt(2)))


def _windows(s: int) -> dict[str, tuple[str, str]]:
    return {"start": (f"{s}-08-01", f"{s}-09-10"), "mid": (f"{s}-12-26", f"{s + 1}-01-14"),
            "end": (f"{s + 1}-02-15", f"{s + 1}-03-31")}


def _ols(rows: list[list[float]], y: list[float]) -> tuple[list[float], float]:
    """Least squares on standardised columns (intercept first); returns coefficients and R^2."""
    cols = list(zip(*rows))
    mu = [st.mean(c) for c in cols]
    sd = [st.pstdev(c) or 1.0 for c in cols]
    X = [[1.0] + [(v - m) / s for v, m, s in zip(r, mu, sd)] for r in rows]
    k = len(X[0])
    A = [[sum(x[i] * x[j] for x in X) for j in range(k)] + [sum(x[i] * t for x, t in zip(X, y))] for i in range(k)]
    for i in range(k):
        p = max(range(i, k), key=lambda r: abs(A[r][i]))
        A[i], A[p] = A[p], A[i]
        if abs(A[i][i]) < 1e-12:
            return [0.0] * k, 0.0
        for r in range(k):
            if r != i:
                f = A[r][i] / A[i][i]
                A[r] = [a - f * b for a, b in zip(A[r], A[i])]
    b = [A[i][k] / A[i][i] for i in range(k)]
    fit = [sum(bi * xi for bi, xi in zip(b, x)) for x in X]
    my = st.mean(y)
    tot = sum((t - my) ** 2 for t in y)
    return b, (1 - sum((t - f) ** 2 for t, f in zip(y, fit)) / tot) if tot else 0.0


def _pct(log_change: float | None) -> float | None:
    return round(math.expm1(log_change) * 100, 1) if log_change is not None else None


def build(conn: sqlite3.Connection, seasons: range = range(2021, 2026)) -> dict:
    games = conn.execute(
        "SELECT game_id, season, week, game_type, home, away, home_score, away_score, home_qb, away_qb, spread_line "
        "FROM games WHERE league = 'NFL' AND home_qb IS NOT NULL").fetchall()

    # --- starts, wins, expectation, playoffs; team points allowed --------------------------------
    qs: dict = defaultdict(lambda: {"starts": 0, "wins": 0.0, "exp": 0.0, "teams": defaultdict(int),
                                    "post_wins": 0.0, "super_bowl": False})
    allowed: dict = defaultdict(lambda: [0, 0])
    post_games: dict = defaultdict(int)
    pre_exp: dict = defaultdict(list)
    names: dict[str, str] = {}
    first_start: dict[str, int] = {}
    for g in games:
        s = g["season"]
        for side, own, opp, sign in (("home", g["home_score"], g["away_score"], 1), ("away", g["away_score"], g["home_score"], -1)):
            team = g[side]
            if g["game_type"] == "REG":
                allowed[(s, team)][0] += opp
                allowed[(s, team)][1] += 1
                if g["spread_line"] is not None and g["week"] is not None and g["week"] <= 3:
                    pre_exp[(s, team)].append(win_prob(sign * g["spread_line"]))
            else:
                post_games[(s, team)] += 1
            qb = g[f"{side}_qb"]
            if qb:
                first_start[_fold_name(qb)] = min(first_start.get(_fold_name(qb), s), s)
            if not qb or s not in seasons:
                continue
            key = _fold_name(qb)
            names.setdefault(key, qb)
            q = qs[(key, s)]
            won = 1.0 if own > opp else 0.5 if own == opp else 0.0
            if g["game_type"] == "REG":
                q["starts"] += 1
                q["wins"] += won
                q["teams"][team] += 1
                q["exp"] += win_prob(sign * (g["spread_line"] or 0.0))
            else:
                q["post_wins"] += won
                q["super_bowl"] |= g["game_type"] == "SB"
    def_rank = {}
    for s in seasons:
        ranked = sorted((pa / n, t) for (ss, t), (pa, n) in allowed.items() if ss == s and n)
        def_rank.update({(s, t): i for i, (_, t) in enumerate(ranked, 1)})

    total_starts: dict = defaultdict(int)
    for (key, _), q in qs.items():
        total_starts[key] += q["starts"]
    starters = {k for k, n in total_starts.items() if n >= MIN_STARTS}

    # --- players, fantasy points, the Prizm rookie --------------------------------------------
    players = {}
    # Two rows can share a name ('Gardner Minshew' from stats, 'Gardner Minshew II' from cards): take the carded one.
    for r in conn.execute("SELECT p.id, p.name, p.debut_season FROM players p WHERE p.sport = 'football' ORDER BY "
                          "(SELECT COUNT(*) FROM cards c WHERE c.player_id = p.id AND c.set_name = 'Prizm') DESC, p.id"):
        players.setdefault(_fold_name(r["name"]), r)
    ppg: dict = defaultdict(list)
    for r in conn.execute("SELECT g.player_id, g.game_id, g.fantasy_ppr FROM player_games g WHERE g.fantasy_ppr IS NOT NULL"):
        s, wk = int(r["game_id"][:4]), int(r["game_id"][5:7])
        if wk <= 18:
            ppg[(r["player_id"], s)].append(r["fantasy_ppr"])

    def prizm_rookie(p) -> tuple[int, int] | None:
        r = conn.execute(
            "SELECT id, year FROM cards WHERE player_id = ? AND set_name = 'Prizm' AND parallel = 'Base' "
            "AND (year = ? OR (? IS NULL AND is_rookie = 1)) ORDER BY year LIMIT 1",
            (p["id"], p["debut_season"], p["debut_season"])).fetchone()
        return (r["id"], r["year"]) if r else None

    # --- prices --------------------------------------------------------------------------------
    def window(cid: int, grade: int, lo: str, hi: str) -> float | None:
        v = [r[0] for r in conn.execute("SELECT price_usd FROM price_snapshots WHERE card_id = ? AND grader = 'PSA' AND grade = ? "
                                        "AND snapshot_date BETWEEN ? AND ?", (cid, grade, lo, hi))]
        return st.median(v) if len(v) >= 3 else None

    football = [r[0] for r in conn.execute(
        "SELECT c.id FROM cards c JOIN players p ON p.id = c.player_id WHERE p.sport = 'football'")]
    market: dict = {}
    for s in seasons:
        w = _windows(s)
        for grade in (10, 9):
            full, reg = [], []
            for cid in football:
                a, z, e = window(cid, grade, *w["start"]), window(cid, grade, *w["mid"]), window(cid, grade, *w["end"])
                if a and e:
                    full.append(math.log(e / a))
                if a and z:
                    reg.append(math.log(z / a))
            market[(s, grade)] = (st.median(full) if len(full) >= 15 else None, st.median(reg) if len(reg) >= 15 else None)

    # --- QB-season rows -------------------------------------------------------------------------
    rows, coverage = [], {"starters": len(starters), "with_prizm": [], "no_prizm_card": [], "not_tracked": []}
    cards_of = {}
    for key in sorted(starters):
        p = players.get(key)
        card = prizm_rookie(p) if p else None
        if card:
            cards_of[key] = (p, card)
            coverage["with_prizm"].append(names[key])
        else:
            # Prizm began in 2012: a QB starting before then has no Prizm rookie card.
            debut = (p["debut_season"] if p else None) or first_start.get(key)
            (coverage["no_prizm_card"] if debut is not None and debut < 2012 else coverage["not_tracked"]).append(names[key])
    for (key, s), q in sorted(qs.items()):
        if key not in starters or q["starts"] < SEASON_STARTS:
            continue
        team = max(q["teams"], key=q["teams"].get)
        p, card = cards_of.get(key, (players.get(key), None))
        pid = p["id"] if p else None
        cur, prev = ppg.get((pid, s), []), ppg.get((pid, s - 1), [])
        pe = pre_exp.get((s, team))
        row = {"qb": names[key], "season": s, "team": team, "starts": q["starts"], "wins": q["wins"],
               "win_pct": round(q["wins"] / q["starts"], 3), "exp_win_pct": round(q["exp"] / q["starts"], 3),
               "pre_exp_win_pct": round(sum(pe) / len(pe), 3) if pe else None,
               "made_playoffs": post_games[(s, team)] > 0, "playoff_wins": q["post_wins"], "super_bowl": q["super_bowl"],
               "def_rank": def_rank.get((s, team)), "ppg": round(st.mean(cur), 1) if len(cur) >= 4 else None,
               "ppg_prev": round(st.mean(prev), 1) if len(prev) >= 4 else None,
               "card_id": card[0] if card else None, "card_year": card[1] if card else None}
        if card:
            w = _windows(s)
            for grade in (10, 9):
                a, z, e = (window(card[0], grade, *w[k]) for k in ("start", "mid", "end"))
                full_m, reg_m = market[(s, grade)]
                row[f"price{grade}_start"], row[f"price{grade}_end"] = a, e
                ex = math.log(e / a) - full_m if a and e and full_m is not None else None
                reg = math.log(z / a) - reg_m if a and z and reg_m is not None else None
                row[f"ex{grade}"] = ex
                row[f"reg{grade}"] = reg
                row[f"post{grade}"] = ex - reg if ex is not None and reg is not None else None
        rows.append(row)

    by = {(r["qb"], r["season"]): r for r in rows}
    for r in rows:
        p = by.get((r["qb"], r["season"] - 1))
        r["prev_win_pct"] = p["win_pct"] if p else None
        r["prev_playoff_wins"] = p["playoff_wins"] if p else None

    return {"as_of": date.today().isoformat(), "seasons": [seasons.start, seasons.stop - 1], "coverage": coverage,
            "market": {f"{s}": {f"psa{g}": _pct(market[(s, g)][0]) for g in (10, 9)} for s in seasons},
            "findings": {f"psa{g}": findings(rows, g) for g in (10, 9)},
            "rows": [{k: (_pct(v) if k[:2] in ("ex", "re", "po") and k not in ("exp_win_pct",) and isinstance(v, float) else v)
                      for k, v in r.items()} for r in rows]}


def findings(rows: list[dict], g: int) -> dict:
    R = [r for r in rows if r.get(f"ex{g}") is not None]
    if len(R) < 15:
        return {"n": len(R)}

    def group(label: str, sel: list[dict], key: str = f"ex{g}") -> dict:
        v = [r[key] for r in sel if r.get(key) is not None]
        return {"label": label, "n": len(v), "median": _pct(st.median(v)) if v else None,
                "beat": round(sum(x > 0 for x in v) / len(v), 2) if v else None}

    def delta(r):
        return r["ppg"] - r["ppg_prev"] if r["ppg"] is not None and r["ppg_prev"] is not None else None

    outcomes = [group("Missed playoffs", [r for r in R if not r["made_playoffs"]]),
                group("Made playoffs, lost first game", [r for r in R if r["made_playoffs"] and r["playoff_wins"] == 0]),
                group("Won 1+ playoff game", [r for r in R if r["playoff_wins"] >= 1]),
                group("Started the Super Bowl", [r for r in R if r["super_bowl"]]),
                group("Pts/game down 3+ vs last season", [r for r in R if (d := delta(r)) is not None and d <= -3]),
                group("Pts/game up 3+ vs last season", [r for r in R if (d := delta(r)) is not None and d >= 3]),
                group("Top-10 defense", [r for r in R if r["def_rank"] and r["def_rank"] <= 10]),
                group("Bottom-10 defense", [r for r in R if r["def_rank"] and r["def_rank"] >= 23])]
    timing = []
    for label, f in (("Missed playoffs", lambda r: not r["made_playoffs"]),
                     ("Made playoffs, lost first game", lambda r: r["made_playoffs"] and r["playoff_wins"] == 0),
                     ("Won 1+ playoff game", lambda r: r["playoff_wins"] >= 1), ("Started the Super Bowl", lambda r: r["super_bowl"])):
        sel = [r for r in R if f(r) and r.get(f"reg{g}") is not None]
        timing.append({"label": label, "n": len(sel), "regular": group("", sel, f"reg{g}")["median"],
                       "playoffs": group("", sel, f"post{g}")["median"]})

    def model(label: str, cols: list[tuple[str, callable]], sel: list[dict]) -> dict:
        data = [([f(r) for _, f in cols], r[f"ex{g}"]) for r in sel]
        data = [(x, y) for x, y in data if None not in x]
        if len(data) < 15:
            return {"label": label, "n": len(data)}
        b, r2 = _ols([x for x, _ in data], [y for _, y in data])
        return {"label": label, "n": len(data), "r2": round(r2, 2),
                "terms": [{"name": n, "per_sd": round(bi * 100, 1)} for (n, _), bi in zip(cols, b[1:])]}

    surprise = model("What moved cards during the season (per 1 standard deviation, % points)", [
        ("Expected win % (Vegas)", lambda r: r["exp_win_pct"]),
        ("Surprise win % (actual - expected)", lambda r: r["win_pct"] - r["exp_win_pct"]),
        ("Playoff wins", lambda r: r["playoff_wins"]),
        ("Change in fantasy pts/game", delta),
        ("Defense rank (1 = best)", lambda r: r["def_rank"])], R)
    august = model("Known in August", [
        ("Preseason expected win % (weeks 1-3 spreads)", lambda r: r["pre_exp_win_pct"]),
        ("Last season's win %", lambda r: r["prev_win_pct"]),
        ("Last season's playoff wins", lambda r: r["prev_playoff_wins"]),
        ("Years since rookie card", lambda r: r["season"] - r["card_year"])], R)
    gap = lambda r: r["pre_exp_win_pct"] - r["prev_win_pct"] if r["pre_exp_win_pct"] is not None and r["prev_win_pct"] is not None else None
    august_rule = [group("Vegas expects better than last season", [r for r in R if (x := gap(r)) is not None and x >= 0.05]),
                   group("About the same", [r for r in R if (x := gap(r)) is not None and abs(x) < 0.05]),
                   group("Vegas expects worse", [r for r in R if (x := gap(r)) is not None and x <= -0.05])]
    by_season = []
    for s in sorted({r["season"] for r in R}):
        sel = [r for r in R if r["season"] == s]
        if len(sel) >= 5:
            a, y = [r["win_pct"] for r in sel], [r[f"ex{g}"] for r in sel]
            ma, my = st.mean(a), st.mean(y)
            den = math.sqrt(sum((p - ma) ** 2 for p in a) * sum((q - my) ** 2 for q in y))
            by_season.append({"season": s, "n": len(sel), "median": _pct(st.median(y)),
                              "corr_win": round(sum((p - ma) * (q - my) for p, q in zip(a, y)) / den, 2) if den else None})
    return {"n": len(R), "qbs": len({r["qb"] for r in R}), "overall": group("All QB-seasons", R),
            "outcomes": outcomes, "timing": timing, "surprise": surprise, "august": august,
            "august_rule": august_rule, "by_season": by_season}
