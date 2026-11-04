"""Alerts and screener-directed entries; the engine manages scheduling and trade legs."""

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path


with Path(__file__).with_name("spec.json").open(encoding="utf-8") as spec_file:
    SPEC = json.load(spec_file)


def _num(v):
    try:
        value = float(v)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _value(row: dict, key: str, label: str):
    return row[key] if key in row else row.get(label)


def _candle_date(candle: dict) -> date | None:
    value = next(
        (
            candle[key]
            for key in ("date", "Date", "timestamp", "time", "t")
            if key in candle
        ),
        None,
    )
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        timestamp = value / 1000 if abs(value) >= 1_000_000_000_000 else value
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
            except ValueError:
                return None
    return None


def _prior_stop(candles: list, signal_date: date, side: str) -> float | None:
    extremes = {}
    price_key = "high" if side == "short" else "low"
    short_key = "h" if side == "short" else "l"
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        candle_date = _candle_date(candle)
        price = _num(candle.get(price_key, candle.get(short_key)))
        if candle_date and candle_date < signal_date and price is not None:
            extremes[candle_date] = (
                max(extremes.get(candle_date, price), price)
                if side == "short"
                else min(extremes.get(candle_date, price), price)
            )
    prior = sorted(extremes.items(), reverse=True)[:3]
    if len(prior) != 3:
        return None
    return (
        max(price for _, price in prior)
        if side == "short"
        else min(price for _, price in prior)
    )


def screener_side(signal: dict) -> str:
    """'long' | 'short' | '' (NEUTRAL or missing) from a direction screener's row."""
    row = signal.get("row") or {}
    text = str(_value(row, "direction", "Direction") or "").strip().upper()
    return {"BUY": "long", "SELL": "short"}.get(text, "")


def evaluate(payload: dict) -> dict:
    """Engine payload -> {actions, notes, screener_params, has_override_key} (same shape the old module returned)."""
    setup = payload.get("setup") or {}
    spec = {**SPEC, **(setup.get("spec") or {})}
    trade = bool(spec.get("trade_legs")) or setup.get("mode") in ("paper", "live")
    sizing = setup.get("sizing") or spec.get("sizing") or {}
    capital = _num(sizing.get("per_position_cap"))
    occupied = {
        str(position.get("stock") or "").upper()
        for position in payload.get("positions") or []
        if position.get("status") in ("open", "pending_pullback", "partially_closed")
    }
    actions, neutral = [], []
    for sig in (payload.get("signals") or {}).get("new") or []:
        if not isinstance(sig, dict) or not sig.get("stock"):
            continue
        stock = sig["stock"]
        row = sig.get("row") or {}
        if not isinstance(row, dict):
            row = {}
        side = screener_side({"row": row})
        direction = str(_value(row, "direction", "Direction") or "UNKNOWN").strip().upper()
        actions.append({
            "type": "alert",
            "stock": stock,
            "side": side or "neutral",
            "text": SPEC["alert"]["template"].format(
                symbol=_value(row, "stock", "Symbol") or stock,
                direction=direction,
                ltp=_value(row, "close", "Last price (LTP)"),
                volume=_value(row, "volume", "Volume"),
            ),
        })
        if not trade:
            continue
        if not side:
            neutral.append(stock)
            continue
        if str(stock).upper() in occupied or capital is None or capital <= 0:
            continue
        candle_data = payload.get("candles") or {}
        candles = (
            candle_data.get(stock) or candle_data.get(str(stock).upper()) or []
            if isinstance(candle_data, dict)
            else []
        )
        signal_date_value = (
            _value(row, "date", "Date")
            or sig.get("date")
            or (payload.get("run") or {}).get("candle_ts")
        )
        signal_date = _candle_date({"date": signal_date_value})
        if signal_date is None:
            continue
        stop = _prior_stop(candles, signal_date, side)
        if stop is None:
            continue
        actions.append({
            "type": "enter",
            "stock": stock,
            "side": side,
            "sizing": {"kind": "capital", "value": capital},
            "sl": {"kind": "price", "value": stop},
        })
        occupied.add(str(stock).upper())
    notes = f"No trade for NEUTRAL or unknown screener signals: {', '.join(neutral)}" if neutral else ""
    return {"actions": actions, "notes": notes, "screener_params": {}, "has_override_key": False}
