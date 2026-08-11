# Musk Polymarket Tracker

Settlement-aligned data collector for Polymarket social-post count markets such as
`Elon Musk # tweets in August 2026`.

The key design choice is to use XTracker as the confirmed source, because the
market rules settle against XTracker before falling back to X itself. X official
streaming can be added later as an early-warning source, but it should not replace
the tracker count used for market decisions.

## Files

- `musk_tracker.py`: standard-library Python CLI.
- `polymarket_pipeline.py`: crawls Gamma event metadata, CLOB Yes-token history,
  XTracker settlement posts, optional external-event JSONL, and trains a ridge model.
- `data/musk_posts.sqlite`: created at runtime. Stores raw posts and count samples.

## Quick Start

Print active XTracker markets for Elon Musk:

```powershell
python .\musk_tracker.py active-trackings
```

Fetch once for a known market window:

```powershell
python .\musk_tracker.py once --start 2026-08-01 --end 2026-09-01
```

Poll every 60 seconds:

```powershell
python .\musk_tracker.py poll --start 2026-08-01 --end 2026-09-01 --interval 60
```

Use the first active XTracker market as the time window:

```powershell
python .\musk_tracker.py poll --use-active-tracking --interval 60
```

With Clash Verge, pass its HTTP/mixed-port explicitly. This machine's running
instance listens on `7897`:

```powershell
python .\musk_tracker.py poll --use-active-tracking --proxy http://127.0.0.1:7897 --interval 60
```

Each sample prints JSON:

```json
{
  "confirmed_count": 148,
  "freshness_seconds": 87,
  "new_posts_inserted": 1,
  "stale": false,
  "window_start_utc": "2026-08-01T04:00:00+00:00",
  "window_end_utc": "2026-09-01T04:00:00+00:00"
}
```

## How To Use In A Predictor

Use `confirmed_count` as the hard current value:

```text
final_count = confirmed_count + simulated_future_count
```

Use `freshness_seconds` as a safety gate:

```text
if stale:
    do not auto-trade
```

The SQLite `count_samples` table gives you a time series of the current count.
That time series can update recent rate, burst state, and residual variance in a
forecast model.

## Offline Backtest And Training

`market_backtest.py` replays already-ended market windows from the stored `posts`
table. For each checkpoint, it only counts posts created before that checkpoint,
predicts the final window count, and compares the prediction with the realized
final count.

Train and test on Polymarket's overlapping 7-day windows:

```powershell
python .\market_backtest.py --window polymarket --start 2026-02-10 --end 2026-08-05 --step-hours 12
```

This mode follows the Elon Musk tweet-count market cadence:

```text
2026-02-10 12:00 ET -> 2026-02-17 12:00 ET
2026-02-13 12:00 ET -> 2026-02-20 12:00 ET
2026-02-17 12:00 ET -> 2026-02-24 12:00 ET
2026-02-20 12:00 ET -> 2026-02-27 12:00 ET
...
```

So each market lasts 7 days, and new markets start every Tuesday and Friday. The
gap between start dates alternates 3 days and 4 days.

Train and test on monthly windows for comparison:

```powershell
python .\market_backtest.py --window month --step-hours 24
```

Train and test on weekly windows:

```powershell
python .\market_backtest.py --window week --step-hours 6
```

Restrict the historical range:

```powershell
python .\market_backtest.py --window week --start 2026-01-01 --end 2026-08-01 --step-hours 6
```

Outputs:

- `data/initial_model.json`: learned initial model parameters.
- `data/backtest_predictions.csv`: one row per simulated in-market checkpoint.

The first model is intentionally simple:

```text
pace_prediction = current_count / elapsed_hours * total_hours
baseline_prediction = current_count + baseline_rate_per_hour * remaining_hours
prediction = blend_weight * pace_prediction + (1 - blend_weight) * baseline_prediction
```

The training step searches `blend_weight` from `0.00` to `1.00` and chooses the
value with the lowest training MAE.

## Polymarket Crawler And Ridge Baseline

The event `startDate` is usually the listing/creation time. The pipeline instead
parses the settlement interval from the event resolution rule, then falls back to
market `gameStartTime`. This is necessary for the first known market:

```text
February 10, 2026 12:00 ET -> February 17, 2026 12:00 ET
```

Collect the two known markets through Clash Verge:

```powershell
python .\polymarket_pipeline.py crawl `
  --slug elon-musk-of-tweets-february-10-february-17 `
  --slug elon-musk-of-tweets-july-28-august-4 `
  --proxy http://127.0.0.1:7897 `
  --db data\market.sqlite
```

For a historical run, generate the Tuesday/Friday cadence from the known
February 10, 2026 anchor:

```powershell
python .\polymarket_pipeline.py crawl `
  --start 2026-02-10 --end 2026-08-05 `
  --proxy http://127.0.0.1:7897 `
  --db data\market.sqlite
```

Train using only ended markets. The split is chronological and purges training
windows that overlap the first holdout window, preventing the same X posts from
appearing in both a training label and a holdout label.

By default, a closed window is also excluded when the fetched XTracker post count
does not land inside Polymarket's resolved bin. Historical post exports can be
incomplete because deleted posts are only counted if the tracker captured them
within roughly five minutes. `--allow-unverified-history` is therefore for
exploration only, not a trading-grade model.

```powershell
python .\polymarket_pipeline.py train `
  --db data\market.sqlite `
  --start 2026-02-10 --end 2026-08-09 `
  --events data\external_events.jsonl
```

