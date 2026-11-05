from datetime import date, timedelta

from fastapi.testclient import TestClient

import main


def test_indicators_are_computed_from_raw_daily_candles():
    rows = [
        {
            "day": date(2025, 1, 1) + timedelta(days=index),
            "open": 100.0 + index,
            "high": 102.0 + index,
            "low": 99.0 + index,
            "close": 101.0 + index,
            "volume": 1000.0,
        }
        for index in range(260)
    ]

    indicators = main._indicators(rows)

    assert indicators[-1]["sma50"] == 335.5
    assert indicators[-1]["sma200"] == 260.5
    assert indicators[-1]["vol_ma20"] == 1000
    assert indicators[-1]["turnover20"] == 350500
    assert indicators[-1]["ret63"] == (360 / 297) - 1
    assert indicators[-1]["atr14"] is not None
    assert indicators[-1]["adx14"] is not None


def test_long_families_are_returned_for_priority_selection():
    rows = [
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1000.0,
        }
        for _ in range(260)
    ]
    rows[-3].update(high=103.0, low=97.0)
    rows[-2].update(high=102.0, low=98.0)
    rows[-1].update(open=103.0, high=106.0, low=104.0, close=105.0, volume=2000)
    indicators = [
        {
            "sma50": 90.0,
            "sma200": 80.0,
            "ema20": 95.0,
            "rsi14": 45.0,
            "atr14": 2.0,
            "adx14": 30.0,
            "slow_k": 10.0,
            "slow_d": 20.0,
            "vol_ma20": 1000.0,
            "turnover20": 100_000_000.0,
            "ret63": 0.1,
        }
        for _ in range(260)
    ]
    indicators[-2]["slow_k"] = 20.0
    indicators[-2]["slow_d"] = 22.0
    indicators[-1]["slow_k"] = 40.0
    indicators[-1]["slow_d"] = 30.0

    found = main._signals(
        rows, indicators, weekly_bullish=True, weekly_bearish=False,
        regime="bull", long_allowed=True, long_member=True, short_member=False,
    )

    assert [family for family, _ in found] == ["IBL", "DTL"]
    assert found[0][1] == 97.5
    assert found[1][1] == 103.0


def test_parameter_values_fall_back_to_confirmed_defaults():
    applied = main._normalize_parameters(
        {
            "side": "other",
            "strength_filter": "weak",
            "max_risk_pct": "9",
            "limit": "7",
        }
    )

    assert applied["side"] == "both"
    assert applied["strength_filter"] == "all"
    assert applied["max_risk_pct"] == "2"
    assert applied["limit"] == 10
    assert applied["min_risk_pct"] == 0.5


def test_health_and_invalid_date_range_return_expected_api_responses():
    client = TestClient(main.app)

    assert client.get("/strategy-api/23f1962b/health").json() == {"status": "ok"}
    response = client.get(
        "/strategy-api/23f1962b/scan",
        params={"from_date": "2026-03-02", "to_date": "2026-03-01"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "success": False,
        "error": "from_date must be on or before to_date",
    }


def test_scan_returns_one_ranked_signal_with_the_confirmed_row_shape(monkeypatch):
    scan_day = date(2026, 10, 30)

    class Result:
        def __init__(self, rows):
            self.result_rows = rows

    class Client:
        closed = False

        def query(self, query, parameters=None):
            if "universe_members FINAL" in query:
                return Result([("AAA", "nifty_500")])
            if "SELECT DISTINCT toDate(date)" in query:
                return Result([(scan_day,)])
            symbols = parameters["symbols"]
            days = []
            cursor = scan_day
            while len(days) < 260:
                if cursor.weekday() < 5:
                    days.append(cursor)
                cursor -= timedelta(days=1)
            days.reverse()
            candle_rows = []
            for symbol in symbols:
                for index, day in enumerate(days):
                    close = 100 + index * 0.5
                    high, low = close + 1, close - 1
                    volume = 1_000_000
                    if index == 258:
                        high, low = close + 0.3, close - 1
                    if index == 259:
                        high, low, volume = close + 1, close - 1, 3_000_000
                    candle_rows.append(
                        (symbol, day, close - 0.2, high, low, close, volume)
                    )
            return Result(candle_rows)

        def close(self):
            self.closed = True

    fake_client = Client()
    monkeypatch.setattr(main, "_client", lambda: fake_client)
    response = main.scan(
        from_date=scan_day.isoformat(),
        to_date=scan_day.isoformat(),
        max_risk_pct="2",
    )

    assert response["success"] is True
    assert response["total"] == 1
    assert response["date_range"]["dates"] == [scan_day.isoformat()]
    assert response["matches"][0]["stock"] == "AAA"
    assert response["matches"][0]["signal"] == "IBL"
    assert response["matches"][0]["direction"] == "BUY"
    assert set(response["matches"][0]) == {column["key"] for column in main.COLUMNS}
    assert fake_client.closed
