# Polymarket 预测与历史研究台

本仓库是一个本地研究项目，用于整理 Polymarket 市场规则、历史价格、盘口、发帖计数、新闻上下文和纸面预测结果。数据链路保持为：

```text
本地数据/受控采集 -> 模型黑箱 -> 决策限制 -> 前端研究显示
```

项目只做离线回放、历史审计、实时观察和纸面模拟。`do_not_auto_trade=true` 是永久约束；项目没有钱包、私钥、签名、下单 API、自动下注或真实资金入口。

## 项目日志摘要

维护过程记录在 `MAINTENANCE-CH.md`，运行边界和排障步骤记录在 `RUNBOOK-CH.md`，持续交接状态记录在 `CONTINUATION-CONTEXT-CH.md`。主要阶段如下：

- 初始阶段建立 Gamma 市场发现、XTracker 发帖计数、CLOB 价格/盘口、SQLite 归档、Ridge 概率预测和纸面回放。
- 纸面预测阶段增加按市场和时间点隔离的回放、盘口深度与成交条件、公开预测/私有结算揭晓、残差和评分显示。
- 实时归档阶段增加约 30 秒 REST/RSS 轮询、市场开盘时间对齐、实时快照、来源状态和缺口证据。该轮询不是 WebSocket，也不是毫秒级实时。
- 历史完整性阶段增加 Data API v2 分页、15 天窗口拆分、每次约 450 行的请求安全上限、5 分钟/30 分钟/3 小时保留期路由和源精度审计。粗粒度记录不会伪装成 5 分钟数据，也不会用当前盘口填补历史盘口。
- 动态研究阶段增加开盘后时间、近期发帖速率、市场预期差和来源年龄等特征，以及独立的映射、固定预测时点和 walk-forward 纸面研究。动态研究结果不自动进入交易决策。
- 数据整理阶段增加只读扫描、重复文件识别和生成合并副本；数据库、备份、订单簿、原始帖子、模型和实时采集源默认受保护。

每条维护记录都说明是否访问外部接口、是否写入 SQLite/CSV/模型、是否重训或回放，以及 `do_not_auto_trade` 状态。

## 前端核心功能

启动研究台后，页面顶部按 slug 分区进入不同工作流：

- `#live-capture`：实时市场、发帖计数、赔率、盘口、新闻和来源状态。显示“正在正常记录当前市场信息”或“已尝试但缺失”，不会把缺失伪装成零值。
- `#history-acquisition`：历史市场发现、价格抓取、请求窗口、实际源精度、分页和失败证据。默认目标是 5 分钟；超过保留期或目标精度失败时显示真实的 30 分钟/3 小时回退来源。
- `#prediction-review`：按完整市场 slug 查看预测时间轴、官方结算揭晓、模型/本地速度/同期市场概率对照和残差。
- `#paper-research`：纸面策略、逐 outcome edge、盘口深度部分成交、持仓动作和纸面 PnL。结果只代表历史模拟。
- `#diagnostics`：健康状态、来源错误、数据库锁、抓取缺口和数据完整性诊断。
- `#backtest`：历史回放、概率评分、校准分桶、市场级汇总和 walk-forward 研究。
- `#data-organization`：扫描可整理文件、生成不覆盖源文件的合并副本；删除操作只针对明确的完全重复低风险副本。
- `#local-reports`：查看本地报告、审计清单和导出状态。

## 启动与基本使用

推荐使用本地启动控制台：

```powershell
Set-Location "C:\Users\宋思远\Desktop\K\AIPRP\co爬虫测试"
powershell -ExecutionPolicy Bypass -File ".\start_dashboard_launcher.ps1"
```

然后打开 `http://127.0.0.1:8760/`，在控制台点击“打开研究台”，研究服务默认使用 `http://127.0.0.1:8765/`。后端或启动器代码更新后需要重启本地服务；仅静态页面更新时按 `Ctrl+F5` 刷新窗口。

不使用启动控制台时，可直接启动研究服务：

```powershell
powershell -ExecutionPolicy Bypass -File ".\start_live_dashboard.ps1" `
  -Db ".\data\extended.sqlite" `
  -Model ".\data\extended_model_v4.json" `
  -Proxy "http://127.0.0.1:7897"
