from datetime import date, timedelta

import pytest

from cardtracker import calendar, trends
from cardtracker.analytics import seasonality as sz
from cardtracker.db import connect, init_db

CLASSIC = """Category: All categories

Week,Luka Doncic: (United States),sports cards: (United States)
2024-04-07,40,20
2024-04-14,<1,21
2024-04-21,55,
"""

# Layout without the preamble, with a quoted header and a date range in the first column.
NEWER = '''"Time","Luka Dončić"
"2024-04-07 - 2024-04-13","40"
"2024-04-14 - 2024-04-20","44"
'''

MONTHLY = """Category: All categories

Month,Luka Doncic: (Worldwide)
2004-01,3
2004-02,4
"""


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    c.execute("INSERT INTO players (id, name, sport) VALUES (1, 'Luka Doncic', 'basketball')")
    return c


def write(tmp_path, name, text):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


def test_parse_classic_export():
    (luka, geo, pts), (term2, _, pts2) = trends.parse(CLASSIC)
    assert (luka, geo) == ("Luka Doncic", "United States")
    assert pts == [(date(2024, 4, 7), 40.0), (date(2024, 4, 14), 0.5), (date(2024, 4, 21), 55.0)]
    assert term2 == "sports cards" and len(pts2) == 2          # blank cell skipped


def test_parse_newer_and_monthly_exports():
    [(term, geo, pts)] = trends.parse(NEWER)
    assert term == "Luka Dončić" and geo == "Worldwide" and pts[0] == (date(2024, 4, 7), 40.0)
    [(_, _, monthly)] = trends.parse(MONTHLY)
    assert monthly[0] == (date(2004, 1, 1), 3.0)


def test_parse_rejects_non_trends_file():
    with pytest.raises(ValueError):
        trends.parse("a,b\n1,2\n")


def test_import_links_players_accent_insensitively_and_is_idempotent(conn, tmp_path):
    f = write(tmp_path, "classic.csv", CLASSIC)
    written, unmatched = trends.import_csv(conn, f, "basketball")
    assert written == 5 and unmatched == ["sports cards"]
    assert trends.import_csv(conn, f, "basketball")[0] == 0

    trends.import_csv(conn, write(tmp_path, "newer.csv", NEWER), "basketball")
    linked = conn.execute("SELECT COUNT(DISTINCT batch) FROM search_interest WHERE player_id = 1").fetchone()[0]
    assert linked == 2                     # 'Luka Dončić' matched; separate file = separate batch


def test_seasonality_on_trends_keeps_batches_separate(conn, tmp_path):
    for season, (start, po, champ) in {2022: ("2022-10-18", "2023-04-15", "2023-06-12"),
                                       2023: ("2023-10-24", "2024-04-20", "2024-06-17")}.items():
        calendar.save_events(conn, "NBA", season, {"season_start": start, "playoffs_start": po,
                                                    "championship": champ}, "test")
    evs = calendar.events(conn, "NBA")

    def weekly(scale):
        lines, d = ["Week,Luka Doncic: (United States)"], date(2022, 9, 4)
        while d <= date(2024, 9, 1):
            in_playoffs = any(e["playoffs_start"] <= d <= e["championship"] for e in evs.values())
            lines.append(f"{d.isoformat()},{round(scale * (2 if in_playoffs else 1))}")
            d += timedelta(weeks=1)
        return "\n".join(lines) + "\n"

    # Two downloads of the same pattern on very different scales.
    trends.import_csv(conn, write(tmp_path, "a.csv", weekly(30)), "basketball")
    trends.import_csv(conn, write(tmp_path, "b.csv", weekly(7)), "basketball")
    assert len(sz.load_series(conn, "NBA", data="trends")) == 2

    res = {r.key: r for r in sz.phase_returns(conn, "NBA", data="trends", **sz.SPARSE_LEVEL)}
    # Attention doubles into the playoffs and falls back after; scale differences don't leak in.
    assert res["late_season"].median_pct == pytest.approx(100, abs=15)
    assert res["offseason"].median_pct == pytest.approx(-50, abs=10)
