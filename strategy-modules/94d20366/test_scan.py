from datetime import date, datetime, timedelta
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
    assert values["signal"] == "LONG"
    assert all(values["long_conditions"].values())
    assert values["previous_volume"] == 500


def test_signal_requires_strict_supertrend_and_moving_average_relationships():
    assert main._truth(candle())[1]["signal"] == "SHORT"
    assert main._truth(candle(close=103.0))[1]["signal"] is None
    assert main._truth(candle(close=100.0))[1]["signal"] is None
    assert main._truth(candle(sma20=None))[1]["signal"] is None


@pytest.mark.parametrize(
    "overrides,direction",
    [
        ({}, "SELL"),
        ({"open": 90.0, "close": 110.0}, "BUY"),
        ({"superTrend": 89.0}, "NEUTRAL"),
        ({"close": 110.0, "superTrend": 111.0}, "NEUTRAL"),
        ({"close": None}, "NEUTRAL"),
    ],
)
def test_direction_uses_signal_candle_indicators(overrides, direction):
    assert main._truth(candle(**overrides))[1]["direction"] == direction


@pytest.mark.parametrize("indicator", ["superTrend", "sma20", "sma50", "sma200"])
@pytest.mark.parametrize("close", [90.0, 110.0])
@pytest.mark.parametrize("relationship", ["equal", "missing", "wrong_side"])
def test_direction_is_neutral_for_incomplete_or_mixed_indicators(
    indicator, close, relationship
):
    value = {
        "equal": close,
        "missing": None,
        "wrong_side": close - 1 if close == 90.0 else close + 1,
    }[relationship]
    row = candle(close=close)
    row[indicator] = value
    assert main._truth(row)[1]["direction"] == "NEUTRAL"


@pytest.mark.parametrize("close,open_price", [(90.0, 105.0), (110.0, 90.0)])
@pytest.mark.parametrize("relationship", ["equal", "wrong_side", "missing"])
def test_directional_eligibility_requires_current_supertrend(close, open_price, relationship):
    supertrend = {
        "equal": close,
        "wrong_side": close - 1 if close == 90.0 else close + 1,
        "missing": None,
    }[relationship]
    matched, values = main._truth(
        candle(close=close, open=open_price, superTrend=supertrend)
    )
    assert not matched
    assert values["signal"] is None
    assert values["all_conditions"] is False
    if relationship == "missing":
        assert values["data_available"] is False


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
        self.universes = {
            "nifty_200": ["AAA", "BBB"],
            "nifty_50": ["AAA"],
            "nifty_100": ["AAA", "BBB"],
            "nifty_500": ["AAA", "CCC"],
        }
        self.trading_dates = [date(2026, 10, 30), date(2026, 10, 29)]

    def query(self, sql, parameters=None):
        self.queries.append((sql, parameters))
        if "universe_members" in sql:
            rows = [
                (stock,)
                for stock in dict.fromkeys(
                    stock
                    for universe in parameters["universes"]
                    for stock in self.universes.get(universe, [])
                )
            ]
        elif "SELECT DISTINCT toDate" in sql:
            rows = [(day,) for day in self.trading_dates]
        elif "SELECT stock, date, high" in sql:
            target = datetime.fromisoformat(parameters["candle_time"])
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
                for stock, volume in [("AAA", 1000), ("BBB", 100), ("CCC", 200)]
                if (
                    "symbols" not in parameters
                    or stock in parameters["symbols"]
                )
                and target <= datetime.fromisoformat(parameters["as_of"])
            ]
        elif "SELECT stock, superTrend, volume" in sql:
            rows = [(stock, 95.0, 500) for stock in parameters["symbols"]]
        elif "daily_volume" in sql:
            rows = [
                ("AAA", self.sessions, 1000.0, 0.5),
                ("BBB", self.sessions, 1000.0, 0.01),
                ("CCC", self.sessions, 2000.0, 0.01),
            ]
        else:
            raise AssertionError(sql)
        return SimpleNamespace(result_rows=rows)

    def close(self):
        self.closed = True


