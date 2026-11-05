import os
from datetime import date, datetime, time, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

import clickhouse_connect
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse


JOB_ID = "a0385ab4"
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_INTERVAL = "daily"
DEFAULT_UNIVERSE = "nifty_200"
DEFAULT_LOOKBACK_DAYS = 40
DEFAULT_THRESHOLD_PCT = 2.0
DEFAULT_DAYS = 5
MAX_DAYS = 90
UNIVERSE_OPTIONS = {
    "nifty_200": "NIFTY 200",
    "nifty_50": "NIFTY 50",
    "nifty_next_50": "NIFTY NEXT 50",
    "nifty_100": "NIFTY 100",
    "nifty_500": "NIFTY 500",
    "nifty_midcap_50": "NIFTY MIDCAP 50",
    "nifty_midcap_100": "NIFTY MIDCAP 100",
    "nifty_midcap_150": "NIFTY MIDCAP 150",
    "nifty_smallcap_50": "NIFTY SMALLCAP 50",
    "nifty_smallcap_100": "NIFTY SMALLCAP 100",
    "nifty_smallcap_250": "NIFTY SMALLCAP 250",
    "all": "All NSE stocks",
}
COLUMNS = [
    {"key": "stock", "label": "Symbol", "format": "text"},
    {"key": "scan_date", "label": "Date", "format": "date"},
    {"key": "direction", "label": "Direction", "format": "direction"},
    {"key": "close", "label": "Last price (LTP)", "format": "price"},
    {"key": "superTrend", "label": "SuperTrend", "format": "price"},
    {"key": "vma20", "label": "Avg volume (20)", "format": "int"},
]
PRESENTATION = {
    "style": "table",
    "sort": {"key": "close", "direction": "desc"},
    "ui_filters": ["symbol_search", "column_sort"],
    "limit": 50,
    "page_size": 25,
    "pagination": "pages",
    "notes": "",
    "group_by_date": True,
    "date_chips": True,
}
DIRECTION = {
    "enabled": True,
    "buy": (
        "Signal candle low is the lowest of the last 40 days, close is above open, "
        "and close is at least 2% above low"
    ),
    "sell": "",
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


def _parse_date(value: str | date | None, name: str) -> date | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, date) else date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ScanParameterError(
            f"{name} must be a date in YYYY-MM-DD format"
        ) from exc
    if parsed.isoformat() != str(value):
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
        except (TypeError, ValueError) as exc:
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


def _truth(
    candles: list[dict[str, Any]], lookback_days: int, threshold_pct: float
) -> tuple[bool, dict[str, Any]]:
    enough_history = len(candles) == lookback_days
    current = candles[0] if candles else {}
    low = current.get("low")
    open_price = current.get("open")
    close = current.get("close")
    prior_low = min((candle["low"] for candle in candles), default=None)
    green_close = (
        open_price is not None and close is not None and close > open_price
    )
    gain_from_low_pct = (
        100 * (close - low) / low
        if low is not None and close is not None and low > 0
        else None
    )
    is_lookback_low = enough_history and low is not None and low == prior_low
    gain_condition = (
        gain_from_low_pct is not None and gain_from_low_pct >= threshold_pct
    )
    matched = (
        enough_history
        and green_close
        and is_lookback_low
        and gain_condition
    )
    values = {
        "data_available": (
            enough_history
            and all(
                candle.get(key) is not None
                for candle in candles
                for key in ("open", "low", "close")
            )
        ),
        "history_candles": len(candles),
        "lookback_days": lookback_days,
        "open": open_price,
        "close": close,
        "low": low,
        "lowest_low_in_lookback": prior_low,
        "is_lowest_low": is_lookback_low,
        "green_close": green_close,
        "gain_from_low_pct": gain_from_low_pct,
        "threshold_pct": threshold_pct,
        "gain_condition": gain_condition,
        "all_conditions": matched,
    }
    return matched, values


def _parse_universe(value: Any) -> tuple[list[str], str]:
    if value is None or not isinstance(value, str):
        value = DEFAULT_UNIVERSE
    requested = list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if not requested:
        return [], "all"
    if any(universe not in UNIVERSE_OPTIONS for universe in requested):
        requested = [DEFAULT_UNIVERSE]
    if "all" in requested:
        return [], "all"
    return requested, ",".join(requested)


def _parse_symbols(value: Any) -> list[str] | None:
    if value is None:
        return None
    symbols = (
        [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, str)
        else [str(part).strip() for part in value if str(part).strip()]
    )
    if not symbols:
        raise ScanParameterError("symbols must contain at least one symbol")
    return list(dict.fromkeys(symbols))


