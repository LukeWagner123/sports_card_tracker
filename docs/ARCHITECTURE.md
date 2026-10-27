# Architecture

## Goal

Answer three questions with data:

1. **Comparison** — how do card prices compare across players, sets, parallels, and grades?
2. **Mispricing** — given a player's performance and a card's supply, is the card cheap or expensive relative to peers?
3. **Alpha** — can we buy cards of players we expect to improve *before* the market reprices them, by enough to beat transaction costs?

## Layers

```
 ┌──────────────────────────────────────────────────────────────────┐
 │ 4. Signals / research   valuation residuals, momentum, backtests │
 ├──────────────────────────────────────────────────────────────────┤
 │ 3. Analytics            price index per card/player, pop-adjusted│
 │                         scarcity, performance scores             │
 ├──────────────────────────────────────────────────────────────────┤
 │ 2. Canonical store      players, cards, sales, listings,         │
 │    (SQLite → Postgres)  pop_snapshots, player_stats              │
 ├──────────────────────────────────────────────────────────────────┤
 │ 1. Ingestion            one module per source; raw payloads kept │
 │                         in raw_records so parsing can be re-run  │
 └──────────────────────────────────────────────────────────────────┘
```

### 1. Ingestion (`src/cardtracker/ingest/`)

Every source implements the same shape: fetch → store the raw payload in `raw_records` → parse into canonical tables. Keeping raw payloads means a parser bug never loses data; fix the parser and replay.

All HTTP goes through `fetcher.Fetcher`: robots.txt checked first, per-host rate limit, retries with backoff on 429/5xx (honoring `Retry-After`), and an immediate `BlockedError` on 403 or a bot challenge — never retried or worked around. Every run is wrapped in `runs.track_run`, which records status and row count in `source_runs` and marks a run `suspect` when it writes nothing or less than half of the previous good run (the usual symptom of a source changing format). `cardtracker runs` exits non-zero when anything is unhealthy, so a scheduler can alert on it.

Sports in scope: **basketball and football**.

| Source | Data | Access | Status |
|---|---|---|---|
| PSA Public API | Cert lookup, spec population | Free token, low daily limit | Client stubbed |
| eBay Browse API | Active auctions observed to their end → observed sale prices (lower bound) | Free developer key, 5,000 calls/day | **Built** (needs keys) |
| SportsCardsPro / PriceCharting | **Current** values by card + grade, yearly sales volume | Paid API ($50/mo). No history ("Historic prices and historic sales are not supported"); data must be purged when the subscription ends | **Working**: daily snapshots |
| nflverse schedules | NFL season dates 1999– (regular season, each playoff round, Super Bowl) | Free | **Working** |
| Historical sales CSV | Any history obtained elsewhere | `cardtracker prices import` | **Built** |
| eBay Marketplace Insights | Sold items, last 90 days | Restricted — apply | Not started |
| nflverse | NFL season stats (QB/RB/WR/TE) | Free, open release CSVs | **Working** |
| stats.nba.com | NBA season stats | Free; unreachable from cloud IPs — run from home | Working (untested live) |

Rule: only sources whose terms permit automated access. A pipeline built on a source that can switch us off leaves unrecoverable gaps in history.

Sources checked and ruled out (Oct 2026):

| Source | Why not |
|---|---|
| 130point, SportsCardsPro site, PSA pop pages, Card Ladder | Cloudflare bot challenge (403) |
| eBay sold search pages, Heritage (ha.com) | 403 to automated requests |
| Goldin | Pages are client-rendered; data comes only from `/api/`, which robots.txt disallows |
| Fanatics Collect | Terms of use §3.5 and §11.3.8 explicitly prohibit scraping |

### 2. Canonical store (`src/cardtracker/schema.sql`)

The hard problem is **identity**: mapping a messy listing title ("Luka Prizm Silver RC PSA 10") to one canonical card. The schema separates:

- `players` — one row per athlete.
- `cards` — one row per distinct printed card (year, set, number, parallel, print run). Grade is *not* part of a card.
- `sales` / `listings` — one row per transaction / active listing, pointing to a `card_id` + `grader` + `grade`. `card_id` may be NULL until matched; `raw_title` is always kept.
- `pop_snapshots` — dated population counts per card/grader/grade. Snapshots, not overwrites, so pop growth over time is measurable.
- `player_stats` — dated performance metrics, generic (`metric`, `value`) so any sport fits.

Start on SQLite (zero setup). Move to Postgres when concurrent writers or data volume require it; the SQL is kept portable.

### 3. Analytics (`src/cardtracker/analytics/`)

- **Price index** (`index.py`): robust (median) price per card+grade per time bucket, then a player index as a weighted basket of their key cards. Medians, because single sales are noisy and shill/best-offer outliers are common.
- **Scarcity**: price vs. population — a PSA 10 with pop 50 and pop 5,000 are different assets.
- **Performance score**: per-player standardized score from `player_stats` relative to peers (same sport, position, experience).

### 4. Signals (`src/cardtracker/analytics/valuation.py`)

First signal — **cross-sectional residual**: regress log(card price) on performance score (and later scarcity, age, market size) across comparable players. A large negative residual means "cheap for how good the player is"; positive means "priced on hype."

Second signal — **forward performance thesis**: your own projection of player improvement vs. what current prices imply. This is where alpha would come from, and it needs a backtest harness before money goes in.

### Reality checks built into the design

- **Transaction costs**: eBay final value fees (~13%), shipping, grading fees, and bid-ask spread mean a position needs roughly 20–30% appreciation to break even. Every backtest must net these out.
- **No backtest without history**: sold-price history is the bottleneck. Start collecting daily now; history not captured cannot be recovered later.
- **Liquidity**: thinly traded cards have stale prices; the index must carry a sales-count / confidence field.

## Roadmap

1. Schema + DB init + CLI ✅
2. Robust fetcher + run health tracking ✅
3. Player stats: NFL (nflverse) ✅, NBA (stats.nba.com) ✅
4. PSA pop snapshots for a watchlist of cards
5. eBay auction tracker for the watchlist ✅ (needs keys + an always-on scheduler)
6. Season calendars (NFL from nflverse, NBA from stats.nba.com) + seasonality analysis ✅
7. Historical sold-price source — open question; PriceCharting/SportsCardsPro has no history
8. Title → card matcher
9. Price index + residual signal + backtest harness with costs
