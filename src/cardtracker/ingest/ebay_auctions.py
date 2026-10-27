"""Build sold-price history by observing eBay auctions through their final minutes.

The Browse API only shows active listings, but an auction's current bid shortly before it
ends is close to its final price. Each `poll`:

1. Searches each watchlist card's auctions (at most once per `search_interval`).
2. Re-fetches individual auctions ending within `refresh_window`, since most bidding happens
   in the last minutes.
3. Finalizes auctions whose end time has passed: the last observed bid becomes a sale, with
   `price_lag_s` recording how long before the end it was seen.

The recorded price is a lower bound on the hammer price; bids after the last observation are
missed. Filter on price_lag_s in analysis. Run poll every ~10 minutes.

eBay content retention: raw API payloads are pruned after `RAW_RETENTION_DAYS`; only derived
facts (price, date, grade, title) are kept. Check this against the eBay API License Agreement.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import requests

from cardtracker.db import store_raw
from cardtracker.ingest.ebay_browse import (EbayBrowseClient, auction_observation, parse_grade,
                                            title_matches)
from cardtracker.names import last_name
from cardtracker.runs import track_run

SOURCE = "ebay_browse"
SALE_SOURCE = "ebay_auction_observed"
DAILY_CALL_BUDGET = 4500          # of the default 5,000; leaves headroom for manual use
RAW_RETENTION_DAYS = 7


def _utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def calls_today(conn: sqlite3.Connection, now: datetime) -> int:
    day = now.date().isoformat()
    return conn.execute(
        "SELECT COUNT(*) FROM raw_records WHERE source = ? AND substr(fetched_at, 1, 10) = ?",
        (SOURCE, day),
    ).fetchone()[0]


def _required_terms(card: sqlite3.Row) -> list[str]:
    return [last_name(card["player_name"]), card["card_number"].lstrip("#")]


def _record(conn: sqlite3.Connection, card: sqlite3.Row, obs: dict, observed_at: str) -> bool:
    exclude = [t.strip() for t in (card["exclude_terms"] or "").split(";") if t.strip()]
    if not title_matches(obs["title"], _required_terms(card), exclude):
        return False
    grader, grade = parse_grade(obs["title"])
    conn.execute(
        "INSERT OR IGNORE INTO listings (card_id, grader, grade, price_usd, listing_type, bid_count, "
        "observed_at, ends_at, source, external_id, raw_title) "
        "VALUES (?, ?, ?, ?, 'AUCTION', ?, ?, ?, ?, ?, ?)",
        (card["card_id"], grader, grade, obs["price"], obs["bid_count"], observed_at,
         obs["ends_at"], SOURCE, obs["item_id"], obs["title"]),
    )
    return True


def _watchlist(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT w.card_id, w.search_query, w.exclude_terms, w.last_searched, c.card_number, "
        "p.name AS player_name FROM watchlist w JOIN cards c ON c.id = w.card_id "
        "JOIN players p ON p.id = c.player_id ORDER BY w.last_searched IS NOT NULL, w.last_searched"
    ).fetchall()


def search_due(conn: sqlite3.Connection, client: EbayBrowseClient, now: datetime,
               search_interval: timedelta, budget: int) -> tuple[int, int]:
    """Search watchlist cards not searched within search_interval. Returns (calls, rows)."""
    observed_at = now.isoformat(timespec="seconds")
    calls = rows = 0
    for card in _watchlist(conn):
        if calls >= budget:
            break
        if card["last_searched"] and now - _utc(card["last_searched"]) < search_interval:
            continue
        payload = client.search_auctions(card["search_query"])
        calls += 1
        store_raw(conn, SOURCE, card["search_query"], payload)
        for item in payload.get("itemSummaries", []):
            obs = auction_observation(item)
            if obs and _record(conn, card, obs, observed_at):
                rows += 1
        conn.execute("UPDATE watchlist SET last_searched = ? WHERE card_id = ?", (observed_at, card["card_id"]))
        conn.commit()
    return calls, rows


def refresh_ending(conn: sqlite3.Connection, client: EbayBrowseClient, now: datetime,
                   refresh_window: timedelta, min_gap: timedelta, budget: int) -> tuple[int, int]:
    """Re-fetch tracked auctions ending soon that weren't seen in the last min_gap."""
    candidates = conn.execute(
        "SELECT l.external_id, l.card_id, MAX(l.observed_at) AS last_seen FROM listings l "
        "WHERE l.source = ? AND l.listing_type = 'AUCTION' AND l.ends_at > ? AND l.ends_at <= ? "
        "GROUP BY l.external_id, l.card_id ORDER BY MIN(l.ends_at)",
        (SOURCE, now.isoformat(timespec="seconds"), (now + refresh_window).isoformat(timespec="seconds")),
    ).fetchall()
    cards = {c["card_id"]: c for c in _watchlist(conn)}
    calls = rows = 0
    for cand in candidates:
        if calls >= budget:
            break
        if now - _utc(cand["last_seen"]) < min_gap or cand["card_id"] not in cards:
            continue
        calls += 1
        try:
            payload = client.get_item(cand["external_id"])
        except requests.HTTPError:
            continue             # ended early or withdrawn; finalize decides from what we have
        store_raw(conn, SOURCE, cand["external_id"], payload)
        obs = auction_observation(payload)
        if obs and _record(conn, cards[cand["card_id"]], obs, now.isoformat(timespec="seconds")):
            rows += 1
        conn.commit()
    return calls, rows


