#!/usr/bin/env python3
"""Collect Alpaca and NewsData.io articles into one SQLite news store."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "schema.sql"
ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
NEWSDATA_URL = "https://newsdata.io/api/1/latest"
USER_AGENT = "df-FinanceTerminal newsAggregation/1.0"
NEWSDATA_CATEGORIES = (
    "business,technology,science,environment,domestic,breaking"
)


@dataclass(frozen=True)
class NewsArticle:
    article_id: str
    provider: str
    headline: str
    summary: str | None
    author: str | None
    created_at: str
    updated_at: str
    content: str | None
    url: str | None
    source: str | None
    symbols: tuple[str, ...]
    raw_json: str


@dataclass(frozen=True)
class CollectionResult:
    alpaca_articles: int
    newsdata_articles: int
    stored_articles: int
    received_at: str


class NewsAggregation:
    """Fetch and normalize news from Alpaca and NewsData.io."""

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
    def _now() -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )

    @staticmethod
    def _request(url: str, headers: dict[str, str]) -> dict:
        request = Request(url, headers={**headers, "User-Agent": USER_AGENT})
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urlopen(request, timeout=30) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    raise RuntimeError("news provider returned an invalid response")
                return payload
            except HTTPError as exc:
                last_error = exc
                detail = exc.read().decode("utf-8", "replace")[:500]
                if exc.code != 429 and exc.code < 500:
                    raise RuntimeError(
                        f"news provider returned HTTP {exc.code}: {detail}"
                    ) from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
        raise RuntimeError(f"news provider request failed after retries: {last_error}")

    @classmethod
    def fetch_alpaca(
        cls,
        key_id: str,
        secret_key: str,
        symbols: list[str] | None = None,
        max_pages: int = 1,
    ) -> list[dict]:
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        params = {
            "limit": "50",
            "sort": "desc",
            "include_content": "true",
        }
        normalized_symbols = [
            symbol.strip().upper().replace("/", "")
            for symbol in (symbols or [])
            if symbol.strip()
        ]
        if normalized_symbols:
            params["symbols"] = ",".join(dict.fromkeys(normalized_symbols))
        headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
        }
        articles: list[dict] = []
        for _ in range(max_pages):
            payload = cls._request(
                f"{ALPACA_NEWS_URL}?{urlencode(params)}", headers
            )
            page = payload.get("news") or []
            if not isinstance(page, list):
                raise RuntimeError("Alpaca returned an invalid news page")
            articles.extend(article for article in page if isinstance(article, dict))
            page_token = payload.get("next_page_token")
            if not page_token:
                break
            params["page_token"] = str(page_token)
        return articles

    @classmethod
    def fetch_newsdata(cls, api_key: str) -> list[dict]:
        query = urlencode(
            {
                "apikey": api_key,
                "country": "us",
                "language": "en",
                "category": NEWSDATA_CATEGORIES,
            }
        )
        payload = cls._request(f"{NEWSDATA_URL}?{query}", {"Accept": "application/json"})
        if payload.get("status") == "error":
            raise RuntimeError(
                f"NewsData error: {payload.get('results') or payload.get('message')}"
            )
        results = payload.get("results") or []
        if not isinstance(results, list):
            raise RuntimeError("NewsData returned an invalid results list")
        return [article for article in results if isinstance(article, dict)]

    @classmethod
    def _alpaca_article(cls, article: dict) -> NewsArticle | None:
        upstream_id = article.get("id")
        headline = str(article.get("headline") or "").strip()
        if upstream_id is None or not headline:
            return None
        created_at = str(article.get("created_at") or cls._now())
        return NewsArticle(
            article_id=f"alpaca:{upstream_id}",
            provider="alpaca",
            headline=headline,
            summary=article.get("summary"),
            author=article.get("author"),
            created_at=created_at,
            updated_at=str(article.get("updated_at") or created_at),
            content=article.get("content"),
            url=article.get("url"),
            source=article.get("source"),
            symbols=tuple(
                dict.fromkeys(
                    str(symbol).upper().replace("/", "")
                    for symbol in (article.get("symbols") or [])
                    if symbol
                )
            ),
            raw_json=json.dumps(article, sort_keys=True, separators=(",", ":")),
        )

    @classmethod
    def _newsdata_article(cls, article: dict) -> NewsArticle | None:
        headline = str(article.get("title") or "").strip()
        if not headline:
            return None
        identity = article.get("article_id") or article.get("link") or headline
        article_id = "newsdata:" + hashlib.sha256(
            str(identity).encode("utf-8")
        ).hexdigest()
        published = str(article.get("pubDate") or cls._now())
        creators = article.get("creator")
        author = ", ".join(str(value) for value in creators) if isinstance(
            creators, list
        ) else creators
        return NewsArticle(
            article_id=article_id,
            provider="newsdata",
            headline=headline,
            summary=article.get("description"),
            author=author,
            created_at=published,
            updated_at=published,
            content=article.get("content"),
            url=article.get("link"),
            source=article.get("source_name") or article.get("source_id"),
            symbols=(),
            raw_json=json.dumps(article, sort_keys=True, separators=(",", ":")),
        )

    @staticmethod
    def _store(
        connection: sqlite3.Connection,
        articles: list[NewsArticle],
        received_at: str,
    ) -> int:
        for article in articles:
            connection.execute(
                """
                INSERT INTO news_articles (
                    article_id, provider, headline, summary, author, created_at,
                    updated_at, content, url, source, received_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_id) DO UPDATE SET
                    provider = excluded.provider,
                    headline = excluded.headline,
                    summary = excluded.summary,
                    author = excluded.author,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    content = excluded.content,
                    url = excluded.url,
                    source = excluded.source,
                    received_at = excluded.received_at,
                    raw_json = excluded.raw_json
                """,
                (
                    article.article_id, article.provider, article.headline,
                    article.summary, article.author, article.created_at,
                    article.updated_at, article.content, article.url,
                    article.source, received_at, article.raw_json,
                ),
            )
            connection.execute(
                "DELETE FROM news_article_symbols WHERE article_id = ?",
                (article.article_id,),
            )
            connection.executemany(
                """
                INSERT INTO news_article_symbols (article_id, symbol)
                VALUES (?, ?)
                """,
                ((article.article_id, symbol) for symbol in article.symbols),
            )
        return len(articles)

    def collect(
        self,
        symbols: list[str] | None = None,
        max_alpaca_pages: int = 1,
    ) -> CollectionResult:
        key_id = os.environ.get("APCA_API_KEY_ID")
        secret_key = os.environ.get("APCA_API_SECRET_KEY")
        newsdata_key = os.environ.get("NEWSDATA_API_KEY")
        if not key_id or not secret_key:
            raise ValueError("set APCA_API_KEY_ID and APCA_API_SECRET_KEY")
        if not newsdata_key:
            raise ValueError("set NEWSDATA_API_KEY")

        alpaca_raw = self.fetch_alpaca(
            key_id, secret_key, symbols, max_pages=max_alpaca_pages
        )
        newsdata_raw = self.fetch_newsdata(newsdata_key)
        articles = [
            normalized
            for normalized in (
                *(self._alpaca_article(article) for article in alpaca_raw),
                *(self._newsdata_article(article) for article in newsdata_raw),
            )
            if normalized is not None
        ]
        received_at = self._now()
        with self.connect() as connection:
            self.initialize(connection)
            stored = self._store(connection, articles, received_at)
        return CollectionResult(
            alpaca_articles=len(alpaca_raw),
            newsdata_articles=len(newsdata_raw),
            stored_articles=stored,
            received_at=received_at,
        )


def default_database() -> Path:
    configured = os.environ.get("FINANCE_DB_FILE") or os.environ.get("MTG_DB_FILE")
    if configured:
        return Path(configured).expanduser()
    server_database = Path("/opt/mtg/mtg.db")
    if server_database.exists():
        return server_database
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "df-financeterminal" / "mtg.db"
