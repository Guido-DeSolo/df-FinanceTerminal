#!/usr/bin/env python3
"""Import a ManaBox collection CSV into the personal MTG SQLite database."""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
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


class MtgLibrary:
    """A single-purpose ManaBox-to-SQLite library importer."""

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
        description="Import a complete ManaBox collection CSV into SQLite.",
    )
    parser.add_argument("csv", type=Path, help="ManaBox collection export")
    parser.add_argument("--database", "-d", type=Path, default=default_database())
    args = parser.parse_args(argv)
    try:
        result = MtgLibrary(args.database).import_csv(args.csv)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"mtgLibrary: {exc}\n")
    print(f"Imported {result.rows} library rows into {args.database}")
    print(
        f"Matched {result.exact_matches} exact Scryfall IDs; "
        f"{result.name_matches} by card name"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