def test_range_combines_liquidity_and_range_independently_of_signal_volume(monkeypatch):
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
    assert all(row["direction"] == "SELL" for row in rows)
    assert metadata["total"] == 2
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
            assert "date = toDateTime({candle_time:String})" in sql
            assert params["candle_time"].endswith("09:15:00")
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
    assert rows[0]["liquidity"] is None
    assert rows[0]["signal_range_pct"] == pytest.approx(100 * 30 / 90)
    assert not any("daily_volume" in sql for sql, _ in client.queries)
    assert all(
        params["symbols"] == ["AAA"]
        for _, params in client.queries
        if params and "symbols" in params
    )


@pytest.mark.parametrize(
    "selection,expected",
    [
        ({}, "BBB"),
        ({"universe": "nifty_500"}, "CCC"),
        ({"universe": " nifty_500 "}, "CCC"),
        ({"universe": "nifty_50,nifty_100"}, "BBB"),
    ],
)
def test_universe_selects_live_members(monkeypatch, selection, expected):
    client = Client()
    monkeypatch.setattr(main, "_client", lambda: client)
    metadata = {}
    rows = main.run_scan(
        {"as_of": "2026-10-30T09:15", **selection}, metadata=metadata
    )
    universes = selection.get("universe", "nifty_200").strip().split(",")
    sql, parameters = client.queries[0]
    assert "universe_id IN {universes:Array(String)} AND active = 1" in sql
    assert parameters == {"universes": universes}
    assert [row["stock"] for row in rows] == [expected, expected]
    assert all(
        set(parameters["symbols"])
        <= set().union(*(set(client.universes[universe]) for universe in universes))
        for _, parameters in client.queries
        if "symbols" in parameters
    )
    assert metadata["criteria"].startswith(
        f"{', '.join(main.UNIVERSE_OPTIONS[universe] for universe in universes)} stocks;"
    )
    assert client.closed


@pytest.mark.parametrize("universe", ["all", ""])
def test_all_or_empty_universe_skips_membership_query(monkeypatch, universe):
    client = Client()
    monkeypatch.setattr(main, "_client", lambda: client)
    rows = main.run_scan(
        {"universe": universe, "as_of": "2026-10-30T09:15"},
    )
    assert [row["stock"] for row in rows] == ["BBB", "BBB"]
    assert not any("universe_members" in sql for sql, _ in client.queries)
    assert all(
        "symbols" not in parameters
        for sql, parameters in client.queries
        if "SELECT DISTINCT toDate" in sql or "SELECT stock, date, high" in sql
    )


def test_all_universe_applies_requested_symbol_filter(monkeypatch):
    client = Client()
    monkeypatch.setattr(main, "_client", lambda: client)
    rows = main.run_scan(
        {
            "universe": "all",
            "symbols": "BBB,CCC",
            "as_of": "2026-10-30T09:15",
        }
    )
    assert [row["stock"] for row in rows] == ["CCC", "CCC"]
    assert not any("universe_members" in sql for sql, _ in client.queries)
    assert all(
        parameters["symbols"] == ["BBB", "CCC"]
        for sql, parameters in client.queries
        if "SELECT DISTINCT toDate" in sql or "SELECT stock, date, high" in sql
    )


@pytest.mark.parametrize("universe", ["unknown", "nifty_50,unknown"])
def test_unknown_universe_falls_back_to_default(monkeypatch, universe):
    client = Client()
    monkeypatch.setattr(main, "_client", lambda: client)
    metadata = {}
    rows = main.run_scan(
        {"universe": universe, "as_of": "2026-10-30T09:15"},
        metadata=metadata,
    )
    assert [row["stock"] for row in rows] == ["BBB", "BBB"]
    assert client.queries[0][1] == {"universes": ["nifty_200"]}
    assert metadata["criteria"].startswith("NIFTY 200 stocks;")


