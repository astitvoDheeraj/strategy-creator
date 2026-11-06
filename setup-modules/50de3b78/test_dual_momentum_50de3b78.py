from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient


def _load_module(name, path):
    module_spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


MODULE_DIR = Path(__file__).parent
rules = _load_module("setup_50de3b78_rules", MODULE_DIR / "rules.py")
existing_rules = sys.modules.get("rules")
sys.modules["rules"] = rules
try:
    main = _load_module("setup_50de3b78_main", MODULE_DIR / "main.py")
finally:
    if existing_rules is None:
        sys.modules.pop("rules", None)
    else:
        sys.modules["rules"] = existing_rules


client = TestClient(main.app)
SPEC = rules.SPEC


def _signal(stock, adx=20, direction="BUY", **row):
    return {
        "stock": stock,
        "row": {
            "stock": stock,
            "date": "2026-10-28",
            "direction": direction,
            "close": 100,
            "stop_price": 95,
            "target_price": 110,
            "adx": adx,
            **row,
        },
    }


def _payload(signals=None):
    return {
        "setup": {
            "setup_id": "50de3b78",
            "mode": "alert",
            "spec": deepcopy(SPEC),
            "sizing": deepcopy(SPEC["sizing"]),
        },
        "run": {"candle_ts": "2026-10-28T15:30:00", "interval": "DAILY"},
        "signals": {"new": signals or [], "still": [], "dropped": []},
        "positions": [],
        "candles": {},
    }


def test_health_manifest_and_configuration():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/manifest").json() == {"parameters": [], "panels": []}
    assert SPEC["setup_id"] == "50de3b78"
    assert SPEC["screener_job_id"] == "23f1962b"
    assert SPEC["direction"] == {"mode": "screener", "column": "direction"}
    assert SPEC["holding_period"] == {
        "mode": "multi_day",
        "max_days": 10,
        "intraday_close_time": "15:15",
    }
    assert SPEC["sizing"]["max_positions"] == 1
    assert SPEC["guardrails"]["max_trades_per_day"] == 1


def test_only_highest_adx_new_signal_is_entered_and_all_are_alerted():
    payload = _payload([_signal("AAA", 25), _signal("BBB", 41), _signal("CCC", 32)])
    actions = client.post("/evaluate", json=payload).json()["actions"]
    assert [action["type"] for action in actions] == [
        "alert", "alert", "enter", "alert"
    ]
    assert [action["stock"] for action in actions if action["type"] == "enter"] == ["BBB"]
    assert "higher-ADX" in actions[0]["text"]
    assert "taken" in actions[1]["text"]
    assert "higher-ADX" in actions[3]["text"]


def test_screener_direction_and_neutral_skip():
    payload = _payload([_signal("LONG", direction="BUY"), _signal("SHORT", direction="SELL", adx=30),
                        _signal("NONE", direction="NEUTRAL", adx=99)])
    actions = client.post("/evaluate", json=payload).json()["actions"]
    entries = [action for action in actions if action["type"] == "enter"]
    assert [(action["stock"], action["side"]) for action in entries] == [("SHORT", "short")]
    assert any("NEUTRAL or missing" in action.get("text", "") for action in actions)


def test_entry_uses_signal_levels_runtime_cap_and_directional_percent_fallback():
    payload = _payload([_signal("AAA", stop_price=None, target_price="bad")])
    payload["setup"]["sizing"]["per_position_cap"] = 75000
    entry = next(action for action in client.post("/evaluate", json=payload).json()["actions"]
                 if action["type"] == "enter")
    assert entry["sizing"] == {"kind": "capital", "value": 75000}
    assert entry["sl"] == {"kind": "pct", "value": 2.5}
    assert entry["targets"] == [{"kind": "pct", "value": 2.5, "qty_pct": 100.0}]

    payload = _payload([_signal("BBB", direction="SELL", stop_price=105, target_price=90)])
    entry = next(action for action in client.post("/evaluate", json=payload).json()["actions"]
                 if action["type"] == "enter")
    assert entry["side"] == "short"
    assert entry["sl"] == {"kind": "price", "value": 105}
    assert entry["targets"] == [{"kind": "price", "value": 90, "qty_pct": 100.0}]


