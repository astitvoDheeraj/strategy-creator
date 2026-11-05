import os
from datetime import date, datetime, timedelta
from math import ceil, isfinite
from typing import Any

import clickhouse_connect
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse


JOB_ID = "23f1962b"
INTERVAL = "daily"
HISTORY_CANDLES = 260
MAX_DAYS = 90
UNIVERSES = ("nifty_500", "fno_stock_futures")
FAMILY_PRIORITY = ("IBL", "DTL", "PBS", "DTS")
FAMILY_DESCRIPTIONS = {
    "IBL": "Inside/NR7 breakout long",
    "DTL": "Dual-timeframe stochastic long",
    "PBS": "Pullback-fade short",
    "DTS": "Dual-timeframe stochastic short",
}
COLUMNS = [
    {"key": "stock", "label": "Stock", "format": "text"},
    {"key": "scan_date", "label": "Date", "format": "date"},
    {"key": "direction", "label": "Direction", "format": "direction"},
    {"key": "signal", "label": "Signal", "format": "text"},
    {"key": "close", "label": "Close", "format": "price"},
    {"key": "stop_price", "label": "Stop", "format": "price"},
    {"key": "target_price", "label": "Target", "format": "price"},
    {"key": "risk_pct", "label": "Risk %", "format": "percent"},
    {"key": "adx", "label": "ADX", "format": "number"},
    {"key": "strength", "label": "Strength", "format": "text"},
    {"key": "regime", "label": "Regime", "format": "text"},
]
PRESENTATION = {
    "style": "table",
    "sort": {"key": "adx", "direction": "desc"},
    "ui_filters": ["symbol_search", "column_sort"],
    "limit": 0,
    "page_size": 25,
    "pagination": "pages",
    "notes": "",
    "group_by_date": True,
    "date_chips": True,
}
PARAMETER_DEFINITIONS = [
    {
        "key": "side",
        "label": "Signals to show",
        "options": [
            {"value": "both", "label": "Long and short"},
            {"value": "long", "label": "Only long"},
            {"value": "short", "label": "Only short"},
        ],
        "default": "both",
    },
    {
        "key": "strength_filter",
        "label": "Signal strength",
        "options": [
            {"value": "all", "label": "All signals"},
            {"value": "strong", "label": "Strong only (top-tercile ADX)"},
        ],
        "default": "all",
    },
    {
        "key": "max_risk_pct",
        "label": "Max stop distance %",
        "options": [
            {"value": "1.5", "label": "1.5%"},
            {"value": "2", "label": "2%"},
            {"value": "2.5", "label": "2.5%"},
            {"value": "3", "label": "3%"},
        ],
        "default": "2",
    },
    {
        "key": "limit",
        "label": "Rows per day",
        "options": [
            {"value": "5", "label": "Top 5"},
            {"value": "10", "label": "Top 10"},
            {"value": "0", "label": "All"},
        ],
        "default": "10",
    },
]
DIRECTION = {
    "enabled": True,
    "buy": "IBL or DTL fires and regime gate allows longs",
    "sell": "PBS or DTS fires and regime gate allows shorts",
    "neutral": "no family fires",
}
router = APIRouter(prefix=f"/strategy-api/{JOB_ID}")


class ScanParameterError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.public_message = message


def _client():
    connection_args = {
        "host": os.getenv("CLICKHOUSE_HOST", "clickhouse"),
        "port": int(os.getenv("CLICKHOUSE_PORT", "8123")),
        "username": os.getenv("CLICKHOUSE_USERNAME", "default"),
        "database": os.getenv("CLICKHOUSE_DATABASE", "default"),
        "secure": os.getenv("CLICKHOUSE_SECURE", "false").strip().lower()
        in {"1", "true", "yes", "on"},
    }
    connection_args["password"] = os.getenv("CLICKHOUSE_PASSWORD", "")
    return clickhouse_connect.get_client(**connection_args)


def _parse_date(value: str | None, name: str) -> date | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ScanParameterError(f"{name} must be a date in YYYY-MM-DD format") from exc
    if parsed.isoformat() != value:
        raise ScanParameterError(f"{name} must be a date in YYYY-MM-DD format")
    return parsed


def _parse_as_of(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError) as exc:
        raise ScanParameterError("as_of must use YYYY-MM-DDTHH:MM format") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M") != value:
        raise ScanParameterError("as_of must use YYYY-MM-DDTHH:MM format")
    return parsed.date()


