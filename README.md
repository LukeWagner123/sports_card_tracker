# sports_card_tracker

A database and research toolkit for valuing sports cards: compare prices across players,
spot cards that look cheap or expensive relative to player performance and supply, and
test whether betting on improving players beats transaction costs.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and roadmap.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # add API keys
cardtracker init-db
pytest

cardtracker stats nfl 2025     # NFL season stats from nflverse
cardtracker stats nba 2025     # NBA 2025-26 stats (run from a home connection)
cardtracker runs               # recent ingestion runs; exits 1 if any are unhealthy
```

## Card Hedge price history (the historical data source)

Daily prices per card and grade back to about spring 2020, plus player index history.
Put `CARDHEDGE_API_KEY` (and your tier's `CARDHEDGE_REQUESTS_PER_MINUTE`) in `.env`.

```bash
cardtracker ch link                          # match watchlist cards to Card Hedge cards
cardtracker ch history                       # page each card's history back to its start
cardtracker ch players                       # player index history for watchlist players
cardtracker season report nba --data snapshots --grader PSA --grade 10
cardtracker season report nfl --data player_index
```

`ch history` costs about 7 calls per card per grade (one per calendar year). At the base tier's
10 requests/minute, 100 cards x 3 grades is ~2,100 calls, about 3.5 hours; it stops at
`--max-calls` (default 4,500/day) and resumes where it left off. Finished years are never
re-fetched; re-running daily only refreshes the current year. Prices are Card Hedge's daily
computed values, not individual sales.

## SportsCardsPro price guide

Current values per card by grade (raw, 7–9.5 any grader, PSA/BGS/SGC/CGC 10) plus yearly sales
volume. The API has **no history**; running `snapshot` daily builds a series going forward.
Their terms require deleting all of it when the subscription ends (`scp purge --yes`), and the
data is licensed for internal use only.

```bash
cardtracker scp link        # match watchlist cards to SportsCardsPro products
cardtracker scp snapshot    # today's values for linked cards (schedule daily)
cardtracker scp purge --yes # required if you cancel
```

## Seasonality analysis

How card prices move through the league calendar: season start, mid-season, the playoff run,
and the offseason.

```bash
cardtracker calendar fetch nfl                 # NFL season dates 1999-present (nflverse schedules)
cardtracker calendar fetch nba --first 2015    # NBA dates from stats.nba.com (run from home)
cardtracker calendar import my_events.csv      # extra events: league,season,event,date,source
cardtracker prices import history.csv          # historical sales (see src/cardtracker/prices.py)
cardtracker season report nfl --grader PSA --grade 10 --event playoffs_start
```

The report shows, pooled across cards and seasons with a per-season breakdown:
- **Return over each phase**: early/mid/late season, playoffs, offseason.
- **Event study**: average price path in the weeks around an event, relative to the event date.
- **Month of year**: each month vs. that card's median for the year.

It needs multi-year price history to say anything; one season is an anecdote. Market-wide
booms and crashes (e.g. 2021-22) affect every card at once, so check the per-season columns
before trusting a pooled number.

## eBay auction tracking

Builds sold-price history for free from the eBay Browse API (5,000 calls/day) by watching
auctions on your watchlist through their final minutes. The last observed bid becomes a sale;
`sales.price_lag_s` records how long before the end it was seen, so treat prices as a lower
bound and filter on lag in analysis. Buy It Now and best-offer sales are not captured.

1. Create a developer account at https://developer.ebay.com, create a **Production** keyset,
   and put the Client ID / Client Secret in `.env`. Read the API License Agreement's data
   retention terms when you accept it: this tool keeps derived prices long-term and deletes raw
   eBay payloads after 7 days.
2. Load cards: `cardtracker watch import watchlists/starter.csv` (card numbers marked
   "verify" in the CSV should be checked before relying on them).
3. Schedule polling every 10 minutes (it skips work that isn't due and stops at 4,500 calls/day):

   ```cron
   */10 * * * * cd /path/to/sports_card_tracker && .venv/bin/cardtracker ebay poll >> data/poll.log 2>&1
   ```

History only accumulates while this runs, so start it early. It needs an always-on machine
(home computer, Raspberry Pi, or a small VPS).

## Layout

```
src/cardtracker/
  schema.sql            canonical tables (players, cards, sales, listings, pop_snapshots, player_stats)
  db.py                 connection, schema init, raw payload storage
  fetcher.py            shared HTTP: robots.txt, rate limits, backoff, stop on block
  runs.py               per-run health tracking (source_runs)
  stats/nfl.py          nflverse season stats
  stats/nba.py          stats.nba.com season stats
  ingest/psa.py         PSA Public API — population snapshots
  ingest/ebay_browse.py eBay Browse API client, title filtering, grade parsing
  ingest/ebay_auctions.py auction observation → observed sale prices
  watchlist.py          load curated watchlist CSVs (watchlists/*.csv)
  analytics/index.py    median card prices, player price index
  analytics/valuation.py  price-vs-performance residuals (cheap / expensive signal)
  cli.py                `cardtracker` command
```

## Data sources

Only sources whose terms allow automated access. Keys you'll need:

- **PSA**: free token at https://www.psacard.com/publicapi
- **eBay**: free developer account at https://developer.ebay.com (Browse API)
- **Sold prices** (planned): SportsCardsPro / PriceCharting API (paid)
