import os
from datetime import date, datetime
from typing import Literal

import clickhouse_connect
from fastapi import APIRouter, FastAPI, Query


router = APIRouter(prefix="/strategy-api/deb31fcc")
app = FastAPI()

UNIVERSE_OPTIONS = [
    {"value": "nifty_50", "label": "NIFTY 50"},
    {"value": "nifty_next_50", "label": "NIFTY NEXT 50"},
    {"value": "nifty_100", "label": "NIFTY 100"},
    {"value": "nifty_200", "label": "NIFTY 200"},
    {"value": "nifty_500", "label": "NIFTY 500"},
    {"value": "nifty_midcap_50", "label": "NIFTY MIDCAP 50"},
    {"value": "nifty_midcap_100", "label": "NIFTY MIDCAP 100"},
    {"value": "nifty_midcap_150", "label": "NIFTY MIDCAP 150"},
    {"value": "nifty_smallcap_50", "label": "NIFTY SMALLCAP 50"},
    {"value": "nifty_smallcap_100", "label": "NIFTY SMALLCAP 100"},
    {"value": "nifty_smallcap_250", "label": "NIFTY SMALLCAP 250"},
    {"value": "all", "label": "All NSE stocks"},
]
DEFAULT_UNIVERSE = "nifty_50"

COLUMNS = [
    {"key": "stock", "label": "Symbol", "format": "text"},
    {"key": "close", "label": "Last price (LTP)", "format": "price"},
    {"key": "sma20", "label": "SMA 20", "format": "price"},
    {"key": "sma50", "label": "SMA 50", "format": "price"},
    {"key": "sma200", "label": "SMA 200", "format": "price"},
    {"key": "atr", "label": "ATR", "format": "number"},
    {"key": "macdSignal", "label": "MACD signal", "format": "number"},
    {"key": "low", "label": "Low", "format": "price"},
    {"key": "date", "label": "Candle time", "format": "datetime"},
]

PRESENTATION = {
    "style": "compact_table",
    "sort": {"key": "close", "direction": "desc"},
    "ui_filters": ["symbol_search", "column_sort"],
    "limit": 50,
    "page_size": 20,
    "pagination": "pages",
    "notes": "",
    "group_by_date": True,
    "date_chips": True,
}


def _clickhouse_client():
    secure = os.getenv("CLICKHOUSE_SECURE", "false").strip().lower() in {
        "true",
        "1",
        "yes",
    }
    return clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "clickhouse"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USERNAME", "default"),
        **{"password": os.getenv("CLICKHOUSE_PASSWORD", "")},
        database=os.getenv("CLICKHOUSE_DATABASE", "default"),
        secure=secure,
    )


def _number(value):
    return float(value) if value is not None else None