def _float_or_none(value: float) -> float | None:
    return value if isfinite(value) else None


def _ema(values: list[float], alpha: float, minimum: int) -> list[float | None]:
    result: list[float | None] = []
    current: float | None = None
    count = 0
    for value in values:
        current = value if current is None else alpha * value + (1 - alpha) * current
        count += 1
        result.append(current if count >= minimum else None)
    return result


def _wilder(values: list[float | None], period: int) -> list[float | None]:
    result: list[float | None] = []
    current: float | None = None
    count = 0
    for value in values:
        if value is None:
            result.append(None)
            continue
        current = value if current is None else current + (value - current) / period
        count += 1
        result.append(current if count >= period else None)
    return result


def _rolling_mean(values: list[float | None], period: int) -> list[float | None]:
    result: list[float | None] = []
    window: list[float] = []
    for value in values:
        if value is None:
            window.clear()
            result.append(None)
            continue
        window.append(value)
        if len(window) > period:
            window.pop(0)
        result.append(sum(window) / period if len(window) == period else None)
    return result


def _indicators(rows: list[dict[str, Any]]) -> list[dict[str, float | None]]:
    count = len(rows)
    closes = [float(row["close"]) for row in rows]
    highs = [float(row["high"]) for row in rows]
    lows = [float(row["low"]) for row in rows]
    volumes = [float(row["volume"]) for row in rows]
    sma50 = _rolling_mean(closes, 50)
    sma200 = _rolling_mean(closes, 200)
    ema20 = _ema(closes, 2 / 21, 1)

    changes: list[float | None] = [None]
    gains: list[float | None] = [None]
    losses: list[float | None] = [None]
    true_ranges: list[float | None] = []
    plus_dm: list[float | None] = [None]
    minus_dm: list[float | None] = [None]
    for index in range(count):
        if index == 0:
            true_ranges.append(highs[index] - lows[index])
            continue
        change = closes[index] - closes[index - 1]
        changes.append(change)
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
        up_move = highs[index] - highs[index - 1]
        down_move = lows[index - 1] - lows[index]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

    period = 14
    avg_gain = _wilder(gains, period)
    avg_loss = _wilder(losses, period)
    rsi: list[float | None] = []
    for gain, loss in zip(avg_gain, avg_loss):
        if gain is None or loss is None:
            rsi.append(None)
        elif loss == 0:
            rsi.append(100.0 if gain > 0 else 50.0)
        else:
            rsi.append(100 - 100 / (1 + gain / loss))
    atr = _wilder(true_ranges, period)
    smoothed_plus = _wilder(plus_dm, period)
    smoothed_minus = _wilder(minus_dm, period)
    dx: list[float | None] = []
    for positive, negative in zip(smoothed_plus, smoothed_minus):
        if positive is None or negative is None or positive + negative == 0:
            dx.append(None)
        else:
            dx.append(100 * abs(positive - negative) / (positive + negative))
    adx = _wilder(dx, period)

    fast_k: list[float | None] = []
    for index in range(count):
        start = index - 13
        if start < 0:
            fast_k.append(None)
            continue
        high = max(highs[start : index + 1])
        low = min(lows[start : index + 1])
        fast_k.append(100 * (closes[index] - low) / (high - low) if high > low else None)
    slow_k = _rolling_mean(fast_k, 3)
    slow_d = _rolling_mean(slow_k, 3)
    vol_ma20 = _rolling_mean(volumes, 20)
    turnover = _rolling_mean(
        [float(row["close"]) * float(row["volume"]) for row in rows], 20
    )
    ret63: list[float | None] = [
        closes[index] / closes[index - 63] - 1 if index >= 63 and closes[index - 63] else None
        for index in range(count)
    ]

    return [
        {
            "sma50": sma50[index],
            "sma200": sma200[index],
            "ema20": ema20[index],
            "rsi14": rsi[index],
            "atr14": atr[index],
            "adx14": adx[index],
            "slow_k": slow_k[index],
            "slow_d": slow_d[index],
            "vol_ma20": vol_ma20[index],
            "turnover20": turnover[index],
            "ret63": ret63[index],
        }
        for index in range(count)
    ]


