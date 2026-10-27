from datetime import date
from pathlib import Path

import pytest

from cardtracker.db import connect, init_db
from cardtracker.ingest import sportscardspro as scp
from cardtracker.watchlist import import_csv

STARTER = Path(__file__).parent.parent / "watchlists" / "starter.csv"


def product(pid, name, console, psa10=10000, loose=1000, volume="100"):
    # Shape of a real /api/product response; values are synthetic.
    return {"id": pid, "product-name": name, "console-name": console, "loose-price": loose,
            "graded-price": 3000, "box-only-price": 5000, "manual-only-price": psa10,
            "bgs-10-price": 20000, "condition-18-price": 0, "gamestop-price": 0,
            "sales-volume": volume, "status": "success"}


LUKA = [
    product("1", "Luka Doncic #280", "Basketball Cards 2018 Panini Prizm"),
    product("2", "Luka Doncic [Silver Prizm] #280", "Basketball Cards 2018 Panini Prizm"),
    product("3", "Luka Doncic #280", "Basketball Cards 2018 Panini Prizm Draft Picks"),
    product("4", "Luka Doncic #2800", "Basketball Cards 2018 Panini Prizm"),
]


class FakeClient:
    def __init__(self, products):
        self.products = {p["id"]: p for p in products}

    def search(self, query):
        return list(self.products.values())

    def product(self, pid):
        return self.products[pid]


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    import_csv(c, STARTER)
    return c


def test_parse_prices_maps_grades_and_skips_zero():
    prices = {(g, gr): p for g, gr, p in scp.parse_prices(LUKA[0])}
    assert prices[("PSA", 10.0)] == 100.0
    assert prices[("RAW", None)] == 10.0
    assert prices[("ANY", 9.5)] == 50.0
    assert ("SGC", 10.0) not in prices


def test_best_match_base_vs_parallel():
    # Product 3 ("...Prizm Draft Picks") is a different set; product 5 has a malformed bracket.
    broken = product("5", "Luka Doncic [Autograph #280", "Basketball Cards 2018 Panini Prizm")
    assert scp.best_match(LUKA + [broken], 2018, "Prizm", "280", "Base", "Luka Doncic")["id"] == "1"
    assert scp.best_match(LUKA, 2018, "Prizm", "#280", "Silver", "Luka Doncic")["id"] == "2"
    assert scp.best_match(LUKA, 2019, "Prizm", "280", "Base", "Luka Doncic") is None


def test_link_snapshot_purge(conn):
    client = FakeClient([LUKA[0], LUKA[1]])
    linked, unmatched = scp.link_watchlist(conn, client)
    assert linked == 1 and len(unmatched) == 7          # only Luka is in the fake catalog
    assert scp.link_watchlist(conn, client)[0] == 0     # already linked cards are skipped

    rows = scp.snapshot(conn, client, today=date(2026, 10, 27))
    assert rows == 5
    scp.snapshot(conn, client, today=date(2026, 10, 27))     # same day: upsert, no duplicates
    scp.snapshot(conn, client, today=date(2026, 10, 28))
    assert conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == 10
    vol = conn.execute("SELECT DISTINCT sales_volume FROM price_snapshots").fetchone()[0]
    assert vol == 100

    counts = scp.purge(conn)
    assert counts["price_snapshots"] == 10 and counts["card_external_ids"] == 1
    assert conn.execute("SELECT COUNT(*) FROM raw_records WHERE source = 'sportscardspro'").fetchone()[0] == 0
