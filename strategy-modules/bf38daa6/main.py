import os
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import clickhouse_connect
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse


JOB_ID = "bf38daa6"
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_INTERVAL = "weekly"
DEFAULT_DAYS = 60
MAX_DAYS = 90
DEFAULT_UNIVERSE = "nifty_500"
UNIVERSE_OPTIONS = [
    {"value": "nifty_500", "label": "NIFTY 500"},
    {"value": "nifty_50", "label": "NIFTY 50"},
    {"value": "nifty_next_50", "label": "NIFTY NEXT 50"},
    {"value": "nifty_100", "label": "NIFTY 100"},
    {"value": "nifty_200", "label": "NIFTY 200"},
    {"value": "nifty_midcap_50", "label": "NIFTY MIDCAP 50"},
    {"value": "nifty_midcap_100", "label": "NIFTY MIDCAP 100"},
    {"value": "nifty_midcap_150", "label": "NIFTY MIDCAP 150"},
    {"value": "nifty_smallcap_50", "label": "NIFTY SMALLCAP 50"},
    {"value": "nifty_smallcap_100", "label": "NIFTY SMALLCAP 100"},
    {"value": "nifty_smallcap_250", "label": "NIFTY SMALLCAP 250"},
    {"value": "all", "label": "All NSE stocks"},
]
COLUMNS = [
    {"key": "stock", "label": "Symbol", "format": "text"},
    {"key": "scan_date", "label": "Date", "format": "date"},
    {"key": "direction", "label": "Direction", "format": "direction"},
    {"key": "close", "label": "Last price (LTP)", "format": "price"},
    {"key": "volume", "label": "Volume", "format": "int"},
    {"key": "date", "label": "Candle time", "format": "datetime"},
]
PRESENTATION = {
    "style": "cards",
    "sort": {"key": "date", "direction": "asc"},
    "ui_filters": ["symbol_search", "column_sort"],
    "limit": 0,
    "page_size": 1,
    "pagination": "load_more",
    "notes": "",
    "group_by_date": True,
    "date_chips": True,
}
DIRECTION = {
    "enabled": True,
    "buy": "close > SuperTrend and low <= SuperTrend",
    "sell": "Not applicable",
    "neutral": "Matches neither the BUY rule nor the SELL rule",
}

router = APIRouter(prefix=f"/strategy-api/{JOB_ID}")


class ScanParameterError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.public_message = message


