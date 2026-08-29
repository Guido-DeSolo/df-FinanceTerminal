#!/usr/bin/env python3
"""Enumerate personal assets and write their current values to SQLite."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from xml.etree import ElementTree
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
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


@dataclass(frozen=True)
class SilverPurchaseResult:
    transaction_id: int
    troy_ounces: Decimal
    total_paid: Decimal
    transacted_at: str


@dataclass(frozen=True)
class SilverSaleResult:
    transaction_id: int
    troy_ounces: Decimal
    proceeds: Decimal
    cost_basis: Decimal
    realized_pl: Decimal
    transacted_at: str


@dataclass(frozen=True)
class SilverEvaluationResult:
    troy_ounces: Decimal
    spot_price: Decimal
    holding_value: Decimal
    cost_basis: Decimal
    unrealized_pl: Decimal
    realized_pl: Decimal
    updated_at: str


@dataclass(frozen=True)
class BitcoinEvaluationResult:
    quantity: Decimal
    current_price: Decimal
    market_value: Decimal
    cost_basis: Decimal
    unrealized_pl: Decimal
    updated_at: str


@dataclass(frozen=True)
class RealEstateImportResult:
    items: int
    value: Decimal
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

    @staticmethod
    def _alpaca_positions(key_id: str, secret_key: str, live: bool) -> list[dict]:
        base_url = (
            "https://api.alpaca.markets"
            if live
            else "https://paper-api.alpaca.markets"
        )
        request = Request(
            f"{base_url}/v2/positions",
            headers={
                "APCA-API-KEY-ID": key_id,
                "APCA-API-SECRET-KEY": secret_key,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                result = json.load(response)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(
                f"Alpaca returned HTTP {exc.code}: {detail}"
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Alpaca positions request failed: {exc}") from exc
        if not isinstance(result, list):
            raise RuntimeError("Alpaca returned an invalid positions response")
        return [position for position in result if isinstance(position, dict)]

    @staticmethod
    def _alpaca_decimal(position: dict, field: str) -> Decimal:
        try:
            value = Decimal(str(position[field]))
        except (KeyError, InvalidOperation) as exc:
            raise RuntimeError(
                f"Alpaca BTC position has an invalid {field}"
            ) from exc
        if not value.is_finite():
            raise RuntimeError(f"Alpaca BTC position has an invalid {field}")
        return value

    def bitcoinEvaluation(
        self,
        key_id: str | None = None,
        secret_key: str | None = None,
        live: bool | None = None,
    ) -> BitcoinEvaluationResult:
        """Value the Alpaca BTC position and update ``wealth.liquid``."""
        alpaca_key = key_id or os.environ.get("APCA_API_KEY_ID")
        alpaca_secret = secret_key or os.environ.get("APCA_API_SECRET_KEY")
        if not alpaca_key or not alpaca_secret:
            raise ValueError(
                "set APCA_API_KEY_ID and APCA_API_SECRET_KEY before evaluating bitcoin"
            )
        use_live = (
            os.environ.get("ALPACA_LIVE", "").casefold() in {"1", "true", "yes"}
            if live is None
            else live
        )
        position = next(
            (
                item
                for item in self._alpaca_positions(
                    alpaca_key, alpaca_secret, use_live
                )
                if str(item.get("symbol", "")).replace("/", "").upper()
                == "BTCUSD"
            ),
            None,
        )
        if position is None:
            quantity = Decimal("0")
            current_price = Decimal("0")
            market_value = Decimal("0")
            cost_basis = Decimal("0")
            unrealized_pl = Decimal("0")
        else:
            quantity = self._alpaca_decimal(position, "qty")
            current_price = self._alpaca_decimal(position, "current_price")
            market_value = self._alpaca_decimal(position, "market_value")
            cost_basis = self._alpaca_decimal(position, "cost_basis")
            unrealized_pl = self._alpaca_decimal(position, "unrealized_pl")
        updated_at = self._timestamp(None)
        with self.connect() as connection:
            self.initialize(connection)
            connection.execute(
                """
                UPDATE wealth
                SET liquid = ?, liquid_updated_at = ?
                WHERE id = 1
                """,
                (float(market_value), updated_at),
            )
        return BitcoinEvaluationResult(
            quantity=quantity,
            current_price=current_price,
            market_value=market_value,
            cost_basis=cost_basis,
            unrealized_pl=unrealized_pl,
            updated_at=updated_at,
        )

    @staticmethod
    def _spreadsheet_price(value: str, row_number: int) -> int:
        cleaned = value.strip().replace("$", "").replace(",", "")
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = f"-{cleaned[1:-1]}"
        try:
            price = Decimal(cleaned)
        except InvalidOperation as exc:
            raise ValueError(
                f"spreadsheet row {row_number}: invalid price {value!r}"
            ) from exc
        cents = price * 100
        if not price.is_finite() or price < 0 or cents != cents.to_integral_value():
            raise ValueError(
                f"spreadsheet row {row_number}: price must be a nonnegative cent value"
            )
        return int(cents)

    @staticmethod
    def _csv_rows(path: Path) -> list[tuple[int, str, str]]:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
            except csv.Error:
                dialect = csv.excel
            return [
                (
                    row_number,
                    row[0].strip() if row else "",
                    row[1].strip() if len(row) > 1 else "",
                )
                for row_number, row in enumerate(csv.reader(handle, dialect), start=1)
            ]

    @staticmethod
    def _xlsx_rows(path: Path) -> list[tuple[int, str, str]]:
        main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
        with zipfile.ZipFile(path) as workbook:
            try:
                shared_root = ElementTree.fromstring(
                    workbook.read("xl/sharedStrings.xml")
                )
                shared_strings = [
                    "".join(node.text or "" for node in item.iter(f"{{{main_ns}}}t"))
                    for item in shared_root.findall(f"{{{main_ns}}}si")
                ]
            except KeyError:
                shared_strings = []

            workbook_root = ElementTree.fromstring(workbook.read("xl/workbook.xml"))
            sheet = workbook_root.find(f".//{{{main_ns}}}sheet")
            if sheet is None:
                raise ValueError("spreadsheet contains no worksheets")
            relationship_id = sheet.get(f"{{{rel_ns}}}id")
            relationships = ElementTree.fromstring(
                workbook.read("xl/_rels/workbook.xml.rels")
            )
            relationship = next(
                (
                    item
                    for item in relationships.findall(
                        f"{{{package_rel_ns}}}Relationship"
                    )
                    if item.get("Id") == relationship_id
                ),
                None,
            )
            if relationship is None or not relationship.get("Target"):
                raise ValueError("spreadsheet first worksheet cannot be resolved")
            target = relationship.get("Target", "").lstrip("/")
            worksheet_path = target if target.startswith("xl/") else f"xl/{target}"
            worksheet = ElementTree.fromstring(workbook.read(worksheet_path))

        rows: list[tuple[int, str, str]] = []
        for row in worksheet.findall(f".//{{{main_ns}}}row"):
            row_number = int(row.get("r", len(rows) + 1))
            values = {"A": "", "B": ""}
            for cell in row.findall(f"{{{main_ns}}}c"):
                column = "".join(character for character in cell.get("r", "") if character.isalpha())
                if column not in values:
                    continue
                cell_type = cell.get("t")
                if cell_type == "inlineStr":
                    value = "".join(
                        node.text or "" for node in cell.iter(f"{{{main_ns}}}t")
                    )
                else:
                    value_node = cell.find(f"{{{main_ns}}}v")
                    value = value_node.text if value_node is not None else ""
                    if cell_type == "s" and value:
                        try:
                            value = shared_strings[int(value)]
                        except (IndexError, ValueError) as exc:
                            raise ValueError(
                                f"spreadsheet row {row_number}: invalid shared text"
                            ) from exc
                values[column] = value.strip()
            rows.append((row_number, values["A"], values["B"]))
        return rows

    @classmethod
    def _real_estate_rows(cls, spreadsheet: Path | str) -> list[tuple[int, str, int]]:
        path = Path(spreadsheet).expanduser()
        if path.suffix.casefold() == ".xlsx":
            raw_rows = cls._xlsx_rows(path)
        elif path.suffix.casefold() in {".csv", ".tsv", ".txt"}:
            raw_rows = cls._csv_rows(path)
        else:
            raise ValueError("spreadsheet must be an .xlsx, .csv, .tsv, or .txt file")

        items: list[tuple[int, str, int]] = []
        for row_number, device, price_text in raw_rows:
            if not device and not price_text:
                continue
            if not items and device.casefold() in {"device", "item", "equipment"}:
                if price_text.casefold() in {"price", "value", "cost", "purchase price"}:
                    continue
            if not device:
                raise ValueError(f"spreadsheet row {row_number}: device is empty")
            if not price_text:
                raise ValueError(f"spreadsheet row {row_number}: price is empty")
            items.append(
                (row_number, device, cls._spreadsheet_price(price_text, row_number))
            )
        if not items:
            raise ValueError("spreadsheet contains no equipment rows")
        return items

    def realEstateEvaluation(
        self, spreadsheet: Path | str
    ) -> RealEstateImportResult:
        """Replace the lab inventory from a two-column spreadsheet."""
        path = Path(spreadsheet).expanduser()
        items = self._real_estate_rows(path)
        updated_at = self._timestamp(None)
        total_cents = sum(price_cents for _, _, price_cents in items)
        with self.connect() as connection:
            self.initialize(connection)
            connection.execute("DELETE FROM realEstate")
            connection.executemany(
                """
                INSERT INTO realEstate (
                    device, purchase_price_cents, source_file, source_row, imported_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (device, price_cents, path.name, row_number, updated_at)
                    for row_number, device, price_cents in items
                ),
            )
            connection.execute(
                """
                UPDATE wealth
                SET real_estate = ?, real_estate_updated_at = ?
                WHERE id = 1
                """,
                (float(Decimal(total_cents) / 100), updated_at),
            )
        return RealEstateImportResult(
            items=len(items),
            value=Decimal(total_cents) / 100,
            updated_at=updated_at,
        )

    @staticmethod
    def _positive_decimal(value: Decimal | str | float, field: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"{field} must be a number") from exc
        if not result.is_finite() or result <= 0:
            raise ValueError(f"{field} must be greater than zero")
        return result

    @staticmethod
    def _money_cents(value: Decimal | str | float, field: str) -> int:
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"{field} must be a dollar amount") from exc
        cents = amount * 100
        if not amount.is_finite() or amount < 0 or cents != cents.to_integral_value():
            raise ValueError(f"{field} must be a nonnegative whole-cent amount")
        return int(cents)

    @staticmethod
    def _timestamp(value: str | None) -> str:
        if value is None:
            return datetime.now(UTC).replace(microsecond=0).isoformat().replace(
                "+00:00", "Z"
            )
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be ISO-8601") from exc
        return value

    def silverPurchase(
        self,
        troy_ounces: Decimal | str | float,
        total_paid: Decimal | str | float,
        transacted_at: str | None = None,
    ) -> SilverPurchaseResult:
        """Record one immutable silver purchase lot."""
        ounces = self._positive_decimal(troy_ounces, "troy ounces")
        total_cents = self._money_cents(total_paid, "total paid")
        timestamp = self._timestamp(transacted_at)
        with self.connect() as connection:
            self.initialize(connection)
            cursor = connection.execute(
                """
                INSERT INTO silver_transactions (
                    transaction_type, troy_ounces, total_cents, transacted_at
                ) VALUES ('purchase', ?, ?, ?)
                """,
                (float(ounces), total_cents, timestamp),
            )
            transaction_id = int(cursor.lastrowid)
        return SilverPurchaseResult(
            transaction_id, ounces, Decimal(total_cents) / 100, timestamp
        )

    def silverSale(
        self,
        troy_ounces: Decimal | str | float,
        total_proceeds: Decimal | str | float,
        transacted_at: str | None = None,
    ) -> SilverSaleResult:
        """Record a silver sale and consume purchase lots using FIFO."""
        ounces = self._positive_decimal(troy_ounces, "troy ounces")
        proceeds_cents = self._money_cents(total_proceeds, "total proceeds")
        timestamp = self._timestamp(transacted_at)
        with self.connect() as connection:
            self.initialize(connection)
            lots = connection.execute(
                """
                SELECT purchase_id, remaining_ounces, remaining_cost_basis_cents
                FROM silver_lots
                WHERE remaining_ounces > 0
                ORDER BY transacted_at, purchase_id
                """
            ).fetchall()
            available = sum(
                (Decimal(str(row["remaining_ounces"])) for row in lots),
                Decimal("0"),
            )
            if ounces > available:
                raise ValueError(
                    f"cannot sell {ounces} troy oz; only {available} available"
                )

            cursor = connection.execute(
                """
                INSERT INTO silver_transactions (
                    transaction_type, troy_ounces, total_cents, transacted_at
                ) VALUES ('sale', ?, ?, ?)
                """,
                (float(ounces), proceeds_cents, timestamp),
            )
            sale_id = int(cursor.lastrowid)
            remaining = ounces
            allocated_cost_cents = 0
            for lot in lots:
                lot_ounces = Decimal(str(lot["remaining_ounces"]))
                if lot_ounces <= 0:
                    continue
                consumed = min(remaining, lot_ounces)
                lot_cost_cents = int(lot["remaining_cost_basis_cents"])
                if consumed == lot_ounces:
                    cost_cents = lot_cost_cents
                else:
                    cost_cents = int(
                        (Decimal(lot_cost_cents) * consumed / lot_ounces).quantize(
                            Decimal("1"), rounding=ROUND_HALF_UP
                        )
                    )
                connection.execute(
                    """
                    INSERT INTO silver_sale_allocations (
                        sale_id, purchase_id, troy_ounces, cost_basis_cents
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (sale_id, int(lot["purchase_id"]), float(consumed), cost_cents),
                )
                allocated_cost_cents += cost_cents
                remaining -= consumed
                if remaining == 0:
                    break
            connection.execute(
                """
                INSERT INTO cash_removed (
                    source_asset, source_transaction_id, amount_cents, removed_at
                ) VALUES ('silver', ?, ?, ?)
                """,
                (sale_id, proceeds_cents, timestamp),
            )
        return SilverSaleResult(
            transaction_id=sale_id,
            troy_ounces=ounces,
            proceeds=Decimal(proceeds_cents) / 100,
            cost_basis=Decimal(allocated_cost_cents) / 100,
            realized_pl=Decimal(proceeds_cents - allocated_cost_cents) / 100,
            transacted_at=timestamp,
        )

    @staticmethod
    def _silver_spot_price(api_key: str) -> Decimal:
        query = urlencode(
            {"api_key": api_key, "base": "USD", "currencies": "XAG"}
        )
        request = Request(
            f"https://api.metalpriceapi.com/v1/latest?{query}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                result = json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"silver price request failed: {exc}") from exc
        if result.get("success") is False:
            raise RuntimeError(f"silver price provider rejected the request: {result}")
        rates = result.get("rates") or {}
        try:
            if rates.get("USDXAG") not in (None, ""):
                spot = Decimal(str(rates["USDXAG"]))
            else:
                ounces_per_dollar = Decimal(str(rates["XAG"]))
                spot = Decimal("1") / ounces_per_dollar
        except (InvalidOperation, KeyError, ZeroDivisionError) as exc:
            raise RuntimeError("silver price provider returned an invalid rate") from exc
        if not spot.is_finite() or spot <= 0:
            raise RuntimeError("silver price provider returned an invalid rate")
        return spot

    def silverEvaluation(self, api_key: str | None = None) -> SilverEvaluationResult:
        """Value remaining silver, calculate P/L, and update wealth.futures."""
        key = api_key or os.environ.get("METALPRICE_API_KEY")
        if not key:
            raise ValueError("set METALPRICE_API_KEY before evaluating silver")
        spot = self._silver_spot_price(key)
        with self.connect() as connection:
            self.initialize(connection)
            position = connection.execute("SELECT * FROM silver_position").fetchone()
        ounces = Decimal(str(position["troy_ounces"]))
        cost_basis = Decimal(int(position["cost_basis_cents"])) / 100
        realized_pl = Decimal(int(position["realized_pl_cents"])) / 100
        holding_value = (ounces * spot).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        unrealized_pl = holding_value - cost_basis
        updated_at = self._timestamp(None)
        with self.connect() as connection:
            self.initialize(connection)
            connection.execute(
                """
                UPDATE wealth
                SET futures = ?, futures_updated_at = ?
                WHERE id = 1
                """,
                (float(holding_value), updated_at),
            )
        return SilverEvaluationResult(
            troy_ounces=ounces,
            spot_price=spot,
            holding_value=holding_value,
            cost_basis=cost_basis,
            unrealized_pl=unrealized_pl,
            realized_pl=realized_pl,
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
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("mtgEvaluation")
    commands.add_parser("bitcoinEvaluation")
    real_estate = commands.add_parser("realEstateEvaluation")
    real_estate.add_argument(
        "spreadsheet",
        type=Path,
        help=".xlsx, .csv, .tsv, or .txt inventory with device and price columns",
    )
    purchase = commands.add_parser("silverPurchase")
    purchase.add_argument("troy_ounces")
    purchase.add_argument("total_paid")
    purchase.add_argument("--at", dest="transacted_at")
    sale = commands.add_parser("silverSale")
    sale.add_argument("troy_ounces")
    sale.add_argument("total_proceeds")
    sale.add_argument("--at", dest="transacted_at")
    commands.add_parser("silverEvaluation")
    args = parser.parse_args(argv)
    enumeration = AssetEnumeration(args.database)
    try:
        if args.operation == "mtgEvaluation":
            result = enumeration.mtgEvaluation()
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
        elif args.operation == "bitcoinEvaluation":
            result = enumeration.bitcoinEvaluation()
            print(f"Bitcoin held: {result.quantity} BTC")
            print(f"Current price: ${result.current_price:,.2f} per BTC")
            print(f"Liquid value: ${result.market_value:,.2f}")
            print(f"Cost basis: ${result.cost_basis:,.2f}")
            print(f"Unrealized P/L: ${result.unrealized_pl:+,.2f}")
            print("Updated wealth.liquid")
        elif args.operation == "realEstateEvaluation":
            result = enumeration.realEstateEvaluation(args.spreadsheet)
            print(f"Imported {result.items} lab equipment items")
            print(f"Real estate value: ${result.value:,.2f}")
            print("Updated wealth.real_estate")
        elif args.operation == "silverPurchase":
            result = enumeration.silverPurchase(
                args.troy_ounces, args.total_paid, args.transacted_at
            )
            print(
                f"Recorded purchase #{result.transaction_id}: "
                f"{result.troy_ounces} troy oz for ${result.total_paid:,.2f}"
            )
        elif args.operation == "silverSale":
            result = enumeration.silverSale(
                args.troy_ounces, args.total_proceeds, args.transacted_at
            )
            print(
                f"Recorded sale #{result.transaction_id}: "
                f"{result.troy_ounces} troy oz for ${result.proceeds:,.2f}"
            )
            print(
                f"Cost basis: ${result.cost_basis:,.2f}; "
                f"realized P/L: ${result.realized_pl:+,.2f}"
            )
            print(f"Recorded ${result.proceeds:,.2f} as cash removed")
        else:
            result = enumeration.silverEvaluation()
            print(f"Silver held: {result.troy_ounces} troy oz")
            print(f"Spot price: ${result.spot_price:,.2f} per troy oz")
            print(f"Futures value: ${result.holding_value:,.2f}")
            print(f"Remaining cost basis: ${result.cost_basis:,.2f}")
            print(f"Unrealized P/L: ${result.unrealized_pl:+,.2f}")
            print(f"Realized P/L: ${result.realized_pl:+,.2f}")
            print("Updated wealth.futures")
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        parser.exit(1, f"assetEnumeration: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
