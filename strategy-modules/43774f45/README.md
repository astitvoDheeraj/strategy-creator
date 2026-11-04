# Strategy Builder 43774f45

Scans active NIFTY 200 members on the completed 09:15 IST 5-minute candle. A short setup requires the candle high above and close below SMA20, SMA50, and SMA200. A long setup requires a bullish candle opening below and closing above all three SMAs, with volume greater than the previous candle. Both setups require the previous SuperTrend within the current candle's low-to-high range, inclusive.

When multiple stocks qualify on a date, the screener selects the highest-liquidity candidate across both setups. Liquidity is average daily volume divided by one plus average daily relative range, calculated from the preceding `liquidity_days` sessions (default 10), excluding the signal date. Liquidity ties prefer signal volume, then symbol. Only the 09:15 candle is eligible, and each date contributes at most one row.

The default window is the latest 60 trading sessions. `/strategy-api/43774f45/scan` accepts inclusive `from_date` and `to_date` values spanning at most 90 calendar days, an IST `as_of` replay time, comma-separated `symbols` intersected with active NIFTY 200 members, plus compatible `limit` and sort overrides. Only interval `5` is supported. Dates are returned newest first, with one selected row per date.

The flat table contains Symbol, Date, Last price (LTP), Volume, Candle time, Avg volume (20), SMA 20, SMA 50, SMA 200, SuperTrend, and Signal. Signal is LONG when close is above SuperTrend and all three SMAs, or SHORT when it is below all four. The presentation disables date grouping and date chips, and provides symbol search, column sorting, and numbered 10-row pages.

Acceptance replays the unchanged `chart-evidence/cbcf05d6839f/acceptance_cases.json` fixture against ClickHouse history. Set `CLICKHOUSE_*` and run `python -m pytest -vv test_acceptance.py` from this module directory. The report is written to `ACCEPTANCE_REPORT_PATH` or `acceptance_report.json`. Run `python -m pytest -vv test_scan.py` for isolated scanner tests.
