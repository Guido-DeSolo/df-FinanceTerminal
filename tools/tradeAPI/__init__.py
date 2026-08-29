#!/usr/bin/env python3
"""Submit guarded Alpaca market orders from the command line."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
USER_AGENT = "df-FinanceTerminal tradeAPI/1.0"
ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "schema.sql"


def default_database() -> Path:
    configured = os.environ.get("FINANCE_DB_FILE") or os.environ.get("MTG_DB_FILE")
    if configured:
        return Path(configured).expanduser()
    server_database = Path("/opt/mtg/mtg.db")
    if server_database.exists():
        return server_database
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "df-financeterminal" / "mtg.db"


def _positive(value: str | Decimal, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return result


class TradeAPI:
    def __init__(
        self,
        key_id: str,
        secret_key: str,
        live: bool = False,
        database: Path | str | None = None,
    ):
        if not key_id or not secret_key:
            raise ValueError("set APCA_API_KEY_ID and APCA_API_SECRET_KEY")
        self.base_url = LIVE_URL if live else PAPER_URL
        self.live = live
        self.database = Path(database).expanduser() if database else default_database()
        self.headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    @classmethod
    def from_environment(
        cls, live: bool = False, database: Path | str | None = None
    ) -> "TradeAPI":
        return cls(
            os.environ.get("APCA_API_KEY_ID", ""),
            os.environ.get("APCA_API_SECRET_KEY", ""),
            live,
            database,
        )

    def _record_order(self, order: dict, request_payload: dict) -> None:
        order_id = str(order.get("id") or "").strip()
        if not order_id:
            raise RuntimeError("Alpaca accepted an order without returning an order ID")
        quantity = request_payload.get("qty")
        notional = request_payload.get("notional")
        notional_cents = (
            int((Decimal(str(notional)) * 100).quantize(Decimal("1")))
            if notional is not None
            else None
        )
        filled_quantity = order.get("filled_qty")
        filled_average_price = order.get("filled_avg_price")
        asset_class = str(order.get("asset_class", "")).lower()
        category = (
            "liquid"
            if asset_class == "crypto" or "/" in request_payload["symbol"]
            else "stocks"
        )
        transacted_at = str(
            order.get("submitted_at")
            or order.get("created_at")
            or order.get("updated_at")
            or ""
        )
        if not transacted_at:
            raise RuntimeError(
                f"Alpaca order {order_id} was submitted but had no timestamp"
            )
        self.database.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(self.database) as connection:
                connection.executescript(SCHEMA.read_text(encoding="utf-8"))
                connection.execute(
                    """
                    INSERT INTO ledger (
                        source, environment, source_transaction_id,
                        category, asset, side,
                        requested_quantity, requested_notional_cents,
                        filled_quantity, filled_average_price, status,
                        transacted_at, raw_json
                    ) VALUES ('alpaca', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "live" if self.live else "paper",
                        order_id,
                        category,
                        request_payload["symbol"],
                        request_payload["side"],
                        float(quantity) if quantity is not None else None,
                        notional_cents,
                        (
                            float(filled_quantity)
                            if filled_quantity is not None
                            else None
                        ),
                        (
                            float(filled_average_price)
                            if filled_average_price is not None
                            else None
                        ),
                        str(order.get("status") or "unknown"),
                        transacted_at,
                        json.dumps(order, sort_keys=True, separators=(",", ":")),
                    ),
                )
        except (OSError, sqlite3.Error, InvalidOperation, ValueError) as exc:
            raise RuntimeError(
                f"Alpaca order {order_id} was submitted, but ledger recording failed: "
                f"{exc}"
            ) from exc

    def _submit_order(self, payload: dict) -> dict:
        result = self.request("POST", "/v2/orders", payload)
        if not isinstance(result, dict):
            raise RuntimeError("Alpaca returned an invalid order response")
        self._record_order(result, payload)
        return result

    def request(self, method: str, path: str, body: dict | None = None):
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        headers = dict(self.headers)
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}", data=encoded, headers=headers, method=method
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"Alpaca HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Alpaca request failed: {exc}") from exc

    def position(self, symbol: str) -> dict:
        positions = self.request("GET", "/v2/positions")
        if not isinstance(positions, list):
            raise RuntimeError("Alpaca returned an invalid positions response")
        normalized = symbol.replace("/", "").upper()
        position = next(
            (
                item for item in positions
                if isinstance(item, dict)
                and str(item.get("symbol", "")).replace("/", "").upper()
                == normalized
            ),
            None,
        )
        if position is None:
            raise ValueError(f"no open Alpaca position for {symbol}")
        return position

    def sell(
        self,
        symbol: str,
        *,
        quantity: str | Decimal | None = None,
        notional: str | Decimal | None = None,
    ) -> dict:
        """Submit one market sell by asset units or USD notional value."""
        if (quantity is None) == (notional is None):
            raise ValueError("provide exactly one of quantity or notional")
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol cannot be empty")
        position = self.position(symbol)
        held = _positive(position.get("qty", "0"), "held quantity")
        market_value = _positive(position.get("market_value", "0"), "market value")
        is_crypto = (
            "/" in symbol
            or str(position.get("asset_class", "")).lower() == "crypto"
        )
        payload = {
            "symbol": symbol,
            "side": "sell",
            "type": "market",
            "time_in_force": "gtc" if is_crypto else "day",
        }
        if quantity is not None:
            amount = _positive(quantity, "quantity")
            if amount > held:
                raise ValueError(f"cannot sell {amount}; only {held} held")
            payload["qty"] = format(amount, "f")
        else:
            amount = _positive(notional, "notional")
            if amount > market_value:
                raise ValueError(
                    f"cannot sell ${amount}; position is worth ${market_value}"
                )
            payload["notional"] = format(amount, "f")
        return self._submit_order(payload)

    def buy(
        self,
        symbol: str,
        *,
        quantity: str | Decimal | None = None,
        notional: str | Decimal | None = None,
    ) -> dict:
        """Submit one market buy by asset units or USD notional value."""
        if (quantity is None) == (notional is None):
            raise ValueError("provide exactly one of quantity or notional")
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol cannot be empty")
        payload = {
            "symbol": symbol,
            "side": "buy",
            "type": "market",
            "time_in_force": "gtc" if "/" in symbol else "day",
        }
        if quantity is not None:
            payload["qty"] = format(_positive(quantity, "quantity"), "f")
        else:
            payload["notional"] = format(_positive(notional, "notional"), "f")
        return self._submit_order(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradeAPI")
    parser.add_argument("--database", "-d", type=Path, default=default_database())
    parser.add_argument("symbol")
    amount = parser.add_mutually_exclusive_group(required=True)
    amount.add_argument("--quantity", "-q")
    amount.add_argument("--notional", "-n", help="USD amount")
    side = parser.add_mutually_exclusive_group(required=True)
    side.add_argument("--buy", action="store_true", help="buy the asset")
    side.add_argument("--sell", action="store_true", help="sell the asset")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--yes", action="store_true", help="confirm a paper order")
    args = parser.parse_args(argv)
    description = (
        f"{args.quantity} units" if args.quantity else f"${args.notional} notional"
    )
    side_name = "buy" if args.buy else "sell"
    if args.live:
        confirmation = input(
            f"LIVE market {side_name}: {description} of {args.symbol.upper()}. "
            "Type LIVE: "
        )
        if confirmation != "LIVE":
            parser.exit(1, "tradeAPI: live order canceled\n")
    elif not args.yes:
        confirmation = input(
            f"PAPER market {side_name}: {description} of {args.symbol.upper()}. "
            "Type YES: "
        )
        if confirmation != "YES":
            parser.exit(1, "tradeAPI: paper order canceled\n")
    try:
        client = TradeAPI.from_environment(args.live, args.database)
        submit = client.buy if args.buy else client.sell
        order = submit(args.symbol, quantity=args.quantity, notional=args.notional)
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f"tradeAPI: {exc}\n")
    print(f"Order ID: {order.get('id')}")
    print(f"Status: {order.get('status')}")
    print(f"Symbol: {order.get('symbol', args.symbol.upper())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
