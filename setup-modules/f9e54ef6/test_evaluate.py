from copy import deepcopy
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

import main
from rules import SPEC

client = TestClient(main.app)


def _payload(mode="alert", spec=None, new=("AAA", "BBB")):
    return {
        "setup": {"setup_id": "f9e54ef6", "mode": mode, "spec": deepcopy(SPEC) if spec is None else spec,
                  "params": {}, "legs": deepcopy(SPEC["legs"]), "sizing": deepcopy(SPEC["sizing"])},
        "run": {"run_id": "r", "candle_ts": "2026-10-28T15:30:00", "interval": "DAILY"},
        "matches": [{"stock": s} for s in new],
        "signals": {"new": [{"stock": s, "row": {"stock": s, "date": "2026-10-28",
                                                "direction": "BUY", "close": 120,
                                                "volume": 5000}} for s in new], "still": [], "dropped": []},
        "positions": [], "candles": {
            s: [
                {"date": "2026-10-27", "low": 97, "high": 103},
                {"date": "2026-10-26", "low": 95, "high": 104},
                {"date": "2026-10-25", "low": 96, "high": 102},
            ] for s in new
        }, "sectors": {}, "market": {},
    }


def test_health_and_manifest():
    assert client.get("/health").json() == {"status": "ok"}
    m = client.get("/manifest").json()
    assert isinstance(m["parameters"], list) and isinstance(m["panels"], list)


def test_one_action_per_new_signal():
    res = client.post("/evaluate", json=_payload(spec={"trade_legs": False})).json()
    assert [a["stock"] for a in res["actions"]] == ["AAA", "BBB"]
    assert {a["type"] for a in res["actions"]} == {"alert"}


def test_trade_legs_enter():
    res = client.post("/evaluate", json=_payload(spec={"trade_legs": True})).json()
    assert [a["type"] for a in res["actions"]] == ["alert", "enter", "alert", "enter"]
    assert [a["stock"] for a in res["actions"]] == ["AAA", "AAA", "BBB", "BBB"]


def test_no_signals_no_actions():
    assert client.post("/evaluate", json=_payload(new=())).json()["actions"] == []


def test_authoritative_configuration():
    assert SPEC["setup_id"] == "f9e54ef6"
    assert SPEC["screener_job_id"] == "777a9e34"
    assert SPEC["screener_params"] == {"universe": "nifty_200"}
    assert SPEC["interval"] == "DAILY"
    assert SPEC["dedupe_policy"] == "once_per_day"
    assert SPEC["alert"]["channels"] == ["inapp", "email"]
    assert SPEC["run_window"] == {
        "start_date": "", "end_date": "", "time_start": "", "time_end": "",
        "skip_first_minutes": 0, "square_off_time": "", "weekdays": [],
    }
    assert SPEC["data_wait"] == {"max_wait_sec": 150}
    assert SPEC["direction"] == {"mode": "screener", "column": "direction"}
    assert SPEC["holding_period"] == {
        "mode": "intraday", "max_days": 5, "intraday_close_time": "15:15",
    }
    assert SPEC["legs"]["sl"] == {"kind": "price", "value": 0.0}
    assert SPEC["legs"]["targets"] == [{"kind": "pct", "value": 500.0, "qty_pct": 100.0}]
    assert SPEC["legs"]["time_exit_minutes"] == 1440
    assert SPEC["sizing"]["product"] == "MIS"
    assert SPEC["sizing"]["per_position_cap"] == 100000
    assert SPEC["guardrails"]["max_trades_per_day"] == 10
    assert SPEC["guardrails"]["cooldown_minutes"] == 30


def test_recorded_specification_preserves_refinement_summary_and_highlights():
    manifest = json.loads(Path(__file__).with_name("manifest.json").read_text())
    assert manifest["setup_id"] == SPEC["setup_id"]
    assert manifest["screener_job_id"] == SPEC["screener_job_id"]
    prompt = manifest["prompt"]
    assert "## Refinement Summary" in prompt
    assert "**Changed:**" in prompt
    assert "three completed daily candles" in prompt
    assert "15:15 IST" in prompt
    assert "MIS" in prompt
    assert SPEC["custom_instructions"] in prompt
    assert SPEC["custom_instructions"]
    assert "Stop-Loss: 1% from entry is not correct stoploss" in prompt


