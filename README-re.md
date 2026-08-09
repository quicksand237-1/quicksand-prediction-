# Polymarket Elon Musk 发帖数预测工具

本项目用于抓取 Polymarket 的 Elon Musk 发帖数预测市场、保存 XTracker 帖子和盘口价格、训练基础 Ridge 回归模型，并以一小时粒度回放历史市场预测。

当前仓库使用 Python 标准库，不需要安装第三方 Python 包。

## 1. 文件与功能对应

| 文件 | 功能 | 关键实现 |
| --- | --- | --- |
| `musk_tracker.py` | 单独轮询 XTracker，保存帖子和计数快照 | `XTrackerClient`、`Store`、`once`、`poll` |
| `polymarket_pipeline.py` | 抓市场、训练、单市场预测、逐小时回放 | `crawl`、`train`、`predict`、`hourly-test` |
| `market_backtest.py` | 早期的基础节奏模型回测工具 | `replay_examples`、`train` |
| `data/*.sqlite` | SQLite 本地数据库 | `posts`、`count_samples`、`market_events`、`market_outcomes`、`market_prices` |
| `data/*.csv` | 训练或一小时回放结果 | 每行对应一个预测时点或一个市场汇总 |

## 2. 数据与市场规则

该市场预测的是 `@elonmusk` 在指定七天结算窗口内的有效 X 帖子总数。

- 市场的结果是互斥区间，例如 `<20`、`20-39`、`40-59`。
- 主贴、引用帖和转帖计入；普通回复通常不计入。
- 被 XTracker 在约五分钟内捕获的删除帖仍可能计入结算。
- 结算优先使用 XTracker 的 Post Counter；X 可作为次级结算来源。
- 市场页面中的事件 `startDate` 可能是上架时间，不是发帖统计开始时间。

代码映射：

- `parse_count_market_window()`：从市场规则文本解析“从何时到何时”的结算窗口。
- `event_window()`：优先使用规则文本窗口，失败时才使用 `gameStartTime` 或事件字段。
- `MarketStore.save_event()`：把正确窗口保存到 `market_events.game_start_utc` 和 `market_events.end_date_utc`。

## 3. 网络与代理

本机检测到 Clash Verge HTTP 代理端口为 `127.0.0.1:7897`。

所有访问 Polymarket、CLOB 或 XTracker 的命令都可以传入：

```powershell
--proxy http://127.0.0.1:7897
```

代码映射：

- `JsonClient`：为 Gamma API 和 CLOB API 构造 HTTP 代理请求。
- `XTrackerClient`：为 XTracker 请求构造 HTTP 代理请求。
- `crawl --proxy`：同时把代理传给上述三个数据源。
- `musk_tracker.py once/poll --proxy`：只为 XTracker 请求设置代理。

## 4. 抓取一个市场

下面示例抓取 2026 年 7 月 28 日到 8 月 4 日的市场，并保存至独立数据库。

```powershell
python .\polymarket_pipeline.py crawl `
  --slug elon-musk-of-tweets-july-28-august-4 `
  --proxy http://127.0.0.1:7897 `
  --db data\market.sqlite `
  --price-interval max `
  --price-fidelity 60
