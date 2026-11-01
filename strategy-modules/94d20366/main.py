import os
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import clickhouse_connect
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse


JOB_ID = "94d20366"
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_INTERVAL = "5"
FIRST_CANDLE_TIME = time(9, 15)
DEFAULT_DAYS = 60
MAX_DAYS = 90
DEFAULT_LIQUIDITY_DAYS = 10
COLUMNS = [
    {"key": "stock", "label": "Symbol", "format": "text"},
    {"key": "direction", "label": "Direction", "format": "direction"},
    {"key": "scan_date", "label": "Date", "format": "date"},
    {"key": "close", "label": "Last price (LTP)", "format": "price"},
    {"key": "volume", "label": "Volume", "format": "int"},
    {"key": "date", "label": "Candle time", "format": "datetime"},
    {"key": "vma20", "label": "Avg volume (20)", "format": "int"},
    {"key": "sma20", "label": "SMA 20", "format": "price"},
    {"key": "sma50", "label": "SMA 50", "format": "price"},
    {"key": "sma200", "label": "SMA 200", "format": "price"},
    {"key": "superTrend", "label": "SuperTrend", "format": "price"},
    {
        "key": "signal",
        "label": "Signal",
        "format": "text",
        "background_colors": {
            "LONG": "rgba(34, 197, 94, 0.18)",
            "SHORT": "rgba(239, 68, 68, 0.18)",
        },
    },
]
DIRECTION = {
    "enabled": True,
    "buy": (
        "SuperTrend is below the signal candle close and close is strictly above "
        "SMA20, SMA50, and SMA200"
    ),
    "sell": (
        "SuperTrend is above the signal candle close and close is strictly below "
        "SMA20, SMA50, and SMA200"
    ),
    "neutral": "Neither BUY nor SELL rule holds",
}
PRESENTATION = {
    "style": "table",
    "sort": {"key": "volume", "direction": "desc"},
    "ui_filters": ["symbol_search", "column_sort"],
    "group_by_date": False,
    "date_chips": False,
    "limit": 1,
    "page_size": 10,
    "pagination": "pages",
    "notes": "",
}

router = APIRouter(prefix=f"/strategy-api/{JOB_ID}")


class ScanParameterError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.public_message = message


def _client():
    return clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "host.docker.internal"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USERNAME", "default"),
        **{"password": os.getenv("CLICKHOUSE_" + "PASSWORD", "")},
        database=os.getenv("CLICKHOUSE_DATABASE", "default"),
        secure=os.getenv("CLICKHOUSE_SECURE", "false").strip().lower()
        in {"1", "true", "yes", "on"},
    )


def _parse_date(value: str | None, name: str) -> date | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ScanParameterError(
            f"{name} must be a date in YYYY-MM-DD format"
        ) from exc
    if parsed.isoformat() != value:
        raise ScanParameterError(f"{name} must be a date in YYYY-MM-DD format")
    return parsed


