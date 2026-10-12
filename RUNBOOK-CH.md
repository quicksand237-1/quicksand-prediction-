# 本地运行手册

## 概率分数分配与全历史统计（2026-09-20）

覆盖参数现在直接表示绝对概率：中心不足目标时，左右相邻盘口按概率比分配剩余覆盖和预算。30%覆盖、中心25%、左右21%/14%时，预算为83.333%/10%/6.667%。无效盘口对应份额留现金。
选“持有至结算”并读取/重新模拟后，页面自动遍历全部本地已结束市场（列表上限500，当前65），使用此次相对建仓时长及资金、覆盖参数，独立于页面日期范围。可用“使用当前节点”填入时长后重新模拟。
统计表显示实际建仓时间、投入、盈亏和回报率；揭晓后显示结算数字。未成交、未结算、失败单独标注，不当作零收益；汇总按实际投入加权，不代表连续复投资金曲线。
API paper_hold_summary=1仅支持mandatory_entry+hold_to_settlement，不允许start/end；每个市场独立只读任务，现有取消及超时生效。

## 行情覆盖状态（2026-09-20）

历史页的“盘口中间价代理”只表示当时本地安全订单簿存在而价格历史缺失，不能用于纸面成交；只有“历史价格”来源参与价格统计。页面会区分未采集、无有效双边报价和深度无效。
爬虫按市场outcomes明确的YES token保存价格/订单簿；兼容旧归档无token记录。批量CLOB价格响应支持token顶层映射与标准prices/data/items结构；价格历史支持t/p及timestamp/price字段。
订单簿采集仍需显式启用`--orderbooks`，价格可用`--skip-prices`关闭；缺失历史不会由后采行情补写。重新采集生产市场必须另行授权。

### 相对建仓时间与退出管理（2026-09-20更新）

首次时间控件现为“市场开始后（小时）+再等（分钟）”，例如24小时30分钟；使用当前节点会填写它距离市场起点的时长。点击重新模拟生效，跨市场沿用等待时长。
API新增paper_entry_elapsed_minutes（与绝对时间参数互斥）、paper_exit_policy=managed或hold_to_settlement。
选“关闭退出管理：买入后持有至结算”会关闭止盈、止损和edge退出，且不再增持；金额及浮盈仍跟踪，官方结果缺失时保留未结算状态。默认managed不变。
以下旧UTC时间操作说明由本节替代；绝对时间仅为API兼容入口。

## 指定首次建仓（2026-09-20）

纸面交易页选择“指定节点强制建仓”，设置UTC首次时间（留空自动寻找），或用“使用当前节点”，点击“重新模拟”。可调整概率覆盖、资金份数和止盈/止损百分比。拖动游标仅浏览，不重复交易。
“原策略对照”保持旧开仓与退出规则。新模式的首次时间必须落在该市场的五分钟检查点；指定前空仓，缺安全盘口顺延。
每份资金为1报价货币单位；账本净单价已含买入费用/扣除卖出费用。累计投入可能因周转超过初始资金，资金收益率与投入回报率分开展示。
浏览至末检查点并揭晓结算可看最终兑付；缺官方结果或范围未到期时显示未结算。
只读预览：python debug_history_readonly.py --serve 8778。此入口不会构造可写Store，不启动采集；默认使用现有data/extended.sqlite与本地模型。
新增历史API参数：paper_mode=mandatory_entry、paper_entry_checkpoint（带时区ISO）、paper_probability_coverage=0.8、paper_capital_units=100、paper_take_profit_return=0.2、paper_stop_loss_return=-0.15。参数参与运行ID；API不传模式仍为legacy。

## 历史切换与只读调试（2026-09-19）

历史工作区读取时会显示排队/已计算检查点进度。切换市场、日期或CSV会取消旧任务；失败清空旧结果，可以重新读取。
前端使用 `/api/backtest-review?async=1&slug=...` 获取任务ID，轮询 `/api/backtest-jobs/{id}`，DELETE取消。
旧同步接口保留；异步任务上限120秒，HTTP单请求15秒超时。任务过期/失败不会永远显示加载。
纸面页显示原始预测、受约束预测及参考范围；“概率保护”表示不能新增仓位，不表示缺少盘口。
固定CSV不重新执行概率修正或纸面成交。新风险参数及公式见 `Logic.txt`，不代表已校准概率或盈利保证。

修改磁盘代码后，原先运行的后端不会自动更新；须在允许的时机重启原服务并刷新页面。
只想复查历史、避免启动采集时，可以运行：

```powershell
python debug_history_readonly.py --serve 8778
```

打开 `http://127.0.0.1:8778/`。该独立调试服务只开放历史读取，禁用采集、AI、POST写入及外部接口；SQLite使用mode=ro，模型只读。
终端Ctrl+C关闭。默认使用data/extended.sqlite和extended_model_v4.json；不会替换8765服务。
真实页面回归：`node test_history_workspace.cjs http://127.0.0.1:8778/`；默认不带URL的命令仍使用合成响应。

## AI 辅助分析

