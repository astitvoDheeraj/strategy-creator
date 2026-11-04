"""Built-in setup rules — what the engine does with the stocks that newly appear in the screener.

A setup has no script of its own. Every decision comes from the setup's configuration (spec / legs / sizing /
guardrails); this module only turns the new signals into the generic actions the executors understand:

- alert mode without trade legs  -> `alert` per new stock
- trade legs defined, or paper / live mode -> `enter` per new stock (the alert executor records it as "would trade")

Direction (`spec.direction`): `{"mode": "fixed"}` (default, uses `legs.side`), `{"mode": "candle"}` (long when the signal
candle closed above its open, short when below) or `{"mode": "column", "column": "<screener column>"}` (long/short from
that column's text — buy/long/bull/up — or sign). `{"mode": "screener"}` is forced when the screener was built with
"Screen with Direction": its `direction` column (BUY / SELL / NEUTRAL) is the side, NEUTRAL matches never open a trade.
"""

DIRECTION_MODES = ("fixed", "candle", "column", "screener")
SCREENER_DIRECTION_COLUMN = "direction"
_LONG_WORDS = ("buy", "long", "bull", "up", "green")
_SHORT_WORDS = ("sell", "short", "bear", "down", "red")


def normalize_direction(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {"mode": raw} if isinstance(raw, str) else {}
    mode = str(raw.get("mode") or "fixed").strip().lower()
    if mode in ("signal_candle", "follow_candle", "candle_direction"):
        mode = "candle"
    if mode not in DIRECTION_MODES:
        mode = "fixed"
    if mode == "screener":
        return {"mode": "screener", "column": SCREENER_DIRECTION_COLUMN}
    column = str(raw.get("column") or "").strip()[:60] if mode == "column" else ""
    if mode == "column" and not column:
        mode = "fixed"
    return {"mode": mode, "column": column}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def screener_side(signal: dict) -> str:
    """'long' | 'short' | '' (NEUTRAL or missing) from a direction screener's row."""
    text = str((signal.get("row") or {}).get(SCREENER_DIRECTION_COLUMN) or "").strip().upper()
    if text in ("BUY", "LONG"):
        return "long"
    if text in ("SELL", "SHORT"):
        return "short"
    return ""


def enforce_screener_direction(actions: list, signals: list) -> tuple[list, list[str]]:
    """Correct module entry sides to match explicit screener direction; block neutral/missing directions."""
    by_stock = {str(s.get("stock") or "").upper(): s for s in signals if isinstance(s, dict)}
    out, corrections = [], []
    for action in actions or []:
        if not isinstance(action, dict) or str(action.get("type") or "").lower() != "enter":
            out.append(action)
            continue
        stock = str(action.get("stock") or "").upper()
        side = screener_side(by_stock.get(stock) or {})
        if not side:
            corrections.append(f"{stock or 'unknown'} entry blocked (screener direction is NEUTRAL or missing)")
            continue
        actual = str(action.get("side") or "").lower()
        if actual not in (side, "buy" if side == "long" else "sell"):
            corrections.append(f"{stock} side corrected from {actual or 'unspecified'} to {side}")
        out.append(dict(action, side=side))
    return out, corrections


def resolve_side(direction: dict, signal: dict, candles: list | None, default: str) -> str:
    """'long' | 'short' for one signal; falls back to `default` when the data needed is missing.
    Screener mode never falls back: '' means the screener said NEUTRAL (no trade)."""
    d = normalize_direction(direction)
    if d["mode"] == "screener":
        return screener_side(signal)
    if d["mode"] == "candle" and candles:
        last = candles[-1]
        o, c = _num(last.get("open")), _num(last.get("close"))
        if o is not None and c is not None and o != c:
            return "long" if c > o else "short"
    elif d["mode"] == "column":
        value = (signal.get("row") or {}).get(d["column"])
        n = _num(value)
        if n is not None and n != 0:
            return "long" if n > 0 else "short"
        text = str(value or "").lower()
        if any(w in text for w in _LONG_WORDS):
            return "long"
        if any(w in text for w in _SHORT_WORDS):
            return "short"
    return default


def evaluate(payload: dict) -> dict:
    """Engine payload -> {actions, notes, screener_params, has_override_key} (same shape the old module returned)."""
    setup = payload.get("setup") or {}
    spec = setup.get("spec") or {}
    legs = setup.get("legs") or spec.get("legs") or {}
    trade = bool(spec.get("trade_legs")) or setup.get("mode") in ("paper", "live")
    default_side = "short" if str(legs.get("side") or "long").lower() in ("short", "sell") else "long"
    candles = payload.get("candles") or {}
    screener_mode = normalize_direction(spec.get("direction"))["mode"] == "screener"
    actions, neutral = [], []
    for sig in (payload.get("signals") or {}).get("new") or []:
        stock = sig["stock"]
        if not trade:
            alert = {"type": "alert", "stock": stock}
            if screener_mode:
                alert["side"] = screener_side(sig) or "neutral"
            actions.append(alert)
            continue
        side = resolve_side(spec.get("direction"), sig, candles.get(stock), default_side)
        if not side:
            neutral.append(stock)
            continue
        actions.append({"type": "enter", "stock": stock, "side": side})
    notes = f"No trade for NEUTRAL screener signals: {', '.join(neutral)}" if neutral else ""
    return {"actions": actions, "notes": notes, "screener_params": {}, "has_override_key": False}
