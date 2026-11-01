import json
from pathlib import Path

from main import SPEC, evaluate


MODULE_DIR = Path(__file__).parent
with (MODULE_DIR / "chart-evidence" / "5551dffecabd" / "acceptance_cases.json").open(
    encoding="utf-8"
) as fixture_file:
    FIXTURE = json.load(fixture_file)


def test_setup_configuration_matches_confirmed_alert_spec():
    assert SPEC["setup_id"] == FIXTURE["setup_id"] == "acff861e"
    assert SPEC["screener_job_id"] == FIXTURE["screener_job_id"] == "94d20366"
    assert SPEC["interval"] == "5"
    assert SPEC["run_window"]["time_start"] == "09:20"
    assert SPEC["run_window"]["time_end"] == "15:15"
    assert SPEC["dedupe_policy"] == "once_ever"
    assert SPEC["alert"]["channels"] == ["inapp", "email"]
    assert SPEC["direction"] == {"mode": "screener", "column": "direction"}
    assert SPEC["data_wait"]["max_wait_sec"] == 300


def test_evaluate_does_not_create_actions_without_new_screener_matches():
    assert evaluate({"signals": {"new": []}}) == {"actions": []}


def test_chart_acceptance_fixture_contains_replayable_cases():
    assert FIXTURE["cases"], (
        "The setup fixture has no replay cases; do not fabricate signals or "
        "claim a real-history acceptance replay."
    )
    assert FIXTURE["charts"][0]["symbol"] == "NMDC"
