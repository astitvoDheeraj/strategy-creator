from datetime import date, datetime, timedelta

import main


SCAN_DATE = date(2026, 10, 30)
SCAN_DATES = (SCAN_DATE, SCAN_DATE - timedelta(days=1))


class Result:
    def __init__(self, rows):
        self.result_rows = rows


class FakeClient:
    def __init__(self):
        self.closed = False
        self.queries = []

    def query(self, query, parameters=None):
        self.queries.append((query, parameters or {}))
        if "min(toDate(date))" in query:
            return Result([(SCAN_DATES[-1], SCAN_DATES[0])])
        if "SELECT DISTINCT toDate(date)" in query:
            return Result([(day,) for day in SCAN_DATES])
        if "{weekly_interval:String}" in query:
            candle_date = datetime.strptime(
                parameters["target"], "%Y-%m-%d %H:%M:%S"
            ).date()
            return Result(
                [
                    (
                        "LOW_PULLBACK",
                        datetime.combine(candle_date, datetime.min.time()),
                        15.0,
                    ),
                    (
                        "LOW_LIQUIDITY",
                        datetime.combine(candle_date, datetime.min.time()),
                        20.0,
                    ),
                    (
                        "WINNER",
                        datetime.combine(candle_date, datetime.min.time()),
                        20.0,
                    ),
                    (
                        "LOW_RANGE",
                        datetime.combine(candle_date, datetime.min.time()),
                        20.0,
                    ),
                ]
            )
        candle_date = datetime.strptime(
            parameters["target"], "%Y-%m-%d %H:%M:%S"
        )
        return Result(
            [
                ("LOW_PULLBACK", candle_date, 9.0, 13.0, 9.0, 10.0, 10000),
                ("LOW_LIQUIDITY", candle_date, 9.0, 10.0, 9.0, 10.0, 100),
                ("WINNER", candle_date, 9.0, 12.0, 9.0, 10.0, 200),
                ("LOW_RANGE", candle_date, 9.0, 11.0, 9.0, 10.0, 200),
            ]
        )

    def close(self):
        self.closed = True


def test_scan_selects_one_stock_per_day_by_pullback_liquidity_then_range(monkeypatch):
    clients = []

    def create_client():
        client = FakeClient()
        clients.append(client)
        return client

    monkeypatch.setattr(main, "_client", create_client)
    params = {
        "from_date": SCAN_DATES[-1].isoformat(),
        "to_date": SCAN_DATE.isoformat(),
        "universe": "all",
    }

    diagnostics = []
    rows = main.run_scan(params, diagnostics=diagnostics)

    assert len(rows) == len(SCAN_DATES)
    assert [row["stock"] for row in rows] == ["WINNER"] * len(SCAN_DATES)
    assert [item["stock"] for item in diagnostics if item["matched"]] == [
        "WINNER"
    ] * len(SCAN_DATES)

    response = main.scan(**params)

    assert response["total"] == len(SCAN_DATES)
    assert len(response["matches"]) == len(SCAN_DATES)
    row = response["matches"][0]
    assert row["stock"] == "WINNER"
    assert row["weekly_pullback_pct"] == 50
    assert row["liquidity"] == 2000
    assert row["range_pct"] == 30
    assert set(row) == {column["key"] for column in main.COLUMNS}
    assert response["presentation"]["group_by_date"] is False
    assert response["presentation"]["date_chips"] is False
    weekly_queries = [
        (query, parameters)
        for query, parameters in clients[0].queries
        if "{weekly_interval:String}" in query
    ]
    assert len(weekly_queries) == len(SCAN_DATES)
    weekly_query = weekly_queries[0]
    assert weekly_query[1]["weekly_interval"] == "weekly"
    assert weekly_query[1]["weekly_lookback"] == main.WEEKLY_LOOKBACK_CANDLES
    assert "LIMIT {weekly_lookback:UInt8} BY stock" in weekly_query[0]
    assert all(client.closed for client in clients)
