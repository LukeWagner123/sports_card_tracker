-- Canonical schema. Kept portable between SQLite and Postgres.

CREATE TABLE IF NOT EXISTS players (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    sport         TEXT NOT NULL,          -- 'basketball', 'baseball', 'football', ...
    position      TEXT,
    birth_date    TEXT,
    debut_season  INTEGER
);

-- Source-specific player IDs (nba stats PERSON_ID, nflverse gsis_id, ...). Identity across
-- sources goes through this table, never through name matching.
CREATE TABLE IF NOT EXISTS player_external_ids (
    player_id     INTEGER NOT NULL REFERENCES players(id),
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE IF NOT EXISTS cards (
    id            INTEGER PRIMARY KEY,
    player_id     INTEGER NOT NULL REFERENCES players(id),
    year          INTEGER NOT NULL,
    manufacturer  TEXT,                   -- 'Panini', 'Topps', ...
    set_name      TEXT NOT NULL,          -- 'Prizm', 'Chrome', ...
    card_number   TEXT NOT NULL,
    parallel      TEXT NOT NULL DEFAULT 'Base',
    print_run     INTEGER,                -- NULL if unnumbered
    is_rookie     INTEGER NOT NULL DEFAULT 0,
    is_auto       INTEGER NOT NULL DEFAULT 0,
    psa_spec_id   INTEGER,
    UNIQUE (year, set_name, card_number, parallel, player_id)
);

-- Every payload fetched from any source, stored before parsing so parsers can be replayed.
CREATE TABLE IF NOT EXISTS raw_records (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,          -- 'psa', 'ebay_browse', ...
    external_id   TEXT,
    fetched_at    TEXT NOT NULL,
    payload       TEXT NOT NULL           -- raw JSON
);
CREATE INDEX IF NOT EXISTS ix_raw_source ON raw_records (source, external_id);

-- Completed transactions.
CREATE TABLE IF NOT EXISTS sales (
    id            INTEGER PRIMARY KEY,
    card_id       INTEGER REFERENCES cards(id),   -- NULL until matched
    grader        TEXT,                   -- 'PSA', 'BGS', 'SGC', 'RAW'
    grade         REAL,
    price_usd     REAL NOT NULL,
    sold_at       TEXT NOT NULL,
    sale_type     TEXT,                   -- 'auction', 'bin', 'best_offer'
    source        TEXT NOT NULL,
    external_id   TEXT,
    raw_title     TEXT,
    price_lag_s   INTEGER,                -- observed prices only: seconds between last observation and end
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS ix_sales_card ON sales (card_id, grader, grade, sold_at);

-- Active listings, observed over time (supply and asking prices).
CREATE TABLE IF NOT EXISTS listings (
    id            INTEGER PRIMARY KEY,
    card_id       INTEGER REFERENCES cards(id),
    grader        TEXT,
    grade         REAL,
    price_usd     REAL NOT NULL,
    listing_type  TEXT,
    bid_count     INTEGER,
    observed_at   TEXT NOT NULL,
    ends_at       TEXT,
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    raw_title     TEXT,
    UNIQUE (source, external_id, observed_at)
);
CREATE INDEX IF NOT EXISTS ix_listings_item ON listings (source, external_id, observed_at);
CREATE INDEX IF NOT EXISTS ix_listings_ends ON listings (ends_at);

-- Dated snapshots, never overwritten, so population growth is measurable.
CREATE TABLE IF NOT EXISTS pop_snapshots (
    id            INTEGER PRIMARY KEY,
    card_id       INTEGER NOT NULL REFERENCES cards(id),
    grader        TEXT NOT NULL,
    grade         REAL NOT NULL,
    population    INTEGER NOT NULL,
    snapshot_date TEXT NOT NULL,
    UNIQUE (card_id, grader, grade, snapshot_date)
);

-- Generic performance metrics so any sport fits.
CREATE TABLE IF NOT EXISTS player_stats (
    id            INTEGER PRIMARY KEY,
    player_id     INTEGER NOT NULL REFERENCES players(id),
    season        INTEGER NOT NULL,
    as_of         TEXT NOT NULL,          -- date the stat line reflects
    metric        TEXT NOT NULL,          -- 'ppg', 'war', 'ops', 'per', ...
    value         REAL NOT NULL,
    source        TEXT NOT NULL,
    UNIQUE (player_id, season, as_of, metric, source)
);

-- Cards we actively collect data on.
CREATE TABLE IF NOT EXISTS watchlist (
    card_id       INTEGER PRIMARY KEY REFERENCES cards(id),
    search_query  TEXT,                   -- query used against listing sources
    exclude_terms TEXT,                   -- ';'-separated title terms that mean a different card
    last_searched TEXT,
    added_at      TEXT NOT NULL,
    notes         TEXT
);

-- One row per ingestion run, so a silently broken source shows up as a status or row-count drop.
CREATE TABLE IF NOT EXISTS source_runs (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,          -- 'running', 'ok', 'idle', 'suspect', 'blocked', 'error'
    rows_written  INTEGER NOT NULL DEFAULT 0,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_source ON source_runs (source, started_at);

-- Key dates per league season (season_start, playoffs_start, championship, ...). Seasons are keyed
-- by the year they start. Phases (offseason, early/mid/late season, playoffs) derive from these.
CREATE TABLE IF NOT EXISTS season_events (
    league        TEXT NOT NULL,          -- 'NFL', 'NBA'
    season        INTEGER NOT NULL,
    event         TEXT NOT NULL,
    date          TEXT NOT NULL,
    source        TEXT NOT NULL,
    PRIMARY KEY (league, season, event)
);

-- Source-specific card IDs (sportscardspro product id, PSA spec id, ...).
CREATE TABLE IF NOT EXISTS card_external_ids (
    card_id       INTEGER NOT NULL REFERENCES cards(id),
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    PRIMARY KEY (source, external_id),
    UNIQUE (card_id, source)
);

-- Point-in-time price-guide values (not individual sales). One row per card/grade/day/source;
-- daily snapshots become a price history going forward.
CREATE TABLE IF NOT EXISTS price_snapshots (
    card_id       INTEGER NOT NULL REFERENCES cards(id),
    grader        TEXT NOT NULL,          -- 'RAW', 'ANY' (any grading company), 'PSA', 'BGS', ...
    grade         REAL,                   -- NULL for RAW
    price_usd     REAL NOT NULL,
    sales_volume  INTEGER,                -- source's yearly units sold, all grades
    snapshot_date TEXT NOT NULL,
    source        TEXT NOT NULL,
    UNIQUE (card_id, grader, grade, snapshot_date, source)
);
CREATE INDEX IF NOT EXISTS ix_snapshots_card ON price_snapshots (card_id, grader, grade, snapshot_date);

-- Player-level price index over time (e.g. Card Hedge: market-cap-weighted top cards).
CREATE TABLE IF NOT EXISTS player_index (
    player_id     INTEGER NOT NULL REFERENCES players(id),
    date          TEXT NOT NULL,
    value         REAL NOT NULL,
    volume_30d    INTEGER,
    card_count    INTEGER,
    source        TEXT NOT NULL,
    PRIMARY KEY (player_id, date, source)
);

-- Google Trends search interest. Values are 0-100 relative to the peak *within one download*,
-- so each downloaded file is its own series (`batch`) and must not be compared or spliced across.
CREATE TABLE IF NOT EXISTS search_interest (
    term          TEXT NOT NULL,          -- as it appears in the export, e.g. 'Luka Doncic'
    player_id     INTEGER REFERENCES players(id),   -- NULL for non-player terms ('sports cards')
    geo           TEXT NOT NULL,          -- 'United States', 'Worldwide', ...
    date          TEXT NOT NULL,          -- start of the week/month the value covers
    value         REAL NOT NULL,
    batch         TEXT NOT NULL,          -- hash of the source file
    source        TEXT NOT NULL,
    PRIMARY KEY (term, geo, batch, date)
);