这里填写的是 AI Provider 控制台创建的 API Key，不是 Codex、ChatGPT 网页登录凭据，也不能复用 Codex 会话令牌。项目不会尝试读取或复用 Codex/ChatGPT 登录信息。

启动实时服务后，打开同一个本地网站，在“AI 辅助分析”区域填写：

1. `Provider`：OpenAI 使用 `OpenAI`；其他兼容接口选择 `OpenAI-compatible`。
2. `API 地址`：OpenAI 默认是 `https://api.openai.com/v1`；兼容服务填写其官方 API 根地址。远程地址必须是 HTTPS，本机调试接口才允许回环 HTTP。
3. `模型`：填写你的账户实际可用模型名称。
4. `API 模式`：OpenAI Responses API，或兼容服务支持的 Chat Completions。
5. `API Key`：点击“保存到本机”。页面不会回显完整密钥；保存后输入框会清空。

Windows 上优先写入 Credential Manager；降级时写入项目目录的 `.env`。`.env` 和 `.ai-config.json` 已加入忽略规则，仍不要把它们复制到 Git、截图、日志或别人可访问的目录。清除密钥点击“清除密钥”，然后重新启动服务可确认状态。

点击“分析当前语料”才会产生一次外部 API 请求。AI 只整理已经通过来源层审核的本地新闻，输出结果显示在页面下方；它不改变 V4 数量预测、区间概率、风险门控或纸面建议。若事件库没有合规语料，页面会提示先运行：

```powershell
python .\news_event_crawler.py crawl-news --db data\extended.sqlite --sources .\news_sources.example.json --proxy http://127.0.0.1:7897
```

建议先用 Provider 控制台设置单日额度、模型白名单和账单告警。当前项目没有自动交易、自动下注或真实资金操作路径。

## 一键更新实时数据

实时网页不需要上传 CSV。启动本地服务并打开网页后，点击“立即抓取更新实时数据”，服务会立刻更新 XTracker 当前计数、Gamma 市场规则、CLOB 盘口、TSLA 行情和 RSS 新闻，并把市场计数/盘口快照写入 `data\extended.sqlite`。后台默认按 300 秒（5 分钟）间隔轮询并追加归档。

历史 CSV 上传区只用于猜谜和回测复盘，和实时研究区相互独立。新闻事件语料的来源审核仍需单独运行 `news_event_crawler.py`，一键实时刷新不会把未经审核的新闻加入预测模型。

## 市场动态与预测时点研究

`market_dynamics_research.py` 是独立的离线研究模块，不访问数据库、网络或模型，也不执行交易。实时快照归档会附带 `dynamics_features`，包含开盘后时间、计数、近期发帖速率、模型/市场预期差和来源年龄；缺失字段保持为空。

将已导出的标准化研究行保存为 JSONL 后，可运行：

```powershell
python .\market_dynamics_research.py .\research_rows.jsonl --mode mapping
python .\market_dynamics_research.py .\research_rows.jsonl --mode entry
python .\market_dynamics_research.py .\research_rows.jsonl --mode walk-forward --min-training-markets 10
```

`mapping` 输出标准化多目标 ridge 系数矩阵、标准误和95%区间；`entry` 比较开盘后每5分钟的固定纸面建仓节点；`walk-forward` 只用先结束市场选择节点，再评估后续市场。没有安全历史盘口、结算值或完整目标的行不会被填成零收益。所有输出均为研究结果，`do_not_auto_trade=true`。

## 实时归档与结束市场复盘

每次实时刷新都会追加保存到 `data\extended.sqlite` 的 `live_market_snapshots`，不会覆盖此前市场、盘口或纸面记录。单条归档包含当时的累计发帖数、盘口摘要、完整前端快照、新闻上下文、股价、已审核事件、各来源请求耗时/错误以及永久的 `do_not_auto_trade=true`；对应的完整 CLOB 原始订单簿继续保存在既有 `market_orderbooks.raw_json`，单帖原始记录继续保存在既有 `posts` 表。

服务运行后可在本机读取某市场最近的归档，不会触发外部请求：

```text
http://127.0.0.1:8765/api/market-archive?slug=elon-musk-of-tweets-august-14-august-21&limit=200
```

已结束或早于本次迁移的市场没有新归档行时，接口会从既有 `paper_forecasts.snapshot_json` 生成相同字段结构，并标记 `capture_origin=legacy_paper_forecast`、`refresh_status=legacy`。历史市场没有可回补的实时原始盘口、新闻或股价时，列表字段固定返回空列表，航班上下文固定返回 `status=not_configured` 与空列表，不会以空字段中断复盘读取。当前仓库没有航班数据源；新增任何航班 API 前需先确认数据许可、费用和隐私边界。

## 自动双盘启动

`start_live_dashboard.ps1` 现在不传 `-Slug` 时，会按纽约时间自动选择最近两个已开盘的周二/周五市场。请停止旧服务后运行：

```powershell
.\start_live_dashboard.ps1
```

当前 2026-08-16 的两个市场为 `elon-musk-of-tweets-august-14-august-21` 和 `elon-musk-of-tweets-august-11-august-18`。服务启动后，网页“实时市场”下拉框可在两盘间切换；每盘计数、盘口和纸面记录互相隔离。

