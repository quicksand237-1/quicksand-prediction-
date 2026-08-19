# 本地运行手册

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

实时网页不需要上传 CSV。启动本地服务并打开网页后，点击“立即抓取更新实时数据”，服务会立刻更新 XTracker 当前计数、Gamma 市场规则、CLOB 盘口、TSLA 行情和 RSS 新闻，并把市场计数/盘口快照写入 `data\extended.sqlite`。正常实时监控仍按下拉框中的 10/30/60 秒间隔轮询。

历史 CSV 上传区只用于猜谜和回测复盘，和实时研究区相互独立。新闻事件语料的来源审核仍需单独运行 `news_event_crawler.py`，一键实时刷新不会把未经审核的新闻加入预测模型。

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

从本地服务打开页面后，公开预测会自动从默认路径读取，不再需要上传或选择 CSV。需要重新读取时点击“刷新固定公开复盘”；需要复盘答案时点击“载入固定私有结算与汇总”，再点击页面原有的“揭晓结算”。默认路径为：

- `data\hourly_extended_v4_fullprob_public.csv`
- `data\hourly_extended_v4_fullprob.csv`
- `data\hourly_extended_v4_fullprob_summary.csv`

其中公开预测不含答案；私有结算明细含逐小时真实结算字段；市场汇总含每个市场的复盘统计。私有文件被载入后，仍必须点击“揭晓结算”才会显示答案。

页面“历史市场自动抓取”面板的结束日期是排他边界。例如选择 `2026-02-10` 到 `2026-08-10`，会依次补充该范围内的周二/周五市场。默认只抓市场元数据和 XTracker 发推计数；需要历史赔率时才勾选“同时抓取”，并选择间隔。“抓取后生成三份固定 CSV”默认勾选，会先备份旧报告到 `data\report_backups\`，再更新三份固定报告并自动载入公开预测。任务会显示当前 slug、完成数和逐盘失败摘要。

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

- `GET /api/health`：服务健康状态。
- `GET /api/live`：当前市场、计数、模型概率、盘口、事件、新闻和建议。
- `GET /api/live-markets`：所有已选择市场的实时快照，供前端市场选择器使用。
- `GET /api/paper-history?limit=100`：历史纸面预测。
- `GET /api/paper-trades?limit=100`：区间级纸面记录及结算结果。

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

5. 在下方“整体准确性与纸面表现”区域查看“全局结算预测残差”。每个点代表一个已结算市场，按结算时间排序；实线是最终预测相对本地最终观测的残差，虚线是相对官方区间中点的代理残差。
6. 全局图用于检查市场级误差结构，不是新的盈利指标。实线结果仍受本地发推计数可能遗漏删除帖文的影响，虚线结果受官方区间中点代理限制。
