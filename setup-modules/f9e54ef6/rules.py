"""Alerts and screener-directed entries; the engine manages scheduling and trade legs."""

import json
import math
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
        actions.append({
            "type": "enter",
            "stock": stock,
            "side": side,
            "sizing": {"kind": "capital", "value": capital},
        })
        occupied.add(str(stock).upper())
    notes = f"No trade for NEUTRAL or unknown screener signals: {', '.join(neutral)}" if neutral else ""
    return {"actions": actions, "notes": notes, "screener_params": {}, "has_override_key": False}