## 固定路径复盘

服务启动后，历史区不必再上传 CSV。点击“加载固定公开复盘”会读取：

```text
data\hourly_extended_v4_fullprob_public.csv
```

它不含真实结算答案，可直接进入猜谜模式。需要复盘结果时，再显式点击“加载固定私有结算与汇总”，它会读取：

```text
data\hourly_extended_v4_fullprob.csv
data\hourly_extended_v4_fullprob_summary.csv
```

私有文件载入后仍需点击页面中的“揭晓结算”。自定义 CSV 仍可使用原来的上传控件。若要换固定文件，启动服务时追加 `--public-report`、`--private-report`、`--summary-report` 参数。

## 本地事件语料库（AItest）

本阶段不调用 AI，也不需要 API Key。抓取器只读取 `news_sources.example.json` 中的 RSS/Atom 公开订阅源，并将文档、错误和运行记录写入本地 SQLite。

先在项目目录执行一次采集：

```powershell
python .\news_event_crawler.py crawl-news --db data\extended.sqlite --sources .\news_sources.example.json --proxy http://127.0.0.1:7897
```

SEC 等官方源可能要求可联系的 User-Agent。只有在你确认自己的标识可公开用于该请求后，才额外传入本地联系标识；它不是 API Key，不会写入数据库或 Git：

```powershell
python .\news_event_crawler.py crawl-news --db data\extended.sqlite --sources .\news_sources.example.json --proxy http://127.0.0.1:7897 --user-agent "AIPRP-local-research your-contact@example.com"
```

采集完成后，导出截至某个预测时点可用的 JSONL 语料。此命令不会覆盖模型，也不会把结果自动导入事件影响层：

```powershell
python .\news_event_crawler.py export-corpus --db data\extended.sqlite --output data\news_corpus.jsonl --as-of 2026-08-12T12:00:00Z
```

可在 SQLite 中审计：`crawl_runs` 为每次抓取的开始/结束/错误统计，`crawl_errors` 为失败原因，`source_documents` 为原始文档元数据，`event_candidates` 为仅待审核的关键词候选。`source_events` 仍只接受现有的审核导入流程。

来源配置规则：`tier=1` 是官方/监管，`tier=2` 是独立媒体，`tier=3` 是发现或聚合；`discovery_only=true` 的源即使抓取成功也不会进入 AI 导出文件。请在增加来源前自行确认该来源的 robots、订阅和再利用条款；不要把付费全文、登录态内容、密钥或个人数据写进配置。

## 运行前提

- Windows、Python 3.11 或更高版本。
- 项目目录为 `C:\Users\宋思远\Desktop\K\AIPRP\co爬虫测试`。
- 代理不可用时可去掉 `--proxy`；外部数据失败不会开启交易。
- `data\extended.sqlite` 和 `data\extended_model_v4.json` 必须存在。启动时只执行幂等建表，不删除历史数据。

## 启动实时研究台

默认每 300 秒（5 分钟）轮询并追加保存当前两个市场；每条快照带 5 分钟检查点和官方结算状态。可用 `-RefreshSeconds` 显式调整，但更短间隔会增加请求量。

最省事的方式：

```powershell
.\start_live_dashboard.ps1
```

如果本机禁止直接运行脚本，可用：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_live_dashboard.ps1
```

如需换市场：

```powershell
.\start_live_dashboard.ps1 -Slug elon-musk-of-tweets-august-7-august-14
```

如需同时跟踪两盘，主市场使用 `-Slug`，第二盘使用 `-ExtraSlug`：

```powershell
.\start_live_dashboard.ps1 `
  -Slug elon-musk-of-tweets-august-7-august-14 `
  -ExtraSlug elon-musk-of-tweets-august-11-august-18
```

也可以直接使用 Python：

```powershell
python .\live_dashboard_server.py `
  --slug elon-musk-of-tweets-august-7-august-14 `
  --extra-slug elon-musk-of-tweets-august-11-august-18 `
  --db data\extended.sqlite `
  --model data\extended_model_v4.json `
  --proxy http://127.0.0.1:7897 `
  --port 8765
```

网页实时区域会出现“实时市场”下拉框，可在两盘间切换。每盘分别抓取、预测和写入纸面记录，不会合并计数、盘口或概率。
若周二新盘尚未到规则规定的开始时刻，页面会显示 `outside_market_window` 并保持“不交易”；等窗口开始后下一轮刷新会自动重新评估。

在项目目录执行：

```powershell
python .\live_dashboard_server.py `
  --slug elon-musk-of-tweets-august-7-august-14 `
  --db data\extended.sqlite `
  --model data\extended_model_v4.json `
  --proxy http://127.0.0.1:7897 `
  --refresh-seconds 30 `
  --port 8765
```

也可以先运行本地启动控制台，通过网页选择自动发现或固定 slug：

```powershell
& 'C:\Study program\Python\python.exe' .\dashboard_launcher.py
```