def test_entry_stop_uses_only_three_completed_candles_before_signal_date():
    payload = _payload(new=("AAA",))
    payload["candles"]["AAA"].extend([
        {"date": "2026-10-28", "low": 1, "high": 999},
        {"date": "2026-10-29", "low": 2, "high": 998},
    ])
    entry = client.post("/evaluate", json=payload).json()["actions"][1]
    assert entry["sl"] == {"kind": "price", "value": 95}


def test_short_entry_stop_uses_high_of_previous_three_candles():
    payload = _payload(new=("AAA",))
    payload["signals"]["new"][0]["row"]["direction"] = "SELL"
    entry = client.post("/evaluate", json=payload).json()["actions"][1]
    assert entry["side"] == "short"
    assert entry["sl"] == {"kind": "price", "value": 104}


@pytest.mark.parametrize("candles", [
    [],
    [{"date": "2026-10-27", "low": 97, "high": 103},
     {"date": "2026-10-26", "low": 95, "high": 104}],
    [{"date": "2026-10-28", "low": 1, "high": 999},
     {"date": "2026-10-29", "low": 2, "high": 998},
     {"date": "2026-10-30", "low": 3, "high": 997}],
])
def test_missing_three_prior_candles_blocks_entry_but_keeps_alert(candles):
    payload = _payload(new=("AAA",))
    payload["candles"]["AAA"] = candles
    assert [a["type"] for a in client.post("/evaluate", json=payload).json()["actions"]] == ["alert"]


@pytest.mark.parametrize("age_days", [1, 15, 29, 30, 31])
@pytest.mark.parametrize("mode", ["alert", "paper", "live"])
def test_daily_holding_uses_engine_exits_without_blocking_new_signals(age_days, mode):
    from datetime import datetime, timedelta

    payload = _payload(mode=mode, new=("BBB",))
    candle_ts = datetime.fromisoformat(payload["run"]["candle_ts"])
    payload["positions"] = [{
        "position_id": "p1", "stock": "AAA", "status": "open",
        "opened_at": (candle_ts - timedelta(days=age_days)).isoformat(),
        "entry_price": 120, "ltp": 120, "sl": 118.8, "target": 720,
    }]
    payload["market"] = {"time_ist": "15:35:00"}
    original = deepcopy(payload)
    actions = client.post("/evaluate", json=payload).json()["actions"]
    assert [a["type"] for a in actions] == ["alert", "enter"]
    assert all(a["stock"] == "BBB" for a in actions)
    payload["signals"]["new"] = []
    assert client.post("/evaluate", json=payload).json()["actions"] == []
    payload["signals"]["new"] = original["signals"]["new"]
    assert payload == original


@pytest.mark.parametrize("direction,side", [("BUY", "long"), ("SELL", "short"), (" buy ", "long")])
@pytest.mark.parametrize("display_labels", [False, True])
def test_screener_columns_and_exact_alert_text(direction, side, display_labels):
    payload = _payload(new=("AAA",))
    payload["signals"]["new"][0]["row"] = (
        {"Symbol": "AAA", "Direction": direction, "Last price (LTP)": 120, "Volume": 5000}
        if display_labels else {"stock": "AAA", "direction": direction, "close": 120, "volume": 5000}
    )
    alert, entry = client.post("/evaluate", json=payload).json()["actions"]
    assert alert["text"] == (
        f"AAA - {direction.strip().upper()} - LTP: 120 - Volume: 5000 - Reason: Biggest green candle."
    )
    assert alert["side"] == entry["side"] == side
    assert alert["stock"] == entry["stock"] == "AAA"
    assert entry == {"type": "enter", "stock": "AAA", "side": side,
                     "sizing": {"kind": "capital", "value": 100000},
                     "sl": {"kind": "price", "value": 104 if side == "short" else 95}}