```

常用离线检查：

```powershell
python -m unittest discover
python .\audit_history_coverage.py --db .\data\extended.sqlite
python .\data_file_organizer.py --help
```

历史抓取、新闻审核、回放和动态研究的完整参数以 `RUNBOOK-CH.md` 为准。未经明确要求，不要运行生产补采、模型重训或生产回放。

## 数据与 GitHub 发布边界

本仓库上传的是源码、测试、运行说明和审计文档。`data/`、`*.sqlite`、`*.db`、日志、Python 缓存、`.env`、`.ai-config.json` 和 `dist_dashboard_launcher/` 被 `.gitignore` 排除；本地数据库和一手采集文件不会随源码提交。构建 exe 可以由 `build_dashboard_launcher.ps1` 重新生成。

上传前必须检查暂存清单，确认不包含数据库、CSV、密钥、日志或模型文件。不要使用强制推送，也不要通过 GitHub 上传本地生产数据库。

## 安全边界

历史数据只能使用当时可用的记录；检查点之后的数据不能进入过去的预测特征、评分或安全 edge。实时服务是 REST/RSS 轮询，不是 WebSocket。缺失、过期、无盘口、不可成交或无法核验结算时，页面应显示证据状态并保持纸面限制，不输出真实交易指令。

---

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

## Project Components

- `musk_tracker.py`: XTracker client, post normalization, de-duplication, count samples, and the standalone polling CLI.
- `polymarket_pipeline.py`: Gamma market metadata, XTracker posts, CLOB prices and order books, SQLite migrations, Ridge training, probability forecasts, purged walk-forward evaluation, and hourly replay.
- `live_dashboard_server.py`: local HTTP service for REST/RSS polling, two isolated overlapping markets, predictions, paper forecasts/trades, approved events, news and stock context, and read-only archive APIs.
- `forecast_dashboard.html`: static research UI for live markets, guessing/review mode, fixed local reports, residual charts, and AI settings. Settlement answers remain hidden until explicitly revealed.
- `news_event_crawler.py`: whitelist-based RSS/Atom corpus collection, source audit records, and pending event candidates.
- `ai_provider.py`: optional OpenAI-compatible structured analysis. It is a context assistant only and cannot change model probabilities, paper controls, or trading state.
- `start_live_dashboard.ps1`: starts the local live service and can automatically select the latest Tuesday/Friday overlapping markets.
- `run_hourly_replay.ps1`: generates hourly replay reports without overwriting existing outputs unless explicitly requested by the user.
- `test_polymarket_pipeline.py`, `test_news_event_crawler.py`, `test_ai_provider.py`: regression tests for data, replay, source auditing, AI fallback, and service behavior.
- `RUNBOOK-CH.md` and `MAINTENANCE-CH.md`: Chinese operating instructions and maintenance audit log.

## Live Snapshot Archive

Every live refresh appends a record to `live_market_snapshots` in the local SQLite database. A snapshot includes the cumulative post count, model forecast, outcome quotes and order-book summaries, news, stock context, approved events, source status and latency, errors, and the full structured response. Existing raw post records remain in `posts`; full raw CLOB order books remain in `market_orderbooks.raw_json`.

The archive is read locally without triggering a new external request:

```text
http://127.0.0.1:8765/api/market-archive?slug=<market-slug>&limit=200
```

Older markets without new archive rows are normalized from `paper_forecasts.snapshot_json` and marked as `legacy_paper_forecast`. Missing historical news, order books, stock data, or flight context are represented by stable empty lists/status objects rather than missing fields. Flight data is currently not configured.

## Replay Residual Charts

After the private settlement report is explicitly loaded and the user clicks the reveal control, the dashboard shows both per-market residuals and the global market residual chart. The global chart uses the final replay checkpoint for each settled market, adds date and residual axes, and supports hover details for the full market slug, replay timestamp, local-observation residual, and official-bin-midpoint proxy residual. The midpoint is only a visualization proxy; it is not an exact official count.

## Safety Boundary

This repository is a local research and paper-forecast system. `do_not_auto_trade=true` is permanent. There are no wallet, signing, order-placement, automatic betting, or real-funds paths. Missing, stale, incomplete, or unverifiable data must result in a no-trade research state. Small historical samples, paper PnL, AI confidence, hit rates, and residual charts are not proof of profitability.
