"""Translate new screener matches into alerts and screener-directed entries."""

import json
import math
from datetime import datetime
from pathlib import Path


with Path(__file__).with_name("spec.json").open(encoding="utf-8") as spec_file:
    SPEC = json.load(spec_file)


def _value(row: dict, key: str, label: str):
    return row[key] if key in row else row.get(label)


def _capital(value):
    try:
        capital = float(value)
        return capital if math.isfinite(capital) and capital > 0 else None
    except (TypeError, ValueError):
        return None


def _date_time(row: dict, run: dict) -> tuple[str, str]:
    candle_ts = run.get("candle_ts")
    date = _value(row, "date", "Date") or candle_ts or ""
    time = run.get("time_ist") or ""
    if not time and isinstance(candle_ts, str):
        try:
            time = datetime.fromisoformat(candle_ts.replace("Z", "+00:00")).strftime(
                "%H:%M"
            )
        except ValueError:
            pass
    if isinstance(date, str):
        date = date[:10]
    return str(time), str(date)


def _side(row: dict) -> str:
    direction = str(_value(row, "direction", "Direction") or "").strip().upper()
    return {"BUY": "long", "SELL": "short"}.get(direction, "")


def _date_key(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).date().isoformat()
        except ValueError:
            return ""
    return ""


def _price(candle: dict, key: str):
    value = candle.get(key, candle.get(key.capitalize()))
    try:
        price = float(value)
        return price if math.isfinite(price) else None
    except (TypeError, ValueError):
        return None


def _engulfing_body(row: dict, signal: dict, run: dict, candles_by_stock: dict):
    signal_date = _date_key(
        _value(row, "date", "Date") or signal.get("date") or run.get("candle_ts")
    )
    stock = signal.get("stock") or _value(row, "stock", "Symbol")
    stock_key = str(stock)
    candles = (
        candles_by_stock.get(stock_key)
        or candles_by_stock.get(stock_key.upper())
        or []
    )
    if not signal_date or not isinstance(candles, list):
        return None

    current = None
    previous = {}
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        candle_date = _date_key(
            candle.get("date", candle.get("Date", candle.get("timestamp")))
        )
        if candle_date == signal_date:
            current = candle
        elif candle_date and candle_date < signal_date and candle_date not in previous:
            previous[candle_date] = candle

    if current is None:
        current = row
    prior = previous[max(previous)] if previous else None
    if prior is None:
        return None

    current_open, current_close = _price(current, "open"), _price(current, "close")
    prior_open, prior_close = _price(prior, "open"), _price(prior, "close")
    if None in (current_open, current_close, prior_open, prior_close):
        return None
    if (
        prior_close < prior_open
        and current_close > current_open
        and current_open <= prior_close
        and current_close >= prior_open
    ):
        return current_close - current_open
    return None


def evaluate(payload: dict) -> dict:
    setup = payload.get("setup") or {}
    spec = {**SPEC, **(setup.get("spec") or {})}
    trade = bool(spec.get("trade_legs")) or setup.get("mode") in ("paper", "live")
    sizing = setup.get("sizing") or spec.get("sizing") or {}
    capital = _capital(sizing.get("per_position_cap"))
    run = payload.get("run") or {}
    positions = payload.get("positions") or []
    occupied = {
        str(position.get("stock") or "").upper()
        for position in positions
        if isinstance(position, dict)
        and position.get("status") in ("open", "pending_pullback", "partially_closed")
    }

    actions = []
    neutral = []
    seen = set()
    signals = payload.get("signals") or {}
    candle_data = payload.get("candles") or {}
    candles_by_stock = candle_data if isinstance(candle_data, dict) else {}
    candidates = []
    for signal in signals.get("new") or []:
        if not isinstance(signal, dict):
            continue
        row = signal.get("row") or {}
        if not isinstance(row, dict):
            row = {}
        stock = signal.get("stock") or _value(row, "stock", "Symbol")
        if not stock or str(stock).upper() in seen:
            continue
        seen.add(str(stock).upper())
        body = _engulfing_body(row, signal, run, candles_by_stock)
        if body is not None:
            candidates.append((body, signal, row, stock))

    selected = [max(candidates, key=lambda candidate: candidate[0])] if candidates else []
    for _, signal, row, stock in selected:
        side = _side(row)
        time, date = _date_time(row, run)
        actions.append(
            {
                "type": "alert",
                "stock": stock,
                "side": side or "neutral",
                "text": spec["alert"]["template"].format(
                    stock=_value(row, "stock", "Symbol") or stock,
                    time=time,
                    date=date,
                    screener=spec.get("screener_title", ""),
                ),
            }
        )
        if not trade:
            continue
        if not side:
            neutral.append(str(stock))
            continue
        if str(stock).upper() in occupied or capital is None:
            continue
        actions.append(
            {
                "type": "enter",
                "stock": stock,
                "side": side,
                "sizing": {"kind": "capital", "value": capital},
                "sl": spec["legs"]["sl"],
            }
        )
        occupied.add(str(stock).upper())

    notes = (
        f"No trade for NEUTRAL or unknown screener signals: {', '.join(neutral)}"
        if neutral
        else ""
    )
    return {
        "actions": actions,
        "notes": notes,
        "screener_params": {},
        "has_override_key": False,
    }
