#!/usr/bin/env python3
"""Enumerate personal assets and write their current values to SQLite."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "schema.sql"
SCRYFALL_COLLECTION_URL = "https://api.scryfall.com/cards/collection"
SCRYFALL_BATCH_SIZE = 75
SCRYFALL_REQUEST_INTERVAL = 0.12
USER_AGENT = "df-FinanceTerminal assetEnumeration/1.0"


@dataclass(frozen=True)
class MtgEvaluationResult:
    value: Decimal
    priced_rows: int
    priced_cards: int
    unpriced_rows: int
    unpriced_cards: int
    updated_at: str


class AssetEnumeration:
    """Evaluate one asset category at a time."""

    def __init__(self, database: Path | str):
        self.database = Path(database).expanduser()

    def connect(self) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))

    @staticmethod
    def _request_batch(card_ids: list[str]) -> list[dict]:
        payload = json.dumps(
            {"identifiers": [{"id": card_id} for card_id in card_ids]}
        ).encode("utf-8")
        request = Request(
            SCRYFALL_COLLECTION_URL,
            data=payload,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json;q=0.9,*/*;q=0.8",
                "Content-Type": "application/json",
            },
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urlopen(request, timeout=30) as response:
                    result = json.load(response)
                return list(result.get("data", []))
            except HTTPError as exc:
                last_error = exc
                if exc.code != 429 and exc.code < 500:
                    detail = exc.read().decode("utf-8", "replace")
                    raise RuntimeError(
                        f"Scryfall returned HTTP {exc.code}: {detail}"
                    ) from exc
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 2 ** attempt
            except URLError as exc:
                last_error = exc
                delay = 2 ** attempt
            if attempt < 2:
                time.sleep(min(delay, 30))
        raise RuntimeError(f"Scryfall request failed after retries: {last_error}")

    @classmethod
    def _prices(cls, card_ids: list[str]) -> dict[str, dict]:
        prices: dict[str, dict] = {}
        for offset in range(0, len(card_ids), SCRYFALL_BATCH_SIZE):
            batch = card_ids[offset:offset + SCRYFALL_BATCH_SIZE]
            for card in cls._request_batch(batch):
                card_id = card.get("id")
                if card_id:
                    prices[str(card_id)] = dict(card.get("prices") or {})
            if offset + SCRYFALL_BATCH_SIZE < len(card_ids):
                time.sleep(SCRYFALL_REQUEST_INTERVAL)
        return prices

    @staticmethod
    def _market_price(prices: dict, finish: str) -> Decimal | None:
        field = {
            "normal": "usd",
            "nonfoil": "usd",
            "foil": "usd_foil",
            "etched": "usd_etched",
        }.get(finish.casefold())
        raw = prices.get(field) if field else None
        if raw in (None, ""):
            return None
        try:
            value = Decimal(str(raw))
        except InvalidOperation:
            return None
        return value if value.is_finite() and value >= 0 else None

    def mtgEvaluation(self) -> MtgEvaluationResult:
        """Value every owned card and atomically update ``wealth.bonds``."""
        with self.connect() as connection:
            self.initialize(connection)
            rows = connection.execute(
                """
                SELECT scryfall_id, foil, quantity
                FROM library_items
                ORDER BY manabox_id
                """
            ).fetchall()

        card_ids = list(dict.fromkeys(str(row["scryfall_id"]) for row in rows))
        prices_by_id = self._prices(card_ids)
        value = Decimal("0")
        priced_rows = 0
        priced_cards = 0
        unpriced_rows = 0
        unpriced_cards = 0
        for row in rows:
            quantity = int(row["quantity"])
            price = self._market_price(
                prices_by_id.get(str(row["scryfall_id"]), {}),
                str(row["foil"]),
            )
            if price is None:
                unpriced_rows += 1
                unpriced_cards += quantity
                continue
            value += price * quantity
            priced_rows += 1
            priced_cards += quantity

        updated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
        with self.connect() as connection:
            self.initialize(connection)
            connection.execute(
                """
                UPDATE wealth
                SET bonds = ?, bonds_updated_at = ?
                WHERE id = 1
                """,
                (float(value), updated_at),
            )
        return MtgEvaluationResult(
            value=value,
            priced_rows=priced_rows,
            priced_cards=priced_cards,
            unpriced_rows=unpriced_rows,
            unpriced_cards=unpriced_cards,
            updated_at=updated_at,
        )


def default_database() -> Path:
    configured = os.environ.get("MTG_DB_FILE")
    if configured:
        return Path(configured).expanduser()
    server_database = Path("/opt/mtg/mtg.db")
    if server_database.exists():
        return server_database
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "df-financeterminal" / "mtg.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="assetEnumeration",
        description="Evaluate personal assets and update the wealth snapshot.",
    )
    parser.add_argument("--database", "-d", type=Path, default=default_database())
    parser.add_argument("operation", choices=("mtgEvaluation",))
    args = parser.parse_args(argv)
    try:
        result = AssetEnumeration(args.database).mtgEvaluation()
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        parser.exit(1, f"assetEnumeration: {exc}\n")
    print(f"Bonds value: ${result.value:,.2f}")
    print(
        f"Priced: {result.priced_rows} library rows / "
        f"{result.priced_cards} cards"
    )
    print(
        f"Unpriced: {result.unpriced_rows} library rows / "
        f"{result.unpriced_cards} cards"
    )
    print("Updated wealth.bonds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
