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

    def card_search(self, search=None, number=None, set_name=None, page_size=100):
        self.calls += 1
        if set_name:
            return [c for c in LUKA_RESULTS if c["set"] == set_name and c["number"] == number]
        return LUKA_RESULTS if search and "Doncic" in search else []

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
    # Duplicate catalog records: the one with more recent sales wins.
    dup = dict(LUKA_RESULTS[0], card_id="A2", **{"30 Day Sales": 40})
    assert ch.best_card(LUKA_RESULTS + [dup], 2018, "Prizm", "basketball", "280", "Base",
                        "Luka Dončić")["card_id"] == "A2"


def test_windows_are_calendar_years():
    w = list(ch._windows(TODAY, ch.HISTORY_FLOOR))
    assert w[0] == (TODAY, 300)
    assert (date(2024, 12, 31), 366) in w and (date(2023, 12, 31), 365) in w
    assert w[-1][0] == date(2020, 12, 31)            # price coverage starts in 2020


def test_history_pages_back_then_skips_finished_windows(conn):
    client = FakeClient()
    linked, unmatched = ch.link_cards(conn, client)
    assert linked == 1 and len(unmatched) == 7

    result = ch.fetch_price_history(conn, client, ["PSA 10", "Raw"], today=TODAY)
    assert result["complete"]
    days = (TODAY - HISTORY_START).days + 1
    n = conn.execute("SELECT COUNT(*) FROM price_snapshots WHERE grader = 'PSA'").fetchone()[0]
    assert n == days
    # Paging stops at the 2020 floor, where coverage starts.
    assert min(r[2] for r in client.requests) == date(2020, 12, 31)

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
    first = ch.INDEX_FLOOR                                           # index coverage starts late 2024
    all_days = [first + timedelta(days=i) for i in range((TODAY - first).days + 1)]
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


def test_player_index_treats_404_as_start_of_history(conn):
    import requests

    class NoOldIndex(FakeClient):
        def player_index_history(self, player_id, start, end):
            if end.year < 2025:
                resp = requests.Response()
                resp.status_code = 404
                raise requests.HTTPError(response=resp)
            return super().player_index_history(player_id, start, end)

    client = NoOldIndex()
    ch.link_players(conn, client, "basketball")
    assert ch.fetch_player_index(conn, client, today=TODAY) > 0
    assert conn.execute("SELECT MIN(date) FROM player_index").fetchone()[0] == "2025-01-02"   # Jan 1 is withheld


# ---------------------------------------------------------------- discovery and daily runs

def set_card(card_id, player, set_name, number, sales, rookie=True, variant="Base"):
    return {"card_id": card_id, "player": player, "set": set_name, "set_type": set_name.split(" ", 1)[1],
            "number": number, "variant": variant, "rookie": rookie, "30 Day Sales": sales}


EPL = "2023 Panini Prizm Premier League Soccer"
NBA = "2018 Panini Prizm Basketball"
SETS = {
    NBA: [set_card("A", "Luka Doncic", NBA, "280", 190), set_card("S", "Luka Doncic", NBA, "280", 150, variant="Silver"),
          set_card("V", "Some Veteran", NBA, "1", 40, rookie=False), set_card("T", "Trae Young", NBA, "78", 60),
          set_card("Z", "Nobody Buys", NBA, "300", 1), set_card("P", "Labelled Prizm", NBA, "5", 30, variant="Prizm"),
          dict(set_card("R", "Flag Missing", NBA, "6", 20, rookie=False), subset="Rookie"),
          dict(set_card("I", "Insert Only", NBA, "7", 20, rookie=False), subset="Rookie Introduction")],
    EPL: [set_card("H", "Erling Haaland", EPL, "1", 30, rookie=False), set_card("K", "Bukayo Saka", EPL, "2", 12),
          set_card("H2", "Erling Haaland", EPL, "200", 10, rookie=False),          # second card: less traded
          set_card("B", "Team Badge", EPL, "3", 10, rookie=False),
          set_card("M", "Bukayo Saka", EPL, "PJ-BS", 10, rookie=False)],         # memorabilia code
}


class SetClient(FakeClient):
    def search_cards_sorted(self, set_name, page, page_size=100):
        self.calls += 1
        return {"cards": SETS.get(set_name, []) if page == 1 else []}

    limit = None

    def set_budget(self, calls=None, minutes=None):
        if calls is not None:
            self.limit = min(self.limit or 10**9, self.calls + calls)

    @property
    def out_of_budget(self):
        return self.limit is not None and self.calls >= self.limit


SPECS = [ch.SetSpec("basketball", NBA, True, 5, 30), ch.SetSpec("soccer", EPL, False, 2, 40)]


def test_discover_keeps_traded_base_rookies_and_all_epl_players(conn):
    client = SetClient()
    added = ch.discover(conn, client, SPECS)
    assert added == {"basketball": 4, "soccer": 2}
    names = {r[0] for r in conn.execute(
        "SELECT p.name FROM card_external_ids x JOIN cards c ON c.id = x.card_id JOIN players p ON p.id = c.player_id")}
    assert names == {"Luka Doncic", "Trae Young", "Labelled Prizm", "Flag Missing", "Erling Haaland", "Bukayo Saka"}
    # The starter watchlist's Luka card is reused, not duplicated.
    assert conn.execute("SELECT COUNT(*) FROM cards c JOIN players p ON p.id = c.player_id "
                        "WHERE p.name = 'Luka Doncic' AND c.year = 2018").fetchone()[0] == 1
    epl = conn.execute("SELECT manufacturer, set_name, year FROM cards c JOIN players p ON p.id = c.player_id "
                       "WHERE p.name = 'Erling Haaland'").fetchone()
    assert tuple(epl) == ("Panini", "Prizm Premier League", 2023)
    calls = client.calls
    assert ch.discover(conn, client, SPECS) == {} and client.calls == calls     # not rescanned within 30 days


