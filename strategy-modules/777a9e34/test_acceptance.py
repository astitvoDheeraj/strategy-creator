import json
import os
import shlex
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from main import COLUMNS, ScanParameterError, run_scan, scan


FIXTURE_PATH = (
    Path(__file__).parent
    / "chart-evidence"
    / "932f786204eb"
    / "acceptance_cases.json"
)
with FIXTURE_PATH.open(encoding="utf-8") as fixture_file:
    FIXTURE = json.load(fixture_file)

CASES = FIXTURE["cases"]
IST = ZoneInfo(FIXTURE["timezone"])
RESULTS = []


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
                "matched": False,
                "matched_row": None,
            }
        )

    target_diagnostic = next(
        (
            item
            for item in diagnostics
            if item.get("stock") == case["symbol"]
            and item.get("at") == target_time
            and item.get("interval") == case["interval"]
        ),
        None,
    )
    condition_values = (
        target_diagnostic["condition_values"]
        if target_diagnostic
        else {
            "data_available": False,
            "reason": "The target candle was not returned by the scan",
        }
    )
    output_row = next(
        (
            row
            for row in rows
            if row["stock"] == case["symbol"]
            and row["scan_date"] == at.date().isoformat()
        ),
        None,
    )
    expected_match = case["expect"] == "must_match"
    passed = (
        scan_completed
        and target_diagnostic is not None
        and target_diagnostic["at"] == target_time
        and target_diagnostic["interval"] == case["interval"]
        and condition_values.get("data_available") is True
        and bool(output_row) == expected_match
        and target_diagnostic.get("matched") == bool(output_row)
        and (
            output_row is None
            or (
                output_row["stock"] == case["symbol"]
                and output_row["scan_date"] == at.date().isoformat()
                and set(output_row) == {column["key"] for column in COLUMNS}
            )
        )
    )
    actual_rows = (
        [
            {
                **output_row,
                "at": target_diagnostic["at"],
                "interval": target_diagnostic["interval"],
            }
        ]
        if output_row
        else []
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
            "actual": "matched" if actual_rows else "not_matched",
            "passed": passed,
            "matched": bool(actual_rows),
            "condition_values": condition_values,
            "matched_rows": actual_rows,
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
        ("2026-10-31", "2026-10-30", "from_date must be on or before to_date"),
        ("2026-01-01", "2026-04-01", "date range must not exceed 90 calendar days"),
        ("2026-10-1", "2026-10-30", "from_date must be a date in YYYY-MM-DD format"),
    ],
)
def test_invalid_date_ranges_return_bad_request(from_date, to_date, error):
    response = scan(from_date=from_date, to_date=to_date)
    assert response.status_code == 400
    assert json.loads(response.body) == {"success": False, "error": error}


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
