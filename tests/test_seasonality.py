import math
from datetime import date, timedelta

import pytest

from cardtracker import calendar
from cardtracker.analytics import seasonality as sz
from cardtracker.db import connect, init_db
from cardtracker.prices import import_csv as import_prices

SCHEDULE = """game_id,season,game_type,week,gameday
a,2023,REG,1,2023-09-07
b,2023,REG,18,2024-01-07
c,2023,WC,19,2024-01-13
d,2023,SB,22,2024-02-11
e,2024,REG,1,2024-09-05
f,2024,REG,18,2025-01-05
g,2024,WC,19,2025-01-11
h,2024,SB,22,2025-02-09
i,2025,REG,1,2025-09-04
"""


def synthetic_price(d: date, events: dict[int, dict[str, date]]) -> float:
    """$100 base; +30% ramp through each playoffs, then -20% through each offseason."""
    price = 100.0
    for season in sorted(events):
        e = events[season]
        if "championship" not in e:
            continue
        po, champ = e["playoffs_start"], e["championship"]
        if d >= po:
            price *= 1 + 0.30 * min((d - po) / (champ - po), 1)
        nxt = events.get(season + 1, {}).get("season_start")
        if nxt and d > champ:
            price *= 1 - 0.20 * min((d - champ) / (nxt - champ), 1)
    return price


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    for season, ev in calendar.nfl_events_from_schedule(SCHEDULE).items():
        calendar.save_events(c, "NFL", season, ev, "test")
    c.execute("INSERT INTO players (id, name, sport) VALUES (1, 'QB One', 'football'), (2, 'WR Two', 'football')")
    c.execute("INSERT INTO cards (id, player_id, year, set_name, card_number) VALUES (1, 1, 2020, 'Prizm', '1'), "
              "(2, 2, 2021, 'Prizm', '2')")
    evs = calendar.events(c, "NFL")
    d = date(2023, 8, 1)
    i = 0
    while d <= date(2025, 9, 30):
        for card_id, scale in ((1, 1.0), (2, 3.0)):
            for _ in range(2):        # two sales per card every 3 days
                i += 1
                c.execute("INSERT INTO sales (card_id, grader, grade, price_usd, sold_at, source, external_id) "
                          "VALUES (?, 'PSA', 10, ?, ?, 'synthetic', ?)",
                          (card_id, scale * synthetic_price(d, evs), d.isoformat(), str(i)))
        d += timedelta(days=3)
    return c


def test_nfl_events_from_schedule():
    ev = calendar.nfl_events_from_schedule(SCHEDULE)
    assert ev[2023] == {"season_start": "2023-09-07", "regular_season_end": "2024-01-07",
                        "playoffs_start": "2024-01-13", "championship": "2024-02-11"}
    assert ev[2025] == {"season_start": "2025-09-04", "regular_season_end": "2025-09-04"}


def test_phases_cover_the_calendar(conn):
    ph = calendar.phases(conn, "NFL")
    names = [(p.season, p.name) for p in ph]
    assert (2023, "offseason") in names and (2024, "offseason") in names
    assert (2025, "playoffs") not in names                      # incomplete season skipped
    s23 = [p for p in ph if p.season == 2023]
    assert s23[0].start == date(2023, 9, 7) and s23[2].end == date(2024, 1, 13)


def test_phase_returns_recover_planted_pattern(conn):
    res = {r.key: r for r in sz.phase_returns(conn, "NFL", grader="PSA", grade=10)}
    assert res["playoffs"].median_pct > 15 and res["playoffs"].hit_rate == 1.0
    assert res["offseason"].median_pct < -10 and res["offseason"].hit_rate == 0.0
    assert abs(res["mid_season"].median_pct) < 3
    assert set(res["playoffs"].by_season) == {2023, 2024}


def test_event_study_rises_after_playoffs_start(conn):
    curve = {r.key: r.median for r in sz.event_study(conn, "NFL", "playoffs_start", weeks_before=2, weeks_after=4)}
    assert curve["+0w"] == pytest.approx(0, abs=0.05)
    assert curve["+4w"] > curve["+0w"] > curve["-2w"] - 0.05


def test_month_of_year_shows_offseason_dip(conn):
    months = {r.key: r.median for r in sz.month_of_year(conn, "NFL")}
    assert months["Feb"] > months["Aug"]


def test_level_requires_min_sales():
    s = [(date(2024, 1, 1), 10.0)]
    assert sz.level_at(s, date(2024, 1, 2)) is None
    assert sz.level_at(s, date(2024, 1, 2), min_sales=1) == 10.0


def test_price_import_idempotent_and_reports_missing(conn, tmp_path):
    f = tmp_path / "hist.csv"
    f.write_text("sport,player,year,set_name,card_number,parallel,grader,grade,date,price_usd,source\n"
                 "football,QB One,2020,Prizm,1,Base,PSA,10,2022-03-01,250,manual\n"
                 "football,QB One,2020,Prizm,1,Base,PSA,10,2022-03-01,250,manual\n"
                 "football,Nobody,2020,Prizm,9,Base,PSA,10,2022-03-01,5,manual\n")
    imported, missing = import_prices(conn, f)
    assert imported == 2 and missing == ["Nobody 2020 Prizm #9"]
    assert import_prices(conn, f)[0] == 0
