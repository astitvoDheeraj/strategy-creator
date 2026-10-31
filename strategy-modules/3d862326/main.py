import os
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import clickhouse_connect
from fastapi import APIRouter, FastAPI, Query
from fastapi.responses import JSONResponse


JOB_ID = "3d862326"
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_INTERVAL = "5"
FIRST_CANDLE_TIME = time(9, 15)
DEFAULT_DAYS = 60
MAX_DAYS = 90
COLUMNS = [
    {"key": "stock", "label": "Symbol", "format": "text"},
    {"key": "scan_date", "label": "Date", "format": "date"},
    {"key": "date", "label": "Candle time", "format": "datetime"},
    {"key": "close", "label": "Last price (LTP)", "format": "price"},
    {"key": "open", "label": "Open", "format": "price"},
    {"key": "high", "label": "High", "format": "price"},
    {"key": "low", "label": "Low", "format": "price"},
]
PRESENTATION = {
    "style": "table",
    "sort": {"key": "date", "direction": "asc"},
    "ui_filters": ["symbol_search", "column_sort"],
    "limit": 0,
    "page_size": 1,
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
        parsed = parsed.replace(tzinfo=IST)
    else:
        parsed = parsed.astimezone(IST)
    return parsed


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _timestamp_ist(value: Any) -> str:
    """Database dates store IST wall-clock values, including driver-annotated datetimes."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _truth(row: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    open_price = row["open"]
    close_price = row["close"]
    indicators = {
        "sma20": row["sma20"],
        "sma50": row["sma50"],
        "sma200": row["sma200"],
        "superTrend": row["superTrend"],
    }
    available = open_price is not None and close_price is not None and all(
        value is not None for value in indicators.values()
    )
    conditions = {
        "open_above_sma20_and_close_below": available
        and open_price > indicators["sma20"]
        and close_price < indicators["sma20"],
        "open_above_sma50_and_close_below": available
        and open_price > indicators["sma50"]
        and close_price < indicators["sma50"],
        "open_above_sma200_and_close_below": available
        and open_price > indicators["sma200"]
        and close_price < indicators["sma200"],
        "open_above_supertrend_and_close_below": available
        and open_price > indicators["superTrend"]
        and close_price < indicators["superTrend"],
        "close_below_supertrend": available
        and close_price < indicators["superTrend"]
        and indicators["superTrend"] > close_price,
    }
    measured = {
        "data_available": available,
        "open": open_price,
        "close": close_price,
        **indicators,
        **conditions,
        "all_conditions": all(conditions.values()),
    }
    return all(conditions.values()), measured


def run_scan(
    params: dict[str, Any],
    diagnostics: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run the screener and return the user-facing result rows."""
    interval = str(params.get("interval") or DEFAULT_INTERVAL)
    if not interval:
        raise ScanParameterError("interval must not be empty")
    as_of = _parse_as_of(params.get("as_of"))
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
                "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
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
                from_date = sessions_through_end[min(DEFAULT_DAYS, len(sessions_through_end)) - 1]
        if from_date > to_date:
            raise ScanParameterError("from_date must be on or before to_date")
        if (to_date - from_date).days + 1 > MAX_DAYS:
            raise ScanParameterError("date range must not exceed 90 calendar days")

        applied_dates = [
            day for day in trading_dates if from_date <= day <= to_date
        ]
        if as_of.date() < to_date:
            applied_dates = [day for day in applied_dates if day <= as_of.date()]

        sort_by = str(params.get("sort_by") or "date")
        allowed_sort_keys = {column["key"] for column in COLUMNS}
        if sort_by not in allowed_sort_keys:
            raise ScanParameterError(
                f"sort_by must be one of {', '.join(sorted(allowed_sort_keys))}"
            )
        sort_dir = str(params.get("sort_dir") or "asc").lower()
        if sort_dir not in {"asc", "desc"}:
            raise ScanParameterError("sort_dir must be 'asc' or 'desc'")
        try:
            limit = int(params.get("limit", 0))
        except (TypeError, ValueError) as exc:
            raise ScanParameterError("limit must be an integer") from exc
        if limit < 0:
            raise ScanParameterError("limit must be zero or a positive integer")

        matches_by_date: dict[date, list[dict[str, Any]]] = {}
        total = 0
        for trading_day in applied_dates:
            day_start = datetime.combine(trading_day, time.min, tzinfo=IST)
            day_end = day_start + timedelta(days=1)
            candle_result = client.query(
                "SELECT stock, date, close, open, high, low, "
                "sma20, sma50, sma200, superTrend "
                "FROM stock__candles_data "
                "WHERE interval = {interval:String} "
                "AND stock IN {symbols:Array(String)} "
                "AND date >= toDateTime({day_start:String}) AND date < toDateTime({day_end:String}) "
                "AND date <= toDateTime({as_of:String}) "
                "AND formatDateTime(date, '%H:%M:%S') = {candle_time:String} "
                "ORDER BY stock",
                parameters={
                    "interval": interval,
                    "symbols": universe_symbols,
                    "day_start": day_start.strftime("%Y-%m-%d %H:%M:%S"),
                    "day_end": day_end.strftime("%Y-%m-%d %H:%M:%S"),
                    "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
                    "candle_time": FIRST_CANDLE_TIME.strftime("%H:%M:%S"),
                },
            )
            day_matches = []
            for values in candle_result.result_rows:
                raw = dict(
                    zip(
                        (
                            "stock",
                            "date",
                            "close",
                            "open",
                            "high",
                            "low",
                            "sma20",
                            "sma50",
                            "sma200",
                            "superTrend",
                        ),
                        values,
                    )
                )
                matches, condition_values = _truth(raw)
                normalized_date = _timestamp_ist(raw["date"])
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
                    total += 1
                    day_matches.append(
                        {
                            "stock": str(raw["stock"]),
                            "scan_date": normalized_date[:10],
                            "date": normalized_date,
                            "close": raw["close"],
                            "open": raw["open"],
                            "high": raw["high"],
                            "low": raw["low"],
                        }
                    )
            reverse = sort_dir == "desc"
            day_matches.sort(
                key=lambda row: (row[sort_by], row["stock"]),
                reverse=reverse,
            )
            matches_by_date[trading_day] = (
                day_matches[:limit] if limit else day_matches
            )
        matches = [
            row
            for trading_day in sorted(applied_dates, reverse=True)
            for row in matches_by_date.get(trading_day, [])
        ]
        if metadata is not None:
            metadata.update({
                "total": total,
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "min_date": earliest_date.isoformat(),
                "max_date": latest_date.isoformat(),
                "dates": [
                    day.isoformat() for day in sorted(applied_dates, reverse=True)
                ],
                "as_of": as_of.strftime("%Y-%m-%dT%H:%M"),
                "criteria": (
                    "NIFTY 200 stocks; first 5-minute candle at 09:15 IST; "
                    "open crosses below SMA20, SMA50, SMA200 and SuperTrend; "
                    f"dates {from_date.isoformat()} through {to_date.isoformat()}; "
                    f"as_of {as_of.strftime('%Y-%m-%dT%H:%M')} IST"
                ),
            })
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
    limit: int = Query(default=0, ge=0),
    sort_by: str | None = None,
    sort_dir: str | None = None,
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
    }
    try:
        date_range = {}
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
        "presentation": PRESENTATION,
        "parameters": [],
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
