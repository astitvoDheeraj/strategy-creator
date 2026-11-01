import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI


MODULE_DIR = Path(__file__).parent
with (MODULE_DIR / "spec.json").open(encoding="utf-8") as spec_file:
    SPEC = json.load(spec_file)

ALERT_FIELDS = (
    ("Symbol", ("Symbol", "symbol")),
    ("Last price (LTP)", ("Last price (LTP)", "close")),
    ("Volume", ("Volume", "volume")),
    ("Candle time", ("Candle time", "date")),
    ("Avg volume (20)", ("Avg volume (20)", "vma20")),
    ("SMA 20", ("SMA 20", "sma20")),
    ("SMA 50", ("SMA 50", "sma50")),
    ("SMA 200", ("SMA 200", "sma200")),
    ("SuperTrend", ("SuperTrend", "superTrend")),
)

app = FastAPI()


def _value(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return "N/A"


def evaluate(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    actions = []
    signals = payload.get("signals")
    if not isinstance(signals, dict) or not isinstance(signals.get("new"), list):
        return {"actions": actions}
    for signal in signals["new"]:
        if not isinstance(signal, dict):
            continue
        row = signal.get("row", {})
        if not isinstance(row, dict):
            continue
        message = "\n".join(
            f"{label}: {_value(row, keys)}" for label, keys in ALERT_FIELDS
        )
        actions.append(
            {
                "type": "alert",
                "symbol": _value(row, ("Symbol", "symbol")),
                "direction": _value(row, ("Direction", "direction")),
                "channels": SPEC["alert"]["channels"],
                "message": message,
            }
        )
    return {"actions": actions}


@app.post("/evaluate")
def evaluate_route(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return evaluate(payload)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
