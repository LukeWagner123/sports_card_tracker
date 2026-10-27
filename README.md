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
  ingest/ebay_browse.py eBay Browse API — active listings
  analytics/index.py    median card prices, player price index
  analytics/valuation.py  price-vs-performance residuals (cheap / expensive signal)
  cli.py                `cardtracker` command
```

## Data sources

Only sources whose terms allow automated access. Keys you'll need:

- **PSA**: free token at https://www.psacard.com/publicapi
- **eBay**: free developer account at https://developer.ebay.com (Browse API)
- **Sold prices** (planned): SportsCardsPro / PriceCharting API (paid)