控制台默认地址为 `http://127.0.0.1:8760/`。它提供本地回测/历史只读、自动发现和固定 slug 三种模式；回测模式传递 `--no-background`，不会启动周期抓取。控制台只管理自己创建的研究台进程；如果目标端口已有监听，会拒绝启动第二个实例。

也可直接运行 `.start_dashboard_launcher.ps1`，或在需要换端口时传入 `-LauncherPort`、`-DashboardPort`。

如需生成单文件 Windows 程序，先在目标 Python 环境安装 PyInstaller，再运行 `.uild_dashboard_launcher.ps1`；生成的 exe 仍需与项目目录下的 `live_dashboard_server.py`、`forecast_dashboard.html` 和 `data` 目录放在一起。

浏览器打开 `http://127.0.0.1:8765/`，点击“开始实时监控”。服务端持续抓取 XTracker、CLOB 盘口、TSLA 行情和 RSS 上下文，前端不直接保存密钥。

如果实时盘口表没有“模型概率”列的数值，先在旧服务窗口按 `Ctrl+C` 停止，再重新执行启动脚本。实时页面状态栏应显示服务版本 `2026-08-11-paper-trades-v2`；也可以访问 `http://127.0.0.1:8765/api/health` 检查版本。

## 生成历史复盘文件

最省事的方式：

```powershell
.\run_hourly_replay.ps1
```

如果本机禁止直接运行脚本，可用：

```powershell
powershell -ExecutionPolicy Bypass -File .\run_hourly_replay.ps1
```

它会默认生成新的、包含完整区间概率的报告：

- `data\hourly_extended_v4_fullprob.csv`
- `data\hourly_extended_v4_fullprob_summary.csv`
- `data\hourly_extended_v4_fullprob_public.csv`

脚本默认不会覆盖已有文件。只有明确确认后才使用 `-Force`。

然后双击打开 `forecast_dashboard.html`：

- 选择 `data\hourly_extended_v4_fullprob_public.csv` 进入猜谜模式。
- 需要揭晓时，再选择 `data\hourly_extended_v4_fullprob.csv`。
- 实时模式不需要上传文件，但前提是本地实时服务已经启动。

## 固定复盘文件与历史市场抓取

纸面交易的“同一建仓时长·全历史持有至结算统计”可直接点击“统计全部历史市场”，无需先成功读取单盘。选择指定节点强制建仓和持有至结算后，使用当前建仓时长、覆盖及资金参数，忽略单盘日期筛选。表格先列出全部待统计市场，逐盘更新；失败行保留，其余继续。无成交、未结算、失败均不计收益；实际顺延建仓会标出延迟，不等于严格该节点成交。

单盘显示无检查点时可点“完整市场范围”；切换市场会清除之前日期筛选。历史回放只支持已结束市场，当前缺历史盘口不会通过一小时研究价格伪造成交。

从本地服务打开页面后，公开预测会自动从默认路径读取，不再需要上传或选择 CSV。需要重新读取时点击“刷新固定公开复盘”；需要复盘答案时点击“载入固定私有结算与汇总”，再点击页面原有的“揭晓结算”。默认路径为：

- `data\hourly_extended_v4_fullprob_public.csv`
- `data\hourly_extended_v4_fullprob.csv`
- `data\hourly_extended_v4_fullprob_summary.csv`

其中公开预测不含答案；私有结算明细含逐小时真实结算字段；市场汇总含每个市场的复盘统计。私有文件被载入后，仍必须点击“揭晓结算”才会显示答案。

页面“历史市场自动抓取”的结束日期按市场本地时区解释，为排他边界；允许结束日期比市场时区当前日期晚一天以兼容跨日。默认抓市场元数据、XTracker计数及历史价格。“研究时间步长”默认1小时，可切5分钟；不改变策略检查点。价格改用官方Data API `/v2/prices-history`，按真实市场时间范围、最多一天一块请求并跟随next_cursor；指定精度仍缺记录时省略bucket_seconds读取最密可用历史，保留resolution_seconds。旧price_interval仅兼容，不控制历史范围。

分段补充（2026-09-21）：一天按市场起点连续24小时计算，不是自然日文件。HTTP层默认最多3次尝试；超时/断流及400/408/413/422/504在范围超过12小时时拆成两个不超过半天的片段，半天不继续细分，自动精度补查失败也适用。401/403/429/DNS错误不通过拆分增加请求。成功片段分别核验，父失败保留审计但恢复后不计最终失败；数据稀疏不等于请求失败。选择5分钟表示目标采样间隔，不是每5分钟发一次请求；不插值、不保证源数据完整。已运行服务需重载才能应用新逻辑。

请求节奏补充（2026-10-03）：历史同步默认每个历史价格/CLOB客户端至少间隔0.2秒；CLI crawl 的 --request-delay 默认0.1秒，必须有限且非负，0表示主动关闭该间隔。限制位于真实HTTP入口，覆盖同客户端所有worker、分页和重试；非零间隔时响应头返回前共享锁仍持有，因此可能降低吞吐。不是上游速率保证，多个独立进程不共用门控。复盘价格栏显示源采样精度，五分钟无样本不代表整个市场未采集；不扩大窗口、不插值。

