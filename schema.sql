-- Personal MTG database schema (SQLite)
--
-- `cards` is the existing Scryfall Oracle Cards catalog. `library_items`
-- contains exactly the information supplied by a ManaBox collection export,
-- except for the card name: that remains in `cards` and is reached through
-- the foreign key. Printing-specific fields remain in the library row because
-- the Oracle Cards catalog does not contain every printing.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS cards (
    id             TEXT PRIMARY KEY,
    oracle_id      TEXT,
    name           TEXT NOT NULL,
    mana_cost      TEXT,
    type_line      TEXT,
    oracle_text    TEXT,
    colors         TEXT,
    color_identity TEXT,
    rarity         TEXT,
    set_code       TEXT,
    image_uri      TEXT
);

CREATE INDEX IF NOT EXISTS cards_name_idx ON cards (name);

CREATE TABLE IF NOT EXISTS library_items (
    manabox_id               INTEGER PRIMARY KEY,
    card_id                  TEXT NOT NULL,
    scryfall_id              TEXT NOT NULL,
    binder_name              TEXT NOT NULL,
    binder_type              TEXT NOT NULL,
    set_code                 TEXT NOT NULL,
    set_name                 TEXT NOT NULL,
    collector_number         TEXT NOT NULL,
    foil                     TEXT NOT NULL,
    rarity                   TEXT NOT NULL,
    quantity                 INTEGER NOT NULL CHECK (quantity >= 0),
    purchase_price_cents     INTEGER CHECK (
        purchase_price_cents IS NULL OR purchase_price_cents >= 0
    ),
    purchase_price_currency  TEXT,
    misprint                 INTEGER NOT NULL CHECK (misprint IN (0, 1)),
    altered                  INTEGER NOT NULL CHECK (altered IN (0, 1)),
    condition                TEXT NOT NULL,
    language                 TEXT NOT NULL,
    added                    TEXT NOT NULL,

    FOREIGN KEY (card_id) REFERENCES cards (id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS library_items_card_id_idx
    ON library_items (card_id);

CREATE INDEX IF NOT EXISTS library_items_scryfall_id_idx
    ON library_items (scryfall_id);

CREATE INDEX IF NOT EXISTS library_items_binder_name_idx
    ON library_items (binder_name);

CREATE VIEW IF NOT EXISTS library AS
SELECT
    li.manabox_id,
    li.scryfall_id,
    c.name,
    c.mana_cost,
    c.type_line,
    c.oracle_text,
    c.colors,
    c.color_identity,
    li.binder_name,
    li.binder_type,
    li.set_code,
    li.set_name,
    li.collector_number,
    li.foil,
    li.rarity,
    li.quantity,
    li.purchase_price_cents,
    li.purchase_price_currency,
    li.misprint,
    li.altered,
    li.condition,
    li.language,
    li.added
FROM library_items AS li
JOIN cards AS c ON c.id = li.card_id;

-- A stored deck is the original text file plus the name derived from its
-- filename. Its reservations disappear automatically when the deck is
-- deleted; the owned quantities in library_items are never changed.
CREATE TABLE IF NOT EXISTS decks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    filename    TEXT NOT NULL,
    content     TEXT NOT NULL,
    added_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS deck_reservations (
    deck_id          INTEGER NOT NULL,
    library_item_id  INTEGER NOT NULL,
    quantity         INTEGER NOT NULL CHECK (quantity > 0),

    PRIMARY KEY (deck_id, library_item_id),

    FOREIGN KEY (deck_id) REFERENCES decks (id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,

    FOREIGN KEY (library_item_id) REFERENCES library_items (manabox_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS deck_reservations_library_item_idx
    ON deck_reservations (library_item_id);

-- Reservations affect availability, not ownership or theoretical wealth.
CREATE VIEW IF NOT EXISTS available_library AS
SELECT
    library.*,
    COALESCE(reservations.quantity, 0) AS reserved_quantity,
    library.quantity - COALESCE(reservations.quantity, 0) AS available_quantity
FROM library
LEFT JOIN (
    SELECT library_item_id, SUM(quantity) AS quantity
    FROM deck_reservations
    GROUP BY library_item_id
) AS reservations
    ON reservations.library_item_id = library.manabox_id;

CREATE VIEW IF NOT EXISTS deck_library AS
SELECT
    decks.id AS deck_id,
    decks.name AS deck_name,
    decks.filename,
    library.*,
    deck_reservations.quantity AS reserved_quantity
FROM deck_reservations
JOIN decks ON decks.id = deck_reservations.deck_id
JOIN library ON library.manabox_id = deck_reservations.library_item_id;

-- Current personal wealth snapshot. Each independent enumeration tool updates
-- only its own category and timestamp. Reserved assets remain fully owned and
-- therefore remain part of these values.
CREATE TABLE IF NOT EXISTS wealth (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    bonds                   REAL NOT NULL DEFAULT 0,
    real_estate             REAL NOT NULL DEFAULT 0,
    liquid                  REAL NOT NULL DEFAULT 0,
    futures                 REAL NOT NULL DEFAULT 0,
    stocks                  REAL NOT NULL DEFAULT 0,
    bonds_updated_at        TEXT,
    real_estate_updated_at  TEXT,
    liquid_updated_at       TEXT,
    futures_updated_at      TEXT,
    stocks_updated_at       TEXT
);

INSERT OR IGNORE INTO wealth (id) VALUES (1);

CREATE VIEW IF NOT EXISTS net_worth AS
SELECT
    bonds,
    real_estate,
    liquid,
    futures,
    stocks,
    bonds + real_estate + liquid + futures + stocks AS total
FROM wealth
WHERE id = 1;

-- Physical equipment in the lab. Purchase values are imported from the
-- current inventory spreadsheet and stored as integer USD cents.
CREATE TABLE IF NOT EXISTS realEstate (
    id                    INTEGER PRIMARY KEY,
    device                TEXT NOT NULL,
    purchase_price_cents  INTEGER NOT NULL CHECK (purchase_price_cents >= 0),
    source_file           TEXT NOT NULL,
    source_row            INTEGER NOT NULL CHECK (source_row > 0),
    imported_at           TEXT NOT NULL
);

-- News from market-specific and general providers shares one normalized store.
-- Provider-prefixed article IDs prevent collisions between upstream systems.
CREATE TABLE IF NOT EXISTS news_articles (
    article_id   TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    headline     TEXT NOT NULL,
    summary      TEXT,
    author       TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    content      TEXT,
    url          TEXT,
    source       TEXT,
    received_at  TEXT NOT NULL,
    raw_json     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS news_articles_updated_idx
    ON news_articles (updated_at DESC);

CREATE TABLE IF NOT EXISTS news_article_symbols (
    article_id  TEXT NOT NULL,
    symbol      TEXT NOT NULL,

    PRIMARY KEY (article_id, symbol),

    FOREIGN KEY (article_id) REFERENCES news_articles (article_id)
        ON UPDATE CASCADE
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS news_article_symbols_symbol_idx
    ON news_article_symbols (symbol, article_id);

-- Idempotent Alpaca stock and cryptocurrency bar history. The source fields
-- distinguish otherwise identical series retrieved from different feeds.
CREATE TABLE IF NOT EXISTS historic_bars (
    asset_class  TEXT NOT NULL CHECK (asset_class IN ('stock', 'crypto')),
    symbol       TEXT NOT NULL,
    timeframe    TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    open         REAL NOT NULL,
    high         REAL NOT NULL,
    low          REAL NOT NULL,
    close        REAL NOT NULL,
    volume       REAL NOT NULL,
    trade_count  INTEGER,
    vwap         REAL,
    feed         TEXT NOT NULL DEFAULT '',
    location     TEXT NOT NULL DEFAULT '',
    adjustment   TEXT NOT NULL DEFAULT '',
    fetched_at   TEXT NOT NULL,
    raw_json     TEXT NOT NULL,

    PRIMARY KEY (
        asset_class, symbol, timeframe, timestamp,
        feed, location, adjustment
    )
);

CREATE INDEX IF NOT EXISTS historic_bars_series_idx
    ON historic_bars (
        asset_class, symbol, timeframe, feed, location, adjustment, timestamp
    );

CREATE TABLE IF NOT EXISTS historic_fetch_runs (
    id               INTEGER PRIMARY KEY,
    asset_class      TEXT NOT NULL,
    symbols          TEXT NOT NULL,
    timeframe        TEXT NOT NULL,
    requested_start  TEXT NOT NULL,
    requested_end    TEXT,
    feed             TEXT NOT NULL DEFAULT '',
    location         TEXT NOT NULL DEFAULT '',
    adjustment       TEXT NOT NULL DEFAULT '',
    pages            INTEGER NOT NULL DEFAULT 0,
    rows_saved       INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL CHECK (
        status IN ('running', 'complete', 'partial', 'failed')
    ),
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    error            TEXT
);

-- OpenInsider's homepage "Latest Insider Buys" rows. The deterministic ID
-- makes repeated homepage scrapes safe while retaining every displayed field.
CREATE TABLE IF NOT EXISTS insiders (
    insider_id             TEXT PRIMARY KEY,
    flags                  TEXT,
    filing_date            TEXT NOT NULL,
    trade_date             TEXT NOT NULL,
    ticker                 TEXT NOT NULL,
    company                TEXT NOT NULL,
    insider                TEXT NOT NULL,
    title                  TEXT,
    trade_type             TEXT NOT NULL,
    price                  REAL,
    quantity               INTEGER,
    owned                  INTEGER,
    ownership_change       REAL,
    ownership_change_text  TEXT,
    trade_value            REAL,
    one_day_change         REAL,
    one_week_change        REAL,
    one_month_change       REAL,
    six_month_change       REAL,
    filing_url             TEXT,
    ticker_url             TEXT,
    insider_url            TEXT,
    scraped_at             TEXT NOT NULL,
    raw_json               TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS insiders_filing_date_idx
    ON insiders (filing_date DESC);

CREATE INDEX IF NOT EXISTS insiders_ticker_idx
    ON insiders (ticker, trade_date DESC);

-- Immutable silver transaction ledger. Amounts paid and received are stored
-- as integer USD cents; metal quantities are troy ounces.
CREATE TABLE IF NOT EXISTS silver_transactions (
    id              INTEGER PRIMARY KEY,
    transaction_type TEXT NOT NULL CHECK (
        transaction_type IN ('purchase', 'sale')
    ),
    troy_ounces     REAL NOT NULL CHECK (troy_ounces > 0),
    total_cents     INTEGER NOT NULL CHECK (total_cents >= 0),
    transacted_at   TEXT NOT NULL,
    recorded_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS silver_transactions_type_date_idx
    ON silver_transactions (transaction_type, transacted_at);

-- A sale consumes purchase lots oldest-first. These rows preserve the exact
-- cost basis assigned to every partial or complete FIFO lot consumption.
CREATE TABLE IF NOT EXISTS silver_sale_allocations (
    sale_id           INTEGER NOT NULL,
    purchase_id       INTEGER NOT NULL,
    troy_ounces       REAL NOT NULL CHECK (troy_ounces > 0),
    cost_basis_cents  INTEGER NOT NULL CHECK (cost_basis_cents >= 0),

    PRIMARY KEY (sale_id, purchase_id),

    FOREIGN KEY (sale_id) REFERENCES silver_transactions (id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,

    FOREIGN KEY (purchase_id) REFERENCES silver_transactions (id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS silver_sale_allocations_purchase_idx
    ON silver_sale_allocations (purchase_id);

-- USD proceeds leave tracked wealth, but remain recorded for reporting.
CREATE TABLE IF NOT EXISTS cash_removed (
    id                     INTEGER PRIMARY KEY,
    source_asset           TEXT NOT NULL,
    source_transaction_id  INTEGER NOT NULL,
    amount_cents           INTEGER NOT NULL CHECK (amount_cents >= 0),
    removed_at             TEXT NOT NULL,

    UNIQUE (source_asset, source_transaction_id)
);

CREATE VIEW IF NOT EXISTS silver_lots AS
SELECT
    purchases.id AS purchase_id,
    purchases.transacted_at,
    purchases.troy_ounces AS purchased_ounces,
    purchases.total_cents AS purchase_cost_cents,
    purchases.troy_ounces - COALESCE(SUM(allocations.troy_ounces), 0)
        AS remaining_ounces,
    purchases.total_cents - COALESCE(SUM(allocations.cost_basis_cents), 0)
        AS remaining_cost_basis_cents
FROM silver_transactions AS purchases
LEFT JOIN silver_sale_allocations AS allocations
    ON allocations.purchase_id = purchases.id
WHERE purchases.transaction_type = 'purchase'
GROUP BY purchases.id;

CREATE VIEW IF NOT EXISTS silver_position AS
SELECT
    COALESCE(SUM(remaining_ounces), 0) AS troy_ounces,
    COALESCE(SUM(remaining_cost_basis_cents), 0) AS cost_basis_cents,
    COALESCE((
        SELECT SUM(sales.total_cents - allocations.cost_basis_cents)
        FROM silver_transactions AS sales
        JOIN (
            SELECT sale_id, SUM(cost_basis_cents) AS cost_basis_cents
            FROM silver_sale_allocations
            GROUP BY sale_id
        ) AS allocations ON allocations.sale_id = sales.id
        WHERE sales.transaction_type = 'sale'
    ), 0) AS realized_pl_cents
FROM silver_lots;
