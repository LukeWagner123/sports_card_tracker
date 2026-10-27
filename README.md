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