@pytest.mark.parametrize("direction", ["NEUTRAL", None, "", "bull", "LONG", "SHORT", 1])
def test_neutral_or_unknown_direction_never_falls_back_to_candle_or_leg_side(direction):
    payload = _payload(new=("AAA",))
    payload["signals"]["new"][0]["row"]["direction"] = direction
    payload["setup"]["spec"]["direction"] = {"mode": "candle"}
    payload["candles"] = {"AAA": [{"open": 100, "close": 120}]}
    res = client.post("/evaluate", json=payload).json()
    assert [action["type"] for action in res["actions"]] == ["alert"]
    assert "AAA" in res["notes"]
    if direction == "NEUTRAL":
        assert " - NEUTRAL - " in res["actions"][0]["text"]


def test_canonical_direction_takes_precedence_over_display_label():
    payload = _payload(new=("AAA",))
    payload["signals"]["new"][0]["row"].update(direction="NEUTRAL", Direction="BUY")
    assert [a["type"] for a in client.post("/evaluate", json=payload).json()["actions"]] == ["alert"]


@pytest.mark.parametrize("capital", [25000, 200000, "45000"])
def test_runtime_capital_overrides_creation_default(capital):
    payload = _payload(new=("AAA",))
    payload["setup"]["sizing"]["per_position_cap"] = capital
    entry = client.post("/evaluate", json=payload).json()["actions"][1]
    assert entry["sizing"] == {"kind": "capital", "value": float(capital)}


def test_creation_defaults_apply_only_when_runtime_configuration_is_absent():
    payload = _payload(new=("AAA",))
    payload.pop("setup")
    entry = client.post("/evaluate", json=payload).json()["actions"][1]
    assert entry["sizing"] == {"kind": "capital", "value": 100000}


@pytest.mark.parametrize("capital", [0, -1, None, "invalid", "NaN", "Infinity"])
def test_invalid_runtime_capital_does_not_fall_back(capital):
    payload = _payload(new=("AAA",))
    payload["setup"]["sizing"]["per_position_cap"] = capital
    assert [a["type"] for a in client.post("/evaluate", json=payload).json()["actions"]] == ["alert"]


@pytest.mark.parametrize("status", ["open", "pending_pullback", "partially_closed"])
def test_existing_position_is_not_reentered(status):
    payload = _payload(new=("AAA",))
    payload["positions"] = [{"position_id": "p1", "stock": "aaa", "status": status}]
    assert [a["type"] for a in client.post("/evaluate", json=payload).json()["actions"]] == ["alert"]


@pytest.mark.parametrize("mode", ["alert", "paper", "live"])
def test_modes_keep_engine_owned_legs_and_ignore_intraday_window(mode):
    payload = _payload(mode=mode, new=("AAA",))
    payload["market"] = {"time_ist": "15:35:00", "nifty_open_direction": "short"}
    entry = client.post("/evaluate", json=payload).json()["actions"][1]
    assert entry["side"] == "long"
    assert not {"entry", "targets", "qty", "trailing"} & entry.keys()
    assert entry["sl"] == {"kind": "price", "value": 95}


def test_only_new_signals_are_acted_on_and_positions_are_engine_managed():
    payload = _payload(new=())
    payload["signals"]["still"] = [{"stock": "AAA", "row": {"direction": "BUY"}}]
    payload["signals"]["dropped"] = [{"stock": "BBB"}]
    payload["matches"] = [{"stock": "CCC", "direction": "BUY"}]
    payload["positions"] = [{"position_id": "p1", "stock": "BBB", "status": "open",
                             "sl": 100, "target": 200, "ltp": 80, "opened_at": "2026-01-01"}]
    assert client.post("/evaluate", json=payload).json()["actions"] == []


def test_empty_or_late_data_is_safe_and_does_not_mutate_payload():
    payload = _payload(new=("AAA",))
    payload["signals"]["new"][0]["row"] = None
    payload["candles"] = payload["market"] = payload["positions"] = None
    original = deepcopy(payload)
    assert [a["type"] for a in main.rules.evaluate(payload)["actions"]] == ["alert"]
    assert payload == original
    assert main.rules.evaluate({})["actions"] == []
    assert client.post("/panels/unknown", json=payload).status_code == 404


def test_duplicate_signal_cannot_create_duplicate_entries():
    payload = _payload(new=("AAA", "AAA"))
    actions = client.post("/evaluate", json=payload).json()["actions"]
    assert len([a for a in actions if a["type"] == "enter"]) == 1
