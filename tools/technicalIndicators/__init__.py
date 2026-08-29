#!/usr/bin/env python3
"""Calculate seven technical indicators from stored OHLCV bars without NumPy."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class SeriesQuery:
    database: Path | str
    symbol: str
    asset_class: str = "stock"
    timeframe: str = "1Day"
    feed: str = "iex"
    location: str = "us"
    adjustment: str = "raw"


@dataclass(frozen=True)
class Bar:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class IndicatorValue:
    timestamp: str
    value: float | None


@dataclass(frozen=True)
class ADXValue:
    timestamp: str
    adx: float | None
    positive_di: float | None
    negative_di: float | None


@dataclass(frozen=True)
class AroonValue:
    timestamp: str
    up: float | None
    down: float | None


@dataclass(frozen=True)
class MACDValue:
    timestamp: str
    macd: float | None
    signal: float | None
    histogram: float | None


@dataclass(frozen=True)
class StochasticValue:
    timestamp: str
    percent_k: float | None
    percent_d: float | None


def _period(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _finite(name: str, index: int, value: float) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name}[{index}] must be finite")
    return result


def _load_bars(query: SeriesQuery) -> tuple[Bar, ...]:
    if query.asset_class not in {"stock", "crypto"}:
        raise ValueError("asset_class must be stock or crypto")
    database = Path(query.database).expanduser()
    if not database.is_file():
        raise ValueError(f"database does not exist: {database}")
    feed = query.feed if query.asset_class == "stock" else ""
    location = query.location if query.asset_class == "crypto" else ""
    adjustment = query.adjustment if query.asset_class == "stock" else ""
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT timestamp, open, high, low, close, volume
            FROM historic_bars
            WHERE asset_class = ? AND symbol = ? AND timeframe = ?
              AND feed = ? AND location = ? AND adjustment = ?
            ORDER BY timestamp
            """,
            (
                query.asset_class,
                query.symbol.upper(),
                query.timeframe,
                feed,
                location,
                adjustment,
            ),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        raise ValueError(
            f"no stored {query.asset_class} bars for "
            f"{query.symbol.upper()} {query.timeframe}"
        )
    bars: list[Bar] = []
    for index, row in enumerate(rows):
        bar = Bar(
            timestamp=str(row[0]),
            open=_finite("open", index, row[1]),
            high=_finite("high", index, row[2]),
            low=_finite("low", index, row[3]),
            close=_finite("close", index, row[4]),
            volume=_finite("volume", index, row[5]),
        )
        if bar.high < bar.low or not bar.low <= bar.close <= bar.high:
            raise ValueError(f"stored OHLC values are invalid at {bar.timestamp}")
        if bar.volume < 0:
            raise ValueError(f"stored volume is negative at {bar.timestamp}")
        bars.append(bar)
    return tuple(bars)