def _weekly_state(rows: list[dict[str, Any]], scan_day: date) -> tuple[bool, bool]:
    weeks: dict[date, dict[str, float]] = {}
    for row in rows:
        day = row["day"]
        week_end = day + timedelta(days=(4 - day.weekday()) % 7)
        if week_end >= scan_day + timedelta(days=(4 - scan_day.weekday()) % 7):
            continue
        current = weeks.setdefault(
            week_end,
            {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            },
        )
        current["high"] = max(current["high"], float(row["high"]))
        current["low"] = min(current["low"], float(row["low"]))
        current["close"] = float(row["close"])

    ordered = [weeks[key] for key in sorted(weeks)]
    if not ordered:
        return False, False
    highs = [week["high"] for week in ordered]
    lows = [week["low"] for week in ordered]
    closes = [week["close"] for week in ordered]
    fast_k: list[float | None] = []
    for index in range(len(ordered)):
        if index < 13:
            fast_k.append(None)
            continue
        high = max(highs[index - 13 : index + 1])
        low = min(lows[index - 13 : index + 1])
        fast_k.append(100 * (closes[index] - low) / (high - low) if high > low else None)
    slow_k = _rolling_mean(fast_k, 3)
    slow_d = _rolling_mean(slow_k, 3)
    if len(ordered) < 1 or slow_k[-1] is None or slow_d[-1] is None:
        return False, False
    return slow_k[-1] > slow_d[-1], slow_k[-1] <= slow_d[-1]


def _signals(
    rows: list[dict[str, Any]],
    indicators: list[dict[str, float | None]],
    weekly_bullish: bool,
    weekly_bearish: bool,
    regime: str,
    long_member: bool,
    short_member: bool,
) -> list[tuple[str, float]]:
    index = len(rows) - 1
    if index < 259:
        return []
    row = rows[index]
    previous = rows[index - 1]
    before_previous = rows[index - 2]
    today = indicators[index]
    yesterday = indicators[index - 1]
    required = ("sma50", "sma200", "rsi14", "atr14", "adx14", "slow_k", "slow_d",
                "vol_ma20", "turnover20")
    if any(today[key] is None for key in required) or yesterday["slow_k"] is None:
        return []
    if today["turnover20"] < 10_000_000:
        return []
    close = float(row["close"])
    previous_high = float(previous["high"])
    previous_low = float(previous["low"])
    uptrend = close > today["sma200"] and today["sma50"] > today["sma200"]
    downtrend = close < today["sma200"] and today["sma50"] < today["sma200"]
    long_allowed = regime != "bear" and regime != "missing"
    short_allowed = regime != "bull" and regime != "missing"
    families: list[tuple[str, float]] = []

    yesterday_inside = (
        float(previous["high"]) <= float(before_previous["high"])
        and float(previous["low"]) >= float(before_previous["low"])
    )
    previous_range = float(previous["high"]) - float(previous["low"])
    previous_nr7 = all(
        previous_range <= float(item["high"]) - float(item["low"])
        for item in rows[index - 7 : index]
    )
    if (
        long_member
        and long_allowed
        and (yesterday_inside or previous_nr7)
        and close > previous_high
        and today["vol_ma20"] > 0
        and float(row["volume"]) > 1.3 * today["vol_ma20"]
        and today["ret63"] is not None
        and today["ret63"] > 0
        and uptrend
    ):
        families.append(("IBL", float(previous["low"]) - 0.25 * today["atr14"]))
    if (
        long_member
        and long_allowed
        and weekly_bullish
        and yesterday["slow_k"] <= yesterday["slow_d"]
        and today["slow_k"] > today["slow_d"]
        and yesterday["slow_k"] < 30
        and close > previous_high
    ):
        families.append(("DTL", float(row["low"]) - 0.5 * today["atr14"]))
    if (
        short_member
        and short_allowed
        and downtrend
        and float(row["high"]) >= today["ema20"]
        and close < float(row["open"])
        and close < previous_low
        and yesterday["rsi14"] is not None
        and yesterday["rsi14"] > 50
    ):
        families.append(("PBS", float(row["high"]) + 0.5 * today["atr14"]))
    if (
        short_member
        and short_allowed
        and weekly_bearish
        and yesterday["slow_k"] >= yesterday["slow_d"]
        and yesterday["slow_k"] > 70
        and today["slow_k"] < today["slow_d"]
        and close < previous_low
        and downtrend
    ):
        families.append(("DTS", float(row["high"]) + 0.5 * today["atr14"]))
    return families


