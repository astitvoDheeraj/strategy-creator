from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

import main


def candle(**overrides):
    return {
        "high": 110.0,
        "low": 80.0,
        "close": 90.0,
        "open": 105.0,
        "volume": 1000,
        "previous_volume": 500,
        "sma20": 100.0,
        "sma50": 101.0,
        "sma200": 102.0,
        "superTrend": 103.0,
        "previous_superTrend": 95.0,
        **overrides,
    }


@pytest.mark.parametrize("indicator", ["sma20", "sma50", "sma200"])
@pytest.mark.parametrize("boundary", ["high", "close"])
def test_indicator_comparisons_are_strict(indicator, boundary):
    row = candle()
    row[indicator] = row[boundary]
    assert main._truth(row)[0] is False


def test_previous_supertrend_uses_candle_range_not_close():
    assert main._truth(candle(previous_close=120.0, superTrend=200.0))[0] is True
    matched, values = main._truth(candle(previous_superTrend=79.99))
    assert not matched
    assert values["previous_superTrend_in_range"] is False


def test_long_setup_matches_without_short_conditions():
    matched, values = main._truth(candle(open=90.0, close=110.0))
    assert matched
    assert values["long_signal"] is True
    assert values["short_signal"] is False
    assert all(values["long_conditions"].values())
    assert values["previous_volume"] == 500


@pytest.mark.parametrize("indicator", ["sma20", "sma50", "sma200"])
@pytest.mark.parametrize("boundary", ["open", "close"])
def test_long_crossings_are_strict(indicator, boundary):
    row = candle(open=90.0, close=110.0)
    row[indicator] = row[boundary]
    assert not main._truth(row)[0]


@pytest.mark.parametrize(
    "overrides",
    [
        {"volume": 500},
        {"volume": 499},
        {"previous_volume": None},
        {"open": None},
        {"open": 110.0},
        {"previous_superTrend": 110.01},
        {"sma200": None},
    ],
)
def test_long_setup_rejects_failed_or_missing_conditions(overrides):
    assert not main._truth(candle(open=90.0, close=110.0) | overrides)[0]


def test_short_does_not_require_bullish_body_or_volume_growth():
    assert main._truth(candle(volume=1, previous_volume=None))[0]


def test_missing_long_history_is_not_a_verified_nonmatch():
    matched, values = main._truth(candle(open=90.0, close=110.0, previous_volume=None))
    assert not matched
    assert values["data_available"] is False
    assert values["long_data_available"] is False


class Client:
    def __init__(self, sessions=10):
        self.sessions = sessions
        self.queries = []
        self.closed = False
        self.trading_dates = [date(2026, 10, 30), date(2026, 10, 29)]

    def query(self, sql, parameters=None):
        self.queries.append((sql, parameters))
        if "universe_members" in sql:
            rows = [("AAA",), ("BBB",)]
        elif "SELECT DISTINCT toDate" in sql:
            rows = [(day,) for day in self.trading_dates]
        elif "SELECT stock, date, high" in sql:
            target = datetime.fromisoformat(parameters["day_start"]).replace(
                hour=9, minute=15
            )
            rows = [
                (
                    stock,
                    target,
                    110.0,
                    80.0,
                    90.0,
                    volume,
                    500.0,
                    100.0,
                    101.0,
                    102.0,
                    103.0,
                    105.0,
                )
                for stock, volume in [("AAA", 1000), ("BBB", 100)]
                if stock in parameters["symbols"]
            ]
        elif "SELECT stock, superTrend, volume" in sql:
            rows = [(stock, 95.0, 500) for stock in parameters["symbols"]]
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
            assert "candle_time" not in params
            assert "ORDER BY date, stock" in sql


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


class IntradayClient(Client):
    def query(self, sql, parameters=None):
        result = super().query(sql, parameters)
        if "SELECT stock, date, high" not in sql:
            return result
        rows = list(result.result_rows)
        day_start = datetime.fromisoformat(parameters["day_start"])
        for minute, supertrend, close, open_price in [
            (20, 120.0, 110.0, 90.0),
            (25, 95.0, 110.0, 90.0),
            (30, 200.0, 90.0, 105.0),
        ]:
            for stock, volume in [("AAA", 2000), ("BBB", 1800)]:
                rows.append(
                    (
                        stock, day_start.replace(hour=9, minute=minute),
                        110.0, 80.0, close, volume + (1000 if minute == 25 else 0),
                        500.0, 100.0, 101.0, 102.0, supertrend, open_price,
                    )
                )
        cutoff = datetime.fromisoformat(parameters["as_of"])
        return SimpleNamespace(result_rows=[row for row in rows if row[1] <= cutoff])


def test_intraday_uses_immediate_previous_candle_and_keeps_daily_best(monkeypatch):
    client = IntradayClient()
    monkeypatch.setattr(main, "_client", lambda: client)
    metadata, diagnostics = {}, []
    rows = main.run_scan(
        {"as_of": "2026-10-30T09:30", "limit": 1},
        metadata=metadata, diagnostics=diagnostics,
    )
    assert [(row["stock"], row["date"]) for row in rows] == [
        ("AAA", "2026-10-30T09:30:00"),
        ("AAA", "2026-10-29T09:30:00"),
    ]
    assert metadata["total"] == 4  # Unique stock/date matches before capping.
    for item in diagnostics:
        values = item["condition_values"]
        if item["at"].endswith("09:20:00"):
            assert values["long_signal"]
            assert values["previous_superTrend"] == 103.0
        elif item["at"].endswith("09:25:00"):
            assert not values["all_conditions"]
            assert values["previous_superTrend"] == 120.0
        elif item["at"].endswith("09:30:00"):
            assert values["short_signal"]
            assert values["previous_superTrend"] == 95.0
    for sql, params in client.queries:
        if "SELECT stock, superTrend, volume" in sql:
            assert "LIMIT 1 BY stock" in sql
            assert params["target"].endswith("00:00:00")


def test_intraday_replay_excludes_later_candles_and_ranks_only_opening(monkeypatch):
    client = IntradayClient()
    monkeypatch.setattr(main, "_client", lambda: client)
    diagnostics = []
    rows = main.run_scan(
        {
            "as_of": "2026-10-30T09:20",
            "from_date": "2026-10-30",
            "to_date": "2026-10-30",
        },
        diagnostics=diagnostics,
    )
    assert {row["stock"] for row in rows} == {"AAA", "BBB"}
    assert all(row["date"] == "2026-10-30T09:20:00" for row in rows)
    assert len(diagnostics) == 4
    assert len([sql for sql, _ in client.queries if "daily_volume" in sql]) == 1


def test_default_window_is_last_sixty_trading_sessions(monkeypatch):
    client = Client()
    client.trading_dates = [
        date(2026, 10, 30) - timedelta(days=offset)
        for offset in range(120)
        if (date(2026, 10, 30) - timedelta(days=offset)).weekday() < 5
    ]
    monkeypatch.setattr(main, "_client", lambda: client)
    metadata = {}
    rows = main.run_scan({"as_of": "2026-10-30T09:15"}, metadata=metadata)
    assert metadata["from_date"] == client.trading_dates[59].isoformat()
    assert metadata["to_date"] == "2026-10-30"
    assert len(rows) == len(metadata["dates"]) == 60


@pytest.mark.parametrize("limit", [0, -1, 11, "bad"])
def test_invalid_limit_returns_bad_request(limit):
    assert main.scan(limit=limit).status_code == 400


def test_other_intervals_are_rejected():
    assert main.scan(interval="15").status_code == 400
