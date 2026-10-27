import json
from pathlib import Path

import pytest
import requests

from cardtracker.db import connect, init_db
from cardtracker.fetcher import BlockedError, DisallowedError, Fetcher
from cardtracker.runs import track_run
from cardtracker.stats import nba, nfl

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, status=200, text="", headers=None, payload=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    """Serves robots.txt plus a queue of responses for everything else."""

    def __init__(self, responses, robots="User-agent: *\nDisallow: /api/\n"):
        self.headers = {}
        self.responses = list(responses)
        self.robots = robots
        self.calls = []

    def get(self, url, **kwargs):
        if url.endswith("/robots.txt"):
            return FakeResponse(text=self.robots)
        self.calls.append(url)
        return self.responses.pop(0)


def make_fetcher(responses, **kw):
    sleeps = []
    f = Fetcher(session=FakeSession(responses), sleep=sleeps.append, **kw)
    return f, sleeps


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    return c


def test_fetcher_respects_robots():
    f, _ = make_fetcher([])
    with pytest.raises(DisallowedError):
        f.get("https://example.com/api/sales")
    assert f.session.calls == []


def test_fetcher_stops_on_block_without_retry():
    f, _ = make_fetcher([FakeResponse(403, headers={"cf-mitigated": "challenge"}), FakeResponse(200)])
    with pytest.raises(BlockedError):
        f.get("https://example.com/page")
    assert len(f.session.calls) == 1


def test_fetcher_retries_with_backoff_and_retry_after():
    f, sleeps = make_fetcher(
        [FakeResponse(503), FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, text="ok")],
        min_interval=0,
    )
    assert f.get("https://example.com/page").text == "ok"
    assert sleeps == [2.0, 7.0]


def test_fetcher_gives_up_after_max_retries():
    f, _ = make_fetcher([FakeResponse(500)] * 3, max_retries=2, min_interval=0)
    with pytest.raises(requests.HTTPError):
        f.get("https://example.com/page")


def test_run_tracking_flags_row_drop(conn):
    with track_run(conn, "src") as run:
        run.rows_written = 100
    with track_run(conn, "src") as run:
        run.rows_written = 10
    statuses = [r["status"] for r in conn.execute("SELECT status FROM source_runs ORDER BY id")]
    assert statuses == ["ok", "suspect"]


def test_run_tracking_records_block(conn):
    with pytest.raises(BlockedError):
        with track_run(conn, "src"):
            raise BlockedError("403")
    assert conn.execute("SELECT status FROM source_runs").fetchone()["status"] == "blocked"


def test_nfl_parse_keeps_skill_positions_only():
    recs = nfl.parse((FIXTURES / "nflverse_stats_player_sample.csv").read_text())
    assert [r["position"] for r in recs] == ["QB", "WR"]
    rodgers = recs[0]
    assert rodgers["name"] == "Aaron Rodgers" and rodgers["season"] == 2025
    assert rodgers["metrics"]["passing_tds"] == 24.0


def test_nfl_ingest_is_idempotent(conn):
    text = (FIXTURES / "nflverse_stats_player_sample.csv").read_text()
    f, _ = make_fetcher([FakeResponse(text=text), FakeResponse(text=text)], min_interval=0)
    first = nfl.ingest_season(conn, 2025, fetcher=f)
    nfl.ingest_season(conn, 2025, fetcher=f)
    assert first > 0
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0] == first


def test_nba_parse_and_ingest(conn):
    payload = json.loads((FIXTURES / "nba_leaguedashplayerstats_sample.json").read_text())
    recs = nba.parse(payload)
    assert recs[0]["name"] == "Luka Doncic" and recs[0]["metrics"]["pts"] == 28.4
    f, _ = make_fetcher([FakeResponse(payload=payload)], min_interval=0)
    assert nba.ingest_season(conn, 2025, fetcher=f) == 26
    assert nba.season_label(2025) == "2025-26"
