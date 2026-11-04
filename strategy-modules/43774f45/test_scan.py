from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main


def candle(**overrides):
    return {
        "high": 110.0,
        "low": 80.0,
        "close": 90.0,
        "open": 105.0,
        "volume": 1000,
        "previous_volume": None,
        "sma20": 100.0,
        "sma50": 101.0,
        "sma200": 102.0,
        "superTrend": 103.0,
        "previous_superTrend": 95.0,
        **overrides,
    }


def test_short_setup_needs_no_volume_lookback():
    matched, values = main._truth(candle())

    assert matched
    assert values["short_signal"]
    assert values["previous_superTrend_in_range"]
    assert values["signal"] == "SHORT"


def test_long_setup_requires_only_the_previous_candle_volume():
    matched, values = main._truth(
        candle(open=90.0, close=110.0, volume=1001, previous_volume=1000)
    )

    assert matched
    assert values["long_signal"]
    assert values["signal"] == "LONG"


@pytest.mark.parametrize("previous_supertrend", [80.0, 110.0])
def test_previous_supertrend_range_boundaries_are_inclusive(previous_supertrend):
    assert main._truth(candle(previous_superTrend=previous_supertrend))[0]


class Client:
    def __init__(self):
        self.queries = []
        self.closed = False
        self.days = [date(2026, 10, 30), date(2026, 10, 29)]
        self.previous = {
            "AAA": (95.0, 500),
            "BBB": (95.0, 400),
        }

    def query(self, sql, parameters=None):
        parameters = parameters or {}
        self.queries.append((sql, parameters))
        if "universe_members" in sql:
            return SimpleNamespace(result_rows=[("AAA",), ("BBB",)])
        elif "SELECT DISTINCT toDate" in sql:
            cutoff = datetime.fromisoformat(parameters["as_of"])
            rows = [(day,) for day in self.days if day <= cutoff.date()]
            return SimpleNamespace(result_rows=rows)
        elif "SELECT stock, date, high" in sql:
            target = datetime.fromisoformat(parameters["candle_time"])
            rows = [
                (
                    "AAA",
                    target,
                    110.0,
                    80.0,
                    90.0,
                    1000,
                    500.0,
                    100.0,
                    101.0,
                    102.0,
                    105.0,
                    105.0,
                ),
                (
                    "BBB",
                    target,
                    120.0,
                    80.0,
                    110.0,
                    800,
                    600.0,
                    100.0,
                    101.0,
                    102.0,
                    105.0,
                    90.0,
                ),
            ]
            return SimpleNamespace(
                result_rows=[
                    row
                    for row in rows
                    if "symbols" not in parameters or row[0] in parameters["symbols"]
                ]
            )
        elif "LIMIT 1 BY stock" in sql:
            assert parameters["target"].endswith("09:15:00")
            return SimpleNamespace(
                result_rows=[
                    (stock, supertrend, volume)
                    for stock, (supertrend, volume) in self.previous.items()
                    if stock in parameters["symbols"]
                ]
            )
        elif "daily_volume" in sql:
            return SimpleNamespace(
                result_rows=[
                    ("AAA", 2, 1000.0, 0.1),
                    ("BBB", 2, 1600.0, 0.5),
                ]
            )
        raise AssertionError(sql)

    def close(self):
        self.closed = True


def test_daily_winner_uses_highest_liquidity_and_returns_dates_descending(monkeypatch):
    client = Client()
    monkeypatch.setattr(main, "_client", lambda: client)
    metadata = {}

    rows = main.run_scan(
        {
            "as_of": "2026-10-30T15:30",
            "liquidity_days": 2,
            "limit": 1,
            "sort_by": "liquidity",
            "sort_dir": "asc",
            "universe": "all",
        },
        metadata=metadata,
    )

    assert [(row["stock"], row["scan_date"]) for row in rows] == [
        ("BBB", "2026-10-30"),
        ("BBB", "2026-10-29"),
    ]
    assert metadata["total"] == 2
    assert client.closed
    universe_query = next(
        parameters
        for sql, parameters in client.queries
        if "universe_members" in sql
    )
    assert universe_query == {"universe": "nifty_200"}
    previous_queries = [
        sql for sql, _ in client.queries if "LIMIT 1 BY stock" in sql
    ]
    assert len(previous_queries) == 2
    assert all("LIMIT 300" not in sql for sql in previous_queries)
    assert len([sql for sql, _ in client.queries if "daily_volume" in sql]) == 2


def test_requested_symbols_are_intersected_with_live_nifty_200(monkeypatch):
    client = Client()
    monkeypatch.setattr(main, "_client", lambda: client)

    rows = main.run_scan(
        {
            "as_of": "2026-10-30T09:15",
            "from_date": "2026-10-30",
            "to_date": "2026-10-30",
            "symbols": "AAA,OUTSIDE",
        }
    )

    assert [row["stock"] for row in rows] == ["AAA"]
    candle_query = next(
        parameters
        for sql, parameters in client.queries
        if "SELECT stock, date, high" in sql
    )
    assert candle_query["symbols"] == ["AAA"]
    assert not any("daily_volume" in sql for sql, _ in client.queries)


def test_equal_liquidity_prefers_higher_signal_volume():
    class HistoryClient:
        def query(self, sql, parameters=None):
            return SimpleNamespace(
                result_rows=[
                    ("AAA", 10, 1000.0, 0.1),
                    ("BBB", 10, 1000.0, 0.1),
                ]
            )

    matches = [
        {"stock": "AAA", "volume": 500},
        {"stock": "BBB", "volume": 1000},
    ]

    rows = main._highest_liquidity(
        HistoryClient(), matches, date(2026, 10, 30), 10
    )

    assert [row["stock"] for row in rows] == ["BBB"]


def test_presentation_matches_flat_table_spec(monkeypatch):
    monkeypatch.setattr(main, "_client", Client)
    with TestClient(main.app) as api:
        response = api.get(
            "/strategy-api/43774f45/scan",
            params={"as_of": "2026-10-30T15:30", "liquidity_days": 2},
        )

    data = response.json()
    assert response.status_code == 200
    assert [column["label"] for column in data["columns"]] == [
        "Symbol",
        "Date",
        "Last price (LTP)",
        "Volume",
        "Candle time",
        "Avg volume (20)",
        "SMA 20",
        "SMA 50",
        "SMA 200",
        "SuperTrend",
        "Signal",
    ]
    assert data["presentation"]["group_by_date"] is False
    assert data["presentation"]["date_chips"] is False
    assert data["presentation"]["page_size"] == 10
    assert data["presentation"]["pagination"] == "pages"
    assert data["presentation"]["sort"] == {"key": "scan_date", "direction": "desc"}
    assert data["total"] == len(data["matches"]) == 2
    assert data["matches"][0]["signal"] == "LONG"
