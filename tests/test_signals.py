from datetime import date, timedelta

import pytest

from cardtracker.analytics import signals as sg
from cardtracker.db import connect, init_db

D0 = date(2021, 1, 1)


def daily(prices, start=D0, step=1):
    return [(start + timedelta(days=i * step), p) for i, p in enumerate(prices)]


def test_price_goes_stale():
    s = sg.Series(daily([100, 110]))
    assert s.at(D0 + timedelta(days=1)) == 110
    assert s.at(D0 + timedelta(days=1 + sg.STALE_DAYS + 1)) is None


def test_snapshot_flags_near_low_falling_and_thin():
    pts = daily([100] * 300 + [70])                          # fresh 52-week low, -30% in a day
    snap = sg.snapshot(sg.Series(pts), pts[-1][0], demand=None, seasonal=None)
    assert {"near_low", "falling"} <= set(snap.signals) and "thin" not in snap.signals
    sparse = daily([100, 101, 99, 100], step=40)
    assert "thin" in sg.snapshot(sg.Series(sparse), sparse[-1][0], None, None).signals


def test_demand_and_seasonal_inputs():
    months = [(f"2026-0{m}", n) for m, n in zip(range(1, 8), [500, 500, 500, 800, 800, 800, 9999])]
    assert sg.demand_change(months, date(2026, 7, 15)) == pytest.approx(60)       # July is not closed
    assert sg.demand_change([(m, n // 10) for m, n in months], date(2026, 7, 15)) is None   # too few sales
    assert sg.seasonal_outlook({11: -0.10, 1: 0.0}, date(2026, 11, 4)) == pytest.approx(10.517, abs=0.01)


def test_fair_value_and_deal_price():
    pts = daily([90, 100, 110] * 40)
    fv = sg.fair_value(sg.Series(pts), pts[-1][0])
    assert fv["fair"] == 100 and fv["fair_window_days"] == 30
    assert fv["deal_under"] == pytest.approx(min(fv["p25_90"], 85.0))


@pytest.fixture
def market():
    """Twelve flat $100 cards, one of which prints a single $50 sale every few months."""
    c = connect(":memory:")
    init_db(c)
    c.execute("INSERT INTO players (id, name, sport) VALUES (1, 'P', 'football')")
    for cid in range(1, 13):
        c.execute("INSERT INTO cards (id, player_id, year, set_name, card_number) VALUES (?, 1, 2020, 'Prizm', ?)",
                  (cid, str(cid)))
        c.execute("INSERT INTO card_external_ids (card_id, source, external_id) VALUES (?, 'cardhedge', ?)",
                  (cid, f"x{cid}"))
        d = date(2020, 1, 1)
        while d <= date(2023, 12, 31):
            bad = cid == 1 and d.day == 1 and d.month % 3 == 0
            c.execute("INSERT INTO price_snapshots (card_id, grader, grade, price_usd, snapshot_date, source) "
                      "VALUES (?, 'PSA', 10, ?, ?, 'cardhedge')", (cid, 50.0 if bad else 100.0, d.isoformat()))
            d += timedelta(days=1)
    return c


def test_one_off_cheap_sales_fool_the_naive_test_only(market):
    naive = {r["signal"]: r for r in sg.backtest(market, today=date(2023, 12, 31), realistic=False)}
    real = {r["signal"]: r for r in sg.backtest(market, today=date(2023, 12, 31), realistic=True)}
    # The naive test buys the $50 print and "earns" 100% when the price is $100 again.
    assert naive["falling"]["median_excess_pct"] == pytest.approx(100)
    assert "falling" not in real or real["falling"]["median_excess_pct"] == pytest.approx(0)
    assert not any(sg.passes(t) for t in real.values())


def test_passes_requires_realistic_out_of_sample_and_size():
    good = {"signal": "near_low", "n": 500, "median_excess_pct": 5, "hit_rate": 0.56, "method": "realistic"}
    assert sg.passes(good)
    assert not sg.passes({**good, "method": "naive"})
    assert not sg.passes({**good, "signal": "seasonal", "in_sample": True})
    assert not sg.passes({**good, "n": 50})
    assert not sg.passes({**good, "signal": "falling"})