def test_invalid_runtime_cap_reports_a_skipped_signal_without_entry():
    payload = _payload([_signal("AAA")])
    payload["setup"]["sizing"]["per_position_cap"] = 0
    actions = client.post("/evaluate", json=payload).json()["actions"]
    assert [action["type"] for action in actions] == ["alert"]
    assert "skipped: invalid runtime position cap" in actions[0]["text"]


def test_invalid_levels_fall_back_to_percent():
    payload = _payload([_signal("AAA", stop_price=105, target_price=90)])
    entry = next(action for action in client.post("/evaluate", json=payload).json()["actions"]
                 if action["type"] == "enter")
    assert entry["sl"] == {"kind": "pct", "value": 2.5}
    assert entry["targets"][0]["kind"] == "pct"


def test_open_position_skips_every_new_signal_with_reason():
    payload = _payload([_signal("AAA"), _signal("BBB", adx=30)])
    payload["positions"] = [{"stock": "OPEN", "status": "open", "side": "long"}]
    actions = client.post("/evaluate", json=payload).json()["actions"]
    assert [action["type"] for action in actions] == ["alert", "alert"]
    assert all("open position" in action["text"] for action in actions)


def test_position_exits_at_five_sessions_when_stalled_or_ten_sessions_anyway():
    payload = _payload()
    payload["positions"] = [
        {"stock": "STALL", "status": "open", "side": "long", "entry_price": 100,
         "opened_at": "2026-10-20T09:15:00"},
        {"stock": "PROFIT", "status": "open", "side": "long", "entry_price": 100,
         "opened_at": "2026-10-20T09:15:00"},
    ]
    payload["candles"] = {
        stock: [
            {"date": f"2026-10-{day}", "close": 99 if stock == "STALL" else 105}
            for day in ("20", "21", "22", "23", "26")
        ]
        for stock in ("STALL", "PROFIT")
    }
    actions = client.post("/evaluate", json=payload).json()["actions"]
    assert actions == [{
        "type": "exit",
        "stock": "STALL",
        "reason": "stall exit after 5 sessions at non-positive unrealized PnL",
    }]

    payload["positions"][1]["opened_at"] = "2026-10-07T09:15:00"
    payload["candles"]["PROFIT"] = [
        {"date": f"2026-10-{day}", "close": 105}
        for day in ("07", "08", "09", "12", "13", "14", "15", "16", "19", "20")
    ]
    actions = client.post("/evaluate", json=payload).json()["actions"]
    assert {action["stock"] for action in actions if action["type"] == "exit"} == {
        "STALL", "PROFIT"
    }
    assert any("maximum holding period" in action["reason"] for action in actions)


def test_runtime_holding_period_overrides_creation_default():
    payload = _payload()
    payload["setup"]["spec"]["holding_period"]["max_days"] = 3
    payload["positions"] = [
        {
            "stock": "AAA",
            "status": "open",
            "side": "long",
            "entry_price": 100,
            "opened_at": "2026-10-26T09:15:00",
        }
    ]
    payload["candles"] = {
        "AAA": [
            {"date": "2026-10-26", "close": 105},
            {"date": "2026-10-27", "close": 105},
            {"date": "2026-10-28", "close": 105},
        ]
    }
    actions = client.post("/evaluate", json=payload).json()["actions"]
    assert actions == [
        {
            "type": "exit",
            "stock": "AAA",
            "reason": "maximum holding period reached (3 sessions)",
        }
    ]


def test_unknown_panel_is_not_found_and_manifest_records_spec():
    assert client.post("/panels/unknown", json={}).status_code == 404
    manifest = json.loads(Path(__file__).with_name("manifest.json").read_text())
    assert manifest["setup_id"] == SPEC["setup_id"]
    assert SPEC["custom_instructions"] in manifest["prompt"]
