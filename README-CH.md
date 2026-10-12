# Polymarket Elon Musk 发推数量研究系统

本项目用于本地研究 Polymarket 的 Elon Musk 七天发推数量市场。系统可以采集市场规则、XTracker 发推计数、CLOB 赔率和盘口、新闻及股价，训练和回放概率模型，并通过本地网页提供实时研究与纸面预测。

系统只做研究模拟，永久禁止自动交易、自动下注、钱包、签名和真实资金操作。任何数据不足、过期、盘口不完整或历史结果无法核验的情况，都应输出“不交易”。

## 代码分层

### 数据采集与数据库

- `musk_tracker.py`：访问 XTracker，解析和去重帖子，保存发推计数样本。
- `polymarket_pipeline.py`：负责 Gamma 市场元数据、XTracker 帖子、CLOB 赔率/订单簿、SQLite 建表迁移、Ridge 训练、概率预测、purged walk-forward 和逐小时回放。
- `data\extended.sqlite`：本地数据库，保存市场、区间、赔率、盘口、帖子、新闻语料、纸面预测和纸面记录。该文件属于用户本地数据，禁止删除或覆盖。

### 实时服务

- `live_dashboard_server.py`：本地 HTTP 服务，默认端口 `8765`。它按约 30 秒轮询 Gamma、XTracker、CLOB、RSS 和股价，并保持周二/周五两个重叠市场按 slug 隔离。
- 服务计算模型预测、盘口分布和保守纸面建议，但不会提交订单。
- `/api/live` 返回单盘实时快照；`/api/live-markets` 返回已选择的多个市场；`/api/market-archive` 只读返回本地归档；`/api/prediction-archive?slug=<slug>&limit=100` 返回统一预测历史记录。

### 前端研究台

- `forecast_dashboard.html`：静态前端，展示实时市场、发推数、盘口、赔率、新闻、纸面建议和历史回放。

前端“历史回测记录”直接调用本地 `/api/backtest-review`，默认以 5 分钟步长生成检查点，可用滑动条查看每条记录。每条记录同时展示模型区间概率、盘口隐含概率、best bid/ask、价差、深度、快照年龄、模型-盘口 edge、质量标记和结算后评分；所有市场按完整 slug 隔离。该功能是固定模型时间点复放，不会重训、抓取或自动交易。

已结算检查点会并列给出三类概率评分：固定模型、只根据当时本地可用帖子计数外推的“本地速度”基准，以及同期完整本地归档盘口的市场概率。模型相对基准的配对差定义为“模型分数减基准分数”，Brier/Log Loss 差为负只表示该配对样本中模型分数较低。市场评分要求每一个结算区间都有质量有效的本地归档价格；盘口不完整时不补零、不评分。市场对照不是独立模型基准，因为当前模型也会使用本地归档价格特征。三类评分都仅用于研究诊断，不构成训练有效性、可成交性或盈利证明。

- 历史模式保留猜谜机制：公开预测可以先载入；私有结算必须显式载入并点击“揭晓结算”后才显示答案、指标和残差。
- 全局残差图按每个市场最后一个回放时点绘制，包含横轴日期、纵轴残差和悬停详情，可显示完整市场 slug 与两种残差口径。

### 新闻与 AI

- `news_event_crawler.py`：只按白名单抓取 RSS/Atom，保存来源、文档版本、抓取运行、错误和待审核事件。
- `ai_provider.py`：可选的 OpenAI-compatible 结构化分析层。AI 不能直接修改模型概率、纸面交易状态或自动交易状态；没有 Key、合规语料或服务失败时正常降级。

### 启动、回放和测试

- `dashboard_launcher.py` / `dashboard_launcher.html`：本地启动控制台。运行后可在网页选择本地回测/历史只读（不抓取）、自动发现或固定 slug，并配置研究台参数；控制台只管理自己创建的唯一后端实例。
- `start_dashboard_launcher.ps1`：启动控制台的便捷脚本。
- `build_dashboard_launcher.ps1`：使用 PyInstaller 将控制台打包为 `dist_dashboard_launcher\AIPRP-DashboardLauncher.exe`。

- `start_live_dashboard.ps1`：自动选择最近的周二/周五市场并启动实时服务。
- `run_hourly_replay.ps1`：生成逐小时历史回放 CSV，不代表盈利证明。
- `test_polymarket_pipeline.py`、`test_news_event_crawler.py`、`test_ai_provider.py`：测试市场窗口、数据库、订单簿、回放、来源审核、AI 降级和实时服务。
- `RUNBOOK-CH.md`：中文运行手册；`MAINTENANCE-CH.md`：维护和操作审计日志。

## 实时归档

每次实时刷新都会追加写入 SQLite 的 `live_market_snapshots` 表，保存：