def test_custom_universe_intersects_requested_symbols(monkeypatch):
    client = Client()
    monkeypatch.setattr(main, "_client", lambda: client)
    rows = main.run_scan(
        {"universe": "nifty_500", "symbols": "BBB,CCC", "as_of": "2026-10-30T09:15"}
    )
    assert [row["stock"] for row in rows] == ["CCC", "CCC"]
    assert not any("daily_volume" in sql for sql, _ in client.queries)


def test_valid_universe_without_active_members_returns_bad_request(monkeypatch):
    client = Client()
    client.universes["nifty_200"] = []
    monkeypatch.setattr(main, "_client", lambda: client)
    with TestClient(main.app) as api:
        response = api.get(
            "/strategy-api/94d20366/scan", params={"universe": "nifty_200"}
        )
    assert response.status_code == 400
    assert response.json() == {
        "success": False,
        "error": "The nifty_200 universe has no active members",
    }
    sql, parameters = client.queries[0]
    assert "nifty_200" not in sql
    assert parameters == {"universes": ["nifty_200"]}
    assert len(client.queries) == 1
    assert client.closed


def test_symbols_outside_selected_universe_return_bad_request(monkeypatch):
    client = Client()
    monkeypatch.setattr(main, "_client", lambda: client)
    response = main.scan(universe="nifty_500", symbols="BBB")
    assert response.status_code == 400
    assert b"No requested symbols are active members of nifty_500" in response.body
    assert client.closed


def test_api_exposes_multi_value_universe_choices(monkeypatch):
    monkeypatch.setattr(main, "_client", Client)
    with TestClient(main.app) as api:
        response = api.get(
            "/strategy-api/94d20366/scan",
            params={"universe": "nifty_50,nifty_100", "as_of": "2026-10-30T09:15"},
        )
    assert response.status_code == 200
    parameter = next(
        item for item in response.json()["parameters"] if item["key"] == "universe"
    )
    assert parameter["default"] == "nifty_200"
    assert parameter["multi"] is True
    assert parameter["options"] == [
        {"value": value, "label": label}
        for value, label in main.UNIVERSE_OPTIONS.items()
    ]


