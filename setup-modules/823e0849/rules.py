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