def _candle_time(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _normalize_universe(value):
    selected = {
        item.strip().lower()
        for item in (value or "").split(",")
        if item.strip()
    }
    allowed = {option["value"] for option in UNIVERSE_OPTIONS}
    if not selected:
        return "all", None
    if not selected <= allowed:
        return DEFAULT_UNIVERSE, [DEFAULT_UNIVERSE]
    if "all" in selected:
        return "all", None
    ordered = [
        option["value"]
        for option in UNIVERSE_OPTIONS
        if option["value"] in selected and option["value"] != "all"
    ]
    return ",".join(ordered), ordered


def _universe_parameter(value):
    return {
        "key": "universe",
        "label": "Universe",
        "options": UNIVERSE_OPTIONS,
        "default": DEFAULT_UNIVERSE,
        "multi": True,
        "value": value,
    }


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/scan")
def scan(
    limit: int = Query(default=50, ge=0),
    sort_by: Literal[
        "stock", "close", "sma20", "sma50", "sma200", "atr", "macdSignal", "low", "date"
    ] = "close",
    sort_dir: Literal["asc", "desc"] = "desc",
    universe: str = DEFAULT_UNIVERSE,
):
    applied_universe, universe_ids = _normalize_universe(universe)
    client = _clickhouse_client()
    try:
        stocks = None
        if universe_ids is not None:
            universe_result = client.query(
                """
                SELECT DISTINCT symbol
                FROM universe_members FINAL
                WHERE universe_id IN {universe_ids:Array(String)}
                  AND active = 1
                """,
                parameters={"universe_ids": universe_ids},
            )
            stocks = [row[0] for row in universe_result.result_rows]

        stock_filter = ""
        parameters = {}
        if stocks is not None:
            stock_filter = "AND stock IN {stocks:Array(String)}"
            parameters["stocks"] = stocks

        result = client.query(
            f"""
            WITH
                latest_session AS (
                    SELECT toDate(max(date)) AS session_date
                    FROM stock__candles_data
                    WHERE interval = '5'
                      {stock_filter}
                ),
                first_candles AS (
                    SELECT stock, min(date) AS first_date
                    FROM stock__candles_data
                    WHERE interval = '5'
                      {stock_filter}
                      AND toDate(date) = (SELECT session_date FROM latest_session)
                    GROUP BY stock
                ),
                previous_candles AS (
                    SELECT candles.stock, max(candles.date) AS previous_date
                    FROM stock__candles_data AS candles
                    INNER JOIN first_candles AS fc
                        ON candles.stock = fc.stock
                    WHERE candles.interval = '5'
                      AND candles.date < fc.first_date
                    GROUP BY candles.stock
                )
            SELECT
                current_candle.stock, current_candle.date, current_candle.low,
                current_candle.close, current_candle.sma20, current_candle.sma50,
                current_candle.sma200, current_candle.atr,
                current_candle.macdSignal, current_candle.volume, current_candle.vma20,
                previous_candle.close, previous_candle.sma20,
                previous_candle.sma50, previous_candle.sma200
            FROM first_candles AS fc
            INNER JOIN stock__candles_data AS current_candle
                ON current_candle.stock = fc.stock
                AND current_candle.interval = '5'
                AND current_candle.date = fc.first_date
            LEFT JOIN previous_candles AS prior
                ON prior.stock = fc.stock
            LEFT JOIN stock__candles_data AS previous_candle
                ON previous_candle.stock = prior.stock
                AND previous_candle.interval = '5'
                AND previous_candle.date = prior.previous_date
            ORDER BY current_candle.stock
            """,
            parameters=parameters,
        )
    finally:
        client.close()

    matches = []
    for row in result.result_rows:
        (
            stock,
            candle_date,
            low,
            close,
            sma20,
            sma50,
            sma200,
            atr,
            macd_signal,
            volume,
            vma20,
            previous_close,
            previous_sma20,
            previous_sma50,
            previous_sma200,
        ) = row
        current_values = [_number(value) for value in (close, sma20, sma50, sma200)]
        previous_values = [
            _number(value)
            for value in (
                previous_close,
                previous_sma20,
                previous_sma50,
                previous_sma200,
            )
        ]
        current_close, current_sma20, current_sma50, current_sma200 = current_values
        previous_close, previous_sma20, previous_sma50, previous_sma200 = previous_values

        if (
            any(value is None for value in current_values + previous_values)
            or volume is None
            or vma20 is None
            or volume <= 0
            or vma20 <= 0
            or not (
                previous_close <= previous_sma20
                and current_close > current_sma20
                and previous_close <= previous_sma50
                and current_close > current_sma50
                and previous_close <= previous_sma200
                and current_close > current_sma200
            )
        ):
            continue

        matches.append(
            {
                "stock": str(stock),
                "close": current_close,
                "sma20": current_sma20,
                "sma50": current_sma50,
                "sma200": current_sma200,
                "atr": _number(atr),
                "macdSignal": _number(macd_signal),
                "low": _number(low),
                "date": _candle_time(candle_date),
            }
        )

    sortable = [match for match in matches if match[sort_by] is not None]
    missing = [match for match in matches if match[sort_by] is None]
    sortable.sort(
        key=lambda match: (
            match[sort_by].casefold()
            if isinstance(match[sort_by], str)
            else match[sort_by]
        ),
        reverse=sort_dir == "desc",
    )
    matches = sortable + missing

    return {
        "success": True,
        "criteria": (
            f"Universe {applied_universe}; 5-minute candles: first candle of the "
            "latest session closes above SMA 20, SMA 50, and SMA 200 after the "
            "previous candle closed at or below each SMA; exclude stocks with "
            "zero first-candle volume or zero 20-candle average volume"
        ),
        "columns": COLUMNS,
        "presentation": PRESENTATION,
        "parameters": [_universe_parameter(applied_universe)],
        "total": len(matches),
        "matches": matches[:limit] if limit else matches,
    }


app.include_router(router)
