from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def _payload(mode="alert", spec=None, new=("AAA", "BBB")):
    return {
        "setup": {"setup_id": "t", "mode": mode, "spec": spec or {}, "params": {}, "legs": {"side": "long"}},
        "run": {"run_id": "r", "candle_ts": "2026-10-28T10:15:00", "interval": "5"},
        "matches": [{"stock": s} for s in new],
        "signals": {"new": [{"stock": s, "row": {"stock": s}} for s in new], "still": [], "dropped": []},
        "positions": [], "candles": {}, "sectors": {}, "market": {},
    }


def test_health_and_manifest():
    assert client.get("/health").json() == {"status": "ok"}
    m = client.get("/manifest").json()
    assert isinstance(m["parameters"], list) and isinstance(m["panels"], list)


def test_one_action_per_new_signal():
    res = client.post("/evaluate", json=_payload()).json()
    assert [a["stock"] for a in res["actions"]] == ["AAA", "BBB"]
    assert {a["type"] for a in res["actions"]} == {"alert"}


def test_trade_legs_enter():
    res = client.post("/evaluate", json=_payload(spec={"trade_legs": True})).json()
    assert {a["type"] for a in res["actions"]} == {"enter"}


def test_no_signals_no_actions():
    assert client.post("/evaluate", json=_payload(new=())).json()["actions"] == []
