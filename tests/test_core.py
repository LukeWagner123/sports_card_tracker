import math

import pytest

from cardtracker.analytics.index import card_price, player_index
from cardtracker.analytics.valuation import fit_residuals, zscores
from cardtracker.db import connect, init_db
from cardtracker.ingest.ebay_browse import parse_grade
from cardtracker.ingest.psa import parse_population


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    c.execute("INSERT INTO players (id, name, sport) VALUES (1, 'Test Player', 'basketball')")
    c.execute("INSERT INTO cards (id, player_id, year, set_name, card_number) VALUES (1, 1, 2018, 'Prizm', '280')")
    for i, price in enumerate([100, 110, 1000, 105]):
        c.execute(
            "INSERT INTO sales (card_id, grader, grade, price_usd, sold_at, source, external_id) "
            "VALUES (1, 'PSA', 10, ?, '2026-01-1' || ?, 'test', ?)",
            (price, i, str(i)),
        )
    return c


def test_card_price_is_median_and_resists_outlier(conn):
    price, n = card_price(conn, 1, "PSA", 10, "2026-01-01", "2026-02-01")
    assert n == 4
    assert price == 107.5


def test_player_index_skips_illiquid(conn):
    assert player_index(conn, 1, "PSA", 10, "2026-01-01", "2026-02-01", min_sales=5) is None
    assert player_index(conn, 1, "PSA", 10, "2026-01-01", "2026-02-01") == 107.5


@pytest.mark.parametrize("title,expected", [
    ("2018 Prizm Luka Doncic #280 PSA 10 Gem Mint", ("PSA", 10.0)),
    ("Luka Silver Prizm BGS 9.5", ("BGS", 9.5)),
    ("Luka Doncic Prizm RC raw", ("RAW", None)),
])
def test_parse_grade(title, expected):
    assert parse_grade(title) == expected


def test_parse_population():
    payload = {"PSAPop": {"Grade10": 500, "Grade9": 1200, "Grade8_5": 30, "Total": 1730}}
    assert parse_population(payload) == {10.0: 500, 9.0: 1200, 8.5: 30}


def test_fit_residuals_flags_cheap_player():
    perf = zscores({1: 20.0, 2: 25.0, 3: 30.0, 4: 30.0})
    # Players 3 and 4 perform the same; 4 trades at a fraction of 3's price.
    prices = {1: 50.0, 2: 100.0, 3: 400.0, 4: 80.0}
    vals = fit_residuals(prices, perf)
    assert vals[0].player_id == 4
    assert vals[0].residual < 0 < vals[-1].residual
    assert math.isclose(sum(v.residual for v in vals), 0.0, abs_tol=1e-9)