`external_events.jsonl` is optional. Each line must have an ISO timestamp and a
kind; `impact` defaults to `1`:

```json
{"id":"launch-1","timestamp":"2026-07-30T13:00:00Z","kind":"launch","impact":2}
{"id":"policy-1","timestamp":"2026-07-31T16:30:00Z","kind":"policy","impact":1}
```

For AI-assisted event analysis, use the separate structured-event importer.
The importer does not call an AI service and does not allow model output to
directly create a trade. It validates the source whitelist, evidence URL,
publication/retrieval timestamps, relevance, direction, confidence cap, and
bounded impact before the event can enter model features:

```powershell
python .\polymarket_pipeline.py ingest-events `
  --db data\extended.sqlite `
  --events data\source_events.jsonl `
  --sources data\source_registry.jsonl
```

Each event should include an `evidence` array. A minimal record is:

```json
{"id":"event-1","timestamp":"2026-07-30T13:00:00Z","kind":"launch","direction":"up","relevance":0.8,"ai_confidence":0.75,"impact":1.0,"evidence":[{"source_id":"tesla_ir","url":"https://www.tesla.com/example","published_at":"2026-07-30T13:00:00Z","retrieved_at":"2026-07-30T13:05:00Z","title":"Example","excerpt":"Evidence excerpt"}]}
```

The default whitelist is seeded into SQLite and can be extended with
`--sources`. Events are only used when `status=approved` and their
`available_at_utc` is no later than the prediction timestamp. Rejected events
and their reasons remain stored for audit. AI confidence is treated as an
input quality signal, never as a calibrated market probability.

Score an active market:

```powershell
python .\polymarket_pipeline.py predict `
  --slug elon-musk-of-tweets-july-28-august-4 `
  --db data\market.sqlite `
  --model data\market_ridge_model.json
```

The prediction command emits `do_not_auto_trade: true` whenever the price
distribution has too little or too much probability mass, the XTracker sample is stale, or the evaluation time
is outside the market's settlement window. An explicit historical `--as-of` is
also always marked as `historical_evaluation` and cannot produce an auto-trade
signal.

## Local Live Dashboard

The static page can also consume a local read-only aggregation service. The
service refreshes the selected market's Gamma metadata, XTracker posts, CLOB
order books, TSLA quote, RSS news context, and approved structured events. Raw
news is shown for inspection but cannot directly change the model.

Start it with the active market slug:

```powershell
python .\live_dashboard_server.py `
  --slug elon-musk-of-tweets-august-7-august-14 `
  --db data\extended.sqlite `
  --model data\extended_model_v4.json `
  --proxy http://127.0.0.1:7897 `
  --refresh-seconds 30 `
  --port 8765
```

Open `http://127.0.0.1:8765/`, then click `开始实时监控`. The page keeps the
existing local CSV guessing/review mode. The live recommendation is limited to
`仅纸面观察` or `不交易`; there is no order or trading API in this service.
The default live endpoint uses CLOB midpoint prices for model context and
displays executable best bid/ask separately. Missing order books, stale
XTracker data, insufficient training windows, invalid probability mass, or
small edge force `不交易`.

The service also refreshes in the background and writes each paper snapshot to
the `paper_forecasts` SQLite table. It compares interval-level model
probabilities with executable Yes best asks, including explicit local fee and
slippage assumptions. A candidate is stored in `paper_trades` only as a paper
record; the service never calls a trading endpoint. Recent snapshots and paper
records are available at
`http://127.0.0.1:8765/api/paper-history?limit=100` and
`http://127.0.0.1:8765/api/paper-trades?limit=100`.

Default paper risk parameters are conservative and configurable: `--min-edge
0.08`, `--max-spread 0.04`, `--max-book-age 30`, `--paper-size 1`,
`--min-depth-multiple 3`, `--paper-fee-rate 0.02`, and
`--paper-slippage-bps 50`. These are research assumptions, not a claim about
current official fees. Missing or stale data produces `不交易`.

## Hourly Local Replay

`hourly-test` reads only the saved local SQLite crawler data. Without `--model`,
it performs a walk-forward test: each market is trained only from earlier,
non-overlapping ended markets, then receives one predicted final count per hour.

```powershell
python .\polymarket_pipeline.py hourly-test `
  --db data\market.sqlite `
  --start 2026-02-10 --end 2026-08-09 `
  --report data\hourly_market_test.csv `
  --summary data\hourly_market_test_summary.csv
```

Outputs:

- `hourly_market_test.csv`: one row per market per hour, including current
  count, predicted final count, forecast bin, local observed final count,
  resolved Polymarket bin, price coverage, and absolute error to the local count.
- `hourly_market_test_summary.csv`: one row per market with hourly MAE and the
  last hourly forecast.

The official market only resolves to a bin, not always an exact count. The
reports therefore distinguish `observed_local_final_count` from `resolved_bin`.
If the local count is outside the resolved bin, the row is marked unverified and
its error metrics are exploratory. To replay such historical data deliberately:

```powershell
python .\polymarket_pipeline.py hourly-test `
  --db data\market.sqlite `
  --allow-unverified-history
```

## Notes

- `--end` is exclusive. For an August market, use `--start 2026-08-01 --end 2026-09-01`.
- Date-only CLI bounds are interpreted in `America/New_York` by default and stored as UTC.
- Raw tracker records are saved in `posts.raw_json` so parser logic can be corrected without losing source data.
- If `stale` is true, treat the current count as unsafe for automated decisions.
