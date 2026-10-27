from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from cardtracker.db import connect, init_db
from cardtracker.ingest import ebay_auctions as ea
from cardtracker.ingest.ebay_browse import auction_observation, title_matches
from cardtracker.watchlist import import_csv

STARTER = Path(__file__).parent.parent / "watchlists" / "starter.csv"


def ebay_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def item(item_id, title, bid, bids, ends, currency="USD"):
    return {"itemId": item_id, "title": title, "buyingOptions": ["AUCTION"],
            "currentBidPrice": {"value": str(bid), "currency": currency},
            "bidCount": bids, "itemEndDate": ebay_time(ends)}


class FakeClient:
    def __init__(self, searches=None, items=None):
        self.searches = searches or {}
        self.items = items or {}
        self.calls = []

    def search_auctions(self, query, limit=200):
        self.calls.append(("search", query))
        return {"itemSummaries": self.searches.get(query, [])}

    def get_item(self, item_id):
        self.calls.append(("item", item_id))
        if item_id not in self.items:
            raise requests.HTTPError("404")
        return self.items[item_id]


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    import_csv(c, STARTER)
    return c


LUKA_Q = "2018 Prizm Luka Doncic 280"


def test_starter_watchlist_imports(conn):
    assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 8
    import_csv(conn, STARTER)   # idempotent
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 8


@pytest.mark.parametrize("title,ok", [
    ("2018 Panini Prizm Luka Doncic #280 RC PSA 10", True),
    ("2018 Prizm Luka Doncic Silver Prizm #280 PSA 9", False),     # parallel
    ("2018 Prizm Luka Doncic #280 REPRINT", False),
    ("Luka Doncic Lot of 5 Prizm 280", False),
    ("2018 Prizm Luka Doncic #2800", False),                       # number must be whole
    ("2018 Prizm Trae Young #78", False),
])
def test_title_filter(title, ok):
    exclude = ["silver", "holo"]
    assert title_matches(title, ["Doncic", "280"], exclude) is ok


def test_auction_observation_rejects_non_auction_and_non_usd():
    end = datetime.now(timezone.utc)
    assert auction_observation(item("1", "t", 10, 1, end, currency="GBP")) is None
    bin_item = item("2", "t", 10, 1, end) | {"buyingOptions": ["FIXED_PRICE"]}
    assert auction_observation(bin_item) is None


def test_full_lifecycle_search_refresh_finalize(conn):
    now = datetime.now(timezone.utc)
    soon = now + timedelta(minutes=10)
    later = now + timedelta(days=2)
    client = FakeClient(searches={LUKA_Q: [
        item("v1|A|0", "2018 Prizm Luka Doncic #280 PSA 10", 300, 12, soon),
        item("v1|B|0", "2018 Prizm Luka Doncic Silver #280 PSA 10", 900, 20, soon),   # filtered out
        item("v1|C|0", "2018 Prizm Luka Doncic #280 raw", 40, 0, soon),               # no bids
        item("v1|D|0", "2018 Prizm Luka Doncic #280 BGS 9.5", 150, 3, later),
    ]})

    calls, rows = ea.search_due(conn, client, now, timedelta(hours=2), budget=100)
    assert calls == 8 and rows == 3

    # Searched just now: nothing due again until the interval passes.
    assert ea.search_due(conn, client, now, timedelta(hours=2), budget=100) == (0, 0)

    # Near-end refresh picks up the late bidding on A (C has no item payload -> 404, skipped).
    client.items["v1|A|0"] = item("v1|A|0", "2018 Prizm Luka Doncic #280 PSA 10", 410, 19, soon)
    calls, rows = ea.refresh_ending(conn, client, now + timedelta(minutes=6),
                                    timedelta(minutes=15), timedelta(minutes=5), budget=100)
    assert ("item", "v1|A|0") in client.calls
    assert rows == 1

    # After A and C end: A becomes a sale at the last observed bid; C (no bids) does not.
    sales = ea.finalize(conn, soon + timedelta(seconds=1))
    assert sales == 1
    sale = conn.execute("SELECT * FROM sales").fetchone()
    assert (sale["price_usd"], sale["grader"], sale["grade"]) == (410.0, "PSA", 10.0)
    assert sale["price_lag_s"] >= 0
    assert ea.finalize(conn, soon + timedelta(seconds=1)) == 0     # idempotent


def test_budget_caps_calls(conn):
    client = FakeClient()
    calls, _ = ea.search_due(conn, client, datetime.now(timezone.utc), timedelta(hours=2), budget=3)
    assert calls == 3


def test_prune_raw_keeps_recent(conn):
    now = datetime.now(timezone.utc)
    conn.execute("INSERT INTO raw_records (source, fetched_at, payload) VALUES (?, ?, '{}')",
                 (ea.SOURCE, (now - timedelta(days=10)).isoformat(timespec="seconds")))
    conn.execute("INSERT INTO raw_records (source, fetched_at, payload) VALUES (?, ?, '{}')",
                 (ea.SOURCE, now.isoformat(timespec="seconds")))
    assert ea.prune_raw(conn, now) == 1
    assert conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 1


def test_poll_idle_run_is_not_flagged(conn):
    conn.execute("UPDATE watchlist SET last_searched = ?",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    ea.poll(conn, FakeClient())
    assert conn.execute("SELECT status FROM source_runs").fetchone()["status"] == "idle"


def test_name_suffix_handled(conn):
    card = {"player_name": "Marvin Harrison Jr.", "card_number": "#301"}
    assert ea._required_terms(card) == ["Harrison", "301"]