def test_api_accepts_and_exposes_universe_parameter(monkeypatch):
    monkeypatch.setattr(main, "_client", Client)
    with TestClient(main.app) as api:
        response = api.get(
            "/strategy-api/94d20366/scan",
            params={"universe": "nifty_500", "as_of": "2026-10-30T09:15"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["criteria"].startswith("NIFTY 500 stocks;")
    assert [row["stock"] for row in data["matches"]] == ["CCC", "CCC"]
    assert data["presentation"]["group_by_date"] is False
    assert data["presentation"]["date_chips"] is False


@pytest.mark.parametrize("invalid_symbols", [{"BBB"}, {"AAA", "BBB"}])
def test_ineligible_stocks_are_removed_before_liquidity(monkeypatch, invalid_symbols):
    class DirectionClient(Client):
        def query(self, sql, parameters=None):
            result = super().query(sql, parameters)
            if "SELECT stock, date, high" in sql:
                rows = [list(row) for row in result.result_rows]
                for row in rows:
                    if row[0] in invalid_symbols:
                        row[10] = row[4] - 1
                return SimpleNamespace(result_rows=rows)
            return result

    client = DirectionClient(sessions=0)
    monkeypatch.setattr(main, "_client", lambda: client)
    diagnostics, metadata = [], {}
    rows = main.run_scan(
        {"as_of": "2026-10-30T09:15"},
        diagnostics=diagnostics,
        metadata=metadata,
    )
    expected = [] if "AAA" in invalid_symbols else ["AAA", "AAA"]
    assert [row["stock"] for row in rows] == expected
    assert all(row["signal"] == "SHORT" for row in rows)
    assert metadata["total"] == len(expected)
    assert not any("daily_volume" in sql for sql, _ in client.queries)
    assert all(
        not item["condition_values"]["all_conditions"]
        for item in diagnostics
        if item["stock"] in invalid_symbols
    )
    assert client.closed


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
    rows = main.run_scan(
        {"symbols": "AAA", "from_date": "2026-10-30", "to_date": "2026-10-30"}
    )
    assert rows == []
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
        day_start = datetime.fromisoformat(parameters["candle_time"])
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


def test_intraday_matches_never_replace_opening_liquidity_winner(monkeypatch):
    client = IntradayClient()
    monkeypatch.setattr(main, "_client", lambda: client)
    metadata, diagnostics = {}, []
    rows = main.run_scan(
        {"as_of": "2026-10-30T09:30"},
        metadata=metadata, diagnostics=diagnostics,
    )
    assert [(row["stock"], row["date"]) for row in rows] == [
        ("BBB", "2026-10-30T09:15:00"),
        ("BBB", "2026-10-29T09:15:00"),
    ]
    assert metadata["total"] == 2
    assert len(diagnostics) == 4
    for item in diagnostics:
        values = item["condition_values"]
        assert item["at"].endswith("09:15:00")
        assert values["short_signal"]
        assert values["previous_superTrend"] == 95.0
        assert values["previous_volume"] == 500
    for sql, params in client.queries:
        if "SELECT stock, superTrend, volume" in sql:
            assert "LIMIT 1 BY stock" in sql
            assert params["target"].endswith("09:15:00")


def test_intraday_replay_returns_only_one_opening_stock(monkeypatch):
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
    assert [row["stock"] for row in rows] == ["BBB"]
    assert all(row["date"] == "2026-10-30T09:15:00" for row in rows)
    assert len(diagnostics) == 2
    assert len([sql for sql, _ in client.queries if "daily_volume" in sql]) == 1


def test_long_and_short_opening_signals_compete_for_one_daily_slot(monkeypatch):
    class MixedClient(Client):
        def query(self, sql, parameters=None):
            result = super().query(sql, parameters)
            if "SELECT stock, date, high" in sql:
                rows = [list(row) for row in result.result_rows]
                for row in rows:
                    if row[0] == "BBB":
                        row[4], row[11] = 110.0, 90.0
                        row[5] = 600
                return SimpleNamespace(result_rows=rows)
            return result

    monkeypatch.setattr(main, "_client", MixedClient)
    diagnostics = []
    rows = main.run_scan(
        {"as_of": "2026-10-30T15:30", "sort_by": "stock", "sort_dir": "asc"},
        diagnostics=diagnostics,
    )
    assert [row["stock"] for row in rows] == ["BBB", "BBB"]
    assert all(row["signal"] == "LONG" for row in rows)
    assert all(row["direction"] == "BUY" for row in rows)
    assert all(
        item["condition_values"]["long_signal" if item["stock"] == "BBB" else "short_signal"]
        for item in diagnostics
    )


def test_no_opening_match_does_not_fall_back_to_intraday(monkeypatch):
    class NoOpeningClient(IntradayClient):
        def query(self, sql, parameters=None):
            result = super().query(sql, parameters)
            if "SELECT stock, date, high" in sql:
                rows = [list(row) for row in result.result_rows]
                for row in rows:
                    if row[1].time() == main.FIRST_CANDLE_TIME:
                        row[7] = row[2]
                return SimpleNamespace(result_rows=rows)
            return result

    client = NoOpeningClient()
    monkeypatch.setattr(main, "_client", lambda: client)
    metadata = {}
    assert main.run_scan({"as_of": "2026-10-30T09:30"}, metadata=metadata) == []
    assert metadata["total"] == 0
    assert not any("daily_volume" in sql for sql, _ in client.queries)
    assert client.closed


def test_api_keeps_date_filter_and_requests_flat_table(monkeypatch):
    monkeypatch.setattr(main, "_client", Client)
    with TestClient(main.app) as api:
        assert api.get("/strategy-api/94d20366/health").json() == {"status": "ok"}
        response = api.get(
            "/strategy-api/94d20366/scan",
            params={
                "from_date": "2026-10-29",
                "to_date": "2026-10-30",
                "as_of": "2026-10-30T15:30",
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data["success"]
    assert data["presentation"]["style"] == "table"
    assert data["presentation"]["group_by_date"] is False
    assert data["presentation"]["date_chips"] is False
    assert data["presentation"]["limit"] == 1
    assert data["columns"][1] == {
        "key": "direction",
        "label": "Direction",
        "format": "direction",
    }
    assert data["direction"] == {
        "enabled": True,
        "buy": (
            "SuperTrend is below the signal candle close and close is strictly above "
            "SMA20, SMA50, and SMA200"
        ),
        "sell": (
            "SuperTrend is above the signal candle close and close is strictly below "
            "SMA20, SMA50, and SMA200"
        ),
        "neutral": "Neither BUY nor SELL rule holds",
    }
    assert data["columns"][-1] == {
        "key": "signal",
        "label": "Signal",
        "format": "text",
        "background_colors": {
            "LONG": "rgba(34, 197, 94, 0.18)",
            "SHORT": "rgba(239, 68, 68, 0.18)",
        },
    }
    assert data["date_range"]["mode"] == "range"
    assert data["date_range"]["from_date"] == "2026-10-29"
    assert data["date_range"]["to_date"] == "2026-10-30"
    assert data["total"] == len(data["matches"]) == 2
    assert all(row["date"].endswith("09:15:00") for row in data["matches"])
    assert all(row["signal"] == "SHORT" for row in data["matches"])
    assert all(row["direction"] == "SELL" for row in data["matches"])
    assert "only the completed 09:15 IST" in data["criteria"]
    assert "medium liquidity and medium signal candle range" in data["criteria"]
    for row in data["matches"]:
        assert row["liquidity"] == pytest.approx(1000 / 1.01)
        assert row["signal_range_pct"] == pytest.approx(100 * 30 / 90)
        assert all(column["key"] in row for column in data["columns"])


@pytest.mark.parametrize(
    "liquidity,ranges,expected",
    [
        ([100, 200, 300], [1, 2, 3], "B"),
        ([100, 200, 300, 400, 500], [1, 3, 5, 4, 2], "B"),
        ([100, 200, 300, 400, 500], [2, 2, 2, 2, 2], "C"),
        ([100, 100, 100, 100, 100], [1, 2, 3, 4, 5], "C"),
        ([100, 200, 200, 200, 300], [1, 2, 3, 4, 5], "C"),
        ([100, 200, 300, 400, 500], [3, 1, 5, 4, 2], "D"),
        ([100, 100, 100], [2, 2, 2], "C"),
        ([100, 200], [1, 2], "B"),
        ([100, 200, 300, 400], [4, 3, 2, 1], "C"),
    ],
)
def test_selection_targets_both_medium_metrics(liquidity, ranges, expected):
    class HistoryClient:
        def query(self, sql, parameters=None):
            return SimpleNamespace(
                result_rows=[
                    (chr(65 + index), 10, value, 0)
                    for index, value in enumerate(liquidity)
                ]
            )

    matches = [
        {
            "stock": chr(65 + index),
            "volume": 1000,
            "liquidity": None,
            "signal_range_pct": value,
        }
        for index, value in enumerate(ranges)
    ]
    for candidates in (matches, list(reversed(matches))):
        rows = main._medium_liquidity_and_range(
            HistoryClient(), candidates, date(2026, 10, 30), 10
        )
        assert [row["stock"] for row in rows] == [expected]


def test_medium_metric_ties_prefer_signal_volume_before_symbol():
    class HistoryClient:
        def query(self, sql, parameters=None):
            return SimpleNamespace(result_rows=[("AAA", 10, 1000, 0), ("BBB", 10, 1000, 0)])

    matches = [
        {"stock": "AAA", "volume": 2000, "signal_range_pct": 2},
        {"stock": "BBB", "volume": 1000, "signal_range_pct": 2},
    ]
    assert main._medium_liquidity_and_range(
        HistoryClient(), matches, date(2026, 10, 30), 10
    )[0]["stock"] == "AAA"


def test_signal_range_is_measured_from_signal_candle_and_normalized(monkeypatch):
    class RangeClient(Client):
        def query(self, sql, parameters=None):
            result = super().query(sql, parameters)
            if "SELECT stock, date, high" in sql:
                rows = [list(row) for row in result.result_rows]
                for row in rows:
                    row[2], row[3] = {
                        "AAA": (105.0, 94.0),
                        "BBB": (110.0, 80.0),
                        "CCC": (200.0, 10.0),
                    }[row[0]]
                    if row[0] == "BBB":
                        for index in (2, 3, 4, 7, 8, 9, 10, 11):
                            row[index] *= 10
                return SimpleNamespace(result_rows=rows)
            if "SELECT stock, superTrend, volume" in sql:
                return SimpleNamespace(
                    result_rows=[
                        (stock, supertrend * (10 if stock == "BBB" else 1), volume)
                        for stock, supertrend, volume in result.result_rows
                    ]
                )
            return result

    monkeypatch.setattr(main, "_client", RangeClient)
    rows = main.run_scan({"universe": "all", "as_of": "2026-10-30T09:15"})
    assert [row["stock"] for row in rows] == ["BBB", "BBB"]
    assert all(row["signal_range_pct"] == pytest.approx(100 * 30 / 90) for row in rows)


@pytest.mark.parametrize(
    "history",
    [
        [],
        [("AAA", 10, None, 0.1), ("BBB", 10, 1000, 0.1)],
        [("AAA", 10, 1000, float("nan")), ("BBB", 10, 1000, 0.1)],
        [("AAA", 10, 1000, -1), ("BBB", 10, 1000, 0.1)],
    ],
)
def test_missing_or_invalid_liquidity_cannot_produce_a_medium_winner(monkeypatch, history):
    class HistoryClient(Client):
        def query(self, sql, parameters=None):
            if "daily_volume" in sql:
                return SimpleNamespace(result_rows=history)
            return super().query(sql, parameters)

    client = HistoryClient()
    monkeypatch.setattr(main, "_client", lambda: client)
    with pytest.raises(main.ScanParameterError, match="liquidity history"):
        main.run_scan({"as_of": "2026-10-30T09:15"})
    assert client.closed


@pytest.mark.parametrize(
    "index,value,error",
    [
        (4, 0, "Invalid signal candle price"),
        (4, -1, "Invalid signal candle price"),
        (2, float("inf"), "Invalid signal candle range"),
    ],
)
def test_invalid_signal_measurements_return_bad_request(monkeypatch, index, value, error):
    class InvalidCandleClient(Client):
        def query(self, sql, parameters=None):
            result = super().query(sql, parameters)
            if "SELECT stock, date, high" in sql:
                rows = [list(row) for row in result.result_rows]
                for row in rows:
                    row[index] = value
                return SimpleNamespace(result_rows=rows)
            return result

    client = InvalidCandleClient()
    monkeypatch.setattr(main, "_client", lambda: client)
    with TestClient(main.app) as api:
        response = api.get(
            "/strategy-api/94d20366/scan", params={"as_of": "2026-10-30T09:15"}
        )
    assert response.status_code == 400
    assert response.json()["error"].startswith(error)
    assert client.closed


def test_replay_before_opening_returns_no_winner(monkeypatch):
    client = Client()
    monkeypatch.setattr(main, "_client", lambda: client)
    metadata = {}
    assert main.run_scan(
        {
            "from_date": "2026-10-30",
            "to_date": "2026-10-30",
            "as_of": "2026-10-30T09:14",
        },
        metadata=metadata,
    ) == []
    assert metadata["total"] == 0
    assert not any("daily_volume" in sql for sql, _ in client.queries)


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
