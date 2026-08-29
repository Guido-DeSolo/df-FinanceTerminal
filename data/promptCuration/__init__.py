#!/usr/bin/env python3
"""Curate symbol-related SQLite news into a plain-text LLM prompt file."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIRECTORY = ROOT / "data" / "promptCuration" / "output"


@dataclass(frozen=True)
class CurationResult:
    symbol: str
    articles: int
    output: Path


def default_database() -> Path:
    configured = os.environ.get("FINANCE_DB_FILE") or os.environ.get("MTG_DB_FILE")
    if configured:
        return Path(configured).expanduser()
    server_database = Path("/opt/mtg/mtg.db")
    if server_database.exists():
        return server_database
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "df-financeterminal" / "mtg.db"


def normalize_symbol(symbol: str) -> str:
    """Return the canonical symbol form used by the news database."""
    normalized = symbol.strip().upper().replace("/", "")
    if not normalized or not re.fullmatch(r"[A-Z0-9._-]+", normalized):
        raise ValueError("symbol must contain letters, numbers, '.', '_' or '-'")
    return normalized


class PromptCuration:
    """Find and render news relevant to one market symbol."""

    def __init__(self, database: Path | str):
        self.database = Path(database).expanduser()

    @staticmethod
    def _mentioned(symbol: str, row: sqlite3.Row) -> bool:
        # Explicit provider mappings are checked in SQL. This fallback allows
        # untagged providers, including NewsData.io, to contribute articles.
        text = "\n".join(
            str(row[field] or "")
            for field in ("headline", "summary", "content")
        )
        aliases = [re.escape(symbol)]
        for quote in ("USD", "USDT", "USDC"):
            if symbol.endswith(quote) and len(symbol) > len(quote):
                base = symbol[: -len(quote)]
                aliases.append(rf"{re.escape(base)}/{quote}")
                break
        pattern = (
            rf"(?<![A-Z0-9._-])\$?(?:{'|'.join(aliases)})(?![A-Z0-9._-])"
        )
        return re.search(pattern, text, flags=re.IGNORECASE) is not None

    def articles(self, symbol: str) -> tuple[sqlite3.Row, ...]:
        """Return unique related articles, newest first."""
        normalized = normalize_symbol(symbol)
        if not self.database.is_file():
            raise FileNotFoundError(f"news database does not exist: {self.database}")
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            required = {"news_articles", "news_article_symbols"}
            if not required.issubset(tables):
                raise RuntimeError("database does not contain the news schema")
            rows = connection.execute(
                """
                SELECT
                    article.article_id,
                    article.provider,
                    article.headline,
                    article.summary,
                    article.author,
                    article.created_at,
                    article.updated_at,
                    article.content,
                    article.url,
                    article.source,
                    EXISTS (
                        SELECT 1
                        FROM news_article_symbols AS tagged
                        WHERE tagged.article_id = article.article_id
                          AND UPPER(REPLACE(tagged.symbol, '/', '')) = ?
                    ) AS explicitly_tagged
                FROM news_articles AS article
                ORDER BY article.created_at DESC, article.article_id
                """,
                (normalized,),
            ).fetchall()
        return tuple(
            row
            for row in rows
            if row["explicitly_tagged"] or self._mentioned(normalized, row)
        )

    def insider_trades(
        self, symbol: str, recent_days: int = 7
    ) -> tuple[sqlite3.Row, ...]:
        """Return recent OpenInsider filings for a symbol, newest first."""
        normalized = normalize_symbol(symbol)
        if recent_days < 1:
            raise ValueError("recent_days must be positive")
        if not self.database.is_file():
            raise FileNotFoundError(f"news database does not exist: {self.database}")
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'insiders'"
            ).fetchone() is None:
                raise RuntimeError("database does not contain the insiders table")
            return tuple(
                connection.execute(
                    """
                    SELECT
                        filing_date, trade_date, ticker, company, insider, title,
                        trade_type, price, quantity, owned,
                        ownership_change_text, trade_value, filing_url
                    FROM insiders
                    WHERE UPPER(REPLACE(ticker, '/', '')) = ?
                      AND datetime(filing_date) >= datetime('now', ?)
                    ORDER BY datetime(filing_date) DESC, insider_id
                    """,
                    (normalized, f"-{recent_days} days"),
                ).fetchall()
            )

    @staticmethod
    def _render(
        symbol: str,
        articles: tuple[sqlite3.Row, ...],
        insider_trades: tuple[sqlite3.Row, ...],
        recent_days: int,
    ) -> str:
        lines = [
            f"NEWS CONTEXT FOR {symbol}",
            f"ARTICLE COUNT: {len(articles)}",
            "ORDER: newest first",
            "",
            f"RECENT INSIDER TRADES (LAST {recent_days} DAYS)",
        ]
        if not insider_trades:
            lines.extend(
                ("There are no insider trades associated with this asset.", "")
            )
        else:
            for number, trade in enumerate(insider_trades, 1):
                price = (
                    f"${trade['price']:,.2f}"
                    if trade["price"] is not None
                    else "Unknown"
                )
                value = (
                    f"${trade['trade_value']:,.2f}"
                    if trade["trade_value"] is not None
                    else "Unknown"
                )
                lines.extend(
                    (
                        f"--- INSIDER TRADE {number} ---",
                        f"Filing date: {trade['filing_date']}",
                        f"Trade date: {trade['trade_date']}",
                        f"Company: {trade['company']} ({trade['ticker']})",
                        f"Insider: {trade['insider']}",
                        f"Title: {trade['title'] or 'Unknown'}",
                        f"Trade type: {trade['trade_type']}",
                        f"Price: {price}",
                        f"Quantity: {trade['quantity'] if trade['quantity'] is not None else 'Unknown'}",
                        f"Owned after trade: {trade['owned'] if trade['owned'] is not None else 'Unknown'}",
                        f"Ownership change: {trade['ownership_change_text'] or 'Unknown'}",
                        f"Trade value: {value}",
                        f"Filing URL: {trade['filing_url'] or 'Unavailable'}",
                        "",
                    )
                )
        lines.append("NEWS ARTICLES")
        lines.append("")
        for number, article in enumerate(articles, 1):
            lines.extend(
                (
                    f"=== ARTICLE {number} ===",
                    f"Headline: {article['headline']}",
                    f"Published: {article['created_at']}",
                    f"Updated: {article['updated_at']}",
                    f"Provider: {article['provider']}",
                    f"Source: {article['source'] or 'Unknown'}",
                    f"Author: {article['author'] or 'Unknown'}",
                    f"URL: {article['url'] or 'Unavailable'}",
                    "Summary:",
                    str(article["summary"] or "Unavailable").strip(),
                    "Content:",
                    str(article["content"] or "Unavailable").strip(),
                    "",
                )
            )
        return "\n".join(lines).rstrip() + "\n"

    def curate(
        self,
        symbol: str,
        output: Path | str | None = None,
        recent_days: int = 7,
    ) -> CurationResult:
        """Write relevant news to a deterministic UTF-8 text file."""
        normalized = normalize_symbol(symbol)
        destination = (
            Path(output).expanduser()
            if output is not None
            else DEFAULT_OUTPUT_DIRECTORY / f"{normalized}.txt"
        )
        articles = self.articles(normalized)
        insider_trades = self.insider_trades(normalized, recent_days)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text(
            self._render(normalized, articles, insider_trades, recent_days),
            encoding="utf-8",
        )
        temporary.replace(destination)
        return CurationResult(normalized, len(articles), destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="promptCuration",
        description="Write news related to a symbol into an LLM-ready text file.",
    )
    parser.add_argument("symbol")
    parser.add_argument("--database", "-d", type=Path, default=default_database())
    parser.add_argument("--output", "-o", type=Path)
    parser.add_argument(
        "--insider-days",
        type=int,
        default=7,
        help="recent OpenInsider filing window (default: 7 days)",
    )
    args = parser.parse_args(argv)
    try:
        result = PromptCuration(args.database).curate(
            args.symbol, args.output, args.insider_days
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"promptCuration: {exc}\n")
    print(f"Wrote {result.articles} {result.symbol} articles to {result.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
