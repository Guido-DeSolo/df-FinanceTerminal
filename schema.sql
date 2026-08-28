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