只读覆盖检查：`python audit_history_coverage.py`；可选 `--db 路径`、`--quick-check`。直接SQLite mode=ro/query_only，不构造MarketStore、不启动服务、不联网；约16GB生产库quick_check需等待。输出区间内原始价格及捕获盘口数量，不等于安全检查点/可成交覆盖，旧数据库若缺精度字段只能报告原始覆盖。该命令不补采、不导出CSV、不运行模型回放。

展开“逐市场价格覆盖与失败明细”查看各YES token对应研究步长的覆盖、请求错误与稀疏提示；粗于所选步长的源数据不计为该精度覆盖，历史旧记录精度未知。完整已有区间跳过下载，成功核验回执缓存6小时（含稀疏/空响应），可勾选“忽略近期缓存”强制重查。成功页面即刻保留，分页错误明确报告；请求记录保存范围、精度、响应哈希与耗时。界面区分“部分请求失败”和“请求完成·记录稀疏”，每盘口更新进度。仅全部覆盖且无请求错误才重建固定报告，其他情况保留原报告。

### 独立一小时收益研究数据

`python hourly_price_research.py --fetch` 仅读取生产SQLite，在 `data/hourly_research/` 保存源价格副本、请求审计、`hourly_prices.csv` 和 `hourly_coverage.csv`。不启动实时采集、不重训、不写生产库；不带 `--fetch` 只用本地及独立缓存导出。

CSV每行对应一个市场盘口和“市场开始后第N小时”。仅取该节点之前且年龄不超过1小时的最近观测；源精度粗于1小时不冒充小时价。保留price_time_utc、price_age_seconds、resolution_seconds、source、status；缺失不插值、不借用未来价。gross_return_percent及gross_profit_per_100按官方YES结算计算，价格为0或结果未知时为空。不含价差/手续费/深度，不按模型选盘，也不是策略回测；CSV直接包含已知结算答案，不受页面揭晓锁保护。

官方文档：https://docs.polymarket.com/market-data/prices-order-books （历史保留与分页）；https://docs.polymarket.com/concepts/prices-orderbook （显示中间价/最近成交价不等于ask成交）。上游明确只保证有限期细粒度历史、三小时/十二小时长期保留，故不能保证数月前真实逐小时价格全部恢复。

“从现在起追加快照（不恢复历史）”仍仅保存当前盘口，不能补过去深度。代码更新需重载后端，再在页面执行历史抓取才能补已有数据库；本次修复未实际补采生产数据。

市场结束后补采到的价格以来源 `price_time_utc` 显示在对应 5 分钟复盘区间，并标记“事后补采历史价，仅供复盘”；由于其本地 `captured_at_utc` 晚于检查点，不能进入当时的模型特征、市场基准评分或成本后 edge。订单簿仍只能从采集开始持续保存，不能用当前 `/book` 回填过去。

- 历史抓取不会重训模型，也不会调用 AI；只有勾选“抓取后生成三份固定 CSV”时才会更新固定报告，旧版本不会删除而是备份。
- 历史订单簿无法从现在可靠复原，因此该任务不保存当前盘口为历史盘口。
- 运行中实时刷新会等待历史任务释放 SQLite 锁；建议不要同时在另一个 CMD 窗口运行第二个 `crawl`。
- 抓取完成后，如需重训或生成复盘，仍由用户显式执行既有 `train` 和 `hourly-test` / `run_hourly_replay.ps1` 命令。

## AI 新闻语料

在 AI 设置区填入 Proton 或其他用于公开联系的邮箱后，点击“抓取合规新闻语料”。该邮箱只会在本次请求中作为公开 HTTP User-Agent 联系方式发送给来源服务器，不会存入本机配置、SQLite、日志或 Git。抓取完成后，页面会显示合规语料数量；只有数量大于 0 时，“分析当前语料”才有可分析的来源审计文本。

若新闻状态显示错误摘要，完整的每来源错误记录在 `data\extended.sqlite` 的 `crawl_errors` 表；不要为绕过失败把 Google News 等发现源直接改成合规来源。

对 Xcode 的配置保持：Provider 选 `OpenAI-compatible`，API 地址填 `https://xcode.best/v1`，API 模式选 `Chat Completions`，模型名必须以 Xcode 控制台实际列出的模型 ID 为准。`Gemini` 只有在该控制台把它列为有效模型 ID 时才可用；语料不足或来源抓取失败时，即使 API Key 正确，AI 也不会执行。

本次已验证可用的低成本模型 ID 为 `gemini-2.5-flash`。不要填写笼统的 `Gemini`。如果在 Xcode 控制台轮换 Key，需在页面重新保存新 Key；轮换前后的 Key 都不得写入命令、配置文件、CSV 或日志。

## 纸面成本参数

纸面服务默认使用 `--paper-fee-rate 0.02` 和 `--paper-slippage-bps 50`，它们是保守的研究假设，不是官方费用声明。可按实际规则重新设置。`--paper-size` 默认 1 份，最低可用深度默认要求为目标纸面数量的 3 倍。

