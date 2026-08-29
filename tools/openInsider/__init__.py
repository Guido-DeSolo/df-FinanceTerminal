#!/usr/bin/env python3
"""Scrape OpenInsider's Latest Insider Buys into SQLite."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "schema.sql"
HOMEPAGE = "http://openinsider.com/"
SCREENER_URL = (
    "http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd=3&fdr=&td=3"
    "&tdr=&fdlyl=&fdlyh=&daysago=&xp=1&xs=1&vl=&vh=&ocl=&och=&sic1=-1"
    "&sicl=100&sich=9999&grp=0&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h="
    "&oc2l=&oc2h=&sortcol=1&cnt=100&page=1"
)
TARGET_SECTION = "Latest Insider Buys"
USER_AGENT = "df-FinanceTerminal openInsider/1.0 (personal research)"


@dataclass(frozen=True)
class InsiderScrapeResult:
    fetched: int
    inserted: int
    updated: int
    scraped_at: str


def _text(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _number(value: str) -> float | None:
    cleaned = value.strip()
    if not cleaned or cleaned.casefold() == "new":
        return None
    cleaned = re.sub(r"[$,%+,<>]", "", cleaned)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError as exc:
        raise ValueError(f"invalid OpenInsider number {value!r}") from exc


def _integer(value: str) -> int | None:
    number = _number(value)
    if number is None:
        return None
    if not number.is_integer():
        raise ValueError(f"invalid OpenInsider integer {value!r}")
    return int(number)


class _LatestBuysParser(HTMLParser):
    """Read only the tinytable immediately following the target heading."""

    def __init__(self, target_section: str | None = TARGET_SECTION) -> None:
        super().__init__(convert_charrefs=True)
        self.target_section = target_section
        self.heading = ""
        self._heading_tag = ""
        self._heading_text: list[str] = []
        self._in_target_table = False
        self._in_body = False
        self._in_cell = False
        self._cell_text: list[str] = []
        self._cell_link = ""
        self._row: list[tuple[str, str]] = []
        self.rows: list[list[tuple[str, str]]] = []

    @staticmethod
    def _attrs(attributes: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key: value or "" for key, value in attributes}

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = self._attrs(attrs)
        if tag in {"h1", "h2", "h3", "h4"}:
            self._heading_tag = tag
            self._heading_text = []
        elif tag == "table":
            classes = attributes.get("class", "").split()
            if (
                "tinytable" in classes
                and (
                    self.target_section is None
                    or self.heading == self.target_section
                )
            ):
                self._in_target_table = True
        elif self._in_target_table and tag == "tbody":
            self._in_body = True
        elif self._in_body and tag == "tr":
            self._row = []
        elif self._in_body and tag == "td":
            self._in_cell = True
            self._cell_text = []
            self._cell_link = ""
        elif self._in_cell and tag == "a" and not self._cell_link:
            self._cell_link = attributes.get("href", "")

    def handle_data(self, data: str) -> None:
        if self._heading_tag:
            self._heading_text.append(data)
        if self._in_cell:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._heading_tag:
            self.heading = _text(self._heading_text)
            self._heading_tag = ""
        elif tag == "td" and self._in_cell:
            self._row.append((_text(self._cell_text), self._cell_link))
            self._in_cell = False
        elif tag == "tr" and self._in_body and self._row:
            self.rows.append(self._row)
            self._row = []
        elif tag == "tbody" and self._in_body:
            self._in_body = False
        elif tag == "table" and self._in_target_table:
            self._in_target_table = False


def _trade(cells: list[tuple[str, str]]) -> dict | None:
    if len(cells) < 17:
        return None
    values = [cell[0] for cell in cells]
    trade = {
        "flags": values[0] or None,
        "filing_date": values[1],
        "trade_date": values[2],
        "ticker": values[3].upper(),
        "company": values[4],
        "insider": values[5],
        "title": values[6] or None,
        "trade_type": values[7],
        "price": _number(values[8]),
        "quantity": _integer(values[9]),
        "owned": _integer(values[10]),
        "ownership_change": _number(values[11]),
        "ownership_change_text": values[11] or None,
        "trade_value": _number(values[12]),
        "one_day_change": _number(values[13]),
        "one_week_change": _number(values[14]),
        "one_month_change": _number(values[15]),
        "six_month_change": _number(values[16]),
        "filing_url": urljoin(HOMEPAGE, cells[1][1]) if cells[1][1] else None,
        "ticker_url": urljoin(HOMEPAGE, cells[3][1]) if cells[3][1] else None,
        "insider_url": urljoin(HOMEPAGE, cells[5][1]) if cells[5][1] else None,
    }
    required = (
        "filing_date", "trade_date", "ticker", "company", "insider", "trade_type"
    )
    if any(not trade[field] for field in required):
        return None
    identity = {
        field: trade[field]
        for field in (
            "filing_url", "ticker", "insider", "trade_date", "trade_type",
            "quantity", "price",
        )
    }
    trade["insider_id"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return trade


def parse_homepage(document: str) -> list[dict]:
    parser = _LatestBuysParser()
    parser.feed(document)
    trades = [trade for row in parser.rows if (trade := _trade(row)) is not None]
    if not trades:
        raise ValueError(
            "OpenInsider homepage contained no Latest Insider Buys rows"
        )
    return trades


def parse_screener(document: str) -> list[dict]:
    parser = _LatestBuysParser(target_section=None)
    parser.feed(document)
    trades = [trade for row in parser.rows if (trade := _trade(row)) is not None]
    if not trades:
        raise ValueError("OpenInsider screener contained no trade rows")
    return trades


class OpenInsider:
    """Fetch and store the homepage's latest individual purchase filings."""

    def __init__(self, database: Path | str):
        self.database = Path(database).expanduser()

    def connect(self) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))

    @staticmethod
    def fetch_document(url: str) -> str:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                with urlopen(request, timeout=30) as response:
                    return response.read().decode(
                        response.headers.get_content_charset() or "utf-8", "replace"
                    )
            except HTTPError as exc:
                last_error = exc
                detail = exc.read().decode("utf-8", "replace")[:500]
                if exc.code != 429 and exc.code < 500:
                    raise RuntimeError(
                        f"OpenInsider returned HTTP {exc.code}: {detail}"
                    ) from exc
            except (URLError, TimeoutError) as exc:
                last_error = exc
            if attempt < 3:
                time.sleep(2 ** attempt)
        raise RuntimeError(f"OpenInsider request failed after retries: {last_error}")

    def scrape(self) -> InsiderScrapeResult:
        trades = parse_homepage(self.fetch_document(HOMEPAGE))
        return self._store(trades)

    def scrape_screener(self) -> InsiderScrapeResult:
        """Run the fixed three-day, purchases-and-sales screener once."""
        trades = parse_screener(self.fetch_document(SCREENER_URL))
        return self._store(trades)

    def _store(self, trades: list[dict]) -> InsiderScrapeResult:
        scraped_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
        ids = tuple(trade["insider_id"] for trade in trades)
        with self.connect() as connection:
            self.initialize(connection)
            existing = {
                row[0]
                for row in connection.execute(
                    f"SELECT insider_id FROM insiders WHERE insider_id IN "
                    f"({','.join('?' for _ in ids)})",
                    ids,
                )
            }
            for trade in trades:
                raw_json = json.dumps(trade, sort_keys=True, separators=(",", ":"))
                connection.execute(
                    """
                    INSERT INTO insiders (
                        insider_id, flags, filing_date, trade_date, ticker,
                        company, insider, title, trade_type, price, quantity,
                        owned, ownership_change, ownership_change_text,
                        trade_value, one_day_change, one_week_change,
                        one_month_change, six_month_change, filing_url,
                        ticker_url, insider_url, scraped_at, raw_json
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(insider_id) DO UPDATE SET
                        flags = excluded.flags,
                        filing_date = excluded.filing_date,
                        trade_date = excluded.trade_date,
                        ticker = excluded.ticker,
                        company = excluded.company,
                        insider = excluded.insider,
                        title = excluded.title,
                        trade_type = excluded.trade_type,
                        price = excluded.price,
                        quantity = excluded.quantity,
                        owned = excluded.owned,
                        ownership_change = excluded.ownership_change,
                        ownership_change_text = excluded.ownership_change_text,
                        trade_value = excluded.trade_value,
                        one_day_change = excluded.one_day_change,
                        one_week_change = excluded.one_week_change,
                        one_month_change = excluded.one_month_change,
                        six_month_change = excluded.six_month_change,
                        filing_url = excluded.filing_url,
                        ticker_url = excluded.ticker_url,
                        insider_url = excluded.insider_url,
                        scraped_at = excluded.scraped_at,
                        raw_json = excluded.raw_json
                    """,
                    (
                        trade["insider_id"], trade["flags"], trade["filing_date"],
                        trade["trade_date"], trade["ticker"], trade["company"],
                        trade["insider"], trade["title"], trade["trade_type"],
                        trade["price"], trade["quantity"], trade["owned"],
                        trade["ownership_change"], trade["ownership_change_text"],
                        trade["trade_value"], trade["one_day_change"],
                        trade["one_week_change"], trade["one_month_change"],
                        trade["six_month_change"], trade["filing_url"],
                        trade["ticker_url"], trade["insider_url"], scraped_at,
                        raw_json,
                    ),
                )
        inserted = sum(trade["insider_id"] not in existing for trade in trades)
        return InsiderScrapeResult(
            fetched=len(trades),
            inserted=inserted,
            updated=len(trades) - inserted,
            scraped_at=scraped_at,
        )

    def prune(self, max_age_days: int = 7) -> int:
        """Delete filings older than the configured rolling retention window."""
        if max_age_days < 1:
            raise ValueError("max_age_days must be positive")
        cutoff = (
            datetime.now(UTC) - timedelta(days=max_age_days)
        ).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            self.initialize(connection)
            cursor = connection.execute(
                "DELETE FROM insiders WHERE datetime(filing_date) < datetime(?)",
                (cutoff,),
            )
            return cursor.rowcount


def default_database() -> Path:
    configured = os.environ.get("FINANCE_DB_FILE") or os.environ.get("MTG_DB_FILE")
    if configured:
        return Path(configured).expanduser()
    server_database = Path("/opt/mtg/mtg.db")
    if server_database.exists():
        return server_database
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "df-financeterminal" / "mtg.db"
