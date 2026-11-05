"""Focused tests for setup 823e0849."""

import json
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

import main
from rules import SPEC


client = TestClient(main.app)


def _payload(new=("AAA",)):
    return {
        "setup": {
            "setup_id": "823e0849",
            "mode": "alert",
            "spec": deepcopy(SPEC),
            "sizing": deepcopy(SPEC["sizing"]),
        },
        "run": {"candle_ts": "2026-10-28T15:30:00", "interval": "DAILY"},
        "signals": {
            "new": [
                {
                    "stock": stock,
                    "row": {
                        "Symbol": stock,
                        "Date": "2026-10-28",
                        "Direction": "BUY",
                    },
                }
                for stock in new
            ],
            "still": [],
            "dropped": [],
        },
        "candles": {
            stock: [
                {"date": "2026-10-27", "open": 100, "close": 99},
                {"date": "2026-10-28", "open": 98, "close": 120},
            ]
            for stock in new
        },
        "positions": [],
    }


def test_health_and_manifest():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/manifest").json() == {"parameters": [], "panels": []}


def test_fixed_configuration():
    assert SPEC["setup_id"] == "823e0849"
    assert SPEC["screener_job_id"] == "777a9e34"
    assert SPEC["screener_params"] == {"universe": "nifty_200"}
    assert SPEC["interval"] == "DAILY"
    assert SPEC["dedupe_policy"] == "once_per_day"
    assert SPEC["alert"] == {
        "template": "New stock alert: {stock} at {time} on {date} - {screener}.",
        "channels": ["inapp"],
        "include_columns": [],
    }
    assert SPEC["direction"] == {"mode": "screener", "column": "direction"}
    assert SPEC["legs"]["sl"] == {"kind": "range", "value": 1.0}
    assert SPEC["legs"]["targets"] == []
    assert SPEC["holding_period"] == {
        "mode": "multi_day",
        "max_days": 60,
        "intraday_close_time": "15:15",
    }
    assert SPEC["sizing"]["capital"] == 1000000
    assert SPEC["sizing"]["per_position_cap"] == 25000
    assert SPEC["sizing"]["max_positions"] == 3
    assert SPEC["sizing"]["product"] == "CNC"
    assert SPEC["data_wait"] == {"max_wait_sec": 600}
    assert SPEC["custom_instructions"] == (
        "FIRST CHECK SHOULD BE BULLISH ENGULFING CANDLE PATTERN, OUT OF ALL "
        "BULLISH ENGULFING FOUND FOR THE DAY , THE BIGGEST ONE WILL BE PICKED"
    )


def test_recorded_specification_preserves_refinement_summary_and_changes():
    manifest = json.loads(Path(__file__).with_name("manifest.json").read_text())
    assert manifest["setup_id"] == SPEC["setup_id"]
    assert manifest["screener_job_id"] == SPEC["screener_job_id"]
    prompt = manifest["prompt"]
    assert "## Refinement Summary" in prompt
    assert "**Changed:**" in prompt
    assert "largest green candle body" in prompt
    assert SPEC["custom_instructions"] in prompt


def test_new_buy_signal_alert_and_entry():
    actions = client.post("/evaluate", json=_payload()).json()["actions"]
    assert actions == [
        {
            "type": "alert",
            "stock": "AAA",
            "side": "long",
            "text": "New stock alert: AAA at 15:30 on 2026-10-28 - Biggest Green Candle Finder.",
        },
        {
            "type": "enter",
            "stock": "AAA",
            "side": "long",
            "sizing": {"kind": "capital", "value": 25000.0},
            "sl": {"kind": "range", "value": 1.0},
        },
    ]


def test_screener_direction_and_neutral_alert_only():
    payload = _payload()
    payload["signals"]["new"][0]["row"]["Direction"] = "SELL"
    actions = client.post("/evaluate", json=payload).json()["actions"]
    assert [action.get("side") for action in actions] == ["short", "short"]

    payload["signals"]["new"][0]["row"]["Direction"] = "NEUTRAL"
    result = client.post("/evaluate", json=payload).json()
    assert [action["type"] for action in result["actions"]] == ["alert"]
    assert result["actions"][0]["side"] == "neutral"


def test_only_new_signals_are_processed_and_duplicate_stock_is_not_repeated():
    payload = _payload(("AAA", "AAA"))
    payload["signals"]["still"] = [{"stock": "BBB", "row": {"Direction": "BUY"}}]
    result = client.post("/evaluate", json=payload).json()
    assert [action["stock"] for action in result["actions"]] == ["AAA", "AAA"]


def test_only_largest_bullish_engulfing_new_signal_is_alerted_and_entered():
    payload = _payload(("AAA", "BBB"))
    payload["candles"]["BBB"][-1].update(open=95, close=130)
    actions = client.post("/evaluate", json=payload).json()["actions"]
    assert [action["stock"] for action in actions] == ["BBB", "BBB"]


def test_non_engulfing_match_is_not_alerted_or_entered():
    payload = _payload()
    payload["candles"]["AAA"][-1].update(open=100, close=120)
    assert client.post("/evaluate", json=payload).json()["actions"] == []


def test_missing_candle_history_does_not_pass_pattern_check():
    payload = _payload()
    payload["candles"] = {}
    assert client.post("/evaluate", json=payload).json()["actions"] == []


def test_runtime_position_cap_and_existing_positions():
    payload = _payload()
    payload["setup"]["sizing"]["per_position_cap"] = 40000
    payload["positions"] = [{"stock": "AAA", "status": "open"}]
    assert [action["type"] for action in client.post("/evaluate", json=payload).json()["actions"]] == ["alert"]

    payload["positions"] = []
    actions = client.post("/evaluate", json=payload).json()["actions"]
    assert actions[1]["sizing"] == {"kind": "capital", "value": 40000.0}
