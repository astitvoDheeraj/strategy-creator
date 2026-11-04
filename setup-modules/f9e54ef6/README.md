# Setup module `f9e54ef6`

Consumes new matches from screener `777a9e34` (NIFTY 200). Each new signal
produces an alert with the exact configured text:

`{symbol} - {direction} - LTP: {ltp} - Volume: {volume} - Reason: Biggest green candle.`

The engine delivers it through in-app and email channels. Both screener keys
(`stock`, `direction`, `close`, `volume`) and display labels (`Symbol`,
`Direction`, `Last price (LTP)`, `Volume`) are supported; canonical keys take
precedence. Only explicit BUY/SELL signals produce long/short entries.
NEUTRAL, missing or unrecognised directions still alert, but never enter.
Existing open, partially closed or pending positions are never re-entered.

Entries use `setup.sizing.per_position_cap` from each request, with the
creation default in `spec.json` only when runtime sizing is absent. Invalid
or nonpositive capital blocks entries, not alerts. The engine computes share
quantities, applies free-capital caps and guardrails, and manages all trade legs.
Alert mode records hypothetical entries; paper/live execution is engine-owned.

`spec.json` preserves the issue's **authoritative fixed configuration**:
market entry, 1% stop, a 500% target for 100% of quantity, and a 1,440-minute
time exit. These differ from the narrative's candle-low stop, open-based 1:5
target and 60-day exit; no narrative overrides are applied.

The engine owns daily-after-close scheduling, the configured run window, the
150-second data wait, once-per-stock-per-day deduplication, 10 trades/day,
30-minute stock cooldown and other configured limits. Evaluation does not wait,
query the screener, or implement an intraday gate. Missing candle/market data is
safe; SL/target/time exits remain engine-owned even with no new signals.

No additional parameters or panels are needed. `/manifest` returns empty lists;
unknown panels return 404. `manifest.json` retains the original request.

From this directory, run `python -m pytest -q`, or start `uvicorn main:app`.
The container exposes the same `/health`, `/manifest` and `/evaluate` endpoints
on port 8000 (`docker build -t setup-f9e54ef6 .` then
`docker run --rm -p 8000:8000 setup-f9e54ef6`).
