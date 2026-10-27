from datetime import date, timedelta
from pathlib import Path

import pytest

from cardtracker import calendar
from cardtracker.analytics import seasonality as sz
from cardtracker.db import connect, init_db
from cardtracker.ingest import cardhedge as ch
from cardtracker.watchlist import import_csv

STARTER = Path(__file__).parent.parent / "watchlists" / "starter.csv"
TODAY = date(2026, 10, 27)
HISTORY_START = date(2020, 4, 1)


def card(card_id, player, set_name, number, variant="Base"):
    # Shape of a card-search result (fields from the published OpenAPI schema).
    return {"card_id": card_id, "player": player, "set": set_name, "number": number,
            "variant": variant, "category": "Basketball", "prices": []}


LUKA_RESULTS = [
    card("A", "Luka Doncic", "2018 Panini Prizm Basketball", "280"),
    card("B", "Luka Doncic", "2018 Panini Prizm Basketball", "280", "Silver"),
    card("C", "Luka Doncic", "2018 Panini Prizm Mosaic Basketball", "280"),
    card("D", "Luka Doncic", "2018 Panini Prizm Draft Picks Basketball", "280"),
]


class FakeClient:
    """Serves daily prices from HISTORY_START to TODAY and a matching player index."""

    def __init__(self):
        self.calls = 0
        self.requests = []

    def card_search(self, search, number=None, page_size=50):
        self.calls += 1
        return LUKA_RESULTS if "Doncic" in search else []

    def prices_by_card(self, card_id, grade, end_date, days):
        self.calls += 1
        self.requests.append((card_id, grade, end_date, days))
        out, d = [], end_date
        while d > end_date - timedelta(days=days):
            if HISTORY_START <= d <= TODAY:
                out.append({"closing_date": f"{d.isoformat()}T23:59:00.000Z", "Grade": grade,
                            "card_id": card_id, "price": f"{100 + (d - HISTORY_START).days * 0.01:.2f}"})
            d -= timedelta(days=1)
        return out

    def player_search(self, name, category):
        self.calls += 1
        return [{"player_id": "P1", "name": "Luka Dončić"}, {"player_id": "P2", "name": "Luka Garza"}] \
            if "Luka" in name else []

    def player_index_history(self, player_id, start, end):
        self.calls += 1
        out, d = [], start
        while d <= end:
            if d >= HISTORY_START:
                out.append({"date": d.isoformat(), "value": 900.0, "volume_30d": 300, "card_count": 10,
                            "withheld": d.day == 1})
            d += timedelta(days=1)
        return out


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    import_csv(c, STARTER)
    return c


def test_parse_grade():
    assert ch.parse_grade("PSA 10") == ("PSA", 10.0)
    assert ch.parse_grade("BGS 9.5") == ("BGS", 9.5)
    assert ch.parse_grade("Raw") == ("RAW", None)
    with pytest.raises(ValueError):
        ch.parse_grade("Authentic")


def test_best_card_rejects_parallels_and_similar_sets():
    assert ch.best_card(LUKA_RESULTS, 2018, "Prizm", "basketball", "280", "Base", "Luka Doncic")["card_id"] == "A"
    assert ch.best_card(LUKA_RESULTS, 2018, "Prizm", "basketball", "#280", "Silver", "Luka Doncic")["card_id"] == "B"
    assert ch.best_card(LUKA_RESULTS + [LUKA_RESULTS[0]], 2018, "Prizm", "basketball", "280", "Base",
                        "Luka Doncic") is None                       # duplicate -> ambiguous


def test_windows_are_calendar_years():
    w = list(ch._windows(TODAY, ch.HISTORY_FLOOR))
    assert w[0] == (TODAY, 300)
    assert (date(2024, 12, 31), 366) in w and (date(2023, 12, 31), 365) in w
    assert w[-1][0] == date(2019, 12, 31)


def test_history_pages_back_then_skips_finished_windows(conn):
    client = FakeClient()
    linked, unmatched = ch.link_cards(conn, client)
    assert linked == 1 and len(unmatched) == 7

    result = ch.fetch_price_history(conn, client, ["PSA 10", "Raw"], today=TODAY)
    assert result["complete"]
    days = (TODAY - HISTORY_START).days + 1
    n = conn.execute("SELECT COUNT(*) FROM price_snapshots WHERE grader = 'PSA'").fetchone()[0]
    assert n == days
    # Paging stopped at the first empty window (2019) instead of running to the floor.
    assert min(r[2] for r in client.requests) == date(2019, 12, 31)

    # Second run: only the year-to-date window is re-fetched, per grade; no duplicates (incl. Raw/NULL).
    client.requests.clear()
    ch.fetch_price_history(conn, client, ["PSA 10", "Raw"], today=TODAY)
    assert {r[2] for r in client.requests} == {TODAY}
    assert conn.execute("SELECT COUNT(*) FROM price_snapshots WHERE grader = 'RAW'").fetchone()[0] == days


def test_history_respects_max_calls(conn):
    client = FakeClient()
    ch.link_cards(conn, client)
    result = ch.fetch_price_history(conn, client, ["PSA 10"], today=TODAY, max_calls=client.calls + 2)
    assert not result["complete"] and result["points"] > 0


def test_players_link_by_accent_insensitive_exact_name_and_skip_withheld(conn):
    client = FakeClient()
    linked, unmatched = ch.link_players(conn, client, "basketball")
    assert linked == 1 and "Luka Doncic" not in unmatched
    written = ch.fetch_player_index(conn, client, today=TODAY)
    all_days = [HISTORY_START + timedelta(days=i) for i in range((TODAY - HISTORY_START).days + 1)]
    assert written == sum(1 for d in all_days if d.day != 1)        # 1st of month is withheld


def test_seasonality_runs_on_history(conn):
    client = FakeClient()
    ch.link_cards(conn, client)
    ch.fetch_price_history(conn, client, ["PSA 10"], today=TODAY)
    calendar.save_events(conn, "NBA", 2023, {"season_start": "2023-10-24", "playoffs_start": "2024-04-20",
                                             "championship": "2024-06-17"}, "test")
    calendar.save_events(conn, "NBA", 2024, {"season_start": "2024-10-22", "playoffs_start": "2025-04-19",
                                             "championship": "2025-06-22"}, "test")
    res = {r.key: r for r in sz.phase_returns(conn, "NBA", "PSA", 10.0, data="snapshots")}
    # Synthetic prices rise steadily, so every phase shows a small positive return.
    assert res["playoffs"].median > 0 and res["offseason"].n == 1
