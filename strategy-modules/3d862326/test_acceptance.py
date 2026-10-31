import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from main import run_scan, scan


FIXTURE_PATH = (
    Path(__file__).parent
    / "chart-evidence"
    / "b51d9ad0178e"
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
                "condition_values": {
                    "data_available": False,
                    "error": re.sub(
                        r"0x[0-9a-fA-F]+", "0x[redacted]", str(exc)
                    ),
                    "error_type": type(exc).__name__,
                }
            }
        )

    actual_rows = [
        row
        for row in rows
        if row["stock"] == case["symbol"]
        and row["date"] == target_time
    ]
    expected_match = case["expect"] == "must_match"
    condition_values = next(
        (
            item["condition_values"]
            for item in diagnostics
            if item.get("stock") == case["symbol"]
            and item.get("at") == target_time
        ),
        diagnostics[-1]["condition_values"]
        if diagnostics
        else {"data_available": False, "reason": "No candidate candle was returned"},
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
                    "stock": row["stock"],
                    "at": row["date"],
                    "interval": case["interval"],
                }
                for row in actual_rows
            ],
        }
    )
    assert passed, (
        f"{case['chart_id']}/{case['mark_id']} expected "
        f"{case['expect']} for {case['symbol']} at {target_time}; "
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
    session = request.session
    terminal = session.config.pluginmanager.getplugin("terminalreporter")
    stats = terminal.stats if terminal else {}
    passed = len(stats.get("passed", []))
    failed = len(stats.get("failed", []))
    skipped = len(stats.get("skipped", []))
    summary = (
        f"{passed} passed, {failed} failed, {skipped} skipped"
    )
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
                "command": "pytest -q strategy-modules/3d862326/test_acceptance.py",
                "summary": summary,
                "exit_code": int(bool(failed)),
                "cases": RESULTS,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