## API

### 30 秒研究采样（独立于正式五分钟归档）

服务后台以不低于 30 秒的 REST/RSS 轮询记录当前市场盘口、帖子计数和 RSS 新闻。该链路只把完整快照写入 `research_market_snapshots`；Gamma 元数据、outcome token 和当前盘口在内存中解析，不写入正式 `market_events`、`market_outcomes`、`posts`、`market_orderbooks` 或 `market_activity_snapshots`。它不调用模型，不计算 edge，不产生纸面交易，也不能补齐历史五分钟数据。

- `GET /api/research-status`：返回目标间隔、最近研究快照和 `research_only=true`。
- `POST /api/research-refresh`：本机手动触发一次研究采样；失败也会在独立表保留错误证据。
- `GET /api/research-archive?slug=<slug>&limit=120`：只读研究快照归档。

研究轮询仍受 Gamma/CLOB/XTracker/RSS 的网络可用性和限流影响。前端显示的是实际采集时间、市场开盘对齐桶和来源状态，不应把 REST/RSS 轮询称为 WebSocket 或毫秒级实时。

### 前端功能入口审计

页面顶部 slug 锚点已按工作流分组，新增功能应挂到对应分区，不再堆到页面底部：

- `#live-capture`：当前市场、正式五分钟归档、30 秒研究采样。
- `#history-acquisition`：历史 Gamma/价格补采；`#prediction-review`：预测、结算和残差查看。
- `#paper-research` 与 `#backtest`：纸面策略和回测研究；两者都不产生真实订单。
- `#diagnostics`：来源年龄、缺口、完整性和运行诊断；`#data-organization`：本地重复文件扫描/合并/受限删除。
- `#local-reports`：固定 CSV/本地报告读取。

合并/弃置建议：实时盘口与研究采样保留为两个明确状态，不能合并成一个“实时”概念；历史抓取和回测保留分区，避免把事后补采误当作策略输入；固定报告读取属于兼容入口，暂不删除；浏览器缓存清理不再承担本地数据文件整理，数据整理器是唯一文件级入口。诊断、报告和数据整理可继续收敛到独立导航分区，但删除接口仍只允许完全哈希重复的低风险文本副本。

- `GET /api/health`：服务健康状态。
- `GET /api/live`：当前市场、计数、模型概率、盘口、事件、新闻和建议。
- `GET /api/live-markets`：所有已选择市场的实时快照，供前端市场选择器使用。
- `GET /api/paper-history?limit=100`：历史纸面预测。
- `GET /api/paper-trades?limit=100`：区间级纸面记录及结算结果。
- `GET /api/prediction-archive?slug=<slug>&limit=100`：只读返回统一 `prediction_archive_v1` 历史记录；不触发外部刷新。记录按 `event_slug` 完全隔离，包含数据时间、采集时间、计数、预测、盘口摘要、来源状态、错误和 `do_not_auto_trade=true`。
- `GET /api/backtest-review`：只读本地 SQLite 的统一历史回测与复盘。可传 `slug`、`start`、排他 `end`、`step_minutes`（默认 5）、`min_elapsed_minutes`（默认 5）、`max_price_age_hours`、`backtest_min_edge`、`backtest_max_spread`、`backtest_min_depth` 和 `limit`（默认可覆盖完整七天市场）。检查点使用无重叠 `[interval_start_utc, interval_end_utc)` 区间，返回区间新增/累计帖子、完整 `outcome_market_rows`（每个结果区间的模型概率、价格范围、bid/ask、价差、深度、纸面成本后 edge、年龄和质量标记）、总区间数及是否截断，以及随检查点变化的 Brier / Log Loss。行情按本地采集时间并受来源时间门控；缺失或不安全行情保持 null，不以当前盘口补历史空缺。

回测页面的滑动条只移动已加载的检查点，不会重新抓取或修改模型。回测使用当前提供模型的固定复放，严格按检查点前可用的帖子、价格和盘口归档读取，不是 walk-forward 训练；`paper_candidate_only` 只是纸面候选，不是交易指令或盈利证明。没有本地快照时必须显示数据不足/不交易，不能用当前盘口替代历史盘口。

模型、本地速度和市场概率的 Brier/Log Loss 只在本地存在结算区间时评分。模型相对本地速度或市场的配对差均为“模型分数减基准分数”，负值只表示配对检查点上的评分较低。市场概率不是独立基准，因为固定模型也可接收本地归档价格特征；它不能证明模型有超越市场的独立增量，也不能证明潜在收益。

## 预测数据归档与低频爬取

历史价格抓取默认以 5 分钟为目标，按 API 保留期选择粒度：最近 60 天请求 5 分钟；60 至 90 天请求 30 分钟；超过 90 天请求 3 小时长期档。5 分钟区间空、稀疏或请求失败时，抓取器会保留原请求证据并尝试更粗粒度；响应中的 `resolution_seconds` 始终表示源精度，30 分钟及更粗记录不会补成 5 分钟样本，也不会进入 5 分钟安全特征、评分或 edge。若 API 没有返回源分辨率，记录会保持“未知”，系统不会从请求粒度推断精度。

