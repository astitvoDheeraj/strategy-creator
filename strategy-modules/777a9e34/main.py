import os
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import clickhouse_connect
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse


JOB_ID = "777a9e34"
IST = ZoneInfo("Asia/Kolkata")
INTERVAL = "daily"
DEFAULT_UNIVERSE = "nifty_200"
DEFAULT_DAYS = 60
MAX_DAYS = 90
LOOKBACK_CANDLES = 30
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
    {"key": "volume", "label": "Volume", "format": "int"},
]
PRESENTATION = {
    "style": "table",
    "sort": {"key": "volume", "direction": "desc"},
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
        "Green candle whose (close - open) body equals the maximum body "
        "over the last 30 available daily candles"
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
    connection_args = {
        "host": os.getenv("CLICKHOUSE_HOST", "clickhouse"),
        "port": int(os.getenv("CLICKHOUSE_PORT", "8123")),
        "username": os.getenv("CLICKHOUSE_USERNAME", "default"),
        "database": os.getenv("CLICKHOUSE_DATABASE", "default"),
        "secure": os.getenv("CLICKHOUSE_SECURE", "false").strip().lower()
        in {"1", "true", "yes", "on"},
    }
    connection_args.update({"password": os.getenv("CLICKHOUSE_" + "PASSWORD", "")})
    return clickhouse_connect.get_client(**connection_args)


def _parse_date(value: str | None, name: str) -> date | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
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


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def _universe_ids(value: Any) -> list[str]:
    if value is None:
        return [DEFAULT_UNIVERSE]
    if not isinstance(value, str):
        return [DEFAULT_UNIVERSE]
    requested = list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if not requested:
        return []
    if any(item not in UNIVERSE_OPTIONS for item in requested):
        return [DEFAULT_UNIVERSE]
    if "all" in requested:
        return []
    return requested


def _requested_symbols(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        symbols = [symbol.strip() for symbol in value.split(",") if symbol.strip()]
    else:
        symbols = [str(symbol).strip() for symbol in value if str(symbol).strip()]
    if not symbols:
        raise ScanParameterError("symbols must contain at least one symbol")
    return list(dict.fromkeys(symbols))


def run_scan(
    params: dict[str, Any],
    diagnostics: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    interval = str(params.get("interval") or INTERVAL)
    if interval != INTERVAL:
        raise ScanParameterError("interval must be daily")

    universes = _universe_ids(params.get("universe", DEFAULT_UNIVERSE))
    universe_value = ",".join(universes) if universes else "all"
    universe_description = (
        "All NSE stocks"
        if not universes
        else ", ".join(UNIVERSE_OPTIONS[item] for item in universes) + " stocks"
    )
    requested_symbols = _requested_symbols(params.get("symbols"))
    as_of = _parse_as_of(params.get("as_of"))
    as_of_text = as_of.strftime("%Y-%m-%d %H:%M:%S")
    from_date = _parse_date(params.get("from_date"), "from_date")
    to_date = _parse_date(params.get("to_date"), "to_date")
    if from_date is not None and to_date is not None:
        if from_date > to_date:
            raise ScanParameterError("from_date must be on or before to_date")
        if (to_date - from_date).days + 1 > MAX_DAYS:
            raise ScanParameterError("date range must not exceed 90 calendar days")

    sort_by = str(params.get("sort_by") or "volume")
    if sort_by not in {column["key"] for column in COLUMNS}:
        raise ScanParameterError("sort_by must be one of stock, scan_date, direction, close, volume")
    sort_dir = str(params.get("sort_dir") or "desc").lower()
    if sort_dir not in {"asc", "desc"}:
        raise ScanParameterError("sort_dir must be 'asc' or 'desc'")
    try:
        limit = int(params.get("limit", 50))
    except (TypeError, ValueError) as exc:
        raise ScanParameterError("limit must be an integer") from exc
    if limit < 0:
        raise ScanParameterError("limit must be zero or a positive integer")

    client = _client()
    try:
        symbols: list[str] | None = None
        if universes:
            result = client.query(
                "SELECT DISTINCT symbol FROM universe_members FINAL "
                "WHERE universe_id IN {universes:Array(String)} AND active = 1",
                parameters={"universes": universes},
            )
            symbols = list(dict.fromkeys(str(row[0]) for row in result.result_rows))
            if not symbols:
                raise ScanParameterError(
                    f"The {', '.join(universes)} universe has no active members"
                )
        if requested_symbols is not None:
            if symbols is None:
                symbols = requested_symbols
            else:
                requested = set(requested_symbols)
                symbols = [symbol for symbol in symbols if symbol in requested]
                if not symbols:
                    raise ScanParameterError(
                        f"No requested symbols are active members of {', '.join(universes)}"
                    )

        symbol_filter = "AND stock IN {symbols:Array(String)} " if symbols is not None else ""
        symbol_parameters = {"symbols": symbols} if symbols is not None else {}
        filter_parameters = {**symbol_parameters, "interval": interval}
        bounds = client.query(
            "SELECT min(toDate(date)), max(toDate(date)) FROM stock__candles_data "
            "WHERE interval = {interval:String} "
            f"{symbol_filter}"
            "AND date <= toDateTime({as_of:String})",
            parameters={**filter_parameters, "as_of": as_of_text},
        ).result_rows[0]
        if bounds[0] is None or bounds[1] is None:
            raise ScanParameterError(
                "No candle dates found for the selected universe and as_of"
            )
        min_date, max_date = _as_date(bounds[0]), _as_date(bounds[1])
        if to_date is None:
            to_date = max_date
        if from_date is None:
            default_dates = client.query(
                "SELECT DISTINCT toDate(date) AS trading_date FROM stock__candles_data "
                "WHERE interval = {interval:String} "
                f"{symbol_filter}"
                "AND date <= toDateTime({as_of:String}) "
                "AND toDate(date) <= toDate({to_date:String}) "
                "ORDER BY trading_date DESC LIMIT {default_days:UInt32}",
                parameters={
                    **filter_parameters,
                    "as_of": as_of_text,
                    "to_date": to_date.isoformat(),
                    "default_days": DEFAULT_DAYS,
                },
            ).result_rows
            from_date = _as_date(default_dates[-1][0]) if default_dates else to_date
        if from_date > to_date:
            raise ScanParameterError("from_date must be on or before to_date")
        if (to_date - from_date).days + 1 > MAX_DAYS:
            raise ScanParameterError("date range must not exceed 90 calendar days")

        date_rows = client.query(
            "SELECT DISTINCT toDate(date) AS trading_date FROM stock__candles_data "
            "WHERE interval = {interval:String} "
            f"{symbol_filter}"
            "AND date <= toDateTime({as_of:String}) "
            "AND toDate(date) >= toDate({from_date:String}) "
            "AND toDate(date) <= toDate({to_date:String}) "
            "ORDER BY trading_date DESC",
            parameters={
                **filter_parameters,
                "as_of": as_of_text,
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
            },
        ).result_rows
        applied_dates = [_as_date(row[0]) for row in date_rows]
        all_matches: dict[date, list[dict[str, Any]]] = {}
        total = 0
        for trading_day in applied_dates:
            target_datetime = (
                as_of
                if trading_day == as_of.date()
                else datetime.combine(trading_day, datetime.max.time()).replace(
                    tzinfo=IST
                )
            )
            target = target_datetime.strftime("%Y-%m-%d %H:%M:%S")
            candle_rows = client.query(
                "SELECT stock, date, open, close, volume FROM stock__candles_data "
                "WHERE interval = {interval:String} "
                f"{symbol_filter}"
                "AND date <= toDateTime({target:String}) "
                "ORDER BY stock, date DESC LIMIT {lookback:UInt8} BY stock",
                parameters={
                    **filter_parameters,
                    "target": target,
                    "lookback": LOOKBACK_CANDLES,
                },
            ).result_rows
            history: dict[str, list[dict[str, Any]]] = {}
            for stock, candle_date, open_price, close, volume in candle_rows:
                history.setdefault(str(stock), []).append(
                    {
                        "date": candle_date,
                        "open": open_price,
                        "close": close,
                        "volume": volume,
                    }
                )

            day_matches = []
            for stock, candles in history.items():
                target_candle = next(
                    (candle for candle in candles if _as_date(candle["date"]) == trading_day),
                    None,
                )
                if target_candle is None:
                    continue
                body = target_candle["close"] - target_candle["open"]
                max_body = max(candle["close"] - candle["open"] for candle in candles)
                green = target_candle["close"] > target_candle["open"]
                body_is_max = body == max_body
                matched = green and body_is_max
                target_timestamp = _timestamp(target_candle["date"])
                condition_values = {
                    "data_available": True,
                    "lookback_candle_count": len(candles),
                    "target_body": body,
                    "max_body_last_30": max_body,
                    "green_candle": green,
                    "body_is_max": body_is_max,
                    "all_conditions": matched,
                }
                row = {
                    "stock": stock,
                    "scan_date": trading_day.isoformat(),
                    "direction": "BUY" if matched else "NEUTRAL",
                    "close": target_candle["close"],
                    "volume": target_candle["volume"],
                }
                if diagnostics is not None:
                    diagnostics.append(
                        {
                            "stock": stock,
                            "at": target_timestamp,
                            "interval": interval,
                            "condition_values": condition_values,
                            "matched": matched,
                            "matched_row": row if matched else None,
                        }
                    )
                if matched:
                    day_matches.append(row)
            day_matches.sort(
                key=lambda row: (row[sort_by], row["stock"]),
                reverse=sort_dir == "desc",
            )
            total += len(day_matches)
            all_matches[trading_day] = (
                day_matches if limit == 0 else day_matches[:limit]
            )

        matches = [
            row
            for trading_day in sorted(applied_dates, reverse=True)
            for row in all_matches.get(trading_day, [])
        ]
        if metadata is not None:
            metadata.update(
                {
                    "universe": universe_value,
                    "universe_description": universe_description,
                    "from_date": from_date.isoformat(),
                    "to_date": to_date.isoformat(),
                    "min_date": min_date.isoformat(),
                    "max_date": max_date.isoformat(),
                    "dates": [day.isoformat() for day in applied_dates],
                    "as_of": as_of.strftime("%Y-%m-%dT%H:%M"),
                    "total": total,
                    "sort_by": sort_by,
                    "sort_dir": sort_dir,
                }
            )
        return matches
    finally:
        client.close()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/scan")
def scan(
    interval: str | None = None,
    universe: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    as_of: str | None = None,
    symbols: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    limit: int = 50,
):
    params = {
        "interval": interval,
        "universe": universe,
        "from_date": from_date,
        "to_date": to_date,
        "as_of": as_of,
        "symbols": symbols,
        "sort_by": sort_by,
        "sort_dir": sort_dir,
        "limit": limit,
    }
    metadata: dict[str, Any] = {}
    try:
        matches = run_scan(params, metadata=metadata)
    except ScanParameterError as exc:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": exc.public_message},
        )

    universe_parameter = {
        "key": "universe",
        "label": "Universe",
        "options": [
            {"value": value, "label": label}
            for value, label in UNIVERSE_OPTIONS.items()
        ],
        "default": DEFAULT_UNIVERSE,
        "multi": True,
        "value": metadata["universe"],
    }
    criteria = (
        f"{metadata['universe_description']}; daily candles; green candle with "
        f"(close - open) equal to the maximum body of the last {LOOKBACK_CANDLES} "
        f"available candles; dates {metadata['from_date']} through "
        f"{metadata['to_date']}; as_of {metadata['as_of']}; "
        f"sorted by {metadata['sort_by']} {metadata['sort_dir']}"
    )
    presentation = {
        **PRESENTATION,
        "sort": {
            "key": metadata["sort_by"],
            "direction": metadata["sort_dir"],
        },
    }
    return {
        "success": True,
        "criteria": criteria,
        "columns": COLUMNS,
        "presentation": presentation,
        "parameters": [universe_parameter],
        "date_range": {
            "mode": "range",
            "from_date": metadata["from_date"],
            "to_date": metadata["to_date"],
            "default_days": DEFAULT_DAYS,
            "max_days": MAX_DAYS,
            "min_date": metadata["min_date"],
            "max_date": metadata["max_date"],
            "dates": metadata["dates"],
        },
        "direction": DIRECTION,
        "total": metadata["total"],
        "matches": matches,
    }


app = FastAPI(title=f"Strategy {JOB_ID}")
app.include_router(router)