def _fetch_candles(client: Any, symbols: list[str], target: date) -> dict[str, list[dict[str, Any]]]:
    if not symbols:
        return {}
    result = client.query(
        "SELECT stock, day, tupleElement(candle, 1) AS open, "
        "tupleElement(candle, 2) AS high, tupleElement(candle, 3) AS low, "
        "tupleElement(candle, 4) AS close, tupleElement(candle, 5) AS volume "
        "FROM ("
        "SELECT stock, toDate(date) AS day, "
        "argMax(tuple(open, high, low, close, volume), date) AS candle "
        "FROM stock__candles_data "
        "WHERE interval = {interval:String} "
        "AND stock IN {symbols:Array(String)} "
        "AND toDate(date) <= toDate({target:String}) "
        "GROUP BY stock, day ORDER BY stock, day DESC LIMIT 260 BY stock"
        ") ORDER BY stock, day",
        parameters={
            "interval": INTERVAL,
            "symbols": symbols,
            "target": target.isoformat(),
        },
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for stock, day, open_price, high, low, close, volume in result.result_rows:
        if isinstance(day, datetime):
            day = day.date()
        elif not isinstance(day, date):
            day = date.fromisoformat(str(day))
        grouped.setdefault(str(stock), []).append(
            {
                "day": day,
                "open": float(open_price),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volume),
            }
        )
    return grouped


