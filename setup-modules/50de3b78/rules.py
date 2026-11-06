"""Screener-directed single-slot entries and session-close exits."""

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path


with Path(__file__).with_name("spec.json").open(encoding="utf-8") as spec_file:
    SPEC = json.load(spec_file)

OPEN_STATUSES = {"open", "pending_pullback", "partially_closed"}
STALL_SESSIONS = 5
FALLBACK_PCT = 2.5


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _value(row: dict, key: str, *labels):
    if key in row:
        return row[key]
    for label in labels:
        if label in row:
            return row[label]
    return None


def _day(value) -> date | None:
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


def _side(row: dict) -> str:
    direction = str(_value(row, "direction", "Direction") or "").strip().upper()
    return {"BUY": "long", "SELL": "short"}.get(direction, "")


def _alert(stock, row, side, reason):
    direction = str(_value(row, "direction", "Direction") or "UNKNOWN").strip().upper()
    symbol = _value(row, "stock", "Stock", "Symbol") or stock
    text = SPEC["alert"]["template"].format(
        symbol=symbol, direction=direction, reason=reason
    )
    return {
        "type": "alert",
        "stock": stock,
        "side": side or "neutral",
        "text": text,
    }


def _signals(payload):
    signals = payload.get("signals") or {}
    seen = set()
    result = []
    for index, signal in enumerate(signals.get("new") or []):
        if not isinstance(signal, dict):
            continue
        row = signal.get("row") or {}
        if not isinstance(row, dict):
            row = {}
        stock = signal.get("stock") or _value(row, "stock", "Stock", "Symbol")
        if not stock:
            continue
        key = str(stock).upper()
        if key in seen:
            continue
        seen.add(key)
        result.append((index, stock, row, _side(row)))
    return result


def _price(row, key, *labels):
    value = _number(_value(row, key, *labels))
    return value if value is not None and value > 0 else None


def _levels(row, side):
    close = _price(row, "close", "Close", "Last price (LTP)")
    stop = _price(row, "stop_price", "Stop", "stop")
    target = _price(row, "target_price", "Target", "target")
    if stop is not None and close is not None:
        if (side == "long" and stop >= close) or (side == "short" and stop <= close):
            stop = None
    if target is not None and close is not None:
        if (side == "long" and target <= close) or (side == "short" and target >= close):
            target = None
    stop_action = (
        {"kind": "price", "value": stop}
        if stop is not None
        else {"kind": "pct", "value": FALLBACK_PCT}
    )
    target_action = (
        {"kind": "price", "value": target}
        if target is not None
        else {"kind": "pct", "value": FALLBACK_PCT}
    )
    return stop_action, target_action


def _candles_for(payload, stock):
    candles = payload.get("candles") or {}
    if not isinstance(candles, dict):
        return []
    return candles.get(stock) or candles.get(str(stock).upper()) or []


def _position_sessions(position, candles):
    opened = _day(
        position.get("opened_at")
        or position.get("entry_time")
        or position.get("opened_on")
    )
    if opened is not None:
        sessions = {
            _day(
                candle.get("date")
                or candle.get("Date")
                or candle.get("timestamp")
                or candle.get("time")
                or candle.get("t")
            )
            for candle in candles
            if isinstance(candle, dict)
        }
        sessions.discard(None)
        count = sum(session >= opened for session in sessions)
        if count:
            return count
    for key in ("sessions_held", "days_held", "holding_days"):
        count = _number(position.get(key))
        if count is not None and count >= 0:
            return int(count)
    return 0


def _unrealized_pnl(position, candles, side):
    for key in ("unrealized_pnl", "unrealizedPnL"):
        pnl = _number(position.get(key))
        if pnl is not None:
            return pnl
    entry = _number(position.get("entry_price"))
    current = _number(position.get("ltp") or position.get("mark_price"))
    if candles:
        latest = max(
            (
                (_day(candle.get("date") or candle.get("Date") or candle.get("timestamp") or candle.get("t")), candle)
                for candle in candles
                if isinstance(candle, dict)
            ),
            key=lambda item: item[0] or date.min,
            default=(None, None),
        )[1]
        if latest:
            current = _number(latest.get("close", latest.get("Close", latest.get("c"))))
    if entry is None or current is None:
        return None
    return current - entry if side == "long" else entry - current


