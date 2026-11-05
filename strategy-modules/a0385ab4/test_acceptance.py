import json
import os
import shlex
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from main import COLUMNS, _truth, health, run_scan, scan


FIXTURE_PATH = (
    Path(__file__).parent
    / "chart-evidence"
    / "5fd05be96718"
    / "acceptance_cases.json"
)
with FIXTURE_PATH.open(encoding="utf-8") as fixture_file:
    FIXTURE = json.load(fixture_file)

CASES = FIXTURE["cases"]
IST = ZoneInfo(FIXTURE["timezone"])
RESULTS = []


def test_truth_requires_lowest_low_green_close_and_gain_threshold():
    candles = [
        {"open": 97.0, "low": 95.0, "close": 98.0},
        {"open": 98.0, "low": 96.0, "close": 97.0},
    ]

    matched, values = _truth(candles, lookback_days=2, threshold_pct=2)

    assert matched
    assert values["is_lowest_low"]
    assert values["green_close"]
    assert values["gain_from_low_pct"] >= 2


@pytest.mark.parametrize(
    ("candles", "lookback_days", "threshold_pct"),
    [
        (
            [
                {"open": 99.0, "low": 95.0, "close": 98.0},
                {"open": 98.0, "low": 96.0, "close": 97.0},
            ],
            2,
            2,
        ),
        (
            [
                {"open": 97.0, "low": 95.0, "close": 98.0},
                {"open": 98.0, "low": 94.0, "close": 97.0},
            ],
            2,
            2,
        ),
        ([{"open": 97.0, "low": 95.0, "close": 98.0}], 2, 2),
        (
            [
                {"open": 97.0, "low": 95.0, "close": 98.0},
                {"open": 98.0, "low": 96.0, "close": 97.0},
            ],
            2,
            4,
        ),
    ],
)
def test_truth_rejects_wrong_direction_low_history_or_threshold(
    candles, lookback_days, threshold_pct
):
    matched, _ = _truth(candles, lookback_days, threshold_pct)
    assert not matched


@pytest.mark.parametrize(
    ("from_date", "to_date", "error"),
    [
        ("2025-04-08", "2025-04-07", "from_date must be on or before to_date"),
        ("2025-01-01", "2025-04-01", "date range must not exceed 90 calendar days"),
        ("2025-04-1", "2025-04-07", "from_date must be a date in YYYY-MM-DD format"),
    ],
)
def test_invalid_date_ranges_return_bad_request(from_date, to_date, error):
    response = scan(from_date=from_date, to_date=to_date)
    assert response.status_code == 400
    assert json.loads(response.body) == {"success": False, "error": error}