```

参数、功能和代码映射：

| 参数 | 功能 | 代码映射 |
| --- | --- | --- |
| `crawl` | 启动市场抓取流程 | `crawl_command()` |
| `--slug ...` | 指定 Polymarket event slug | `GammaClient.get_event()` |
| `--proxy ...` | 使用 Clash Verge | `JsonClient`、`XTrackerClient` |
| `--db ...` | 指定 SQLite 文件 | `MarketStore(Path(args.db))` |
| `--price-interval max` | 尽量抓取完整价格历史 | `ClobClient.get_history()` |
| `--price-fidelity 60` | 请求 60 秒粒度的价格数据 | CLOB `prices-history` 参数 |

抓取后数据库中的主要内容：

| 表 | 内容 | 写入代码 |
| --- | --- | ---|
| `market_events` | 标题、规则、结算窗口、原始 Gamma JSON | `save_event()` |
| `market_outcomes` | 每个计数区间和 Yes/No token | `save_event()` |
| `market_prices` | 每个区间 Yes token 的历史价格 | `save_prices()` |
| `posts` | XTracker 返回并去重后的帖子 | `upsert_posts()` |
| `count_samples` | 当前抓取时的计数与新鲜度 | `insert_sample()` |

## 5. 按日期生成市场并批量抓取

已知市场从 2026 年 2 月 10 日开始，按周二、周五启动，每个市场持续七天。

```powershell
python .\polymarket_pipeline.py crawl `
  --start 2026-02-10 `
  --end 2026-08-05 `
  --proxy http://127.0.0.1:7897 `
  --db data\market.sqlite
```

代码映射：

- `generate_slugs()`：从日期范围生成市场 slug。
- `DEFAULT_ANCHOR = "2026-02-10T12:00:00"`：首个已知市场开始时间。
- `MARKET_GAPS_DAYS = (3, 4)`：周二到周五为三天，周五到周二为四天。
- `market_slug()`：生成类似 `elon-musk-of-tweets-july-28-august-4` 的 slug。

注意：`--end` 为排他上界。`--end 2026-08-05` 会包含结束于 8 月 4 日的市场。

## 6. 单独持续收集 XTracker 数据

对于未来市场，建议在市场进行期间持续收集。这能减少删除帖或后续历史回填缺失造成的数据偏差。

```powershell
python .\musk_tracker.py poll `
  --start 2026-08-11T12:00:00 `
  --end 2026-08-18T12:00:00 `
  --timezone America/New_York `
  --proxy http://127.0.0.1:7897 `
  --db data\market.sqlite `
  --interval 60
```

代码映射：

| 参数 | 功能 | 代码映射 |
| --- | --- | --- |
| `poll` | 每隔固定时间持续抓取 | `run_poll()` |
| `--interval 60` | 每 60 秒运行一次 | `time.sleep(args.interval)` |
| `--start/--end` | 设置结算窗口 | `parse_bound()` |
| `--timezone America/New_York` | 使用市场规则中的美国东部时间 | `ZoneInfo` |
| `count_samples` | 记录每次抓取的计数和新鲜度 | `insert_sample()` |

## 7. 训练基础 Ridge 回归模型

基础模型的目标是预测“该市场结束时的总有效帖子数”。

```powershell
python .\polymarket_pipeline.py train `
  --db data\market.sqlite `
  --start 2026-02-10 `
  --end 2026-08-09 `
  --events data\external_events.jsonl `
  --model-out data\market_ridge_model.json `
  --report data\market_predictions.csv
```

模型特征和代码映射：

| 特征 | 含义 | `market_features()` 字段 |
| --- | --- | --- |
| 当前计数 | 截至预测时点已确认的帖子数 | `current_count` |
| 已过/剩余时间 | 市场进度 | `elapsed_hours`、`remaining_hours` |
| 节奏预测 | 用当前速度外推到结算时 | `pace_projection` |
| 6/24 小时速度 | 最近发帖活跃程度 | `last_6h_rate`、`last_24h_rate` |
| 历史速度预测 | 使用过去结束市场的平均速度 | `prior_rate_projection` |
| 盘口隐含期望 | 各计数区间价格归一化后得到的期望 | `market_expected_count` |
| 盘口质量 | 所有 Yes 区间价格之和 | `market_probability_mass` |
| 事件特征 | 新闻、发布、政策等外部事件 | `event_count`、`event_impact`、`event_*` |

训练流程对应：

1. `build_examples()`：为每个历史市场生成多个预测时点。
2. `split_examples_purged()`：按时间划分训练和测试，并删除与测试市场重叠的训练窗口。
3. `fit_ridge()`：标准化特征，使用带 L2 正则的线性回归。
4. `write_report()`：把预测与本地观察到的最终计数写入 CSV。

