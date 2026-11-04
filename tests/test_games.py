import math
from datetime import date, timedelta

import pytest

from cardtracker import games
from cardtracker.analytics import game_reactions as gr
from cardtracker.db import connect, init_db

SCHEDULE = """game_id,season,game_type,week,gameday,home_team,away_team,home_score,away_score
2024_01_AAA_BBB,2024,REG,1,2024-09-08,BBB,AAA,10,20
2024_02_AAA_CCC,2024,REG,2,2024-09-15,CCC,AAA,30,10
2024_03_AAA_DDD,2024,REG,3,2024-09-22,AAA,DDD,7,14
2024_04_AAA_EEE,2024,REG,4,2024-09-29,AAA,EEE,3,28
2024_05_AAA_FFF,2024,REG,5,2024-10-06,AAA,FFF,24,17
2024_06_AAA_GGG,2024,REG,6,2024-10-13,AAA,GGG,,
"""
WEEK = """player_id,player_display_name,season,week,game_id,team,fantasy_points_ppr
00-1,Tracked Guy,2024,1,2024_01_AAA_BBB,AAA,30
00-1,Tracked Guy,2024,2,2024_02_AAA_CCC,AAA,8
00-1,Tracked Guy,2024,3,2024_03_AAA_DDD,AAA,9
00-1,Tracked Guy,2024,4,2024_04_AAA_EEE,AAA,7
00-1,Tracked Guy,2024,5,2024_05_AAA_FFF,AAA,10
00-2,Same Name,2024,1,2024_01_AAA_BBB,AAA,5
00-3,Same Name,2024,1,2024_01_AAA_BBB,BBB,5
"""


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    games._save_games(c, games.nfl_games_from_schedule(SCHEDULE))
    c.execute("INSERT INTO players (id, name, sport) VALUES (1, 'Tracked Guy', 'football'), (2, 'Same Name', 'football')")
    c.execute("INSERT INTO cards (id, player_id, year, set_name, card_number) VALUES (1, 1, 2023, 'Prizm', '1'), "
              "(2, 2, 2023, 'Prizm', '2')")
    c.execute("INSERT INTO watchlist (card_id, added_at) VALUES (1, 'now'), (2, 'now')")
    return c


def test_unplayed_games_are_skipped(conn):
    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 5


def test_streaks_are_signed_runs(conn):
    res = gr.team_results(conn, "NFL")
    got = [res[(f"2024_0{i}_AAA_{t * 3}", "AAA")][:2] for i, t in zip(range(1, 6), "BCDEF")]
    assert [(r, s) for r, s in got] == [("W", 1), ("L", -1), ("L", -2), ("L", -3), ("W", 1)]
    assert gr.streak_bucket(-3) == "L3+" and gr.streak_bucket(2) == "W2"


def test_player_games_skip_ambiguous_names(conn):
    rows = games.nfl_player_games(WEEK, {"tracked guy": 1, "same name": 2}, {})
    assert {r[0] for r in rows} == {1} and len(rows) == 5


def test_reactions_find_planted_losing_streak_drop(conn):
    games.ingest_nfl_player_games  # (fetching is tested live; insert directly)
    conn.executemany("INSERT INTO player_games (player_id, game_id, team, fantasy_ppr, source) VALUES (?, ?, 'AAA', ?, 't')",
                     [(1, g, p) for g, p in [("2024_01_AAA_BBB", 30), ("2024_02_AAA_CCC", 8), ("2024_03_AAA_DDD", 9),
                                             ("2024_04_AAA_EEE", 7), ("2024_05_AAA_FFF", 10)]])
    # Flat $100, except the card drops 20% in the days after the third straight loss (Sep 29).
    d, rows = date(2023, 9, 1), []
    while d <= date(2025, 9, 30):
        price = 80.0 if date(2024, 9, 30) <= d <= date(2024, 10, 5) else 100.0
        rows.append((d.isoformat(), price))
        d += timedelta(days=1)
    conn.executemany("INSERT INTO price_snapshots (card_id, grader, grade, price_usd, snapshot_date, source) "
                     "VALUES (1, 'PSA', 10, ?, ?, 'cardhedge')", [(p, ds) for ds, p in rows])
    rs = gr.reactions(conn, "NFL")
    by_game = {r.game_id: r for r in rs}
    assert by_game["2024_04_AAA_EEE"].returns[7] == pytest.approx(math.log(0.8), abs=1e-6)
    assert by_game["2024_04_AAA_EEE"].ppr_z is not None
    summary = {(s["grade"], s["horizon_days"], s["group_type"], s["group"]): s for s in gr.summarize(rs)}
    assert summary[("PSA 10", 7, "streak", "L3+")]["median_pct"] == pytest.approx(-20, abs=0.01)
    assert by_game["2024_01_AAA_BBB"].returns[7] == pytest.approx(0, abs=1e-9)
    # The next win's "before" week overlaps the drop, so it shows the rebound: windows overlap.
    assert by_game["2024_05_AAA_FFF"].returns[7] == pytest.approx(math.log(100 / 80), abs=1e-6)
    assert summary[("All grades", 7, "performance", "Big game")]["n"] == 1


def test_epl_skips_misdated_matches():
    payload = {"matches": [{"date": "2019-07-01", "team1": "A", "team2": "B", "score": {"ft": [1, 0]}},
                           {"date": "2019-08-10", "team1": "C", "team2": "D", "score": {"ft": [2, 2]}},
                           {"date": "2019-08-11", "team1": "E", "team2": "F"},
                           {"date": "2019-08-12", "team1": "G", "team2": "H", "score": [3, 1]}]}
    rows = games.epl_games_from_fixtures(payload, 2019)
    assert [r[0] for r in rows] == ["EPL_2019-08-10_C_D", "EPL_2019-08-12_G_H"]


def test_before_window_measures_the_run_up(conn):
    conn.execute("INSERT INTO player_games (player_id, game_id, team, fantasy_ppr, source) VALUES (1, '2024_05_AAA_FFF', 'AAA', 10, 't')")
    d = date(2023, 9, 1)
    while d <= date(2025, 9, 30):
        price = 100.0 if d < date(2024, 9, 29) else 120.0          # rises in the week before the Oct 6 game
        conn.execute("INSERT INTO price_snapshots (card_id, grader, grade, price_usd, snapshot_date, source) "
                     "VALUES (1, 'PSA', 10, ?, ?, 'cardhedge')", (price, d.isoformat()))
        d += timedelta(days=1)
    (r,) = gr.reactions(conn, "NFL", detrend=False, pre=gr.BEFORE_PRE, horizons=gr.BEFORE)
    assert r.returns[-14] == pytest.approx(math.log(1.2))