def test_scan_route_returns_confirmed_result_layout():
    response = scan(
        as_of="2025-04-07T00:00",
        symbols="RELIANCE",
        from_date="2025-04-07",
        to_date="2025-04-07",
    )

    assert health() == {"status": "ok"}
    assert response["success"]
    assert response["columns"] == COLUMNS
    assert response["presentation"]["sort"] == {"key": "close", "direction": "desc"}
    assert response["presentation"]["limit"] == 50
    assert response["presentation"]["page_size"] == 25
    assert response["parameters"][0]["value"] == "nifty_200"
    assert "as_of 2025-04-07T00:00 IST" in response["criteria"]
    assert response["matches"][0]["stock"] == "RELIANCE"
    assert response["matches"][0]["scan_date"] == "2025-04-07"


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=[f"{case['chart_id']}-{case['mark_id']}" for case in CASES],
)
def test_chart_acceptance(case):
    at = datetime.fromtimestamp(case["at"], IST)
    target_time = at.strftime("%Y-%m-%dT%H:%M:%S")
    range_start = (
        datetime.fromtimestamp(case.get("from", case["at"]), IST).date()
        if case["kind"] == "range"
        else at.date()
    )
    range_end = at.date()
    diagnostics = []
    try:
        rows = run_scan(
            {
                "interval": case["interval"],
                "as_of": at.strftime("%Y-%m-%dT%H:%M"),
                "symbols": [case["symbol"]],
                "from_date": range_start.isoformat(),
                "to_date": range_end.isoformat(),
            },
            diagnostics=diagnostics,
        )
        scan_completed = True
    except Exception as exc:
        rows = []
        scan_completed = False
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
                "matched": False,
            }
        )

    case_diagnostics = [
        item
        for item in diagnostics
        if item.get("stock") == case["symbol"]
        and item.get("interval") == case["interval"]
        and range_start.isoformat()
        <= item.get("at", "")[:10]
        <= range_end.isoformat()
    ]
    target_diagnostic = next(
        (item for item in case_diagnostics if item.get("at") == target_time), None
    )
    measured = (
        target_diagnostic["condition_values"]
        if target_diagnostic
        else {
            "data_available": False,
            "reason": "The marked candle was not evaluated by the scan",
        }
    )
    output_rows = [
        row
        for row in rows
        if row["stock"] == case["symbol"]
        and range_start.isoformat() <= row["scan_date"] <= range_end.isoformat()
    ]
    matched_rows = []
    for row in output_rows:
        row_diagnostic = next(
            (
                item
                for item in case_diagnostics
                if item.get("at", "").startswith(row["scan_date"])
                and item.get("matched")
                and item.get("matched_row") == row
            ),
            None,
        )
        if row_diagnostic:
            matched_rows.append(
                {
                    **row,
                    "at": row_diagnostic["at"],
                    "interval": row_diagnostic["interval"],
                }
            )
    expected_match = case["expect"] == "must_match"
    actual_matches = bool(output_rows)
    passed = (
        scan_completed
        and bool(case_diagnostics)
        and all(item["condition_values"].get("data_available") for item in case_diagnostics)
        and actual_matches == expected_match
        and len(matched_rows) == len(output_rows)
        and (
            case["kind"] != "candle"
            or (
                target_diagnostic is not None
                and target_diagnostic["matched"] == expected_match
                and actual_matches == expected_match
            )
        )
        and all(
            set(row) == {column["key"] for column in COLUMNS}
            and row["direction"] == "BUY"
            for row in output_rows
        )
    )
    RESULTS.append(
        {
            "chart_id": case["chart_id"],
            "mark_id": case["mark_id"],
            "symbol": case["symbol"],
            "interval": case["interval"],
            "at": case["at"],
            "timestamp_ist": target_time,
            "expected": case["expect"],
            "actual": "matched" if actual_matches else "not_matched",
            "passed": passed,
            "matched": actual_matches,
            "condition_values": (
                measured
                if case["kind"] == "candle"
                else {
                    "range_start": range_start.isoformat(),
                    "range_end": range_end.isoformat(),
                    "evaluations": [
                        {
                            "at": item["at"],
                            **item["condition_values"],
                        }
                        for item in case_diagnostics
                    ],
                    "matched_dates": [row["scan_date"] for row in output_rows],
                }
            ),
            "matched_rows": matched_rows,
        }
    )
    assert passed, (
        f"{case['chart_id']}/{case['mark_id']} expected {case['expect']} for "
        f"{case['symbol']} in {range_start} through {range_end}; "
        f"actual rows: {matched_rows}; conditions: {measured}"
    )


@pytest.fixture(scope="session", autouse=True)
def write_acceptance_report(request):
    yield
    terminal = request.session.config.pluginmanager.getplugin("terminalreporter")
    stats = terminal.stats if terminal else {}
    passed = len(stats.get("passed", []))
    failed = len(stats.get("failed", [])) + len(stats.get("error", []))
    skipped = len(stats.get("skipped", []))
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
                "summary": f"{passed} passed, {failed} failed, {skipped} skipped",
                "exit_code": int(bool(failed or skipped)),
                "cases": RESULTS,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
