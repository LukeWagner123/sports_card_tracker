-- Canonical schema. Kept portable between SQLite and Postgres.

CREATE TABLE IF NOT EXISTS players (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    sport         TEXT NOT NULL,          -- 'basketball', 'baseball', 'football', ...
    position      TEXT,
    birth_date    TEXT,
    debut_season  INTEGER,
    external_ids  TEXT,                   -- JSON: {"nba_api": 1629029, ...}
    UNIQUE (name, sport, birth_date)
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
    observed_at   TEXT NOT NULL,
    ends_at       TEXT,
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    raw_title     TEXT,
    UNIQUE (source, external_id, observed_at)
);

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
    added_at      TEXT NOT NULL,
    notes         TEXT
);
