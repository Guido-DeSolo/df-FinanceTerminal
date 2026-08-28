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

