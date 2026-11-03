This screener checks the first 5-minute candle of the latest session for a close above SMA 20, SMA 50, and SMA 200 after the previous candle closed at or below each average. Stocks with zero first-candle volume or zero 20-candle average volume are excluded. Results show the requested price, indicator, and candle-time fields in a compact table, sorted by last price descending, with 20 rows per page and a 50-row cap.

The `universe` scan parameter accepts one or more comma-separated universe IDs. It defaults to `nifty_50`; selecting `all` or an empty value scans all NSE stocks with candles. Unknown IDs fall back to `nifty_50`.

## [CHANGED] Runtime parameters — Universe

The viewer can choose NIFTY 50, NIFTY NEXT 50, NIFTY 100, NIFTY 200, NIFTY 500, NIFTY MIDCAP 50/100/150, NIFTY SMALLCAP 50/100/250, or all NSE stocks. Multiple choices are combined as a union. Universe membership is read from ClickHouse for each scan rather than hard-coded.