def _normalize_parameters(params: dict[str, Any]) -> dict[str, Any]:
    side = str(params.get("side", "both")).lower()
    if side not in {"both", "long", "short"}:
        side = "both"
    strength_filter = str(params.get("strength_filter", "all")).lower()
    if strength_filter not in {"all", "strong"}:
        strength_filter = "all"
    try:
        max_risk_pct = str(float(params.get("max_risk_pct", "2")))
        if max_risk_pct.endswith(".0"):
            max_risk_pct = max_risk_pct[:-2]
        if max_risk_pct not in {"1.5", "2", "2.5", "3"}:
            max_risk_pct = "2"
    except (TypeError, ValueError):
        max_risk_pct = "2"
    try:
        limit = int(params.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    if limit not in {0, 5, 10}:
        limit = 10
    try:
        min_risk_pct = float(params.get("min_risk_pct", 0.5))
    except (TypeError, ValueError):
        min_risk_pct = 0.5
    if not isfinite(min_risk_pct) or min_risk_pct < 0:
        min_risk_pct = 0.5
    sort_by = str(params.get("sort_by") or "adx")
    if sort_by not in {column["key"] for column in COLUMNS}:
        sort_by = "adx"
    sort_dir = str(params.get("sort_dir") or "desc").lower()
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "desc"
    return {
        "side": side,
        "strength_filter": strength_filter,
        "max_risk_pct": max_risk_pct,
        "limit": limit,
        "min_risk_pct": min_risk_pct,
        "sort_by": sort_by,
        "sort_dir": sort_dir,
    }


def _sort_value(row: dict[str, Any], key: str) -> Any:
    value = row[key]
    return value.casefold() if isinstance(value, str) else value


def run_scan(params: dict[str, Any], metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    applied = _normalize_parameters(params)
    from_date = _parse_date(params.get("from_date"), "from_date")
    to_date = _parse_date(params.get("to_date"), "to_date")
    as_of = _parse_as_of(params.get("as_of"))
    requested_symbols = params.get("symbols")
    if isinstance(requested_symbols, str):
        requested_symbols = [item.strip() for item in requested_symbols.split(",") if item.strip()]
    if requested_symbols is not None and not requested_symbols:
        raise ScanParameterError("symbols must contain at least one symbol")

    client = _client()
    try:
        universe_result = client.query(
            "SELECT DISTINCT symbol FROM universe_members FINAL "
            "WHERE universe_id IN {universes:Array(String)} AND active = 1",
            parameters={"universes": list(UNIVERSES)},
        )
        long_symbols: set[str] = set()
        short_symbols: set[str] = set()
        for symbol, universe in universe_result.result_rows:
            if universe == "nifty_500" or universe == "fno_stock_futures":
                long_symbols.add(str(symbol))
            if universe == "fno_stock_futures":
                short_symbols.add(str(symbol))
        # Some deployments return one distinct symbol per universe query row.
        if not long_symbols and universe_result.result_rows:
            long_symbols = {str(row[0]) for row in universe_result.result_rows}
            short_symbols = set(long_symbols)
        universe_symbols = long_symbols | short_symbols
        if requested_symbols is not None:
            requested = set(requested_symbols)
            long_symbols &= requested
            short_symbols &= requested
            universe_symbols &= requested
        if not universe_symbols:
            raise ScanParameterError("No active symbols found in the selected universes")

        as_of_filter = "AND toDate(date) <= toDate({as_of:String}) " if as_of else ""
        date_parameters: dict[str, Any] = {}
        if as_of:
            date_parameters["as_of"] = as_of.isoformat()
        dates_result = client.query(
            "SELECT DISTINCT toDate(date) AS trading_date "
            "FROM stock__candles_data WHERE interval = {interval:String} "
            "AND stock IN {symbols:Array(String)} "
            f"{as_of_filter}ORDER BY trading_date DESC",
            parameters={
                "interval": INTERVAL,
                "symbols": sorted(universe_symbols),
                **date_parameters,
            },
        )
        trading_dates = []
        for (day,) in dates_result.result_rows:
            if isinstance(day, datetime):
                trading_dates.append(day.date())
            elif isinstance(day, date):
                trading_dates.append(day)
            else:
                trading_dates.append(date.fromisoformat(str(day)))
        trading_dates = sorted(set(trading_dates), reverse=True)
        if not trading_dates:
            raise ScanParameterError("No daily candles found for the selected universe")
        latest_date = trading_dates[0]
        earliest_date = min(trading_dates)
        if to_date is None:
            to_date = latest_date
        if from_date is None:
            eligible = [day for day in trading_dates if day <= to_date]
            from_date = eligible[0] if eligible else to_date
        if from_date > to_date:
            raise ScanParameterError("from_date must be on or before to_date")
        if (to_date - from_date).days + 1 > MAX_DAYS:
            raise ScanParameterError("date range must not exceed 90 calendar days")
        applied_dates = [day for day in trading_dates if from_date <= day <= to_date]

        all_rows: list[dict[str, Any]] = []
        total = 0
        for scan_day in applied_dates:
            candle_data = _fetch_candles(client, sorted(universe_symbols), scan_day)
            benchmark = _fetch_candles(client, ["NIFTY 50"], scan_day).get("NIFTY 50", [])
            benchmark_regime = "missing"
            if len(benchmark) >= HISTORY_CANDLES and benchmark[-1]["day"] == scan_day:
                benchmark_indicators = _indicators(benchmark)
                latest_benchmark = benchmark_indicators[-1]
                close = benchmark[-1]["close"]
                if latest_benchmark["sma50"] is not None and latest_benchmark["ema20"] is not None:
                    if close > latest_benchmark["sma50"] and close > latest_benchmark["ema20"]:
                        benchmark_regime = "bull"
                    elif close < latest_benchmark["sma50"] and close < latest_benchmark["ema20"]:
                        benchmark_regime = "bear"
                    else:
                        benchmark_regime = "neutral"

            day_rows: list[dict[str, Any]] = []
            for stock, descending_rows in candle_data.items():
                rows = sorted(descending_rows, key=lambda candle: candle["day"])
                if len(rows) < HISTORY_CANDLES or rows[-1]["day"] != scan_day:
                    continue
                indicators = _indicators(rows)
                weekly_bullish, weekly_bearish = _weekly_state(rows, scan_day)
                candidates = _signals(
                    rows,
                    indicators,
                    weekly_bullish,
                    weekly_bearish,
                    benchmark_regime,
                    stock in long_symbols,
                    stock in short_symbols,
                )
                if not candidates:
                    continue
                adx = indicators[-1]["adx14"]
                close = rows[-1]["close"]
                family_stops = {family: stop for family, stop in candidates}
                family = min(candidates, key=lambda item: FAMILY_PRIORITY.index(item[0]))[0]
                stop = family_stops[family]
                risk_pct = abs(close - stop) / close * 100 if close else float("inf")
                if not isfinite(risk_pct) or risk_pct < applied["min_risk_pct"] or risk_pct > float(applied["max_risk_pct"]):
                    continue
                direction = "BUY" if family in {"IBL", "DTL"} else "SELL"
                target = close + 2 * (close - stop) if direction == "BUY" else close - 1.5 * (stop - close)
                day_rows.append(
                    {
                        "stock": stock,
                        "scan_date": scan_day.isoformat(),
                        "direction": direction,
                        "signal": family,
                        "close": close,
                        "stop_price": stop,
                        "target_price": target,
                        "risk_pct": round(risk_pct, 2),
                        "adx": round(float(adx), 2),
                        "strength": "normal",
                        "regime": benchmark_regime,
                    }
                )

            strength_count = ceil(len(day_rows) / 3)
            strong_stocks = {
                row["stock"]
                for row in sorted(day_rows, key=lambda item: (-item["adx"], item["stock"]))[:strength_count]
            }
            for row in day_rows:
                row["strength"] = "strong" if row["stock"] in strong_stocks else "normal"
            if applied["side"] == "long":
                day_rows = [row for row in day_rows if row["direction"] == "BUY"]
            elif applied["side"] == "short":
                day_rows = [row for row in day_rows if row["direction"] == "SELL"]
            if applied["strength_filter"] == "strong":
                day_rows = [row for row in day_rows if row["strength"] == "strong"]
            day_rows.sort(
                key=lambda row: (
                    -_sort_value(row, applied["sort_by"])
                    if applied["sort_dir"] == "desc"
                    else _sort_value(row, applied["sort_by"]),
                    row["stock"],
                )
            )
            total += len(day_rows)
            all_rows.extend(day_rows if applied["limit"] == 0 else day_rows[: applied["limit"]])

        if metadata is not None:
            metadata.update(
                {
                    "total": total,
                    "from_date": from_date.isoformat(),
                    "to_date": to_date.isoformat(),
                    "min_date": earliest_date.isoformat(),
                    "max_date": latest_date.isoformat(),
                    "dates": [day.isoformat() for day in applied_dates],
                    "applied": applied,
                    "criteria": (
                        "Daily IBL, DTL, PBS, and DTS signals; long universe is "
                        "NIFTY 500 plus F&O stock futures, short universe is F&O stock "
                        "futures; 20-day average turnover >= INR 1 crore; at least 260 "
                        "daily candles; NIFTY 50 regime gate; "
                        f"side={applied['side']}, strength_filter={applied['strength_filter']}, "
                        f"risk_pct=[{applied['min_risk_pct']:g},"
                        f"{applied['max_risk_pct']}], limit={applied['limit']} per date; "
                        f"dates {from_date.isoformat()} through {to_date.isoformat()}"
                    ),
                }
            )
        return all_rows
    finally:
        client.close()


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/scan")
def scan(
    from_date: str | None = None,
    to_date: str | None = None,
    as_of: str | None = None,
    symbols: str | None = None,
    side: str = "both",
    strength_filter: str = "all",
    max_risk_pct: str = "2",
    min_risk_pct: str = "0.5",
    limit: str = "10",
    sort_by: str | None = None,
    sort_dir: str | None = None,
):
    params = {
        "from_date": from_date,
        "to_date": to_date,
        "as_of": as_of,
        "symbols": symbols,
        "side": side,
        "strength_filter": strength_filter,
        "max_risk_pct": max_risk_pct,
        "min_risk_pct": min_risk_pct,
        "limit": limit,
        "sort_by": sort_by,
        "sort_dir": sort_dir,
    }
    metadata: dict[str, Any] = {}
    try:
        matches = run_scan(params, metadata)
    except ScanParameterError as exc:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": exc.public_message},
        )
    parameters = [
        {**definition, "value": str(metadata["applied"][definition["key"]])}
        for definition in PARAMETER_DEFINITIONS
    ]
    return {
        "success": True,
        "criteria": metadata["criteria"],
        "columns": COLUMNS,
        "presentation": PRESENTATION,
        "parameters": parameters,
        "date_range": {
            "mode": "range",
            "from_date": metadata["from_date"],
            "to_date": metadata["to_date"],
            "default_days": 1,
            "max_days": MAX_DAYS,
            "min_date": metadata["min_date"],
            "max_date": metadata["max_date"],
            "dates": metadata["dates"],
        },
        "direction": DIRECTION,
        "total": metadata["total"],
        "matches": matches,
    }


app = FastAPI(title="Dual Momentum Swing")
app.include_router(router)
