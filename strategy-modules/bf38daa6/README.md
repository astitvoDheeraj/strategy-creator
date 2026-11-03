This module scans active NIFTY 500 members on weekly candles and returns a BUY when the candle closes above SuperTrend while its low is at or below SuperTrend; it supports bounded date-range replay, historical `as_of` evaluation, and symbol filtering using ClickHouse data.

`/strategy-api/bf38daa6/scan` accepts a comma-separated `universe` parameter.
It defaults to `nifty_500`; multiple listed universe IDs scan their union.
`all` or an empty value scans all stocks with candles without reading universe
membership. Unknown IDs fall back to `nifty_500`. The optional `symbols` filter
further restricts either selection. Available options are returned in `parameters`.