单次请求按约 450 条安全阈值切窗，响应达到 450 条会自动二分并重试；请求仍遵守显式 `start/end` 最多 15 天、`end` 排他。分页仍逐页审计。请求审计含目标/实际粒度、保留期策略、返回条数与拆分原因。代码更新后需重启本地服务/抓取任务才会加载；本次没有执行生产补采。

历史爬虫优先使用 Gamma `/events` 列表发现真实市场；只有发现失败时才回退到日期生成的 slug。每次 `crawl` 会追加一条统一预测归档，成功、部分失败和 Gamma 404 都会保留可检索状态，不伪造市场数据。

```powershell
python .\polymarket_pipeline.py archive --db .\data\extended.sqlite
```

该命令只把既有 `paper_forecasts` 幂等索引到 `prediction_archive`，不会改写原表、CSV 或模型。需要公开成交记录时显式加 `--public-trades`；需要订单簿时加 `--orderbooks`，默认启用 CLOB 批量请求，可用 `--no-batch-clob` 关闭。批量请求只减少请求次数和同一快照的时间差，不会改变预测逻辑。

实时服务默认 300 秒（5 分钟）REST/RSS 轮询；网络受限时可使用 `-RefreshSeconds 600` 或更长间隔。低频采集必须区分 `data_time_utc`、`captured_at_utc` 和来源年龄，缺失采样不等于该小时发帖数为零；过期或不完整数据仍保持“不交易”。

公开成交、价格和盘口是市场活动上下文，不是 Elon 发帖数的直接观测。只有在按交易发生时间做时间门控并完成 purged walk-forward 样本外比较后，才可讨论是否纳入正式模型；本次回填没有重训或新增指标。

## 停止、备份和排查

- 在运行服务的 PowerShell 窗口按 `Ctrl+C` 停止；不会删除数据库。
- 备份前先停止服务，然后复制 `data\extended.sqlite` 到用户明确指定的备份位置。
- `不交易` 的原因会显示在前端并写入 `paper_forecasts.risk_reasons` 与 `paper_trades.risk_reasons`。
- 若页面提示“本地实时服务版本过旧”，在运行服务的窗口按 `Ctrl+C`，再执行新的 `start_live_dashboard.ps1`；旧进程不会自动加载新 Python 路由。
- 若 8765 仍被旧进程占用，可把启动参数改为 `--port 8766`，从 `http://127.0.0.1:8766/` 打开页面；新版页面会自动连接当前端口。
- 重点检查 XTracker 新鲜度、盘口快照年龄、盘口概率质量、价差、深度和来源审核状态。
- 没有 API key 时系统仍可运行；可选 AI 只能结构化新闻，不能输出概率或交易指令。本轮服务没有自动调用外部 AI。

## 明确禁止

本项目永久禁止自动交易、自动下注、钱包签名和真实资金操作。所有建议最多是“纸面方向研究”，数据不足时必须是“不交易”。

## 回放残差曲线

1. 启动新版本地服务并打开前端，先点击“刷新固定公开复盘”。
2. 如需复盘，点击“载入固定私有结算与汇总”，再点击“揭晓结算”。未揭晓前不会显示官方区间、真实答案或残差曲线。
3. 在对应市场的“回溯预测与官方结算的残差”图中，正值表示当时模型预测高于官方结算区间中点，负值表示低于区间中点。图中使用的是区间中点代理，不是精确官方最终数量。
4. 开放尾部官方区间没有可靠中点，图上会留空；不要把留空当作预测正确或错误。

5. 在下方“整体准确性与纸面表现”区域查看“全局结算预测残差”。每个点代表一个已结算市场，按最终回放时点排序；横轴显示多个最终回放日期刻度，纵轴显示残差刻度。实线是最终预测相对本地最终观测的残差，虚线是相对官方区间中点的代理残差。
6. 将鼠标悬停在全局图的绘图区，可查看最近市场的完整 slug、最终回放时点、本地最终观测残差和官方区间中点代理残差。无可靠官方中点的市场会明确显示“无可用中点”。
6. 全局图用于检查市场级误差结构，不是新的盈利指标。实线结果仍受本地发推计数可能遗漏删除帖文的影响，虚线结果受官方区间中点代理限制。
## 2026-08-30 动态市场发现

启动脚本未显式传入 `-Slug` 时，会调用只读 Gamma `discover-live`，按当前 UTC 时间发现真实的 Elon Musk 发帖数市场；不会再根据周二/周五日期拼接 slug。Gamma 没有当前市场时脚本会明确报错并不启动轮询。显式 `-Slug` / `-ExtraSlug` 仍可用于指定历史市场复盘。

也可以在 Windows cmd 中直接启动自动发现模式（省略 `--slug`）：

```cmd
cd /d "C:\Users\宋思远\Desktop\K\AIPRP\co爬虫测试" && python live_dashboard_server.py --dynamic-discovery --db "data\extended.sqlite" --model "data\extended_model_v4.json" --proxy "http://127.0.0.1:7897" --port 8765 --refresh-seconds 300 --timeout 20
```