def _client():
    return clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "clickhouse"),
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
        if parsed.strftime("%Y-%m-%dT%H:%M") != value:
            raise ScanParameterError("as_of must use YYYY-MM-DDTHH:MM format in IST")
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
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _truth(row: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    close = row["close"]
    low = row["low"]
    supertrend = row["superTrend"]
    available = close is not None and low is not None and supertrend is not None
    close_above_supertrend = available and close > supertrend
    low_touches_supertrend = available and low <= supertrend
    matches = close_above_supertrend and low_touches_supertrend
    return matches, {
        "data_available": available,
        "close": close,
        "low": low,
        "superTrend": supertrend,
        "close_above_superTrend": close_above_supertrend,
        "low_at_or_below_superTrend": low_touches_supertrend,
        "all_conditions": matches,
        "direction": "BUY" if matches else "NEUTRAL",
    }


def _parse_universe(value: str | None) -> list[str]:
    if value is None:
        value = DEFAULT_UNIVERSE
    universe_ids = list(dict.fromkeys(item.strip() for item in value.split(",")))
    allowed = {option["value"] for option in UNIVERSE_OPTIONS}
    if any(item and item not in allowed for item in universe_ids):
        return [DEFAULT_UNIVERSE]
    if "all" in universe_ids or not any(universe_ids):
        return []
    return [item for item in universe_ids if item]


def run_scan(
    params: dict[str, Any],
    diagnostics: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run the weekly SuperTrend screener and return its result rows."""
    interval = str(params.get("interval") or DEFAULT_INTERVAL)
    if not interval:
        raise ScanParameterError("interval must not be empty")
    as_of = _parse_as_of(params.get("as_of"))
    universe_ids = _parse_universe(params.get("universe"))
    universe_label = ", ".join(
        option["label"]
        for option in UNIVERSE_OPTIONS
        if option["value"] in (universe_ids or ["all"])
    )
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
    default_window = from_date is None and to_date is None
    if from_date is not None and to_date is not None:
        if from_date > to_date:
            raise ScanParameterError("from_date must be on or before to_date")
        if not default_window and (to_date - from_date).days + 1 > MAX_DAYS:
            raise ScanParameterError("date range must not exceed 90 calendar days")

    try:
        limit = int(params.get("limit", 0))
    except (TypeError, ValueError) as exc:
        raise ScanParameterError("limit must be an integer") from exc
    if limit < 0:
        raise ScanParameterError("limit must be zero or a positive integer")
    sort_by = str(params.get("sort_by") or "date")
    allowed_sort_keys = {column["key"] for column in COLUMNS}
    if sort_by not in allowed_sort_keys:
        raise ScanParameterError(
            f"sort_by must be one of {', '.join(sorted(allowed_sort_keys))}"
        )
    sort_dir = str(params.get("sort_dir") or "asc").lower()
    if sort_dir not in {"asc", "desc"}:
        raise ScanParameterError("sort_dir must be 'asc' or 'desc'")

    client = _client()
    try:
        universe_symbols = None
        if universe_ids:
            universe_result = client.query(
                "SELECT DISTINCT symbol FROM universe_members FINAL "
                "WHERE universe_id IN {universe_ids:Array(String)} AND active = 1",
                parameters={"universe_ids": universe_ids},
            )
            universe_symbols = sorted(
                {str(row[0]) for row in universe_result.result_rows}
            )
            if not universe_symbols:
                raise ScanParameterError(
                    "The selected universe has no active members"
                )
        if requested_symbols is not None and universe_symbols is not None:
            requested = set(requested_symbols)
            universe_symbols = [
                symbol for symbol in universe_symbols if symbol in requested
            ]
            if not universe_symbols:
                raise ScanParameterError(
                    "No requested symbols are active members of the selected universe"
                )
        elif requested_symbols is not None:
            universe_symbols = sorted(set(requested_symbols))

        symbol_filter = (
            "AND stock IN {symbols:Array(String)} "
            if universe_symbols is not None
            else ""
        )
        symbol_params = (
            {"symbols": universe_symbols} if universe_symbols is not None else {}
        )

        as_of_text = as_of.strftime("%Y-%m-%d %H:%M:%S")
        date_result = client.query(
            "SELECT DISTINCT toDate(date) AS trading_date "
            "FROM stock__candles_data "
            "WHERE interval = {interval:String} "
            f"{symbol_filter}"
            "AND date <= toDateTime({as_of:String}) "
            "ORDER BY trading_date DESC",
            parameters={
                "interval": interval,
                **symbol_params,
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
        if not default_window and (to_date - from_date).days + 1 > MAX_DAYS:
            raise ScanParameterError("date range must not exceed 90 calendar days")

        applied_dates = [
            day for day in trading_dates if from_date <= day <= to_date
        ]
        matches_by_date: dict[date, list[dict[str, Any]]] = {}
        total = 0
        for trading_day in applied_dates:
            day_start = datetime.combine(trading_day, time.min)
            day_end = day_start + timedelta(days=1)
            candle_result = client.query(
                "SELECT stock, date, close, low, volume, superTrend "
                "FROM stock__candles_data "
                "WHERE interval = {interval:String} "
                f"{symbol_filter}"
                "AND date >= toDateTime({day_start:String}) "
                "AND date < toDateTime({day_end:String}) "
                "AND date <= toDateTime({as_of:String}) "
                "ORDER BY stock, date",
                parameters={
                    "interval": interval,
                    **symbol_params,
                    "day_start": day_start.strftime("%Y-%m-%d %H:%M:%S"),
                    "day_end": day_end.strftime("%Y-%m-%d %H:%M:%S"),
                    "as_of": as_of_text,
                },
            )
            day_matches = []
            for values in candle_result.result_rows:
                raw = dict(
                    zip(
                        ("stock", "date", "close", "low", "volume", "superTrend"),
                        values,
                    )
                )
                matched, condition_values = _truth(raw)
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
                if matched:
                    day_matches.append(
                        {
                            "stock": str(raw["stock"]),
                            "scan_date": normalized_date[:10],
                            "direction": "BUY",
                            "close": raw["close"],
                            "volume": raw["volume"],
                            "date": normalized_date,
                            "interval": interval,
                        }
                    )
            total += len(day_matches)
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
                    "universe": ",".join(universe_ids) if universe_ids else "all",
                    "criteria": (
                        f"{universe_label}; {interval} candles; close > SuperTrend and "
                        "low <= SuperTrend; "
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
    limit: int = 0,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    universe: str = DEFAULT_UNIVERSE,
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
        "universe": universe,
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
        "parameters": [
            {
                "key": "universe",
                "label": "Universe",
                "multi": True,
                "options": UNIVERSE_OPTIONS,
                "default": DEFAULT_UNIVERSE,
                "value": date_range["universe"],
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
        "direction": DIRECTION,
        "total": date_range["total"],
        "matches": matches,
    }


app = FastAPI()
app.include_router(router)