### 外部事件文件示例

`data\external_events.jsonl` 每一行是一个 JSON 对象：

```json
{"id":"launch-1","timestamp":"2026-07-30T13:00:00Z","kind":"launch","impact":2}
{"id":"policy-1","timestamp":"2026-07-31T16:30:00Z","kind":"policy","impact":1}
```

- `timestamp`：事件发生时间。
- `kind`：推荐使用 `news`、`launch`、`conflict`、`policy` 或 `other`。
- `impact`：事件强度，默认值为 `1`。
- 写入代码：`save_external_events()`。

## 8. 单市场预测

```powershell
python .\polymarket_pipeline.py predict `
  --slug elon-musk-of-tweets-july-28-august-4 `
  --db data\market.sqlite `
  --model data\market_ridge_model.json
```

输出字段说明：

| 字段 | 含义 | 触发代码 |
| --- | --- | --- |
| `model_forecast_final_count` | 模型预测的结算总数 | `RidgeModel.predict()` |
| `market_expected_count` | 盘口区间价格推导的预期总数 | `market_distribution()` |
| `price_probability_mass` | 价格分布总质量 | `market_distribution()` |
| `post_feed_stale` | XTracker 数据是否过期 | `latest_sample()` |
| `outside_market_window` | 是否在市场结算窗口外 | `predict_command()` |
| `historical_evaluation` | 是否手动指定历史 `--as-of` | `predict_command()` |
| `do_not_auto_trade` | 任一安全门控失败时为 `true` | `predict_command()` |

安全门控：

- 概率质量低于 `0.6` 或高于 `1.2` 时，盘口分布无效。
- 没有近期 XTracker 样本或样本超过 `--max-sample-age-seconds` 时，数据过期。
- 市场窗口外或历史时点回放时，不允许自动交易。

## 9. 一小时粒度本地回放测试

`hourly-test` 是本次新增的便捷测试功能。它从本地 SQLite 读取数据，默认对每个待测市场单独执行 walk-forward 训练，再从开盘后一小时开始，每小时输出一次预测。

```powershell
python .\polymarket_pipeline.py hourly-test `
  --db data\market.sqlite `
  --start 2026-02-10 `
  --end 2026-08-09 `
  --report data\hourly_market_test.csv `
  --summary data\hourly_market_test_summary.csv
```

调用、功能和代码映射：

| 参数/输出 | 功能 | 代码映射 |
| --- | --- | --- |
| `hourly-test` | 启动小时回放 | `hourly_test_command()` |
| 不传 `--model` | 每个市场使用其以前的市场训练 | `eligible_prior_windows()`、`fit_ridge()` |
| `--model model.json` | 使用指定模型，不重新训练 | `load_model()` |
| `--min-elapsed-hours 1` | 从开盘后一小时开始预测 | `hourly_checkpoints()` |
| `--training-step-hours 12` | 训练集每 12 小时取一个时点 | `build_examples()` |
| `hourly_market_test.csv` | 每市场每小时一行的预测明细 | `write_hourly_test_reports()` |
| `hourly_market_test_summary.csv` | 每市场一行的回放汇总 | `write_hourly_test_reports()` |

明细 CSV 中的重要字段：

| 字段 | 含义 |
| --- | --- |
| `checkpoint_utc` | 当前这一行预测发生的时点 |
| `current_count` | 截止该时点本地帖子累计数 |
| `model_forecast_final_count` | 模型预测的最终结算数 |
| `forecast_bin` | 预测数对应的 Polymarket 区间 |
| `observed_local_final_count` | 本地历史帖子表最终观测数 |
| `resolved_bin` | Polymarket 已结算的官方结果区间 |
| `forecast_in_resolved_bin` | 预测是否落入官方结算区间 |
| `absolute_error_to_observed_count` | 预测相对本地观测最终数的绝对误差 |
| `price_distribution_valid` | 当前盘口概率质量是否在有效范围内 |