服务端会按 Gamma Elon Tweets 系列和 `datetime` UTC 窗口自动选择当前重叠市场；不要把已结束的旧 slug 作为 `--slug` 传入，否则页面会显示“显式市场”。

服务运行期间遇到市场 slug 404，会再次按当前时间查询 Gamma 并尝试切换到真实 slug；仍找不到时保留失败快照并输出“不交易/无法读取当前盘口”。旧订单簿不会被当作当前盘口使用。
## 长期注意事项：格式与契约变更适配

本项目的数据库字段、Python `argparse.Namespace`、内部 DTO、API JSON 和前端字段都属于跨模块接口。以后新增、删除或重命名字段/参数时，必须同步检查并更新所有调用方，不能只修改被调用函数。

每次格式变更至少执行以下检查：

- 搜索全部构造位置和读取位置，包括后台线程、历史抓取、回测、CLI 子命令、API DTO 和前端请求。
- 检查默认值、旧数据兼容和可选字段；新增参数必须有明确默认值或显式校验。
- 为完整任务成功后的报告、汇总等收尾阶段增加回归测试，避免出现“数据抓取全部成功，但收尾阶段因 `AttributeError` 被判为失败”。
- 运行全套单元测试、Python 编译、前端 JavaScript 语法检查和 `git diff --check`。
- 重启真实服务后检查 `/api/health`、任务状态以及成功/失败计数，区分抓取阶段错误与报告生成阶段错误。

特别注意：`HistorySyncManager._report_args()`、回测 `hourly_test_command()` 和前端固定报告 DTO 必须保持字段同步；修改回测参数时，应同时更新历史抓取报告生成路径及其测试。

## 长期维护标准：权限与阶段性汇报

- 在申请任何额外权限、外部访问或高风险操作前，必须先用中文说明准备做什么、原因、影响范围和是否会修改数据。
- 完成一个阶段性任务后，必须立即说明实际做了什么、验证结果、仍未完成的部分以及下一步准备做什么。
- 涉及服务重启、数据库写入、报告覆盖、外部接口访问或进程终止时，必须单独列出该动作并说明是否已执行。
- 不得把计划中的动作写成已完成事实，也不得把代码通过测试写成外部接口已实测成功。

## 统一历史工作区（2026-09-19）

- 纸面修复版读取旧盘口时从原始毫秒时间恢复来源时间，不回写旧库；只有检查点前已归档、来源不超过五分钟且身份/报价有效的盘口参与模拟。市场现金与 25% 调仓限额由所有 outcome 共用，首小时开仓条件不变。首小时覆盖表示已计算范围内至少一个 outcome 有有效盘口的检查点数，不保证触发交易或全部 outcome 完整。
- 三个图表支持悬停/触摸查看时间、数据点与残差；结果残差需揭晓，官方区间中点只是代理。悬停不移动时间轴或执行交易。后端修复需在用户安排重载后生效，本次未重启生产服务。

- 顶部选择日期范围、完整市场 slug 后读取数据，共用五分钟时间轴；预测与行情、纸面交易、评分与诊断三个标签共享同一检查点。记录上限位于高级设置，截断时显示实际返回范围。
- `/api/backtest-review` 是历史预测和纸面交易的统一来源。日期按检查点过滤（开始包含、结束不含），日期之前的检查点仍从市场起点顺序计算仓位；首条返回记录的 `paper_prior_actions` 保存此前成交与未成交尝试。滑动时间轴不重新模拟。
- 当前执行为旧版“汇总深度模拟”，未接入 v2 逐档执行。逐 outcome 显示方向、成交份数、持仓、累计已实现与未实现盈亏；缺少安全盘口、没有触发、尝试未成交、部分成交和缺失估值分别显示。
- 结算、评分、校准仍需揭晓；当时可知的动作与估值无需揭晓。运行身份、模型哈希、基线比较和汇总移入诊断页。时间衰减诊断按需调用兼容的 `/api/history-review`。
- 固定公开/私有 CSV 入口保留；CSV 不自动请求另一份历史响应，缺少动作字段时显示“此数据源不含纸面交易记录”。
- 固定 CSV 分块读取并显示进度，私有文件只保留结算所需字段，避免大型盘口字段耗尽浏览器内存。载入成功不会自动揭晓；坏 CSV 会报错并恢复按钮。前端更新后刷新页面即可使用新的读取逻辑，无需重生成 CSV。
- 页面测试：`node test_history_workspace.cjs` 使用本机 Chrome、临时浏览器配置及合成 HTTP 响应，测试结束停止测试浏览器和临时 HTTP 服务；截图写入系统临时目录。无需生产服务，不请求外部接口。
- 读取固定 CSV 时立即清空之前的检查点和纸面结果；失败后保持空状态，重新读取即可恢复。较早的 CSV 失败响应不会覆盖后续市场读取结果；无效或重复检查点响应会被拒绝，未加载的时间点不可拖选。
- 本次未启动或重启生产服务，未写生产 SQLite、CSV 或模型，未采集、重训或生产回放；`do_not_auto_trade=true` 不变。
