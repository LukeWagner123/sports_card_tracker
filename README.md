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

## Google Trends search interest (free, back to 2004)

Attention, not prices, but with 20 years of history it shows whether interest in a player
follows the season calendar.

1. Go to https://trends.google.com/trends/explore, enter **one player per search**, pick a
   region (e.g. United States) and time range, then click the download icon on "Interest over
   time". Ranges of 5 years or less give weekly data; longer ranges give monthly.
2. Import (names are matched to players accent-insensitively; unknown terms are kept unlinked):

   ```bash
   cardtracker trends import luka_2021-2026.csv wemby.csv --sport basketball
   cardtracker season report nba --data trends
   ```

Values are 0-100 relative to the peak *within each download*, so each file is analysed as its
own series and never compared to another file. Put one player per file: when several share a
file, the quieter ones get compressed into a few low numbers.

## Card Hedge price history (the historical data source)

Daily prices per card and grade back to about spring 2020, plus player index history (from
October 2024). Put `CARDHEDGE_API_KEY` (and your tier's `CARDHEDGE_REQUESTS_PER_MINUTE`) in `.env`.

```bash
cardtracker ch daily                         # everything below, within the day's call budget
cardtracker ch discover                      # add the most-traded cards of flagship sets
cardtracker ch history --grades "PSA 10"     # page each linked card's history back to its start
cardtracker ch players                       # player index history for watchlist players
cardtracker season report epl --data snapshots --grader PSA --grade 10 --detrend
```

**Card universe** (`ch discover`): base rookie cards from Panini Prizm Basketball and Football,
2012 to now, and base cards of every player in the Premier League's flagship sets (Panini Prizm
2019-2024, Topps from 2025). For each set it keeps the most-traded cards by Card Hedge's 30-day
sales: up to 30 per NBA/NFL set with 5+ sales a month, up to 40 per Premier League set with 1+.
Sets are rescanned monthly. Card Hedge's catalog needs care. Base cards are labelled "Base" or
"Prizm". Inserts are also labelled "Base", so the subset must be the base set. The newest sets
carry no rookie flags, so NFL rookies there are checked against nflverse rookie seasons.

**Budget**: the plan allows 5,000 calls a day at 10 a minute (8.3 hours to use it all). Calls are
counted per UTC day in `api_usage` across every run, and `ch daily --budget` (default 4,800)
stops there. A full history costs about 6 calls per card per grade. A card's finished years are
never re-fetched, and its current year is refreshed every `--refresh-days` (default 14). The
order is PSA 10 for every card, then PSA 9, then Raw, then player indexes, alternating between
sports so a short run covers all three.

**Scheduled run** (`.github/workflows/daily-pull.yml`): runs `ch daily` for 45 minutes a day and
keeps the database on a `data` branch (`scripts/db_snapshot.sh`), always encrypted with your
Card Hedge API key (or `SNAPSHOT_KEY`), so the branch exposes only ciphertext. Keep the key: it
is the only way to restore. The job does nothing unless the repository is **private**. Setup:
1. Make the repository private.
2. Add the repository secret `CARDHEDGE_API_KEY`.
3. Merge the workflow into the default branch; scheduled workflows only run from there.
Private repositories get 2,000 free Actions minutes a month, so 45 minutes a day is about 450
calls. That keeps 430 cards current. A large backfill is faster run locally:
`cardtracker ch daily` uses the full day's allowance in about 8 hours.

## Game-level reactions (NFL)

```bash
cardtracker games fetch --first 2019     # NFL games + player game logs (nflverse), EPL results (openfootball)
cardtracker games report --grade "PSA 10"
```

For every game a tracked player played, compares the card's price in the week before with the week
after and the second week after (net of the card's trend), grouped by result, team win/loss streak,
the player's own fantasy output (big/typical/bad game vs their average) and regular season vs
playoffs. The export also measures the two weeks *before* each game: if cards had already moved
before a result nobody knew, a difference afterwards is momentum, not a reaction. Premier League
results are stored, but without rosters players aren't tied to their clubs' results yet.

## Personal site Studio

```bash
scripts/push_studio.sh       # export JSON and commit it to cards/ in the private studio repo
```

The personal site's logged-in Studio (`/studio/cards`) reads `cards/*.json` from the private
`LukeWagner123/studio` repo, never the site's own repo, because the prices are licensed Card
Hedge data. The daily workflow runs this after each pull when the `STUDIO_DATA_TOKEN` secret (a
fine-grained token with Contents read/write on the studio repo) is set.

## Charts

```bash
cardtracker export dashboard --out data/dashboard   # CSVs behind the dashboard
```

`ch daily` also pulls monthly sales counts per player (`player_sales`): every sale whose title
names the player, two years back, 6 players per call (the endpoint allows 150 player-months per
request). One-word names are skipped because they match unrelated sales.

Market indexes per sport and grade, player indexes, and the seasonality tables. Indexes chain the
average daily price change of their cards from 100, so cards entering or leaving don't move them
(`src/cardtracker/analytics/chain_index.py`).

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
cardtracker calendar fetch epl --first 2012    # Premier League first/last matchday (openfootball)
cardtracker calendar import calendars/epl_overrides.csv       # 2019-20: the source file is wrong
cardtracker calendar import calendars/soccer_tournaments.csv  # World Cups, Euros, Club World Cup
cardtracker calendar import my_events.csv      # extra events: league,season,event,date,source
cardtracker prices import history.csv          # historical sales (see src/cardtracker/prices.py)
cardtracker season report nfl --grader PSA --grade 10 --event playoffs_start
```

The Premier League has no playoffs: its phases are early/mid/late season and the offseason, and
international tournaments are events for `--event tournament_start`.

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