def _exit_action(position, candles, spec):
    if not isinstance(position, dict) or position.get("status") not in OPEN_STATUSES:
        return None
    holding = spec.get("holding_period") or {}
    maximum = _number(holding.get("max_days"))
    if maximum is None or maximum <= 0:
        maximum = 10
    stock = position.get("stock")
    if not stock:
        return None
    stock_candles = _candles_for({"candles": candles}, stock)
    sessions = _position_sessions(position, stock_candles)
    side = str(position.get("side") or position.get("direction") or "").lower()
    side = {"buy": "long", "sell": "short"}.get(side, side)
    pnl = _unrealized_pnl(position, stock_candles, side)
    reason = (
        f"maximum holding period reached ({int(maximum)} sessions)"
        if sessions >= maximum
        else (
            f"stall exit after {STALL_SESSIONS} sessions at non-positive unrealized PnL"
            if sessions >= STALL_SESSIONS and pnl is not None and pnl <= 0
            else ""
        )
    )
    if not reason:
        return None
    return {"type": "exit", "stock": stock, "reason": reason}


def evaluate(payload: dict) -> dict:
    setup = payload.get("setup") or {}
    spec = {**SPEC, **(setup.get("spec") or {})}
    trade = bool(spec.get("trade_legs")) or setup.get("mode") in ("paper", "live")
    sizing = setup.get("sizing") or spec.get("sizing") or {}
    capital = _number(sizing.get("per_position_cap"))
    actions = []
    positions = [
        position
        for position in payload.get("positions") or []
        if isinstance(position, dict) and position.get("status") in OPEN_STATUSES
    ]
    for position in positions:
        exit_action = _exit_action(position, payload.get("candles") or {}, spec)
        if exit_action:
            actions.append(exit_action)

    candidates = _signals(payload)
    occupied = bool(positions)
    if occupied:
        for _, stock, row, side in candidates:
            actions.append(_alert(stock, row, side, "skipped: the setup already has an open position"))
        return {
            "actions": actions,
            "notes": "",
            "screener_params": {},
            "has_override_key": False,
        }

    eligible = [(index, stock, row, side) for index, stock, row, side in candidates if side]
    selected = None
    if eligible:
        selected = max(
            eligible,
            key=lambda item: (
                (
                    _number(_value(item[2], "adx", "ADX"))
                    if _number(_value(item[2], "adx", "ADX")) is not None
                    else float("-inf")
                ),
                -item[0],
            ),
        )
    for _, stock, row, side in candidates:
        if not side:
            actions.append(_alert(stock, row, side, "skipped: signal direction is NEUTRAL or missing"))
        elif selected is not None and stock != selected[1]:
            actions.append(_alert(stock, row, side, "skipped: a higher-ADX signal was selected"))
        elif selected is not None and stock == selected[1]:
            if trade and (capital is None or capital <= 0):
                actions.append(_alert(stock, row, side, "skipped: invalid runtime position cap"))
            else:
                actions.append(_alert(stock, row, side, "taken: highest-ADX signal while flat"))
            if trade and capital is not None and capital > 0:
                stop, target = _levels(row, side)
                actions.append(
                    {
                        "type": "enter",
                        "stock": stock,
                        "side": side,
                        "sizing": {"kind": "capital", "value": capital},
                        "sl": stop,
                        "targets": [
                            {**target, "qty_pct": 100.0}
                        ],
                    }
                )
        else:
            actions.append(_alert(stock, row, side, "skipped: no eligible signal"))

    return {
        "actions": actions,
        "notes": "",
        "screener_params": {},
        "has_override_key": False,
    }
