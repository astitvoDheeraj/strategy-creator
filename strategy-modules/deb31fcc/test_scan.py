from datetime import datetime

import main


class QueryResult:
    def __init__(self, rows):
        self.result_rows = rows


class FakeClient:
    def __init__(self, symbols):
        self.symbols = symbols
        self.queries = []
        self.closed = False

    def query(self, query, parameters=None):
        self.queries.append((query, parameters or {}))
        if "universe_members FINAL" in query:
            return QueryResult([(symbol,) for symbol in self.symbols])
        return QueryResult(
            [
                (
                    "AAA",
                    datetime(2026, 10, 25, 9, 15),
                    10,
                    11,
                    10,
                    10,
                    10,
                    1.5,
                    0.5,
                    100,
                    50,
                    9,
                    10,
                    10,
                    10,
                )
            ]
        )

    def close(self):
        self.closed = True


def _mock_client(monkeypatch, client):
    monkeypatch.setattr(main, "_clickhouse_client", lambda: client)


def test_universe_union_fetches_union_members_and_echoes_value(monkeypatch):
    client = FakeClient(["AAA", "BBB"])
    _mock_client(monkeypatch, client)

    response = main.scan(limit=50, universe="nifty_100,nifty_50")

    universe_query, universe_parameters = client.queries[0]
    candle_query, candle_parameters = client.queries[1]
    assert "universe_id IN {universe_ids:Array(String)}" in universe_query
    assert universe_parameters == {"universe_ids": ["nifty_50", "nifty_100"]}
    assert candle_parameters == {"stocks": ["AAA", "BBB"]}
    assert "stock IN {stocks:Array(String)}" in candle_query
    assert response["parameters"][0]["value"] == "nifty_50,nifty_100"
    assert response["criteria"].startswith("Universe nifty_50,nifty_100;")
    assert list(response["matches"][0]) == [column["key"] for column in main.COLUMNS]
    assert client.closed


def test_all_skips_universe_query_and_stock_restriction(monkeypatch):
    client = FakeClient([])
    _mock_client(monkeypatch, client)

    response = main.scan(limit=50, universe="all")

    assert len(client.queries) == 1
    candle_query, parameters = client.queries[0]
    assert "universe_members" not in candle_query
    assert "stock IN" not in candle_query
    assert parameters == {}
    assert response["parameters"][0]["value"] == "all"


def test_empty_universe_means_all_and_unknown_falls_back_to_default():
    assert main._normalize_universe("") == ("all", None)
    assert main._normalize_universe("not_a_universe") == (
        "nifty_50",
        ["nifty_50"],
    )
    assert main._normalize_universe("nifty_50,unknown") == (
        "nifty_50",
        ["nifty_50"],
    )
    assert main._normalize_universe("all,unknown") == (
        "nifty_50",
        ["nifty_50"],
    )


def test_presentation_and_universe_parameter_match_spec(monkeypatch):
    client = FakeClient(["AAA"])
    _mock_client(monkeypatch, client)

    response = main.scan(limit=50)

    assert response["presentation"] == {
        "style": "compact_table",
        "sort": {"key": "close", "direction": "desc"},
        "ui_filters": ["symbol_search", "column_sort"],
        "limit": 50,
        "page_size": 20,
        "pagination": "pages",
        "notes": "",
        "group_by_date": True,
        "date_chips": True,
    }
    assert response["parameters"] == [
        {
            "key": "universe",
            "label": "Universe",
            "options": main.UNIVERSE_OPTIONS,
            "default": "nifty_50",
            "multi": True,
            "value": "nifty_50",
        }
    ]
    assert response["total"] == 1
