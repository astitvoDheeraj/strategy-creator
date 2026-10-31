This module scans the active NIFTY 200 members for the first 5-minute candle of each trading date at 09:15 IST whose open is above and close below SMA20, SMA50, SMA200, and SuperTrend; scans use the latest 60 trading sessions by default and accept an inclusive date range, IST replay cutoff, symbol restriction, result limit, and sort override.


## Confirmed repair

Candle filtering preserves the database IST wall-clock timestamps. All same-candle SMA/SuperTrend comparisons are unchanged. The user removed COLPAL as a required positive; the fixture now checks that this candle is excluded. This supersedes its original positive-example expectation.


## Confirmed repair

Candle filtering preserves the database IST wall-clock timestamps. All same-candle SMA/SuperTrend comparisons are unchanged. The user removed COLPAL as a required positive; the fixture now checks that this candle is excluded. This supersedes its original positive-example expectation.
