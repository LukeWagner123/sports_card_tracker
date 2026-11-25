"""The QB study on a tiny league: two QBs, one wins and one loses, cards move accordingly."""
from datetime import date, timedelta

from cardtracker import games
from cardtracker.analytics import qbs
from cardtracker.db import connect, init_db


def _schedule():
    lines = ["game_id,season,game_type,week,gameday,home_team,away_team,home_score,away_score,home_qb_name,away_qb_name,spread_line"]
    d = date(2022, 9, 11)
    for wk in range(1, 13):
        # WIN (QB Winner) beats LOS (QB Loser) every week; both were expected to be even.
        lines.append(f"2022_{wk:02d}_LOS_WIN,2022,REG,{wk},{d + timedelta(days=7 * wk)},WIN,LOS,30,10,Winner Guy II,Loser Guy,0")
    lines.append("2022_19_X_WIN,2022,WC,19,2023-01-14,WIN,X,24,20,Winner Guy II,Someone,3")
    return "\n".join(lines) + "\n"


def test_winner_card_beats_loser_card_and_counts_playoffs():
    c = connect(":memory:")
    init_db(c)
    games._save_games(c, games.nfl_games_from_schedule(_schedule()))
    c.execute("INSERT INTO players (id, name, sport, debut_season) VALUES (1, 'Winner Guy', 'football', 2021), "
              "(2, 'Loser Guy', 'football', 2021), (3, 'Market Filler', 'football', 2020)")
    c.execute("INSERT INTO cards (id, player_id, year, set_name, card_number) VALUES (1, 1, 2021, 'Prizm', '1'), "
              "(2, 2, 2021, 'Prizm', '2')")
    for i in range(3, 20):     # a flat market of other cards
        c.execute("INSERT INTO cards (id, player_id, year, set_name, card_number) VALUES (?, 3, 2020, 'Prizm', ?)", (i, str(i)))
    day = date(2022, 8, 1)
    while day <= date(2023, 3, 31):
        after = day > date(2022, 9, 10)
        rows = [(1, 100.0 * (2 if after else 1)), (2, 100.0 * (0.5 if after else 1))] + [(i, 50.0) for i in range(3, 20)]
        for cid, p in rows:
            for grade in (10, 9):
                c.execute("INSERT INTO price_snapshots (card_id, grader, grade, price_usd, snapshot_date, source) "
                          "VALUES (?, 'PSA', ?, ?, ?, 'cardhedge')", (cid, grade, p, day.isoformat()))
        day += timedelta(days=3)
    out = qbs.build(c, seasons=range(2022, 2023))
    rows = {r["qb"]: r for r in out["rows"]}
    w, l = rows["Winner Guy II"], rows["Loser Guy"]
    assert w["win_pct"] == 1.0 and l["win_pct"] == 0.0 and w["playoff_wins"] == 1 and w["made_playoffs"]
    assert round(w["ex10"]) == 100 and round(l["ex10"]) == -50        # % vs a flat market
    assert abs(w["exp_win_pct"] - 0.5) < 0.01                          # pick'em spreads
    assert "Winner Guy II" in out["coverage"]["with_prizm"]