- 分时累计发推数、最新帖子采集新鲜度和模型预测；
- 每个结果区间的 bid/ask、价差、深度和盘口摘要；
- 对应时段新闻、已审核事件和股价上下文；
- Gamma、XTracker、CLOB、RSS、股价请求的状态、耗时和错误；
- 完整结构化快照以及 `do_not_auto_trade=true`。

完整帖子原始记录仍在 `posts`，完整 CLOB 原始订单簿仍在 `market_orderbooks.raw_json`。市场结束后，实时快照不会因为前端切换市场而消失。

读取某个市场最近的归档：

```text
http://127.0.0.1:8765/api/market-archive?slug=<market-slug>&limit=200
```

没有新归档记录的旧市场会从 `paper_forecasts.snapshot_json` 兼容生成统一结构，并标记 `capture_origin=legacy_paper_forecast`。历史无法回补的新闻、盘口、股价或航班信息使用空列表和明确状态对象，不使用缺失字段。当前没有配置航班数据源。

## 预测历史统一归档

`python .\polymarket_pipeline.py archive --db .\data\extended.sqlite` 会幂等地把既有 `paper_forecasts` 索引到 `prediction_archive`。爬虫成功、部分失败或市场发现失败时也会追加同一结构的记录，保存 `data_time_utc`、`captured_at_utc`、来源状态、预测输出、错误和风险原因，便于后续前端适配。

历史日期抓取查询 Gamma 真实事件列表；发现失败不自动猜测日期 slug，CLI仅显式禁用发现时使用日期生成。页面优先1小时价格研究，可切换5分钟；按实际市场时间调用Data API v2分页，保留源实际精度，已有完整区间及近期成功核验跳过重复下载。稀疏记录与请求错误分开显示。独立一小时数据可运行 `python hourly_price_research.py --fetch`，只读生产库、写入 `data/hourly_research/`。需要当前 CLOB 盘口时使用 `--orderbooks`（不能恢复历史深度）；需要公开成交时显式使用 `--public-trades`。详见RUNBOOK-CH.md。

## 启动实时研究台

```powershell
powershell -ExecutionPolicy Bypass -File ".\start_live_dashboard.ps1" `
  -Db ".\data\extended.sqlite" `
  -Model ".\data\extended_model_v4.json" `
  -Proxy "http://127.0.0.1:7897"
```

打开：`http://127.0.0.1:8765/`

实时服务是 REST/RSS 轮询，不是毫秒级 WebSocket。若修改了 Python 服务，需要重启服务；仅修改静态前端时，刷新浏览器即可。

## 历史回放与残差口径

逐小时回放输出公开预测、私有结算明细和市场汇总。官方结算通常是区间而非精确数量，因此：

- 本地观测残差 = 最终模型预测 - 本地最终发推观测；
- 官方代理残差 = 最终模型预测 - 官方结算区间中点；
- 开放尾部或缺少上下界时，不计算官方中点代理；
- 这些图表用于研究误差结构，不是 MAE、收益或盈利能力证明。

全局残差图按最终回放时点排序。将鼠标悬停在图中某个位置，可以查看完整市场 slug、最终回放时点、本地观测残差和官方中点代理残差。

## 安全边界

`do_not_auto_trade=true` 永久保持。项目没有钱包、签名、下单、自动下注或真实资金操作路径。小样本命中率、AI 置信度、paper PnL 和残差图都不能解释为稳定盈利证明。
## 长期开发注意事项

格式或契约发生变化时，必须同步适配所有生产函数、参数构造器、后台任务、CLI、API DTO 和前端读取逻辑。重点检查 `HistorySyncManager._report_args()` 与 `hourly_test_command()` 的参数一致性；抓取、报告生成、汇总和前端显示要分别测试，不能只看到抓取成功就认为整项任务成功。

每次此类修改后都要运行全套单元测试、Python 编译、前端脚本语法检查和 `git diff --check`，并在重启服务后核对健康接口及任务状态。

维护沟通标准：申请额外权限或执行外部/高风险动作前，先说明准备做什么、原因和影响；完成每个阶段性任务后，说明实际完成内容、验证结果、未完成部分和下一步计划。计划、代码测试通过和外部实测结果必须明确区分。
# 2026-09-19 历史工作区修复说明

连续切换市场改为有进度、可取消的只读后台任务；回放不再等待实时采集锁，盘口查询复用既有复合索引。
纸面模拟启用早期速度稳定和尾部新增仓位保护，展示原始/受约束预测；旧版汇总深度执行及首小时策略保留。
使用方式、只读调试启动和重启边界见 `RUNBOOK-CH.md`，公式和参数见 `Logic.txt`。本次未重训或修改生产数据。
