import json
import os
import shlex
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from main import _truth, run_scan, scan


FIXTURE_PATH = (
    Path(__file__).parent
    / "chart-evidence"
    / "90348f960a5b"
    / "acceptance_cases.json"
)
with FIXTURE_PATH.open(encoding="utf-8") as fixture_file:
    FIXTURE = json.load(fixture_file)

CASES = FIXTURE["cases"]
IST = ZoneInfo(FIXTURE["timezone"])
RESULTS = []


def test_truth_matches_confirmed_supertrend_conditions():
    matched, values = _truth({"close": 1353.9, "low": 1345.0, "superTrend": 1345.0})

    assert matched
    assert values["close_above_superTrend"] is True
    assert values["low_at_or_below_superTrend"] is True


def test_truth_requires_close_to_be_strictly_above_supertrend():
    matched, values = _truth({"close": 1345.0, "low": 1340.6, "superTrend": 1345.0})

    assert not matched
    assert values["close_above_superTrend"] is False


def test_truth_rejects_missing_indicator_data():
    matched, values = _truth({"close": 1353.9, "low": 1340.6, "superTrend": None})

    assert not matched
    assert values["data_available"] is False


def test_default_window_selects_last_60_candle_dates():
    metadata = {}
    run_scan(
        {
            "interval": "weekly",
            "as_of": "2025-08-26T00:00",
            "symbols": "RELIANCE",
        },
        metadata=metadata,
    )

    assert len(metadata["dates"]) == 60
    date_span = (
        datetime.fromisoformat(metadata["to_date"])
        - datetime.fromisoformat(metadata["from_date"])
    ).days
    assert date_span > 90


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=[f"{case['chart_id']}-{case['mark_id']}" for case in CASES],
)
def test_chart_acceptance(case):
    at = datetime.fromtimestamp(case["at"], IST)
    target_time = at.strftime("%Y-%m-%dT%H:%M:%S")
    diagnostics = []
    scan_completed = False
    try:
        rows = run_scan(
            {
                "interval": case["interval"],
                "as_of": at.strftime("%Y-%m-%dT%H:%M"),
                "symbols": [case["symbol"]],
                "from_date": at.date().isoformat(),
                "to_date": at.date().isoformat(),
            },
            diagnostics=diagnostics,
        )
        scan_completed = True
    except Exception as exc:
        rows = []
        diagnostics.append(
            {
                "stock": case["symbol"],
                "at": target_time,
                "interval": case["interval"],
                "condition_values": {
                    "data_available": False,
                    "error_type": type(exc).__name__,
                    "error": "Scan failed before measurements were available",
                },
            }
        )

    actual_rows = [
        row
        for row in rows
        if row["stock"] == case["symbol"] and row["date"] == target_time
    ]
    expected_match = case["expect"] == "must_match"
    condition_values = next(
        (
            item["condition_values"]
            for item in diagnostics
            if item.get("stock") == case["symbol"]
            and item.get("at") == target_time
            and item.get("interval") == case["interval"]
        ),
        {
            "data_available": False,
            "reason": "The target candle was not returned for measurement",
        },
    )
    passed = (
        scan_completed
        and condition_values.get("data_available") is True
        and bool(actual_rows) == expected_match
    )
    RESULTS.append(
        {
            "chart_id": case["chart_id"],
            "mark_id": case["mark_id"],
            "symbol": case["symbol"],
            "interval": case["interval"],
            "at": case["at"],
            "expected": case["expect"],
            "passed": passed,
            "matched": bool(actual_rows),
            "condition_values": condition_values,
            "matched_rows": [
                {
                    **row,
                    "at": int(
                        datetime.fromisoformat(row["date"])
                        .replace(tzinfo=IST)
                        .timestamp()
                    ),
                    "interval": case["interval"],
                }
                for row in actual_rows
            ],
        }
    )
    assert passed, (
        f"{case['chart_id']}/{case['mark_id']} expected {case['expect']} for "
        f"{case['symbol']} at {target_time} on interval {case['interval']}; "
        f"actual rows: {actual_rows}; conditions: {condition_values}"
    )


@pytest.mark.parametrize(
    ("from_date", "to_date", "error"),
    [
        ("2025-08-27", "2025-08-26", "from_date must be on or before to_date"),
        ("2025-01-01", "2025-04-01", "date range must not exceed 90 calendar days"),
        ("2025-8-1", "2025-08-26", "from_date must be a date in YYYY-MM-DD format"),
    ],
)
def test_invalid_date_ranges_return_bad_request(from_date, to_date, error):
    response = scan(from_date=from_date, to_date=to_date)
    assert response.status_code == 400
    assert json.loads(response.body) == {"success": False, "error": error}


def test_invalid_as_of_returns_bad_request():
    response = scan(as_of="2025-8-26T00:00")
    assert response.status_code == 400
    assert json.loads(response.body) == {
        "success": False,
        "error": "as_of must use YYYY-MM-DDTHH:MM format in IST",
    }


@pytest.fixture(scope="session", autouse=True)
def write_acceptance_report(request):
    yield
    terminal = request.session.config.pluginmanager.getplugin("terminalreporter")
    stats = terminal.stats if terminal else {}
    passed = len(stats.get("passed", []))
    failed = len(stats.get("failed", [])) + len(stats.get("error", []))
    skipped = len(stats.get("skipped", []))
    summary = f"{passed} passed, {failed} failed, {skipped} skipped"
    report_path = Path(
        os.getenv(
            "ACCEPTANCE_REPORT_PATH",
            str(Path(__file__).with_name("acceptance_report.json")),
        )
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "command": shlex.join(["python", "-m", "pytest", *sys.argv[1:]]),
                "summary": summary,
                "exit_code": int(bool(failed or skipped)),
                "cases": RESULTS,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