### 使用当前本地历史数据的示例

现有 `data\live_smoke.sqlite` 的历史回填数据存在本地计数与结算区间不一致的情况。若仅用于研究，可以显式允许：

```powershell
python .\polymarket_pipeline.py hourly-test `
  --db data\live_smoke.sqlite `
  --allow-unverified-history `
  --report data\hourly_live_smoke.csv `
  --summary data\hourly_live_smoke_summary.csv
```

`--allow-unverified-history` 与代码的对应关系：

1. 默认 `require_verified_final_count=True`。
2. `MarketStore.final_count_is_verified()` 检查本地最终计数是否落在官方结算区间。
3. 不一致的市场默认不能进入训练。
4. 加入该参数后，允许它们进入回放训练，但汇总文件会写入 `exploratory` 提示。

这类回放结果不能用于自动交易决策，因为历史回填可能缺少被删除或未完整导出的帖子。

## 10. 验证

运行所有单元测试：

```powershell
python -m unittest -v .\test_polymarket_pipeline.py
```

测试覆盖：

- 市场规则文本的结算窗口解析。
- 夏令时窗口转换。
- 区间价格分布归一化。
- 异常概率质量拦截。
- 重叠市场窗口的 purged 切分。
- 每小时预测时点生成。
- 预测值到官方计数区间的映射。
## 11. 前端竞猜与复盘

前端文件为 `forecast_dashboard.html`。它是一个不依赖后端的本地静态页面，双击打开即可使用。

使用步骤：

1. 运行 `hourly-test` 生成公开预测文件 `data\hourly_market_predictions_public.csv`，
   同时生成私有明细 `data\hourly_market_test.csv`。
2. 打开 `forecast_dashboard.html`。
3. 选择公开预测 CSV，点击“加载本地结果”。
4. 默认使用“竞猜模式”，选择市场和小时，先记录方向判断。
5. 需要复盘时，再选择私有结算明细 CSV，点击“揭晓结算”，查看 MAE、官方区间命中率、方向准确率和纸面优势代理。

代码映射：

- `parseCsv()`：兼容算法版本增加或减少的 CSV 列。
- `loadRows()`：按 `slug` 组织市场并动态重建选择器。
- `signalFor()`：比较模型预测和盘口隐含期望，生成偏高、偏低、观望或不交易信号。
- `update()`：根据当前市场和小时刷新页面。
- `updateReview()`：揭晓后计算实际结果指标。

页面默认不显示最终实际计数。当前 CSV 没有逐区间可成交 bid/ask、费用和滑点，所以“纸面优势代理”不是真实收益。

## 12. 扩展采集与模型分支

在 `model` 分支中，爬虫支持以下扩展：

```powershell
# 快速批量补齐历史规则和帖子，不请求每个区间的历史价格
python .\polymarket_pipeline.py crawl `
  --start 2026-02-10 --end 2026-08-10 `
  --proxy http://127.0.0.1:7897 `
  --db data\extended.sqlite `
  --skip-prices

# 活跃市场补抓价格和订单簿
python .\polymarket_pipeline.py crawl `
  --slug elon-musk-of-tweets-august-7-august-14 `
  --proxy http://127.0.0.1:7897 `
  --db data\extended.sqlite `
  --price-interval 1d `
  --price-workers 8 `
  --orderbooks
```

模型回放可以使用官方结算区间中点作为历史训练标签：

```powershell
python .\polymarket_pipeline.py hourly-test `
  --db data\extended.sqlite `
  --start 2026-02-10 --end 2026-08-09 `
  --resolved-bin-labels `
  --report data\hourly_extended_v4.csv `
  --summary data\hourly_extended_v4_summary.csv
```

`--resolved-bin-labels` 不代表知道真实精确发帖数，而是用官方区间中点作为带不确定性的训练目标；回放中仍会同时记录本地观测值、官方区间和数据核验状态。