def finalize(conn: sqlite3.Connection, now: datetime) -> int:
    """Turn ended auctions with at least one bid into sales. Returns sales written."""
    ended = conn.execute(
        "SELECT l.* FROM listings l JOIN ("
        "  SELECT external_id, MAX(observed_at) AS last_seen FROM listings "
        "  WHERE source = ? AND listing_type = 'AUCTION' AND ends_at <= ? GROUP BY external_id"
        ") last ON last.external_id = l.external_id AND last.last_seen = l.observed_at "
        "WHERE l.source = ? AND NOT EXISTS ("
        "  SELECT 1 FROM sales s WHERE s.source = ? AND s.external_id = l.external_id)",
        (SOURCE, now.isoformat(timespec="seconds"), SOURCE, SALE_SOURCE),
    ).fetchall()
    written = 0
    for row in ended:
        if not row["bid_count"]:
            continue             # no bids: unsold
        lag = int((_utc(row["ends_at"]) - _utc(row["observed_at"])).total_seconds())
        conn.execute(
            "INSERT OR IGNORE INTO sales (card_id, grader, grade, price_usd, sold_at, sale_type, "
            "source, external_id, raw_title, price_lag_s) VALUES (?, ?, ?, ?, ?, 'auction', ?, ?, ?, ?)",
            (row["card_id"], row["grader"], row["grade"], row["price_usd"], row["ends_at"],
             SALE_SOURCE, row["external_id"], row["raw_title"], max(lag, 0)),
        )
        written += 1
    conn.commit()
    return written


def prune_raw(conn: sqlite3.Connection, now: datetime, days: int = RAW_RETENTION_DAYS) -> int:
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
    cur = conn.execute("DELETE FROM raw_records WHERE source = ? AND fetched_at < ?", (SOURCE, cutoff))
    conn.commit()
    return cur.rowcount


def poll(conn: sqlite3.Connection, client: EbayBrowseClient,
         search_interval: timedelta = timedelta(hours=2),
         refresh_window: timedelta = timedelta(minutes=15),
         min_gap: timedelta = timedelta(minutes=5),
         daily_budget: int = DAILY_CALL_BUDGET) -> dict:
    now = datetime.now(timezone.utc)
    # Run sizes vary with what is due, so only zero-observation runs are flagged, not drops.
    with track_run(conn, SALE_SOURCE, compare_previous=False) as run:
        remaining = max(daily_budget - calls_today(conn, now), 0)
        # Near-end refreshes first: they decide price accuracy and are the most time-sensitive.
        r_calls, r_rows = refresh_ending(conn, client, now, refresh_window, min_gap, remaining)
        s_calls, s_rows = search_due(conn, client, now, search_interval, remaining - r_calls)
        sales = finalize(conn, now)
        pruned = prune_raw(conn, now)
        run.rows_written = r_rows + s_rows + sales
        run.idle = r_calls + s_calls == 0 and sales == 0
    return {"calls": r_calls + s_calls, "observations": r_rows + s_rows, "sales": sales,
            "budget_left": remaining - r_calls - s_calls, "raw_pruned": pruned}
