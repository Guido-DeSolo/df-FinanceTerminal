#!/usr/bin/env python3
"""Persist subscribed Alpaca real-time stock trades to SQLite."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "schema.sql"
USER_AGENT = "df-FinanceTerminal liveTrade/1.0"
STREAM_URLS = {
    "iex": "wss://stream.data.alpaca.markets/v2/iex",
    "sip": "wss://stream.data.alpaca.markets/v2/sip",
    "delayed_sip": "wss://stream.data.alpaca.markets/v2/delayed_sip",
    "test": "wss://stream.data.alpaca.markets/v2/test",
}
CRYPTO_STREAM_URLS = {
    "us": "wss://stream.data.alpaca.markets/v1beta3/crypto/us",
    "us-1": "wss://stream.data.alpaca.markets/v1beta3/crypto/us-1",
    "us-2": "wss://stream.data.alpaca.markets/v1beta3/crypto/us-2",
    "eu-1": "wss://stream.data.alpaca.markets/v1beta3/crypto/eu-1",
    "bs-1": "wss://stream.data.alpaca.markets/v1beta3/crypto/bs-1",
}


def timestamp_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def normalize_symbols(values: list[str]) -> tuple[str, ...]:
    symbols: list[str] = []
    for value in values:
        for part in value.split(","):
            symbol = part.strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    if not symbols:
        raise ValueError("at least one live-trade symbol is required")
    return tuple(symbols)


def default_database() -> Path:
    configured = os.environ.get("FINANCE_DB_FILE") or os.environ.get("MTG_DB_FILE")
    if configured:
        return Path(configured).expanduser()
    server_database = Path("/opt/mtg/mtg.db")
    if server_database.exists():
        return server_database
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "df-financeterminal" / "mtg.db"


def duration_seconds(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)(s|min|h|d|w)", value)
    if not match:
        raise ValueError("duration must look like 30min, 6h, 1d, or 1w")
    multiplier = {
        "s": 1,
        "min": 60,
        "h": 60 * 60,
        "d": 24 * 60 * 60,
        "w": 7 * 24 * 60 * 60,
    }[match.group(2)]
    return int(match.group(1)) * multiplier


def prune_live_trades(database: Path | str, max_age: str = "1d") -> int:
    """Delete stored live trades older than a compact retention duration."""
    cutoff = datetime.now(UTC) - timedelta(seconds=duration_seconds(max_age))
    cutoff_text = cutoff.isoformat().replace("+00:00", "Z")
    path = Path(database).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))
        cursor = connection.execute(
            "DELETE FROM live_trades WHERE datetime(timestamp) < datetime(?)",
            (cutoff_text,),
        )
        return cursor.rowcount


def _websocket_client():
    try:
        try:
            from websockets.asyncio.client import connect
        except ImportError:
            from websockets import connect
        from websockets.exceptions import ConnectionClosed
    except ImportError as exc:
        raise RuntimeError(
            "live trades require the 'websockets' Python package"
        ) from exc
    return connect, ConnectionClosed


class AlpacaLiveTrades:
    """Maintain one Alpaca stream connection and store subscribed trades."""

    def __init__(
        self,
        database: Path | str,
        symbols: tuple[str, ...],
        key_id: str,
        secret_key: str,
        feed: str = "iex",
        asset_class: str = "stock",
        location: str = "us",
    ) -> None:
        if not key_id or not secret_key:
            raise ValueError("set APCA_API_KEY_ID and APCA_API_SECRET_KEY")
        if feed not in STREAM_URLS:
            raise ValueError(f"unsupported Alpaca stream feed: {feed}")
        if asset_class not in {"stock", "crypto"}:
            raise ValueError("asset_class must be stock or crypto")
        if location not in CRYPTO_STREAM_URLS:
            raise ValueError(f"unsupported Alpaca crypto location: {location}")
        if not symbols:
            raise ValueError("at least one symbol is required")
        self.database = Path(database).expanduser()
        self.symbols = symbols
        self.key_id = key_id
        self.secret_key = secret_key
        self.feed = feed
        self.asset_class = asset_class
        self.location = location

    @property
    def stream_url(self) -> str:
        return (
            CRYPTO_STREAM_URLS[self.location]
            if self.asset_class == "crypto"
            else STREAM_URLS[self.feed]
        )

    @property
    def source(self) -> str:
        return (
            f"crypto:{self.location}"
            if self.asset_class == "crypto"
            else self.feed
        )

    @classmethod
    def from_environment(
        cls,
        database: Path | str,
        symbols: tuple[str, ...],
        feed: str,
        asset_class: str,
        location: str,
    ) -> "AlpacaLiveTrades":
        return cls(
            database,
            symbols,
            os.environ.get("APCA_API_KEY_ID", ""),
            os.environ.get("APCA_API_SECRET_KEY", ""),
            feed,
            asset_class,
            location,
        )

    def connect_database(self) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))
        return connection

    @staticmethod
    def _decode(raw: str | bytes) -> list[dict]:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("Alpaca sent invalid stream JSON") from exc
        if not isinstance(payload, list) or not all(
            isinstance(message, dict) for message in payload
        ):
            raise RuntimeError("Alpaca sent an invalid stream message")
        return payload

    @classmethod
    async def _receive(cls, websocket) -> list[dict]:
        return cls._decode(await websocket.recv())

    @staticmethod
    def _raise_stream_error(messages: list[dict]) -> None:
        error = next(
            (message for message in messages if message.get("T") == "error"),
            None,
        )
        if error:
            raise RuntimeError(
                f"Alpaca stream error {error.get('code')}: {error.get('msg')}"
            )

    @staticmethod
    def store_trades(
        connection: sqlite3.Connection,
        feed: str,
        messages: list[dict],
    ) -> int:
        received_at = timestamp_now()
        rows: list[tuple] = []
        for message in messages:
            if message.get("T") != "t":
                continue
            try:
                symbol = str(message["S"]).upper()
                trade_id = str(message["i"])
                timestamp = str(message["t"])
                price = float(message["p"])
                size = float(message["s"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("Alpaca sent an invalid trade message") from exc
            if not symbol or not timestamp or not math.isfinite(price) or size < 0:
                raise RuntimeError("Alpaca sent an invalid trade message")
            raw_json = json.dumps(message, sort_keys=True, separators=(",", ":"))
            rows.append(
                (
                    feed,
                    symbol,
                    trade_id,
                    timestamp,
                    price,
                    size,
                    message.get("x"),
                    json.dumps(message.get("c") or [], separators=(",", ":")),
                    message.get("z"),
                    received_at,
                    raw_json,
                )
            )
        connection.executemany(
            """
            INSERT INTO live_trades (
                feed, symbol, trade_id, timestamp, price, size, exchange,
                conditions, tape, received_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(feed, symbol, trade_id, timestamp) DO UPDATE SET
                price = excluded.price,
                size = excluded.size,
                exchange = excluded.exchange,
                conditions = excluded.conditions,
                tape = excluded.tape,
                received_at = excluded.received_at,
                raw_json = excluded.raw_json
            """,
            rows,
        )
        return len(rows)

    async def _session(self, connection: sqlite3.Connection, websocket_connect) -> None:
        async with websocket_connect(
            self.stream_url,
            ping_interval=20,
            ping_timeout=20,
            max_queue=4096,
        ) as websocket:
            connected = await self._receive(websocket)
            self._raise_stream_error(connected)
            if not any(
                message.get("T") == "success"
                and message.get("msg") == "connected"
                for message in connected
            ):
                raise RuntimeError("Alpaca stream did not confirm connection")
            await websocket.send(
                json.dumps(
                    {
                        "action": "auth",
                        "key": self.key_id,
                        "secret": self.secret_key,
                    }
                )
            )
            authenticated = await self._receive(websocket)
            self._raise_stream_error(authenticated)
            if not any(
                message.get("T") == "success"
                and message.get("msg") == "authenticated"
                for message in authenticated
            ):
                raise RuntimeError("Alpaca stream did not confirm authentication")
            await websocket.send(
                json.dumps({"action": "subscribe", "trades": list(self.symbols)})
            )
            subscription = await self._receive(websocket)
            self._raise_stream_error(subscription)
            subscribed = next(
                (
                    message.get("trades") or []
                    for message in subscription
                    if message.get("T") == "subscription"
                ),
                None,
            )
            if subscribed is None:
                raise RuntimeError("Alpaca stream did not confirm subscriptions")
            missing = sorted(set(self.symbols) - {str(value) for value in subscribed})
            if missing:
                raise RuntimeError(
                    f"Alpaca did not subscribe to: {', '.join(missing)}"
                )
            print(
                f"Subscribed to {', '.join(self.symbols)} on {self.source}",
                flush=True,
            )
            async for raw in websocket:
                messages = self._decode(raw)
                self._raise_stream_error(messages)
                saved = self.store_trades(connection, self.source, messages)
                if saved:
                    connection.commit()

    async def run(self) -> None:
        websocket_connect, connection_closed = _websocket_client()
        delay = 1
        with self.connect_database() as connection:
            while True:
                try:
                    await self._session(connection, websocket_connect)
                    delay = 1
                except connection_closed as exc:
                    print(
                        f"Alpaca stream disconnected: {exc}; reconnecting in {delay}s",
                        file=sys.stderr,
                        flush=True,
                    )
                except (OSError, TimeoutError) as exc:
                    print(
                        f"Alpaca connection failed: {exc}; reconnecting in {delay}s",
                        file=sys.stderr,
                        flush=True,
                    )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="liveOrderBooks",
        description="Store subscribed Alpaca real-time stock trades in SQLite.",
    )
    parser.add_argument(
        "symbols",
        nargs="*",
        help="stock symbols; commas are accepted (or set ALPACA_LIVE_SYMBOLS)",
    )
    parser.add_argument(
        "--feed",
        choices=tuple(STREAM_URLS),
        default=os.environ.get("ALPACA_DATA_FEED", "iex"),
    )
    parser.add_argument(
        "--class",
        dest="asset_class",
        choices=("auto", "stock", "crypto"),
        default=os.environ.get("ALPACA_ASSET_CLASS", "auto"),
    )
    parser.add_argument(
        "--location",
        choices=tuple(CRYPTO_STREAM_URLS),
        default=os.environ.get("ALPACA_CRYPTO_LOCATION", "us"),
    )
    parser.add_argument("--database", "-d", type=Path, default=default_database())
    parser.add_argument(
        "--prune-older-than",
        metavar="DURATION",
        help="delete stored live trades older than a duration and exit",
    )
    args = parser.parse_args(argv)
    if args.prune_older_than:
        try:
            deleted = prune_live_trades(args.database, args.prune_older_than)
        except (OSError, sqlite3.Error, ValueError) as exc:
            parser.exit(1, f"liveOrderBooks: {exc}\n")
        print(f"Deleted live trades: {deleted}")
        print(f"Retention: {args.prune_older_than}")
        print(f"Database: {args.database}")
        return 0
    symbol_values = args.symbols or [
        os.environ.get("ALPACA_LIVE_SYMBOLS", "BTC/USD")
    ]
    try:
        symbols = normalize_symbols(symbol_values)
        contains_pairs = ["/" in symbol for symbol in symbols]
        if any(contains_pairs) and not all(contains_pairs):
            raise ValueError(
                "stock symbols and cryptocurrency pairs require separate services"
            )
        asset_class = (
            "crypto" if all(contains_pairs) else "stock"
        ) if args.asset_class == "auto" else args.asset_class
        if asset_class == "crypto" and not all(contains_pairs):
            raise ValueError("crypto symbols must use pair notation such as BTC/USD")
        if asset_class == "stock" and any(contains_pairs):
            raise ValueError("stock symbols cannot contain '/'")
        stream = AlpacaLiveTrades.from_environment(
            args.database,
            symbols,
            args.feed,
            asset_class,
            args.location,
        )
        asyncio.run(stream.run())
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        parser.exit(1, f"liveOrderBooks: {exc}\n")
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