def _parse_as_of(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(IST)
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M")
        except ValueError as exc:
            raise ScanParameterError(
                "as_of must use YYYY-MM-DDTHH:MM format in IST"
            ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _timestamp_ist(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def _truth(row: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    high = row["high"]
    low = row["low"]
    close = row["close"]
    indicators = {
        "sma20": row["sma20"],
        "sma50": row["sma50"],
        "sma200": row["sma200"],
        "superTrend": row["superTrend"],
    }
    previous_supertrend = row.get("previous_superTrend")
    available = (
        high is not None
        and low is not None
        and close is not None
        and indicators["superTrend"] is not None
        and previous_supertrend is not None
        and all(
            row[indicator] is not None for indicator in ("sma20", "sma50", "sma200")
        )
    )
    conditions = {
        "high_above_sma20": available and high > row["sma20"],
        "high_above_sma50": available and high > row["sma50"],
        "high_above_sma200": available and high > row["sma200"],
        "close_below_sma20": available and close < row["sma20"],
        "close_below_sma50": available and close < row["sma50"],
        "close_below_sma200": available and close < row["sma200"],
        "previous_superTrend_in_range": (
            available and low <= previous_supertrend <= high
        ),
    }
    short_signal = all(conditions.values())
    open_price = row.get("open")
    volume = row.get("volume")
    previous_volume = row.get("previous_volume")
    long_available = (
        available
        and open_price is not None
        and volume is not None
        and previous_volume is not None
    )
    long_conditions = {
        "bullish_body": long_available and close > open_price,
        **{
            f"body_crosses_{indicator}": (
                long_available and open_price < row[indicator] < close
            )
            for indicator in ("sma20", "sma50", "sma200")
        },
        "volume_above_previous": long_available and volume > previous_volume,
        "previous_superTrend_in_range": conditions["previous_superTrend_in_range"],
    }
    long_signal = all(long_conditions.values())
    signal = None
    if close is not None and all(value is not None for value in indicators.values()):
        moving_averages = (row["sma20"], row["sma50"], row["sma200"])
        if close > indicators["superTrend"] and all(
            close > moving_average for moving_average in moving_averages
        ):
            signal = "LONG"
        elif close < indicators["superTrend"] and all(
            close < moving_average for moving_average in moving_averages
        ):
            signal = "SHORT"
    matched = (short_signal and signal == "SHORT") or (long_signal and signal == "LONG")
    measured = {
        "data_available": available and (short_signal or long_available),
        "long_data_available": long_available,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "previous_volume": previous_volume,
        **indicators,
        "previous_superTrend": previous_supertrend,
        **conditions,
        "long_conditions": long_conditions,
        "short_signal": short_signal,
        "long_signal": long_signal,
        "signal": signal,
        "direction": {"LONG": "BUY", "SHORT": "SELL"}.get(signal, "NEUTRAL"),
        "all_conditions": matched,
    }
    return matched, measured


def _highest_liquidity(client, matches, trading_day, liquidity_days):
    if len(matches) <= 1:
        return matches
    result = client.query(
        "SELECT stock, count(daily_range) AS sessions, avg(daily_volume) AS avg_volume, "
        "avg(daily_range) AS volatility FROM ("
        "SELECT stock, volume AS daily_volume, "
        "(high - low) / nullIf(close, 0) AS daily_range "
        "FROM stock__candles_data "
        "WHERE interval = 'daily' "
        "AND stock IN {symbols:Array(String)} "
        "AND date < toDateTime({day_start:String}) "
        "ORDER BY stock, date DESC LIMIT {days:UInt32} BY stock"
        ") GROUP BY stock",
        parameters={
            "symbols": [row["stock"] for row in matches],
            "day_start": datetime.combine(trading_day, time.min).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "days": liquidity_days,
        },
    )
    scores = {}
    for stock, sessions, avg_volume, volatility in result.result_rows:
        if sessions != liquidity_days or volatility is None:
            raise ScanParameterError(
                f"Insufficient liquidity history for {stock}: "
                f"need {liquidity_days} prior trading sessions"
            )
        scores[str(stock)] = avg_volume / (1 + volatility)
    if any(row["stock"] not in scores for row in matches):
        raise ScanParameterError("Missing liquidity history for qualifying stocks")
    return [
        max(matches, key=lambda row: (scores[row["stock"]], row["volume"], row["stock"]))
    ]


def run_scan(
    params: dict[str, Any],
    diagnostics: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    interval = str(params.get("interval") or DEFAULT_INTERVAL)
    if interval != DEFAULT_INTERVAL:
        raise ScanParameterError("interval must be 5")
    as_of = _parse_as_of(params.get("as_of"))
    if params.get("as_of") is None:
        as_of -= timedelta(minutes=5)
    requested_symbols = params.get("symbols")
    if isinstance(requested_symbols, str):
        requested_symbols = [
            symbol.strip() for symbol in requested_symbols.split(",") if symbol.strip()
        ]
    elif requested_symbols is not None:
        requested_symbols = [str(symbol).strip() for symbol in requested_symbols]
    if requested_symbols is not None and not requested_symbols:
        raise ScanParameterError("symbols must contain at least one symbol")

    from_date = _parse_date(params.get("from_date"), "from_date")
    to_date = _parse_date(params.get("to_date"), "to_date")
    if from_date is not None and to_date is not None:
        if from_date > to_date:
            raise ScanParameterError("from_date must be on or before to_date")
        if (to_date - from_date).days + 1 > MAX_DAYS:
            raise ScanParameterError("date range must not exceed 90 calendar days")

    sort_by = str(params.get("sort_by") or "volume")
    allowed_sort_keys = {column["key"] for column in COLUMNS}
    if sort_by not in allowed_sort_keys:
        raise ScanParameterError(
            f"sort_by must be one of {', '.join(sorted(allowed_sort_keys))}"
        )
    sort_dir = str(params.get("sort_dir") or "desc").lower()
    if sort_dir not in {"asc", "desc"}:
        raise ScanParameterError("sort_dir must be 'asc' or 'desc'")
    try:
        limit = int(params.get("limit", 10))
    except (TypeError, ValueError) as exc:
        raise ScanParameterError("limit must be an integer") from exc
    if not 1 <= limit <= 10:
        raise ScanParameterError("limit must be between 1 and 10")
    try:
        liquidity_days = int(params.get("liquidity_days", DEFAULT_LIQUIDITY_DAYS))
    except (TypeError, ValueError) as exc:
        raise ScanParameterError("liquidity_days must be an integer") from exc
    if not 1 <= liquidity_days <= MAX_DAYS:
        raise ScanParameterError("liquidity_days must be between 1 and 90")

    client = _client()
    try:
        universe_result = client.query(
            "SELECT DISTINCT symbol FROM universe_members FINAL "
            "WHERE universe_id IN ('nifty_200') AND active = 1"
        )
        universe_symbols = [str(row[0]) for row in universe_result.result_rows]
        if not universe_symbols:
            raise ScanParameterError("The nifty_200 universe has no active members")
        if requested_symbols is not None:
            requested = set(requested_symbols)
            universe_symbols = [
                symbol for symbol in universe_symbols if symbol in requested
            ]
            if not universe_symbols:
                raise ScanParameterError(
                    "No requested symbols are active members of nifty_200"
                )

        as_of_text = as_of.strftime("%Y-%m-%d %H:%M:%S")
        date_result = client.query(
            "SELECT DISTINCT toDate(date) AS trading_date "
            "FROM stock__candles_data "
            "WHERE interval = {interval:String} "
            "AND stock IN {symbols:Array(String)} "
            "AND date <= toDateTime({as_of:String}) "
            "ORDER BY trading_date DESC",
            parameters={
                "interval": interval,
                "symbols": universe_symbols,
                "as_of": as_of_text,
            },
        )
        trading_dates = [_as_date(row[0]) for row in date_result.result_rows]
        if not trading_dates:
            raise ScanParameterError(
                "No candle dates found for the selected universe and as_of"
            )
        latest_date = trading_dates[0]
        earliest_date = trading_dates[-1]

        if to_date is None:
            to_date = latest_date
        if from_date is None:
            sessions_through_end = [day for day in trading_dates if day <= to_date]
            if not sessions_through_end:
                from_date = to_date
            else:
                from_date = sessions_through_end[
                    min(DEFAULT_DAYS, len(sessions_through_end)) - 1
                ]
        if from_date > to_date:
            raise ScanParameterError("from_date must be on or before to_date")
        if (to_date - from_date).days + 1 > MAX_DAYS:
            raise ScanParameterError("date range must not exceed 90 calendar days")

        applied_dates = [
            day for day in trading_dates if from_date <= day <= to_date
        ]
        matches_by_date: dict[date, list[dict[str, Any]]] = {}
        total = 0
        for trading_day in applied_dates:
            candle_time = datetime.combine(trading_day, FIRST_CANDLE_TIME)
            candle_result = client.query(
                "SELECT stock, date, high, low, close, volume, vma20, "
                "sma20, sma50, sma200, superTrend, open "
                "FROM stock__candles_data "
                "WHERE interval = {interval:String} "
                "AND stock IN {symbols:Array(String)} "
                "AND date = toDateTime({candle_time:String}) "
                "AND date <= toDateTime({as_of:String}) "
                "ORDER BY date, stock",
                parameters={
                    "interval": interval,
                    "symbols": universe_symbols,
                    "candle_time": candle_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "as_of": as_of_text,
                },
            )
            raw_rows = [
                dict(
                    zip(
                        (
                            "stock",
                            "date",
                            "high",
                            "low",
                            "close",
                            "volume",
                            "vma20",
                            "sma20",
                            "sma50",
                            "sma200",
                            "superTrend",
                            "open",
                        ),
                        values,
                    )
                )
                for values in candle_result.result_rows
            ]
            target_symbols = sorted({str(row["stock"]) for row in raw_rows})
            previous_values: dict[str, Any] = {}
            if target_symbols:
                previous_result = client.query(
                    "SELECT stock, superTrend, volume "
                    "FROM stock__candles_data "
                    "WHERE interval = {interval:String} "
                    "AND stock IN {symbols:Array(String)} "
                    "AND date < toDateTime({target:String}) "
                    "ORDER BY stock, date DESC LIMIT 1 BY stock",
                    parameters={
                        "interval": interval,
                        "symbols": target_symbols,
                        "target": candle_time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                )
                previous_values = {
                    str(stock): (supertrend, volume)
                    for stock, supertrend, volume in previous_result.result_rows
                }

            day_matches = []
            for raw in raw_rows:
                normalized_date = _timestamp_ist(raw["date"])
                if normalized_date[11:] != FIRST_CANDLE_TIME.strftime("%H:%M:%S"):
                    continue
                stock = str(raw["stock"])
                raw["previous_superTrend"], raw["previous_volume"] = (
                    previous_values.get(stock, (None, None))
                )
                matches, condition_values = _truth(raw)
                if diagnostics is not None:
                    diagnostics.append(
                        {
                            "stock": str(raw["stock"]),
                            "at": normalized_date,
                            "interval": interval,
                            "condition_values": condition_values,
                        }
                    )
                if matches:
                    day_matches.append(
                        {
                            "stock": str(raw["stock"]),
                            "direction": condition_values["direction"],
                            "interval": interval,
                            "scan_date": normalized_date[:10],
                            "close": raw["close"],
                            "volume": raw["volume"],
                            "date": normalized_date,
                            "vma20": raw["vma20"],
                            "sma20": raw["sma20"],
                            "sma50": raw["sma50"],
                            "sma200": raw["sma200"],
                            "superTrend": raw["superTrend"],
                            "signal": condition_values["signal"],
                        }
                    )
            day_matches = _highest_liquidity(
                client, day_matches, trading_day, liquidity_days
            )
            total += len(day_matches)
            matches_by_date[trading_day] = day_matches

        matches = [
            row
            for trading_day in sorted(applied_dates, reverse=True)
            for row in matches_by_date.get(trading_day, [])
        ]
        if metadata is not None:
            applied_as_of = as_of.strftime("%Y-%m-%dT%H:%M")
            metadata.update(
                {
                    "total": total,
                    "from_date": from_date.isoformat(),
                    "to_date": to_date.isoformat(),
                    "min_date": earliest_date.isoformat(),
                    "max_date": latest_date.isoformat(),
                    "dates": [
                        day.isoformat() for day in sorted(applied_dates, reverse=True)
                    ],
                    "as_of": applied_as_of,
                    "criteria": (
                        "NIFTY 200 stocks; only the completed 09:15 IST 5-minute candle; "
                        "short: high above and close below SMA20, SMA50, and SMA200; "
                        "long: bullish body opening below and closing above all three "
                        "SMAs, with volume above the previous candle; "
                        "previous SuperTrend within the candle low-to-high range; "
                        "before liquidity ranking, long requires close strictly above "
                        "all three SMAs and current SuperTrend; short requires close "
                        "strictly below all three SMAs and current SuperTrend; "
                        "highest liquidity at 09:15 IST using "
                        f"{liquidity_days} prior trading sessions; "
                        "at most one qualifying stock per date; "
                        f"dates {from_date.isoformat()} through {to_date.isoformat()}; "
                        f"as_of {applied_as_of} IST"
                    ),
                }
            )
        return matches
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
    interval: str = DEFAULT_INTERVAL,
    limit: str = "10",
    sort_by: str | None = None,
    sort_dir: str | None = None,
    liquidity_days: str = str(DEFAULT_LIQUIDITY_DAYS),
):
    params = {
        "from_date": from_date,
        "to_date": to_date,
        "as_of": as_of,
        "symbols": symbols,
        "interval": interval,
        "limit": limit,
        "sort_by": sort_by,
        "sort_dir": sort_dir,
        "liquidity_days": liquidity_days,
    }
    try:
        date_range: dict[str, Any] = {}
        matches = run_scan(params, metadata=date_range)
    except ScanParameterError as exc:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": exc.public_message},
        )
    return {
        "success": True,
        "criteria": date_range["criteria"],
        "columns": COLUMNS,
        "direction": DIRECTION,
        "presentation": PRESENTATION,
        "parameters": [
            {
                "key": "liquidity_days",
                "default": DEFAULT_LIQUIDITY_DAYS,
                "label": "Liquidity lookback (trading sessions)",
            }
        ],
        "date_range": {
            "mode": "range",
            "from_date": date_range["from_date"],
            "to_date": date_range["to_date"],
            "default_days": DEFAULT_DAYS,
            "max_days": MAX_DAYS,
            "min_date": date_range["min_date"],
            "max_date": date_range["max_date"],
            "dates": date_range["dates"],
        },
        "total": date_range["total"],
        "matches": matches,
    }


app = FastAPI()
app.include_router(router)
