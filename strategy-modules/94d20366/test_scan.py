from datetime import date, datetime
from types import SimpleNamespace

import pytest

import main


def candle(**overrides):
    return {
        "high": 110.0,
        "close": 90.0,
        "sma20": 100.0,
        "sma50": 101.0,
        "sma200": 102.0,
        "superTrend": 103.0,
        "previous_superTrend": 89.0,
        **overrides,
    }


@pytest.mark.parametrize("indicator", ["sma20", "sma50", "sma200", "superTrend"])
@pytest.mark.parametrize("boundary", ["high", "close"])
def test_indicator_comparisons_are_strict(indicator, boundary):
    row = candle()
    row[indicator] = row[boundary]
    assert main._truth(row)[0] is False


def test_previous_supertrend_compares_with_current_not_previous_close():
    assert main._truth(candle(previous_close=80.0))[0] is True
    matched, values = main._truth(candle(previous_superTrend=90.0))
    assert not matched
    assert values["previous_superTrend_below_close"] is False


class Client:
    def __init__(self, sessions=10):
        self.sessions = sessions
        self.queries = []
        self.closed = False

    def query(self, sql, parameters=None):
        self.queries.append((sql, parameters))
        if "universe_members" in sql:
            rows = [("AAA",), ("BBB",)]
        elif "SELECT DISTINCT toDate" in sql:
            rows = [(date(2026, 10, 30),), (date(2026, 10, 29),)]
        elif "SELECT stock, date, high" in sql:
            target = datetime.fromisoformat(parameters["day_start"]).replace(
                hour=9, minute=15
            )
            rows = [
                (stock, target, 110.0, 90.0, volume, 500.0, 100.0, 101.0, 102.0, 103.0)
                for stock, volume in [("AAA", 1000), ("BBB", 100)]
                if stock in parameters["symbols"]
            ]
        elif "argMax(tuple" in sql:
            rows = [(stock, (80.0, 89.0)) for stock in parameters["symbols"]]
        elif "daily_volume" in sql:
            rows = [
                ("AAA", self.sessions, 1000.0, 0.5),
                ("BBB", self.sessions, 1000.0, 0.01),
            ]
        else:
            raise AssertionError(sql)
        return SimpleNamespace(result_rows=rows)

    def close(self):
        self.closed = True


def test_range_selects_highest_liquidity_independently_of_signal_volume(monkeypatch):
    client = Client()
    monkeypatch.setattr(main, "_client", lambda: client)
    metadata = {}
    diagnostics = []
    rows = main.run_scan(
        {
            "from_date": "2026-10-29",
            "to_date": "2026-10-30",
            "as_of": "2026-10-30T09:15",
            "limit": 1,
        },
        metadata=metadata,
        diagnostics=diagnostics,
    )
    assert [(row["stock"], row["scan_date"]) for row in rows] == [
        ("BBB", "2026-10-30"),
        ("BBB", "2026-10-29"),
    ]
    assert all(row["interval"] == "5" for row in rows)
    assert metadata["total"] == 4
    assert metadata["as_of"] == "2026-10-30T09:15"
    assert len(diagnostics) == 4
    assert client.closed
    liquidity_queries = [
        (sql, params) for sql, params in client.queries if "daily_volume" in sql
    ]
    assert len(liquidity_queries) == 2
    for sql, params in liquidity_queries:
        assert "WHERE interval = 'daily'" in sql
        assert "date < toDateTime({day_start:String})" in sql
        assert "LIMIT {days:UInt32} BY stock" in sql
        assert params["days"] == 10
    for sql, params in client.queries:
        if "SELECT stock, date, high" in sql:
            assert "stock IN {symbols:Array(String)}" in sql
            assert "date <= toDateTime({as_of:String})" in sql
            assert params["as_of"] == "2026-10-30 09:15:00"
            assert params["candle_time"] == "09:15:00"


def test_single_match_skips_liquidity_and_intersects_live_universe(monkeypatch):
    client = Client(sessions=0)
    monkeypatch.setattr(main, "_client", lambda: client)
    rows = main.run_scan(
        {
            "symbols": "AAA,OUTSIDE",
            "as_of": "2026-10-30T09:15",
            "from_date": "2026-10-30",
            "to_date": "2026-10-30",
        }
    )
    assert [row["stock"] for row in rows] == ["AAA"]
    assert not any("daily_volume" in sql for sql, _ in client.queries)
    assert all(
        params["symbols"] == ["AAA"]
        for _, params in client.queries
        if params and "symbols" in params
    )


def test_missing_liquidity_history_fails_and_closes_client(monkeypatch):
    client = Client(sessions=9)
    monkeypatch.setattr(main, "_client", lambda: client)
    with pytest.raises(main.ScanParameterError, match="Insufficient liquidity history"):
        main.run_scan({"as_of": "2026-10-30T09:15"})
    assert client.closed


def test_liquidity_lookback_is_parameterized(monkeypatch):
    client = Client(sessions=5)
    monkeypatch.setattr(main, "_client", lambda: client)
    assert main.run_scan({"as_of": "2026-10-30T09:15", "liquidity_days": 5})
    assert all(
        params["days"] == 5
        for sql, params in client.queries
        if "daily_volume" in sql
    )


@pytest.mark.parametrize("value", ["bad", 0, 91])
def test_invalid_liquidity_lookback_returns_bad_request(value):
    response = main.scan(liquidity_days=value)
    assert response.status_code == 400


def test_live_scan_excludes_unfinished_candle(monkeypatch):
    client = Client()
    monkeypatch.setattr(main, "_client", lambda: client)
    monkeypatch.setattr(
        main, "_parse_as_of", lambda value: datetime(2026, 10, 30, 9, 18, tzinfo=main.IST)
    )
    main.run_scan({"symbols": "AAA"})
    assert all(
        params["as_of"] == "2026-10-30 09:13:00"
        for _, params in client.queries
        if params and "as_of" in params
    )
