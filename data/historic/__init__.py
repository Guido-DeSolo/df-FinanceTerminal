#!/usr/bin/env python3
"""Download paginated Alpaca stock and cryptocurrency bars into SQLite."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "schema.sql"
DATA_URL = "https://data.alpaca.markets"
USER_AGENT = "df-FinanceTerminal historic/1.0"
TIMEFRAME = re.compile(
    r"(?:[1-9]|[1-5][0-9])(?:Min|T)"
    r"|(?:[1-9]|1[0-9]|2[0-3])(?:Hour|H)"
    r"|1(?:Day|D|Week|W)"
    r"|(?:1|2|3|4|6|12)(?:Month|M)"
)


@dataclass(frozen=True)
class HistoricResult:
    symbols: tuple[str, ...]
    pages: int
    rows_saved: int
    status: str
    database: Path


def timestamp_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be RFC-3339 or YYYY-MM-DD") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_timeframe(value: str) -> str:
    if not TIMEFRAME.fullmatch(value):
        raise ValueError(
            "timeframe must be 1-59Min, 1-23Hour, 1Day, 1Week, "
            "or 1/2/3/4/6/12Month (short aliases are accepted)"
        )
    suffixes = {
        "T": "Min",
        "H": "Hour",
        "D": "Day",
        "W": "Week",
        "M": "Month",
    }
    for suffix, replacement in suffixes.items():
        if value.endswith(suffix) and not value.endswith(replacement):
            return value[:-len(suffix)] + replacement
    return value


def positive_int(value: str, field: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if number < 1:
        raise ValueError(f"{field} must be a positive integer")
    return number


def page_limit(value: str) -> int:
    number = positive_int(value, "limit")
    if number > 10000:
        raise ValueError("limit must be between 1 and 10000")
    return number


def normalize_symbols(values: list[str], asset_class: str) -> tuple[str, ...]:
    symbols: list[str] = []
    for value in values:
        for part in value.split(","):
            symbol = part.strip().upper()
            if not symbol:
                continue
            if asset_class == "stock" and "/" in symbol:
                raise ValueError(f"stock symbol cannot contain '/': {symbol}")
            if symbol not in symbols:
                symbols.append(symbol)
    if not symbols:
        raise ValueError("at least one symbol is required")
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


class HistoricData:
    """Fetch Alpaca bar pages and persist each page transactionally."""

    def __init__(self, database: Path | str, key_id: str, secret_key: str):
        if not key_id or not secret_key:
            raise ValueError("Alpaca key ID and secret key are required")
        self.database = Path(database).expanduser()
        self.headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    @classmethod
    def from_environment(cls, database: Path | str) -> "HistoricData":
        return cls(
            database,
            os.environ.get("APCA_API_KEY_ID", ""),
            os.environ.get("APCA_API_SECRET_KEY", ""),
        )

    def connect(self) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))

    def request(self, path: str, params: dict[str, str | int | None]) -> dict:
        query = urlencode(
            {key: value for key, value in params.items() if value is not None}
        )
        request = Request(f"{DATA_URL}{path}?{query}", headers=self.headers)
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                with urlopen(request, timeout=30) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    raise RuntimeError("Alpaca returned an invalid bars response")
                return payload
            except HTTPError as exc:
                last_error = exc
                detail = exc.read().decode("utf-8", "replace")[:500]
                if exc.code != 429 and exc.code < 500:
                    raise RuntimeError(
                        f"Alpaca returned HTTP {exc.code}: {detail}"
                    ) from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < 4:
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Alpaca request failed after retries: {last_error}")

    @staticmethod
    def save_bars(
        connection: sqlite3.Connection,
        asset_class: str,
        timeframe: str,
        bars_by_symbol: dict[str, list[dict]],
        feed: str,
        location: str,
        adjustment: str,
    ) -> int:
        fetched_at = timestamp_now()
        rows: list[tuple] = []
        for symbol, bars in bars_by_symbol.items():
            if not isinstance(bars, list):
                raise RuntimeError(f"Alpaca returned invalid bars for {symbol}")
            for bar in bars:
                if not isinstance(bar, dict):
                    raise RuntimeError(f"Alpaca returned an invalid bar for {symbol}")
                try:
                    row = (
                        asset_class,
                        symbol.upper(),
                        timeframe,
                        bar["t"],
                        bar["o"],
                        bar["h"],
                        bar["l"],
                        bar["c"],
                        bar["v"],
                        bar.get("n"),
                        bar.get("vw"),
                        feed,
                        location,
                        adjustment,
                        fetched_at,
                        json.dumps(bar, sort_keys=True, separators=(",", ":")),
                    )
                except KeyError as exc:
                    raise RuntimeError(
                        f"Alpaca bar for {symbol} is missing {exc.args[0]}"
                    ) from exc
                rows.append(row)
        connection.executemany(
            """
            INSERT INTO historic_bars (
                asset_class, symbol, timeframe, timestamp, open, high, low,
                close, volume, trade_count, vwap, feed, location, adjustment,
                fetched_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (
                asset_class, symbol, timeframe, timestamp,
                feed, location, adjustment
            ) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                trade_count = excluded.trade_count,
                vwap = excluded.vwap,
                fetched_at = excluded.fetched_at,
                raw_json = excluded.raw_json
            """,
            rows,
        )
        return len(rows)

    def fetch(
        self,
        symbols: tuple[str, ...],
        asset_class: str,
        timeframe: str,
        start: str,
        end: str | None = None,
        feed: str = "iex",
        location: str = "us",
        adjustment: str = "raw",
        limit: int = 10000,
        max_pages: int | None = None,
    ) -> HistoricResult:
        if limit < 1 or limit > 10000:
            raise ValueError("limit must be between 1 and 10000")
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be positive")
        parse_timestamp(start, "start")
        if end:
            parse_timestamp(end, "end")
        source_feed = feed if asset_class == "stock" else ""
        source_location = location if asset_class == "crypto" else ""
        source_adjustment = adjustment if asset_class == "stock" else ""
        started_at = timestamp_now()
        with self.connect() as connection:
            self.initialize(connection)
            cursor = connection.execute(
                """
                INSERT INTO historic_fetch_runs (
                    asset_class, symbols, timeframe, requested_start,
                    requested_end, feed, location, adjustment, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    asset_class,
                    ",".join(symbols),
                    timeframe,
                    start,
                    end,
                    source_feed,
                    source_location,
                    source_adjustment,
                    started_at,
                ),
            )
            run_id = int(cursor.lastrowid)

        token: str | None = None
        seen_tokens: set[str] = set()
        pages = 0
        rows_saved = 0
        capped = False
        try:
            while True:
                params: dict[str, str | int | None] = {
                    "symbols": ",".join(symbols),
                    "timeframe": timeframe,
                    "start": start,
                    "end": end,
                    "limit": limit,
                    "page_token": token,
                    "sort": "asc",
                }
                if asset_class == "stock":
                    path = "/v2/stocks/bars"
                    params.update({"feed": feed, "adjustment": adjustment})
                else:
                    path = f"/v1beta3/crypto/{location}/bars"
                payload = self.request(path, params)
                bars = payload.get("bars") or {}
                if not isinstance(bars, dict):
                    raise RuntimeError("Alpaca returned an invalid bars collection")
                with self.connect() as connection:
                    count = self.save_bars(
                        connection,
                        asset_class,
                        timeframe,
                        bars,
                        source_feed,
                        source_location,
                        source_adjustment,
                    )
                    pages += 1
                    rows_saved += count
                    connection.execute(
                        """
                        UPDATE historic_fetch_runs
                        SET pages = ?, rows_saved = ?
                        WHERE id = ?
                        """,
                        (pages, rows_saved, run_id),
                    )
                print(f"Page {pages}: saved {count:,} bars ({rows_saved:,} total)")

                next_token = payload.get("next_page_token")
                if next_token is not None and not isinstance(next_token, str):
                    raise RuntimeError("Alpaca returned an invalid pagination token")
                if not next_token:
                    break
                if max_pages is not None and pages >= max_pages:
                    capped = True
                    break
                if next_token in seen_tokens:
                    raise RuntimeError("Alpaca repeated a pagination token")
                seen_tokens.add(next_token)
                token = next_token

            status = "partial" if capped else "complete"
            with self.connect() as connection:
                connection.execute(
                    """
                    UPDATE historic_fetch_runs
                    SET status = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (status, timestamp_now(), run_id),
                )
            return HistoricResult(
                symbols=symbols,
                pages=pages,
                rows_saved=rows_saved,
                status=status,
                database=self.database,
            )
        except (Exception, KeyboardInterrupt) as exc:
            with self.connect() as connection:
                connection.execute(
                    """
                    UPDATE historic_fetch_runs
                    SET status = 'failed', finished_at = ?, error = ?
                    WHERE id = ?
                    """,
                    (timestamp_now(), str(exc)[:1000], run_id),
                )
            raise