def test_year_to_date_refresh_waits_refresh_days(conn):
    client = FakeClient()
    ch.link_cards(conn, client)
    ch.fetch_price_history(conn, client, ["PSA 10"], today=TODAY)
    client.requests.clear()
    ch.fetch_price_history(conn, client, ["PSA 10"], today=TODAY + timedelta(days=3), refresh_days=7)
    assert client.requests == []
    ch.fetch_price_history(conn, client, ["PSA 10"], today=TODAY + timedelta(days=8), refresh_days=7)
    assert [r[2] for r in client.requests] == [TODAY + timedelta(days=8)]


def test_empty_year_to_date_does_not_stop_paging(conn):
    class Quiet(FakeClient):
        def prices_by_card(self, card_id, grade, end_date, days):
            return [] if end_date == TODAY else super().prices_by_card(card_id, grade, end_date, days)

    client = Quiet()
    ch.link_cards(conn, client)
    assert ch.fetch_price_history(conn, client, ["PSA 9"], today=TODAY)["points"] > 1000


def test_daily_quota_429_stops_cleanly(conn):
    import requests
    from cardtracker.fetcher import Fetcher

    class Session(requests.Session):
        def request(self, method, url, **kw):
            r = requests.Response()
            r.status_code, r.headers["Retry-After"] = 429, "36000"
            return r

    client = ch.CardHedgeClient(api_key="k", fetcher=Fetcher(min_interval=0, check_robots=False, max_wait=120,
                                                             session=Session(), sleep=lambda s: None), conn=conn)
    conn.execute("INSERT INTO card_external_ids (card_id, source, external_id) SELECT id, 'cardhedge', 'X' "
                 "FROM cards LIMIT 1")
    result = ch.fetch_price_history(conn, client, ["PSA 10"], today=TODAY)
    assert not result["complete"] and ch.calls_today(conn) == 1


def test_daily_runs_grades_in_order_within_budget(conn):
    client = SetClient()
    ch.discover(conn, client, SPECS)
    client.limit = client.calls + 12
    report = ch.daily(conn, client, budget=10_000, today=TODAY, specs=SPECS)
    assert report["out_of_budget"] and report["PSA 10"] > 0 and "PSA 9" not in report
    grades = {r[0] for r in conn.execute("SELECT DISTINCT grader || grade FROM price_snapshots")}
    assert grades == {"PSA10.0"}
    # Sports alternate, so a short run still reaches soccer.
    assert conn.execute("SELECT COUNT(DISTINCT p.sport) FROM price_snapshots s JOIN cards c ON c.id = s.card_id "
                        "JOIN players p ON p.id = c.player_id").fetchone()[0] == 2


def test_unflagged_set_treats_first_prizm_card_as_rookie(conn):
    new = "2025 Panini Prizm Basketball"
    SETS[new] = [set_card("N1", "Fresh Rookie", new, "301", 50, rookie=False),
                 set_card("N2", "Luka Doncic", new, "77", 50, rookie=False)]     # has a 2018 Prizm card
    try:
        client = SetClient()
        ch.discover(conn, client, SPECS + [ch.SetSpec("basketball", new, True, 5, 30)])
        got = {r[0] for r in conn.execute("SELECT p.name FROM cards c JOIN players p ON p.id = c.player_id "
                                          "WHERE c.year = 2025")}
        assert got == {"Fresh Rookie"}
    finally:
        del SETS[new]


def test_inserts_labelled_base_are_rejected_and_pruned(conn):
    insert = dict(set_card("X", "Trae Young", NBA, "4", 90), subset="Instant Impact")
    SETS[NBA].append(insert)
    client = SetClient()
    try:
        # A looser earlier scan (simulated) linked the insert; the current rules reject it.
        ch.discover(conn, client, SPECS)
        assert conn.execute("SELECT COUNT(*) FROM card_external_ids WHERE external_id = 'X'").fetchone()[0] == 0
        players = {ch._fold(r["name"]): r["id"] for r in conn.execute("SELECT id, name FROM players")}
        stale = ch._upsert_card(conn, insert, "basketball", players)
        conn.execute("INSERT INTO price_snapshots (card_id, grader, grade, price_usd, snapshot_date, source) "
                     "VALUES (?, 'PSA', 10, 5, '2026-01-01', 'cardhedge')", (stale,))
        assert ch.prune_discovered(conn) == 1
        assert conn.execute("SELECT COUNT(*) FROM cards WHERE id = ?", (stale,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM price_snapshots WHERE card_id = ?", (stale,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM card_external_ids WHERE external_id = 'T'").fetchone()[0] == 1
    finally:
        SETS[NBA].remove(insert)


def test_unflagged_football_set_uses_known_rookie_seasons(conn):
    new = "2025 Panini Prizm Football"
    SETS[new] = [set_card("F1", "Real Rookie", new, "332", 50, rookie=False),
                 set_card("F2", "Barry Sanders", new, "100", 50, rookie=False),
                 set_card("F3", "Second Year", new, "120", 50, rookie=False)]
    try:
        ch.discover(conn, SetClient(), [ch.SetSpec("football", new, True, 5, 30)],
                    rookie_seasons={"football": {"real rookie": {2025}, "barry sanders": {1989},
                                                 "second year": {2023}}})
        got = {r[0] for r in conn.execute("SELECT p.name FROM cards c JOIN players p ON p.id = c.player_id "
                                          "WHERE c.year = 2025")}
        assert got == {"Real Rookie"}
    finally:
        del SETS[new]
