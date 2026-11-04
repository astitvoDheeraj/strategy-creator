# Biggest Green Candle Finder

This daily screener scans the selected universe (NIFTY 200 by default) over a
date range (60 trading sessions by default, up to 90 calendar days). A
candidate must be a green candle whose `(close - open)` body is the maximum
among that stock's 30 most recent available daily candles.

For each date, the screener returns at most one candidate. It chooses the
largest percentage pullback from the highest high among the stock's 56 most
recent available weekly candles, then ranks ties by daily traded value
(`close × volume`) and daily candle range percentage (`100 × (high - low) /
close`), all descending. A candidate needs available weekly highs and valid
daily liquidity and range values. The selected row is marked `BUY`.

The `/strategy-api/777a9e34/scan` response includes each selected stock's
pullback percentage, liquidity, and candle range percentage. Date grouping and
date chips are disabled in the result presentation.
