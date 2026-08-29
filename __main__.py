#!/usr/bin/env python3
"""Command-line entry point for every DF-FinanceTerminal module."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from data.historic import (
    HistoricData,
    default_database as historic_database,
    normalize_symbols as normalize_historic_symbols,
    normalize_timeframe,
    page_limit,
    parse_timestamp,
    positive_int,
)
from data.promptCuration import PromptCuration, default_database as prompt_database
from tools.assetEnumeration import (
    AssetEnumeration,
    default_database as asset_database,
)
from tools.liveOrderBooks import (
    CRYPTO_STREAM_URLS,
    STREAM_URLS,
    AlpacaLiveTrades,
    default_database as live_orders_database,
    normalize_symbols as normalize_live_symbols,
    prune_live_trades,
)
from tools.mtgLibrary import MtgLibrary, default_database as mtg_database
from tools.newsAggregation import NewsAggregation, default_database as news_database
from tools.openInsider import OpenInsider, default_database as insider_database
from tools.technicalIndicators import (
    INDICATORS,
    SeriesQuery,
    _latest_calculated,
    default_database as indicators_database,
)
from tools.tradeAPI import TradeAPI, default_database as trade_database


def build_historic_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="historic",
        description="Download Alpaca stock or crypto bar history into SQLite.",
    )
    parser.add_argument("symbols", nargs="+", help="symbols or crypto pairs")
    parser.add_argument(
        "--class", dest="asset_class", choices=("stock", "crypto"), required=True
    )
    parser.add_argument("--timeframe", type=normalize_timeframe, default="1Day")
    window = parser.add_mutually_exclusive_group()
    window.add_argument(
        "--start", help="RFC-3339 or YYYY-MM-DD; defaults to 1970-01-01"
    )
    window.add_argument(
        "--weeks",
        type=lambda value: positive_int(value, "weeks"),
        help="retrieve a relative number of weeks ending at --end or now",
    )
    parser.add_argument("--end", help="RFC-3339 or YYYY-MM-DD")
    parser.add_argument("--feed", choices=("iex", "sip", "boats", "otc"), default="iex")
    parser.add_argument(
        "--adjustment", choices=("raw", "split", "dividend", "all"), default="raw"
    )
    parser.add_argument(
        "--location",
        choices=("us", "us-1", "us-2", "eu-1", "bs-1"),
        default="us",
    )
    parser.add_argument("--limit", type=page_limit, default=10000)
    parser.add_argument(
        "--max-pages",
        type=lambda value: positive_int(value, "max-pages"),
        help="optional safety cap; a capped run is recorded as partial",
    )
    parser.add_argument("--database", "-d", type=Path, default=historic_database())
    return parser


def command_historic(argv: list[str] | None = None) -> int:
    parser = build_historic_parser()
    args = parser.parse_args(argv)
    try:
        symbols = normalize_historic_symbols(args.symbols, args.asset_class)
        if args.weeks:
            end_time = (
                parse_timestamp(args.end, "end") if args.end else datetime.now(UTC)
            )
            start = (end_time - timedelta(weeks=args.weeks)).isoformat().replace(
                "+00:00", "Z"
            )
            end = end_time.isoformat().replace("+00:00", "Z")
        else:
            start = args.start or "1970-01-01"
            end = args.end
        result = HistoricData.from_environment(args.database).fetch(
            symbols=symbols,
            asset_class=args.asset_class,
            timeframe=args.timeframe,
            start=start,
            end=end,
            feed=args.feed,
            location=args.location,
            adjustment=args.adjustment,
            limit=args.limit,
            max_pages=args.max_pages,
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        parser.exit(1, f"historic: {exc}\n")
    print(
        f"History sync {result.status}: {result.rows_saved:,} bars "
        f"across {result.pages:,} pages"
    )
    print(f"Database: {result.database}")
    return 0


def command_promptCuration(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="promptCuration",
        description="Write news related to a symbol into an LLM-ready text file.",
    )
    parser.add_argument("symbol")
    parser.add_argument("--database", "-d", type=Path, default=prompt_database())
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


def command_assetEnumeration(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="assetEnumeration",
        description="Evaluate personal assets and update the wealth snapshot.",
    )
    parser.add_argument("--database", "-d", type=Path, default=asset_database())
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("mtgEvaluation")
    commands.add_parser("bitcoinEvaluation")
    commands.add_parser("stocksEvaluation")
    real_estate = commands.add_parser("realEstateEvaluation")
    real_estate.add_argument(
        "spreadsheet",
        type=Path,
        help="LibreOffice Calc .ods inventory with device and price columns",
    )
    real_estate_add = commands.add_parser("realEstateAdd")
    real_estate_add.add_argument("name", help="equipment name")
    real_estate_add.add_argument("price", help="purchase price in USD")
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
        elif args.operation == "stocksEvaluation":
            result = enumeration.stocksEvaluation()
            for position in result.positions:
                print(
                    f"{position.symbol}: {position.quantity} @ "
                    f"${position.current_price:,.2f} = "
                    f"${position.market_value:,.2f}; "
                    f"P/L ${position.unrealized_pl:+,.2f}"
                )
            print(f"Stock positions: {len(result.positions)}")
            print(f"Stocks value: ${result.market_value:,.2f}")
            print(f"Cost basis: ${result.cost_basis:,.2f}")
            print(f"Unrealized P/L: ${result.unrealized_pl:+,.2f}")
            print("Updated wealth.stocks")
        elif args.operation == "realEstateEvaluation":
            result = enumeration.realEstateEvaluation(args.spreadsheet)
            print(f"Imported {result.items} lab equipment items")
            print(f"Real estate value: ${result.value:,.2f}")
            print("Updated wealth.real_estate")
        elif args.operation == "realEstateAdd":
            result = enumeration.realEstateAdd(args.name, args.price)
            print(
                f"Added equipment #{result.item_id}: "
                f"{result.device} for ${result.price:,.2f}"
            )
            print(f"Real estate value: ${result.total_value:,.2f}")
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


def command_liveOrderBooks(argv: list[str] | None = None) -> int:
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
    parser.add_argument(
        "--database", "-d", type=Path, default=live_orders_database()
    )
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
        symbols = normalize_live_symbols(symbol_values)
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


def command_mtgLibrary(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mtgLibrary",
        description="Import a ManaBox library and reserve cards for decks.",
    )
    parser.add_argument("--database", "-d", type=Path, default=mtg_database())
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


def command_newsAggregation(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="newsAggregation",
        description="Collect Alpaca and NewsData.io articles into SQLite.",
    )
    parser.add_argument("symbols", nargs="*", help="optional Alpaca symbol filters")
    parser.add_argument("--alpaca-pages", type=int, default=1)
    parser.add_argument("--database", "-d", type=Path, default=news_database())
    args = parser.parse_args(argv)
    try:
        result = NewsAggregation(args.database).collect(
            args.symbols, max_alpaca_pages=args.alpaca_pages
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        parser.exit(1, f"newsAggregation: {exc}\n")
    print(f"Alpaca articles: {result.alpaca_articles}")
    print(f"NewsData articles: {result.newsdata_articles}")
    print(f"Stored articles: {result.stored_articles}")
    print(f"Database: {args.database}")
    return 0


def command_openInsider(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openInsider",
        description="Store OpenInsider's Latest Insider Buys in SQLite.",
    )
    parser.add_argument(
        "operation",
        nargs="?",
        choices=("homepage", "screener", "prune"),
        default="homepage",
        help="scrape the homepage, run the fixed screener, or prune old filings",
    )
    parser.add_argument("--database", "-d", type=Path, default=insider_database())
    args = parser.parse_args(argv)
    try:
        scraper = OpenInsider(args.database)
        if args.operation == "prune":
            deleted = scraper.prune()
            print(f"Deleted: {deleted}")
            print(f"Database: {args.database}")
            return 0
        result = scraper.scrape_screener() if args.operation == "screener" else scraper.scrape()
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        parser.exit(1, f"openInsider: {exc}\n")
    print(f"Fetched: {result.fetched}")
    print(f"Inserted: {result.inserted}")
    print(f"Updated: {result.updated}")
    print(f"Database: {args.database}")
    return 0


def command_technicalIndicators(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="technicalIndicators",
        description="Calculate technical indicators from stored historic bars.",
    )
    parser.add_argument("symbol")
    parser.add_argument(
        "--indicator", choices=(*INDICATORS, "all"), default="all"
    )
    parser.add_argument(
        "--class", dest="asset_class", choices=("stock", "crypto"), default="stock"
    )
    parser.add_argument("--timeframe", default="1Day")
    parser.add_argument("--feed", default="iex")
    parser.add_argument("--location", default="us")
    parser.add_argument("--adjustment", default="raw")
    parser.add_argument(
        "--database", "-d", type=Path, default=indicators_database()
    )
    args = parser.parse_args(argv)
    query = SeriesQuery(
        database=args.database,
        symbol=args.symbol,
        asset_class=args.asset_class,
        timeframe=args.timeframe,
        feed=args.feed,
        location=args.location,
        adjustment=args.adjustment,
    )
    selected = INDICATORS if args.indicator == "all" else {
        args.indicator: INDICATORS[args.indicator]
    }
    try:
        for name, function in selected.items():
            latest = _latest_calculated(function(query))
            print(f"{name}: {json.dumps(asdict(latest), sort_keys=True)}")
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"technicalIndicators: {exc}\n")
    return 0


def command_tradeAPI(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradeAPI")
    parser.add_argument("--database", "-d", type=Path, default=trade_database())
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


COMMANDS = {
    "historic": command_historic,
    "promptCuration": command_promptCuration,
    "assetEnumeration": command_assetEnumeration,
    "liveOrderBooks": command_liveOrderBooks,
    "mtgLibrary": command_mtgLibrary,
    "newsAggregation": command_newsAggregation,
    "openInsider": command_openInsider,
    "technicalIndicators": command_technicalIndicators,
    "tradeAPI": command_tradeAPI,
}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="df-FinanceTerminal",
        description="Run a DF-FinanceTerminal data, research, or trading module.",
    )
    parser.add_argument("module", choices=tuple(COMMANDS), help="module to invoke")
    if not arguments or arguments[0] in ("-h", "--help"):
        parser.print_help()
        return 0
    module = arguments[0]
    if module not in COMMANDS:
        parser.error(
            f"argument module: invalid choice: {module!r} "
            f"(choose from {', '.join(COMMANDS)})"
        )
    return COMMANDS[module](arguments[1:])


if __name__ == "__main__":
    raise SystemExit(main())
