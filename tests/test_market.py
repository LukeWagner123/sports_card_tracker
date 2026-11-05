from datetime import date, timedelta

import pytest

from cardtracker.analytics import market as mk
from cardtracker.analytics import signals as sg
from cardtracker.db import connect, init_db

D0 = date(2024, 1, 1)


def daily(prices, start=D0):
    return sg.Series([(start + timedelta(days=i), p) for i, p in enumerate(prices)])


def test_liquidity_grades():
    assert [mk.liquidity(n) for n in (200, 60, 20, 3)] == ["A", "B", "C", "D"]


def test_risk_uses_medians_so_one_bad_sale_does_not_make_a_card_volatile():
    prices = [100.0] * 400
    prices[200] = 5.0                                     # a $5 'sale' of a $100 card
    s = daily(prices)
    day = D0 + timedelta(days=399)
    r = mk.risk(s, mk.clean(s), {}, day)
    assert r["vol_pct"] == 0 and r["max_drop_pct"] == 0 and r["liquidity"] == "A"


def test_spread_flags_the_cheap_grade():
    n = 500
    s10 = daily([300.0] * n)
    s9 = daily([100.0] * (n - 40) + [70.0] * 40)          # PSA 9 drops: PSA 10 now 4.3x, norm 3x
    ratios = mk.spread_series(s10, s9, D0 + timedelta(days=30), D0 + timedelta(days=n - 1))
    noisy = [(d, r + (0.05 if i % 2 else -0.05)) for i, (d, r) in enumerate(ratios[:-1])] + ratios[-1:]
    sp = mk.spread_at(noisy, D0 + timedelta(days=n - 1))
    assert sp["z"] > mk.SPREAD_Z
    assert mk.cheap_grade(sp, "PSA 9") and not mk.cheap_grade(sp, "PSA 10")


def test_ols_recovers_coefficients():
    x = [[1.0, a, b] for a in range(5) for b in range(5)]
    y = [2 + 3 * r[1] - 1.5 * r[2] for r in x]
    assert mk._ols(x, y) == pytest.approx([2, 3, -1.5], abs=1e-3)


def test_valuation_finds_cards_cheap_for_output():
    cards, series, games, pos = {}, {}, {}, {}
    day = date(2025, 12, 1)
    for i in range(40):
        ppg = 8 + i % 10 * 2
        price = 20 * ppg ** 1.5 * (0.4 if i == 7 else 1.0)    # card 7 is 60% cheap for its output
        cards[i] = {"player_id": i, "sport": "football", "year": 2022}
        series[(i, "PSA 10")] = daily([price] * 40, day - timedelta(days=39))
        games[i] = [(day - timedelta(days=7 * k), 2025, float(ppg)) for k in range(10)]
        pos[i] = mk.POSITIONS[i % 4]
    v = mk.valuation(cards, series, games, pos, day)
    assert v[7]["vs_output_pct"] < -40 and v[7]["cheap"]
    assert abs(v[8]["vs_output_pct"]) < 15 and not v[8]["cheap"]


def test_production_needs_six_recent_games():
    g = [(D0 + timedelta(days=7 * k), 2024, 10.0) for k in range(5)]
    assert mk.production(g, D0 + timedelta(days=60)) is None
    g.append((D0 + timedelta(days=35), 2024, 22.0))
    assert mk.production(g, D0 + timedelta(days=60)) == (12.0, 6)


def test_paper_opens_once_a_month_and_closes_after_the_horizon():
    c = connect(":memory:")
    init_db(c)
    c.execute("INSERT INTO players (id, name, sport) VALUES (1, 'P', 'football')")
    c.execute("INSERT INTO cards (id, player_id, year, set_name, card_number) VALUES (1, 1, 2020, 'Prizm', '1')")
    row = {"card_id": 1, "grade": "PSA 10", "fair": 100.0, "signals": ["near_low"], "liquidity": "A", "range_pos": 0.1}
    day = date(2026, 11, 5)
    assert mk.paper_step(c, [row], day)["opened"] == 2          # market and near_low
    assert mk.paper_step(c, [row], day + timedelta(days=3))["opened"] == 0
    later = {**row, "fair": 130.0}
    assert mk.paper_step(c, [later], day + timedelta(days=sg.HORIZON))["closed"] == 2
    s = mk.paper_summary(c, [later], {1: "Card"})
    near = next(x for x in s["strategies"] if x["key"] == "near_low")
    done = [p for p in near["positions"] if p["closed"]]          # January opened a second batch at $130
    assert len(done) == 1 and done[0]["gross_pct"] == pytest.approx(30)
    assert done[0]["net_pct"] == pytest.approx(130 * 0.85 - 100)
    assert near["n"] == 2 and near["gross_pct"] == pytest.approx(15) and near["vs_market_pct"] == 0


def test_verdict_reads_in_plain_english():
    e = {"chg_90d": 30.0, "market_90d": 2.0, "vs_output_pct": -40.0, "ppg": 15.0, "sales_chg": 50.0,
         "sales_3m": 2000, "vol_pct": 40.0, "corr": 0.6, "liquidity": "B"}
    head, points = mk.verdict(e)
    assert head.startswith("Outrunning the market, cheap for the output, demand rising")
    assert len(points) == 5 and "±40%" in points[3]
