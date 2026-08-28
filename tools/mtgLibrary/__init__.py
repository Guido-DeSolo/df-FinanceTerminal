#!/usr/bin/env python3
"""Manage a ManaBox collection and reserve its cards for deck files."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema.sql"
EXPECTED_COLUMNS = (
    "Binder Name",
    "Binder Type",
    "Name",
    "Set code",
    "Set name",
    "Collector number",
    "Foil",
    "Rarity",
    "Quantity",
    "ManaBox ID",
    "Scryfall ID",
    "Purchase price",
    "Misprint",
    "Altered",
    "Condition",
    "Language",
    "Purchase price currency",
    "Added",
)


@dataclass(frozen=True)
class ImportResult:
    rows: int
    exact_matches: int
    name_matches: int


@dataclass(frozen=True)
class DeckResult:
    name: str
    cards: int
    library_rows: int


class DeckShortageError(ValueError):
    """Raised when a deck requests more cards than are currently available."""

    def __init__(self, shortages: list[str]):
        self.shortages = shortages
        super().__init__("cards unavailable:\n  " + "\n  ".join(shortages))


class MtgLibrary:
    """Import owned cards and reserve them for immutable deck files."""

    DECK_LINE = re.compile(
        r"^(?P<quantity>[1-9][0-9]*)\s+"
        r"(?P<name>.+)\s+"
        r"\((?P<set_code>[^()]+)\)\s+"
        r"(?P<collector_number>\S+)\s*$"
    )

    def __init__(self, database: Path | str):
        self.database = Path(database).expanduser()

    def connect(self) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self, connection: sqlite3.Connection) -> None:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))

    @staticmethod
    def rows(csv_file: Path | str) -> Iterator[dict[str, str]]:
        path = Path(csv_file).expanduser()
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = tuple(reader.fieldnames or ())
            missing = [column for column in EXPECTED_COLUMNS if column not in columns]
            if missing:
                raise ValueError(f"missing ManaBox columns: {', '.join(missing)}")
            for line, row in enumerate(reader, start=2):
                cleaned = {key: (value or "").strip() for key, value in row.items()}
                cleaned["_line"] = str(line)
                yield cleaned

    @staticmethod
    def _boolean(value: str, field: str, line: str) -> int:
        lowered = value.casefold()
        if lowered == "true":
            return 1
        if lowered == "false":
            return 0
        raise ValueError(f"line {line}: {field} must be true or false")

    @staticmethod
    def _price_cents(value: str, line: str) -> int | None:
        if not value:
            return None
        try:
            price = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"line {line}: invalid purchase price {value!r}") from exc
        cents = price * 100
        if price < 0 or cents != cents.to_integral_value():
            raise ValueError(f"line {line}: purchase price must be a nonnegative cent value")
        return int(cents)

    @staticmethod
    def _card_id(connection: sqlite3.Connection, row: dict[str, str]) -> tuple[str, bool]:
        exact = connection.execute(
            "SELECT id FROM cards WHERE id = ?", (row["Scryfall ID"],)
        ).fetchone()
        if exact:
            return str(exact["id"]), True

        matches = connection.execute(
            """
            SELECT id
            FROM cards
            WHERE name = ? COLLATE NOCASE
            ORDER BY (set_code = ?) DESC, id
            LIMIT 2
            """,
            (row["Name"], row["Set code"]),
        ).fetchall()
        if not matches:
            raise ValueError(
                f"line {row['_line']}: card not found: {row['Name']} "
                f"({row['Set code']} #{row['Collector number']})"
            )
        return str(matches[0]["id"]), False

    def import_csv(self, csv_file: Path | str) -> ImportResult:
        imported = 0
        exact_matches = 0
        name_matches = 0
        with self.connect() as connection:
            self.initialize(connection)
            for row in self.rows(csv_file):
                card_id, exact = self._card_id(connection, row)
                try:
                    quantity = int(row["Quantity"])
                    manabox_id = int(row["ManaBox ID"])
                except ValueError as exc:
                    raise ValueError(
                        f"line {row['_line']}: quantity and ManaBox ID must be integers"
                    ) from exc
                if quantity < 0:
                    raise ValueError(f"line {row['_line']}: quantity cannot be negative")
                values = (
                    manabox_id,
                    card_id,
                    row["Scryfall ID"],
                    row["Binder Name"],
                    row["Binder Type"],
                    row["Set code"],
                    row["Set name"],
                    row["Collector number"],
                    row["Foil"],
                    row["Rarity"],
                    quantity,
                    self._price_cents(row["Purchase price"], row["_line"]),
                    row["Purchase price currency"] or None,
                    self._boolean(row["Misprint"], "Misprint", row["_line"]),
                    self._boolean(row["Altered"], "Altered", row["_line"]),
                    row["Condition"],
                    row["Language"],
                    row["Added"],
                )
                connection.execute(
                    """
                    INSERT INTO library_items (
                        manabox_id, card_id, scryfall_id, binder_name, binder_type,
                        set_code, set_name, collector_number, foil, rarity, quantity,
                        purchase_price_cents, purchase_price_currency, misprint,
                        altered, condition, language, added
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(manabox_id) DO UPDATE SET
                        card_id = excluded.card_id,
                        scryfall_id = excluded.scryfall_id,
                        binder_name = excluded.binder_name,
                        binder_type = excluded.binder_type,
                        set_code = excluded.set_code,
                        set_name = excluded.set_name,
                        collector_number = excluded.collector_number,
                        foil = excluded.foil,
                        rarity = excluded.rarity,
                        quantity = excluded.quantity,
                        purchase_price_cents = excluded.purchase_price_cents,
                        purchase_price_currency = excluded.purchase_price_currency,
                        misprint = excluded.misprint,
                        altered = excluded.altered,
                        condition = excluded.condition,
                        language = excluded.language,
                        added = excluded.added
                    """,
                    values,
                )
                imported += 1
                exact_matches += int(exact)
                name_matches += int(not exact)
        return ImportResult(imported, exact_matches, name_matches)

    @classmethod
    def deck_entries(
        cls, content: str
    ) -> Counter[tuple[str, str, str]]:
        """Parse all deck lines; section comments have no inventory meaning."""
        entries: Counter[tuple[str, str, str]] = Counter()
        for line_number, original in enumerate(content.splitlines(), start=1):
            line = original.strip()
            if not line or line.startswith("//"):
                continue
            match = cls.DECK_LINE.fullmatch(line)
            if not match:
                raise ValueError(f"deck line {line_number}: unrecognized entry {line!r}")
            entries[
                (
                    match["name"],
                    match["set_code"].upper(),
                    match["collector_number"],
                )
            ] += int(match["quantity"])
        if not entries:
            raise ValueError("deck contains no card entries")
        return entries

    @staticmethod
    def _available_printings(
        connection: sqlite3.Connection, set_code: str, collector_number: str
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT manabox_id, quantity, reserved_quantity, available_quantity
            FROM available_library
            WHERE set_code = ? COLLATE NOCASE
              AND collector_number = ? COLLATE NOCASE
            ORDER BY (foil = 'normal') DESC, manabox_id
            """,
            (set_code, collector_number),
        ).fetchall()

    def reserve_deck(self, deck_file: Path | str) -> DeckResult:
        """Atomically store a deck file and reserve every card it enumerates."""
        path = Path(deck_file).expanduser()
        content = path.read_text(encoding="utf-8-sig")
        entries = self.deck_entries(content)
        name = path.stem
        if not name:
            raise ValueError("deck filename must have a name")

        allocations: list[tuple[int, int]] = []
        shortages: list[str] = []
        with self.connect() as connection:
            self.initialize(connection)
            if connection.execute(
                "SELECT 1 FROM decks WHERE name = ?", (name,)
            ).fetchone():
                raise ValueError(f"deck already exists: {name}")

            for (card_name, set_code, collector_number), requested in entries.items():
                candidates = self._available_printings(
                    connection, set_code, collector_number
                )
                available = sum(int(row["available_quantity"]) for row in candidates)
                if available < requested:
                    shortages.append(
                        f"{card_name} ({set_code}) {collector_number}: "
                        f"needs {requested}, available {available}"
                    )
                    continue
                remaining = requested
                for row in candidates:
                    reserved = min(remaining, int(row["available_quantity"]))
                    if reserved:
                        allocations.append((int(row["manabox_id"]), reserved))
                        remaining -= reserved
                    if remaining == 0:
                        break

            if shortages:
                raise DeckShortageError(shortages)

            cursor = connection.execute(
                "INSERT INTO decks (name, filename, content) VALUES (?, ?, ?)",
                (name, path.name, content),
            )
            deck_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO deck_reservations (deck_id, library_item_id, quantity)
                VALUES (?, ?, ?)
                """,
                ((deck_id, library_item_id, quantity)
                 for library_item_id, quantity in allocations),
            )
        return DeckResult(name, sum(entries.values()), len(allocations))

    def remove_deck(self, name: str) -> int:
        """Delete one stored deck; cascading reservations become available."""
        with self.connect() as connection:
            self.initialize(connection)
            cursor = connection.execute("DELETE FROM decks WHERE name = ?", (name,))
            if cursor.rowcount == 0:
                raise ValueError(f"deck not found: {name}")
            return cursor.rowcount

    def decks(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            self.initialize(connection)
            return connection.execute(
                """
                SELECT decks.name, decks.filename, decks.added_at,
                       COALESCE(SUM(deck_reservations.quantity), 0) AS cards
                FROM decks
                LEFT JOIN deck_reservations
                    ON deck_reservations.deck_id = decks.id
                GROUP BY decks.id
                ORDER BY decks.name
                """
            ).fetchall()


def default_database() -> Path:
    configured = os.environ.get("MTG_DB_FILE")
    if configured:
        return Path(configured).expanduser()
    server_database = Path("/opt/mtg/mtg.db")
    if server_database.exists():
        return server_database
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "df-fintechterm" / "mtg.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mtgLibrary",
        description="Import a ManaBox library and reserve cards for decks.",
    )
    parser.add_argument("--database", "-d", type=Path, default=default_database())
    commands = parser.add_subparsers(dest="command", required=True)
    library_parser = commands.add_parser("import-library")
    library_parser.add_argument("csv", type=Path, help="ManaBox collection export")
    deck_parser = commands.add_parser("reserve-deck")
    deck_parser.add_argument("deck", type=Path, help="ManaBox deck text export")
    remove_parser = commands.add_parser("remove-deck")
    remove_parser.add_argument("name", help="deck name (filename without .txt)")
    commands.add_parser("decks")
    args = parser.parse_args(argv)
    library = MtgLibrary(args.database)
    try:
        if args.command == "import-library":
            result = library.import_csv(args.csv)
            print(f"Imported {result.rows} library rows into {args.database}")
            print(
                f"Matched {result.exact_matches} exact Scryfall IDs; "
                f"{result.name_matches} by card name"
            )
        elif args.command == "reserve-deck":
            result = library.reserve_deck(args.deck)
            print(
                f"Reserved {result.cards} cards for {result.name} "
                f"across {result.library_rows} library rows"
            )
        elif args.command == "remove-deck":
            library.remove_deck(args.name)
            print(f"Removed {args.name}; its cards are available again")
        else:
            for deck in library.decks():
                print(f"{deck['name']}\t{deck['cards']}\t{deck['filename']}")
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"mtgLibrary: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
