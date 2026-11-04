import math
from datetime import date, timedelta

import pytest

from cardtracker.analytics.chain_index import change, chain

D0 = date(2024, 1, 1)


def daily(start, prices):
    return [(start + timedelta(days=i), p) for i, p in enumerate(prices)]


def test_new_expensive_series_does_not_jump_the_index():
    cheap = daily(D0, [50] * 10)
    pricey = daily(D0 + timedelta(days=5), [2000] * 5)       # enters mid-way at a far higher price
    idx = chain([cheap, pricey])
    assert all(level == pytest.approx(100) for _, level, _ in idx)


def test_moves_are_averaged_and_chained():
    a = daily(D0, [100, 110, 121])                            # +10% a day
    b = daily(D0, [100, 100, 100])
    idx = chain([a, b])
    assert idx[-1][1] == pytest.approx(100 * math.exp(2 * math.log(1.1) / 2))
    assert [n for _, _, n in idx] == [2, 2, 2]


def test_gaps_carry_the_move_to_the_next_observation():
    a = [(D0, 100), (D0 + timedelta(days=3), 120)]
    idx = chain([a])
    assert [round(level, 4) for _, level, _ in idx] == [100, 100, 100, 120]


def test_bad_print_is_clipped_and_min_series_holds_thin_days():
    a = daily(D0, [100, 1000, 100])                           # one absurd print
    assert max(level for _, level, _ in chain([a], clip=0.4)) == pytest.approx(100 * math.exp(0.4))
    assert chain([a], min_series=2)[-1][1] == 100            # a single series never moves it


def test_change_needs_enough_history():
    idx = chain([daily(D0, [100] * 50 + [120])])
    assert change(idx, 30) == pytest.approx(20)
    assert change(idx, 365) is None


def test_last_price_carries_forward_until_stale():
    a = daily(D0, [100] * 100)
    b = [(D0, 50), (D0 + timedelta(days=10), 50)]            # stops selling on day 10
    idx = chain([a, b], stale_days=30)
    by_day = {d: n for d, _, n in idx}
    assert by_day[D0 + timedelta(days=40)] == 2 and by_day[D0 + timedelta(days=41)] == 1


def test_trailing_thin_days_are_trimmed():
    a = daily(D0, [100] * 10)
    b = daily(D0, [100] * 12)
    idx = chain([a, b], min_series=2, stale_days=0)
    assert idx[-1][0] == D0 + timedelta(days=9)