def _ema(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = math.fsum(values[:period]) / period
    result[period - 1] = current
    multiplier = 2.0 / (period + 1)
    for index in range(period, len(values)):
        current += (values[index] - current) * multiplier
        result[index] = current
    return result


def on_balance_volume(query: SeriesQuery) -> tuple[IndicatorValue, ...]:
    """Return cumulative OBV, adding or subtracting volume by close direction."""
    bars = _load_bars(query)
    total = 0.0
    result = [IndicatorValue(bars[0].timestamp, total)]
    for previous, current in zip(bars, bars[1:]):
        if current.close > previous.close:
            total += current.volume
        elif current.close < previous.close:
            total -= current.volume
        result.append(IndicatorValue(current.timestamp, total))
    return tuple(result)


def accumulation_distribution_line(
    query: SeriesQuery,
) -> tuple[IndicatorValue, ...]:
    """Return Chaikin's cumulative Accumulation/Distribution Line."""
    bars = _load_bars(query)
    total = 0.0
    result: list[IndicatorValue] = []
    for bar in bars:
        spread = bar.high - bar.low
        multiplier = (
            0.0
            if spread == 0
            else ((bar.close - bar.low) - (bar.high - bar.close)) / spread
        )
        total += multiplier * bar.volume
        result.append(IndicatorValue(bar.timestamp, total))
    return tuple(result)


def average_directional_index(
    query: SeriesQuery, period: int = 14
) -> tuple[ADXValue, ...]:
    """Return Wilder ADX together with positive and negative DI lines."""
    _period("period", period)
    bars = _load_bars(query)
    length = len(bars)
    true_range = [0.0] * length
    positive_dm = [0.0] * length
    negative_dm = [0.0] * length
    for index in range(1, length):
        current = bars[index]
        previous = bars[index - 1]
        true_range[index] = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        upward = current.high - previous.high
        downward = previous.low - current.low
        positive_dm[index] = upward if upward > downward and upward > 0 else 0.0
        negative_dm[index] = downward if downward > upward and downward > 0 else 0.0

    positive_di: list[float | None] = [None] * length
    negative_di: list[float | None] = [None] * length
    dx: list[float | None] = [None] * length
    adx: list[float | None] = [None] * length
    if length > period:
        smoothed_tr = math.fsum(true_range[1 : period + 1])
        smoothed_positive = math.fsum(positive_dm[1 : period + 1])
        smoothed_negative = math.fsum(negative_dm[1 : period + 1])
        for index in range(period, length):
            if index > period:
                smoothed_tr += true_range[index] - smoothed_tr / period
                smoothed_positive += positive_dm[index] - smoothed_positive / period
                smoothed_negative += negative_dm[index] - smoothed_negative / period
            if smoothed_tr == 0:
                positive_di[index] = negative_di[index] = dx[index] = 0.0
            else:
                positive_di[index] = 100.0 * smoothed_positive / smoothed_tr
                negative_di[index] = 100.0 * smoothed_negative / smoothed_tr
                directional_total = positive_di[index] + negative_di[index]
                dx[index] = (
                    0.0
                    if directional_total == 0
                    else 100.0
                    * abs(positive_di[index] - negative_di[index])
                    / directional_total
                )
        first_adx = 2 * period - 1
        if length > first_adx:
            current_adx = math.fsum(
                value for value in dx[period : first_adx + 1] if value is not None
            ) / period
            adx[first_adx] = current_adx
            for index in range(first_adx + 1, length):
                current_adx = (
                    (period - 1) * current_adx + float(dx[index])
                ) / period
                adx[index] = current_adx
    return tuple(
        ADXValue(bar.timestamp, adx[index], positive_di[index], negative_di[index])
        for index, bar in enumerate(bars)
    )


def aroon_indicator(
    query: SeriesQuery, period: int = 25
) -> tuple[AroonValue, ...]:
    """Return Aroon Up and Down based on time since rolling price extremes."""
    _period("period", period)
    bars = _load_bars(query)
    result: list[AroonValue] = []
    for index, bar in enumerate(bars):
        if index < period:
            result.append(AroonValue(bar.timestamp, None, None))
            continue
        window = bars[index - period : index + 1]
        highest = max(item.high for item in window)
        lowest = min(item.low for item in window)
        high_index = max(i for i, item in enumerate(window) if item.high == highest)
        low_index = max(i for i, item in enumerate(window) if item.low == lowest)
        result.append(
            AroonValue(
                bar.timestamp,
                100.0 * high_index / period,
                100.0 * low_index / period,
            )
        )
    return tuple(result)


def moving_average_convergence_divergence(
    query: SeriesQuery,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[MACDValue, ...]:
    """Return MACD, its signal EMA, and the MACD histogram."""
    for name, value in (
        ("fast_period", fast_period),
        ("slow_period", slow_period),
        ("signal_period", signal_period),
    ):
        _period(name, value)
    if fast_period >= slow_period:
        raise ValueError("fast_period must be less than slow_period")
    bars = _load_bars(query)
    closes = [bar.close for bar in bars]
    fast = _ema(closes, fast_period)
    slow = _ema(closes, slow_period)
    macd_line = [
        None if fast_value is None or slow_value is None else fast_value - slow_value
        for fast_value, slow_value in zip(fast, slow)
    ]
    available = [value for value in macd_line if value is not None]
    available_signal = _ema(available, signal_period)
    signal: list[float | None] = [None] * len(bars)
    for index, value in enumerate(available_signal, start=slow_period - 1):
        signal[index] = value
    return tuple(
        MACDValue(
            bar.timestamp,
            macd_line[index],
            signal[index],
            None
            if macd_line[index] is None or signal[index] is None
            else macd_line[index] - signal[index],
        )
        for index, bar in enumerate(bars)
    )


def relative_strength_index(
    query: SeriesQuery, period: int = 14
) -> tuple[IndicatorValue, ...]:
    """Return Wilder RSI comparing smoothed recent gains and losses."""
    _period("period", period)
    bars = _load_bars(query)
    values: list[float | None] = [None] * len(bars)
    if len(bars) > period:
        changes = [
            bars[index].close - bars[index - 1].close
            for index in range(1, len(bars))
        ]
        average_gain = math.fsum(max(change, 0.0) for change in changes[:period]) / period
        average_loss = math.fsum(max(-change, 0.0) for change in changes[:period]) / period

        def current_rsi() -> float:
            if average_loss == 0:
                return 100.0 if average_gain > 0 else 50.0
            return 100.0 - 100.0 / (1.0 + average_gain / average_loss)

        values[period] = current_rsi()
        for index in range(period + 1, len(bars)):
            change = changes[index - 1]
            average_gain = (
                (period - 1) * average_gain + max(change, 0.0)
            ) / period
            average_loss = (
                (period - 1) * average_loss + max(-change, 0.0)
            ) / period
            values[index] = current_rsi()
    return tuple(
        IndicatorValue(bar.timestamp, values[index])
        for index, bar in enumerate(bars)
    )


def stochastic_oscillator(
    query: SeriesQuery, k_period: int = 14, d_period: int = 3
) -> tuple[StochasticValue, ...]:
    """Return stochastic %K and its %D simple moving average."""
    _period("k_period", k_period)
    _period("d_period", d_period)
    bars = _load_bars(query)
    percent_k: list[float | None] = [None] * len(bars)
    percent_d: list[float | None] = [None] * len(bars)
    for index in range(k_period - 1, len(bars)):
        window = bars[index - k_period + 1 : index + 1]
        highest = max(bar.high for bar in window)
        lowest = min(bar.low for bar in window)
        spread = highest - lowest
        percent_k[index] = (
            0.0
            if spread == 0
            else min(100.0, max(0.0, 100.0 * (bars[index].close - lowest) / spread))
        )
        if index >= k_period + d_period - 2:
            percent_d[index] = math.fsum(
                float(value)
                for value in percent_k[index - d_period + 1 : index + 1]
            ) / d_period
    return tuple(
        StochasticValue(bar.timestamp, percent_k[index], percent_d[index])
        for index, bar in enumerate(bars)
    )


INDICATORS = {
    "obv": on_balance_volume,
    "adl": accumulation_distribution_line,
    "adx": average_directional_index,
    "aroon": aroon_indicator,
    "macd": moving_average_convergence_divergence,
    "rsi": relative_strength_index,
    "stochastic": stochastic_oscillator,
}


def default_database() -> Path:
    configured = os.environ.get("FINANCE_DB_FILE") or os.environ.get("MTG_DB_FILE")
    if configured:
        return Path(configured).expanduser()
    server_database = Path("/opt/mtg/mtg.db")
    if server_database.exists():
        return server_database
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "df-financeterminal" / "mtg.db"


def _latest_calculated(series: tuple) -> object:
    for point in reversed(series):
        values = tuple(asdict(point).values())[1:]
        if any(value is not None for value in values):
            return point
    return series[-1]


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument("--database", "-d", type=Path, default=default_database())
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


if __name__ == "__main__":
    raise SystemExit(main())
