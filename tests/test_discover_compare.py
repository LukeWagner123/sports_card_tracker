import csv
from datetime import date

import pytest

from cardtracker.analytics import compare
from cardtracker.db import connect, init_db
from cardtracker.ingest import sportscardspro as scp
from cardtracker.watchlist import import_csv


def prod(pid, name, year, sport="Football", console_suffix=""):
    return {"id": pid, "product-name": name, "console-name": f"{sport} Cards {year} Panini Prizm{console_suffix}"}


class FakeClient:
    """Search results keyed by year in the query; only cards of that year come back."""

    def __init__(self, catalog):
        self.catalog = catalog
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        return [p for p in self.catalog if p["console-name"].split()[2] in query]


CATALOG = [
    prod("1", "Patrick Mahomes II #269", 2017),
    prod("2", "Patrick Mahomes II [Silver] #269", 2017),
    prod("3", "Patrick Mahomes II #1", 2023),
    prod("4", "Amon Ra St. Brown #358", 2021),
    prod("5", "Amon Ra St. Brown #95", 2024),
    prod("6", "Matthew Stafford #67", 2012),
    prod("7", "Derrick Henry #18", 2017),                         # his 2016 base is missing
    prod("8", "Victor Wembanyama #136", 2023, "Basketball"),
    prod("9", "Victor Wembanyama #136", 2023, "Basketball", " Draft Picks"),
]


@pytest.mark.parametrize("a,b", [("Patrick Mahomes II", "Patrick Mahomes"), ("Amon Ra St. Brown", "Amon-Ra St. Brown"),
                                 ("Travis Etienne Jr.", "Travis Etienne"), ("Luka Dončić", "Luka Doncic")])
def test_name_key_matches_catalog_spellings(a, b):
    assert scp._name_key(a) == scp._name_key(b)


def test_known_debut_searches_only_rookie_year():
    client = FakeClient(CATALOG)
    card = scp.find_first_prizm(client, "Amon-Ra St. Brown", "football", debut=2021)
    assert (card["year"], card["card_number"], card["is_rookie"]) == (2021, "358", True)
    assert client.queries == ["Amon-Ra St. Brown 2021 prizm"]


def test_known_debut_miss_is_not_replaced_by_later_card():
    assert scp.find_first_prizm(FakeClient(CATALOG), "Derrick Henry", "football", debut=2016) is None


def test_pre_prizm_debut_uses_2012_and_is_not_a_rookie_card():
    card = scp.find_first_prizm(FakeClient(CATALOG), "Matthew Stafford", "football", debut=2009)
    assert (card["year"], card["is_rookie"]) == (2012, False)


def test_unknown_debut_scans_years_and_ignores_other_sets():
    client = FakeClient(CATALOG)
    card = scp.find_first_prizm(client, "Victor Wembanyama", "basketball", last_year=2024)
    assert (card["year"], card["product_id"]) == (2023, "8")
    assert client.queries[0] == "Victor Wembanyama 2012 prizm"


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    return c


def test_build_link_and_drop_superseded(conn, tmp_path):
    # An earlier, wrong discovery: St. Brown's 2024 card.
    old = tmp_path / "old.csv"
    scp.build_watchlist(FakeClient([CATALOG[4]]), [("Amon-Ra St. Brown", "football", None)], str(old))
    import_csv(conn, old)
    scp.link_from_csv(conn, str(old))

    new = tmp_path / "new.csv"
    missing = scp.build_watchlist(FakeClient(CATALOG), [("Amon-Ra St. Brown", "football", 2021),
                                                         ("Derrick Henry", "football", 2016)], str(new))
    assert missing == ["Derrick Henry"]
    import_csv(conn, new)
    assert scp.link_from_csv(conn, str(new)) == 1
    assert scp.drop_superseded(conn, str(new)) == 1
    left = conn.execute("SELECT c.year, c.card_number, x.external_id FROM watchlist w JOIN cards c ON c.id = w.card_id "
                        "JOIN card_external_ids x ON x.card_id = c.id").fetchall()
    assert [tuple(r) for r in left] == [(2021, "358", "4")]
    assert list(csv.DictReader(open(new)))[0]["notes"] == ""


def _snapshot(conn, card_id, day, raw, nine, ten):
    for grader, grade, price in (("RAW", None, raw), ("ANY", 9.0, nine), ("PSA", 10.0, ten)):
        conn.execute("INSERT INTO price_snapshots (card_id, grader, grade, price_usd, sales_volume, snapshot_date, "
                     "source) VALUES (?, ?, ?, ?, 100, ?, 'sportscardspro')", (card_id, grader, grade, price, day))


def test_compare_rows_multiples_change_and_residuals(conn):
    for pid, name, pos, ppr in ((1, "QB A", "QB", 300), (2, "QB B", "QB", 300), (3, "QB C", "QB", 150)):
        conn.execute("INSERT INTO players (id, name, sport, position) VALUES (?, ?, 'football', ?)", (pid, name, pos))
        conn.execute("INSERT INTO cards (id, player_id, year, set_name, card_number) VALUES (?, ?, 2020, 'Prizm', ?)",
                     (pid, pid, str(pid)))
        for metric, value in (("fantasy_points_ppr", ppr), ("games", 17)):
            conn.execute("INSERT INTO player_stats (player_id, season, as_of, metric, value, source) "
                         "VALUES (?, 2025, '2026-01-10', ?, ?, 'nflverse')", (pid, metric, value))
    _snapshot(conn, 1, "2026-10-27", 10, 40, 200)
    _snapshot(conn, 1, "2026-10-28", 10, 40, 220)
    _snapshot(conn, 2, "2026-10-28", 5, 20, 50)        # same output as A, a quarter of the price
    _snapshot(conn, 3, "2026-10-28", 5, 20, 60)

    day, rows = compare.card_rows(conn)
    by = {r.player: r for r in rows}
    assert day == "2026-10-28"
    assert by["QB A"].psa10_over_raw == 22 and by["QB A"].psa10_over_9 == 5.5
    assert by["QB A"].psa10_change_pct == pytest.approx(10)

    ranked = compare.cheap_vs_performance(rows, compare.nfl_performance(conn, 2025))
    assert ranked[0][0].player == "QB B"