def run_scan(
    params: dict[str, Any],
    diagnostics: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    interval = str(params.get("interval") or DEFAULT_INTERVAL)
    if interval != DEFAULT_INTERVAL:
        interval = DEFAULT_INTERVAL
    universes, applied_universe = _parse_universe(params.get("universe"))
    requested_symbols = _parse_symbols(params.get("symbols"))
    as_of = _parse_as_of(params.get("as_of"))
    from_date = _parse_date(params.get("from_date"), "from_date")
    to_date = _parse_date(params.get("to_date"), "to_date")
    if from_date is not None and to_date is not None:
        if from_date > to_date:
            raise ScanParameterError("from_date must be on or before to_date")
        if (to_date - from_date).days + 1 > MAX_DAYS:
            raise ScanParameterError("date range must not exceed 90 calendar days")
    try:
        lookback_days = int(params.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
    except (TypeError, ValueError) as exc:
        raise ScanParameterError("lookback_days must be a positive integer") from exc
    if lookback_days < 1 or lookback_days > 365:
        raise ScanParameterError("lookback_days must be between 1 and 365")
    try:
        threshold_pct = float(params.get("threshold_pct", DEFAULT_THRESHOLD_PCT))
    except (TypeError, ValueError) as exc:
        raise ScanParameterError("threshold_pct must be a finite nonnegative number") from exc
    if not isfinite(threshold_pct) or threshold_pct < 0:
        raise ScanParameterError("threshold_pct must be a finite nonnegative number")
    sort_by = str(params.get("sort_by") or "close")
    allowed_sort_keys = {column["key"] for column in COLUMNS}
    if sort_by not in allowed_sort_keys:
        raise ScanParameterError(
            f"sort_by must be one of {', '.join(sorted(allowed_sort_keys))}"
        )
    sort_dir = str(params.get("sort_dir") or "desc").lower()
    if sort_dir not in {"asc", "desc"}:
        raise ScanParameterError("sort_dir must be 'asc' or 'desc'")
    try:
        limit = int(params.get("limit", 50))
    except (TypeError, ValueError) as exc:
        raise ScanParameterError("limit must be a nonnegative integer") from exc
    if limit < 0:
        raise ScanParameterError("limit must be a nonnegative integer")

    client = _client()
    try:
        symbol_filter = ""
        symbol_parameters: dict[str, Any] = {}
        universe_symbols: list[str] | None = None
        if universes:
            result = client.query(
                "SELECT DISTINCT symbol FROM universe_members FINAL "
                "WHERE universe_id IN {universes:Array(String)} AND active = 1",
                parameters={"universes": universes},
            )
            universe_symbols = list(dict.fromkeys(str(row[0]) for row in result.result_rows))
            if not universe_symbols:
                raise ScanParameterError(
                    f"The {', '.join(universes)} universe has no active members"
                )
        if requested_symbols is not None:
            if universe_symbols is None:
                universe_symbols = requested_symbols
            else:
                wanted = set(requested_symbols)
                universe_symbols = [symbol for symbol in universe_symbols if symbol in wanted]
        if universe_symbols is not None:
            if not universe_symbols:
                if requested_symbols is not None:
                    raise ScanParameterError(
                        f"No requested symbols are active members of {applied_universe}"
                    )
                raise ScanParameterError("The selected universe has no active members")
            symbol_filter = "AND stock IN {symbols:Array(String)} "
            symbol_parameters = {"symbols": universe_symbols}

        as_of_text = as_of.strftime("%Y-%m-%d %H:%M:%S")
        date_result = client.query(
            "SELECT DISTINCT toDate(date) AS trading_date "
            "FROM stock__candles_data "
            "WHERE interval = {interval:String} "
            f"{symbol_filter}"
            "AND date <= toDateTime({as_of:String}) "
            "ORDER BY trading_date DESC",
            parameters={
                **symbol_parameters,
                "interval": interval,
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
            count = min(DEFAULT_DAYS, len(sessions_through_end))
            from_date = (
                sessions_through_end[count - 1] if count else to_date
            )
        if from_date > to_date:
            raise ScanParameterError("from_date must be on or before to_date")
        if (to_date - from_date).days + 1 > MAX_DAYS:
            raise ScanParameterError("date range must not exceed 90 calendar days")

        applied_dates = sorted(
            (day for day in trading_dates if from_date <= day <= to_date),
            reverse=True,
        )
        total = 0
        matches: list[dict[str, Any]] = []
        for trading_day in applied_dates:
            cutoff = datetime.combine(trading_day, time.max)
            if trading_day == as_of.date():
                cutoff = min(cutoff, as_of.replace(tzinfo=None))
            candle_result = client.query(
                "SELECT stock, date, open, high, low, close, volume, "
                "superTrend, finalUpper, finalLower, vma20 "
                "FROM stock__candles_data "
                "WHERE interval = {interval:String} "
                f"{symbol_filter}"
                "AND date <= toDateTime({cutoff:String}) "
                "ORDER BY stock, date DESC "
                "LIMIT {lookback_days:UInt32} BY stock",
                parameters={
                    **symbol_parameters,
                    "interval": interval,
                    "cutoff": cutoff.strftime("%Y-%m-%d %H:%M:%S"),
                    "lookback_days": lookback_days,
                },
            )
            history_by_stock: dict[str, list[dict[str, Any]]] = {}
            for values in candle_result.result_rows:
                candle = dict(
                    zip(
                        (
                            "stock",
                            "date",
                            "open",
                            "high",
                            "low",
                            "close",
                            "volume",
                            "superTrend",
                            "finalUpper",
                            "finalLower",
                            "vma20",
                        ),
                        values,
                    )
                )
                history_by_stock.setdefault(str(candle["stock"]), []).append(candle)
            day_matches = []
            for stock, history in history_by_stock.items():
                history.sort(key=lambda candle: candle["date"], reverse=True)
                if _as_date(history[0]["date"]) != trading_day:
                    continue
                matched, values = _truth(history, lookback_days, threshold_pct)
                candle_at = _timestamp_ist(history[0]["date"])
                if diagnostics is not None:
                    diagnostics.append(
                        {
                            "stock": stock,
                            "at": candle_at,
                            "interval": interval,
                            "condition_values": values,
                            "matched": matched,
                        }
                    )
                if not matched:
                    continue
                row = {
                    "stock": stock,
                    "scan_date": trading_day.isoformat(),
                    "direction": "BUY",
                    "close": history[0]["close"],
                    "superTrend": history[0]["superTrend"],
                    "vma20": history[0]["vma20"],
                }
                day_matches.append(row)
                if diagnostics is not None:
                    diagnostics[-1]["matched_row"] = row.copy()
            day_matches.sort(
                key=lambda row: (
                    row[sort_by] is None,
                    row[sort_by] if row[sort_by] is not None else 0,
                    row["stock"],
                ),
                reverse=sort_dir == "desc",
            )
            total += len(day_matches)
            matches.extend(day_matches if limit == 0 else day_matches[:limit])

        if metadata is not None:
            applied_as_of = as_of.strftime("%Y-%m-%dT%H:%M")
            universe_description = (
                "All NSE stocks"
                if not universes or applied_universe == "all"
                else ", ".join(UNIVERSE_OPTIONS[value] for value in universes)
            )
            metadata.update(
                {
                    "total": total,
                    "from_date": from_date.isoformat(),
                    "to_date": to_date.isoformat(),
                    "min_date": earliest_date.isoformat(),
                    "max_date": latest_date.isoformat(),
                    "dates": [day.isoformat() for day in applied_dates],
                    "as_of": applied_as_of,
                    "universe": applied_universe,
                    "interval": interval,
                    "threshold_pct": threshold_pct,
                    "lookback_days": lookback_days,
                    "criteria": (
                        f"{universe_description}; interval {interval}; "
                        f"low is the lowest of the last {lookback_days} candles; "
                        f"close is above open and at least {threshold_pct:g}% above low; "
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
    limit: str = "50",
    sort_by: str | None = None,
    sort_dir: str | None = None,
    lookback_days: str = str(DEFAULT_LOOKBACK_DAYS),
    threshold_pct: str = str(DEFAULT_THRESHOLD_PCT),
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
        "lookback_days": lookback_days,
        "threshold_pct": threshold_pct,
        "universe": universe,
    }
    try:
        date_range: dict[str, Any] = {}
        matches = run_scan(params, metadata=date_range)
    except ScanParameterError as exc:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": exc.public_message},
        )
    direction = {
        **DIRECTION,
        "buy": (
            f"Signal candle low is the lowest of the last "
            f"{date_range['lookback_days']} days, close is above open, and close is "
            f"at least {date_range['threshold_pct']:g}% above low"
        ),
    }
    return {
        "success": True,
        "criteria": date_range["criteria"],
        "columns": COLUMNS,
        "direction": direction,
        "presentation": PRESENTATION,
        "parameters": [
            {
                "key": "universe",
                "label": "Universe",
                "options": [
                    {"value": value, "label": label}
                    for value, label in UNIVERSE_OPTIONS.items()
                ],
                "default": DEFAULT_UNIVERSE,
                "multi": True,
                "value": date_range["universe"],
            },
            {
                "key": "lookback_days",
                "label": "Low lookback (trading sessions)",
                "default": DEFAULT_LOOKBACK_DAYS,
                "value": date_range["lookback_days"],
            },
            {
                "key": "threshold_pct",
                "label": "Minimum gain from low (%)",
                "default": DEFAULT_THRESHOLD_PCT,
                "value": date_range["threshold_pct"],
            },
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
