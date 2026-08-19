# 中文维护日志

## M-20260816-FIXED-LOCAL-REPLAY-REPORTS

- 操作目的：取消每次历史复盘都手工选择三个本地 CSV 的重复操作，同时保留猜谜模式的答案隔离。
- 代码变更：实时服务新增固定复盘文件路由；前端新增“加载固定公开复盘”和“加载固定私有结算与汇总”按钮。
- 默认路径：公开预测 `data/hourly_extended_v4_fullprob_public.csv`；私有结算 `data/hourly_extended_v4_fullprob.csv`；汇总 `data/hourly_extended_v4_fullprob_summary.csv`。均可通过服务启动参数覆盖。
- 猜谜边界：公开路由会拒绝包含 `observed_local_final_count`、`resolved_bin`、Brier、LogLoss 等答案字段的 CSV；私有结算和汇总只能由用户显式点击后以本机 POST 读取，仍需点击原有“揭晓结算”才在界面显示答案。
- 风险：本机用户仍能读取自己的私有 CSV，因此这不是多用户访问控制；它的作用是防止常规复盘流程自动泄露答案，不改变自动交易永久关闭状态。
- HTTP 验证：临时本机服务的公开、私有、汇总复盘接口分别返回 200；测试仅核对响应状态和文件大小，未输出任何结算内容。

## M-20260816-ACTIVE-DUAL-MARKET-LAUNCH

- 问题：旧启动默认值固定为已结算的 `elon-musk-of-tweets-august-7-august-14`，导致窗口外、旧 token CLOB 404 和前端只显示单盘。
- 代码变更：`start_live_dashboard.ps1` 在未传 `-Slug` 时按纽约时间选择最近两个已经开盘的周二/周五窗口；显式传入 `-Slug` 和 `-ExtraSlug` 的旧用法保持不变。
- Gamma 核查：只读确认 `elon-musk-of-tweets-august-11-august-18` 与 `elon-musk-of-tweets-august-14-august-21` 均存在且 `closed=false`。
- 验证：PowerShell 语法检查通过；既有 Python 实时服务测试 19/19 通过。
- 风险：市场 token 仍可能因 Polymarket 端更新、流动性不足或 CLOB 暂时错误而缺少盘口；此时单盘会显示风险原因并保持不交易，不会混用另一盘数据。

## M-20260816-MANUAL-LIVE-REFRESH

- 操作目的：减少用户依赖历史 CSV 上传的频率，增加同一实时前端中的“一键立即抓取更新”。
- 代码变更：新增本机受限 `POST /api/refresh-now`；前端新增“立即抓取更新实时数据”按钮。该接口强制刷新所有已选实时市场，绕过正常轮询缓存。
- 数据范围：刷新 Gamma 市场元数据、XTracker 帖子和计数样本、CLOB 盘口快照、TSLA 行情和 RSS 新闻上下文；市场计数与订单簿会写入既有本地 SQLite。历史猜谜 CSV 不会被删除或覆盖。
- 安全边界：接口仅允许回环客户端访问，`do_not_auto_trade=true` 不变；按钮不会提交 AI Key、不会调用 AI、不会执行交易。
- 限制：RSS 新闻上下文会立即更新，但“合规 AI 语料”仍必须经过 `news_event_crawler.py` 的来源、发布时间和可用时间审核，不能把聚合发现源自动升级为模型证据。
- HTTP 验证：临时本机服务在 `8776` 端口调用 `POST /api/refresh-now` 返回 200、1 个市场快照和 `do_not_auto_trade=true`；未配置或发送任何 AI Key。

## M-20260816-XCODE-COMPATIBILITY-CHECK

- 操作目的：核对用户指定的 `https://xcode.best/keys` 是否可作为本项目的 OpenAI-compatible Provider。
- 只读核查：公开前端脚本包含 QuantumNous `new-api` 标识，以及 `/v1/models`、`/v1/chat/completions`、`/v1/responses` 标准路径。无密钥访问 `https://xcode.best/v1/models` 返回 HTTP 401，确认 API 根地址为 `https://xcode.best/v1`，而非猜测地址。
- 配置建议：Provider 选择 `OpenAI-compatible`，API 地址填写 `https://xcode.best/v1`，优先使用 `Chat Completions`；模型名必须以用户本人 Xcode 控制台中可用模型列表为准。
- 安全边界：未登录、未提交、未读取任何 Key，未调用模型或产生费用。第三方网关仍可能转发/处理输入语料，使用前须由用户确认其条款、隐私和费用规则。

## M-20260812-AI-ASSIST-V1

- 操作目的：在 `AItest` 分支增加可脱离 Codex 独立运行的本地 AI 辅助分析入口。
- 文件变更：新增 `ai_provider.py`；实时服务增加 `/api/ai/status`、`/api/ai/config`、`/api/ai/clear`、`/api/ai/analyze`；前端增加 Provider、API 地址、模型、API 模式、密钥保存/清除和手动分析控件。
- 密钥边界：服务默认绑定 `127.0.0.1`。Windows 优先使用 Credential Manager；不可用时才将密钥写入被 `.gitignore` 忽略的本地 `.env`。密钥不进入 SQLite、CSV、前端响应、日志或 Git。页面保存成功后立即清空密码框，只显示末四位。
- AI 边界：仅发送本地已审核、`published_at_utc` 和 `available_at_utc` 不晚于当前时点的文档；AI 只输出主题、方向、时间范围、证据引用和受限置信度。概率、推荐、仓位和自动交易字段由本地程序固定为不变/关闭，AI 结果不会写入 `source_events`。
- 成本边界：只有用户点击“分析当前语料”才发起外部 API 请求，服务端有 30 秒冷却和文档/输出长度上限。API 费用、额度和数据保留条款由用户选择的 Provider 账户承担；默认不会在后台刷新中调用 AI。
- 验证：新增 `test_ai_provider.py`；全套 29 项单元测试通过，Python 编译通过。尚未使用真实 API Key 调用，避免在测试中产生费用或泄露密钥。
- 风险：当前数据库中合规可导出的新闻语料仍为 0，AI 页面会明确提示先运行新闻抓取并配置合规来源；Google News 发现条目不能直接作为 AI 事实依据。
- HTTP 烟测：临时启动本地 `8775` 端口后，`/api/health` 与 `/api/ai/status` 均返回 200；未配置密钥、密钥末四位为空、`do_not_auto_trade=true`。未调用真实 AI，未产生 Provider 费用。
- 安全补丁：前端 AI 请求拒绝非回环 API 地址，服务端 AI POST 接口同时校验回环客户端和本机 Origin；即使用户修改实时数据地址，API Key 也不会随请求发送到远程服务。

## M-20260812-LOCAL-EVENT-CORPUS-V1

- 操作目的：在 `AItest` 分支建立可审计的本地事件语料库，为后续可选 AI 结构化提供输入，避免新闻直接绕过来源审核进入模型。
- 文件变更：新增 `news_event_crawler.py`、`news_sources.example.json`、`test_news_event_crawler.py`；SQLite 增加 `source_documents`、`document_versions`、`crawl_runs`、`crawl_errors`、`event_candidates` 五张表。
- 数据边界：采集 RSS/Atom 元数据与短摘要；官方/监管和配置允许的二级来源可作为候选语料。发现源仅用于发现线索，`discovery_only` 文档永远不导出给 AI、不能作为事实证据或模型特征。
- 时间防泄漏：每条文档同时保存 `published_at_utc`、`retrieved_at_utc`、`available_at_utc`；语料导出只允许发布时间和可用时间均不晚于 `--as-of` 的文档。
- 审核边界：关键词候选的状态固定为 `pending_review`，不会写入 `source_events`，不会改变 V4 模型、预测概率、纸面建议或自动交易状态。
- 风险：RSS 可能缺少发布时间、变更链接或受来源条款限制；缺失发布时间的文档被保留为拒绝审计记录且不进入语料。来源配置是白名单样例，不代表对任何站点全文抓取或再利用已获授权。
- 首次真实抓取：使用已移除的本地令牌读取 90 条订阅条目，去重后有 85 条 `source_documents`、80 条待审核候选、0 条已批准 `source_events`。SEC Tesla Atom 返回 HTTP 403，已写入 `crawl_errors`；三个 Google News 源按 `discovery_only` 留在审计层，因此当前可导出语料为 0。这是预期的保守降级，不得用聚合源填补正式语料。
- 后续处理：增加显式 `--user-agent`，供用户在符合官方源公平访问规则时自行提供公开联系标识；不伪造身份、不绕过 403、不保存 API Key。需要下一阶段加入经条款确认的原始官方 RSS/Atom 或白名单媒体订阅源，才会产生可供 AI 审核的正式语料。

本文件记录本项目的代码、数据库迁移、验证命令和已知限制。数据库、模型和 CSV 位于 `data/`，由本地运行时生成，不纳入 Git，也不在维护操作中删除。

## M-20260811-PAPER-TRADES

- 操作目的：把实时纸面建议从方向代理扩展为可审计的区间级纸面成交记录。
- 代码变更：新增幂等 `paper_trades` 表、纸面记录写入方法和已结算市场的本地结算方法。
- 安全边界：只写 SQLite 纸面记录；没有交易 API、私钥、钱包或下单路径。`do_not_auto_trade` 永久保持为真。
- 成本口径：卖一作为 Yes 买入的可执行价格；手续费率和滑点为本地可配置的保守假设，必须在结果中显示，不能解释为 Polymarket 官方费率。
- 验证：`python -m unittest -v .\test_polymarket_pipeline.py` 15/15 通过；`python -m py_compile .\polymarket_pipeline.py .\live_dashboard_server.py .\musk_tracker.py .\test_polymarket_pipeline.py` 通过；`/api/health`、`/api/paper-trades`、`/api/live` 烟测返回 200。
- 风险：历史盘口样本不完整，RSS 新闻当前只作为上下文；纸面收益不代表可实现收益；`/api/live` 仍可能因外部源失败而只显示 `不交易`。

## 审计规则

- 修改前先检查 Git 状态和现有文件。
- 不删除、不回退、不覆盖用户数据库、模型、CSV 或未提交改动。
- 新模型必须与 V4 基线做严格样本外比较；本轮不替换 V4，不宣称盈利。
- API、新闻、盘口或 XTracker 失败时，系统记录错误并降级为“不交易”。

## M-20260816-HISTORY-MARKET-SYNC

- 操作目的：让历史市场、发推计数和可选历史赔率可由本地网页显式启动，不再要求用户每次手工拼接长 `crawl` 命令；同时固定公开预测、私有结算明细和市场汇总 CSV 的默认本地路径。
- 文件变更：`live_dashboard_server.py` 新增 `HistorySyncManager`、`POST /api/history-sync` 和 `GET /api/history-sync/status`；`forecast_dashboard.html` 新增历史抓取日期、可选赔率和进度面板；`test_polymarket_pipeline.py` 新增任务参数、顺序执行和日期校验测试。
- 数据行为：任务按周二/周五市场顺序调用既有 `crawl_one`，写入既有 `data\extended.sqlite`。默认只抓 Gamma 元数据和 XTracker 发推计数；用户明确勾选后才抓 CLOB 历史赔率。历史订单簿快照不会抓取，因为当前订单簿不能还原过去的可执行盘口。
- 并发与安全：任务必须从 `127.0.0.1` 网页显式发起；使用与实时刷新共享的 SQLite 锁，避免不同连接同时写库。不会自动训练、不会自动生成或覆盖 CSV、不会启动 AI、不会提交交易，`do_not_auto_trade=true` 不变。
- 验证：`python -m unittest -v .\test_polymarket_pipeline.py .\test_news_event_crawler.py .\test_ai_provider.py` 共 35 项通过；Python 编译、前端内联 JavaScript 解析和 `git diff --check` 通过。历史任务测试使用无网络假爬虫确认默认 `skip_prices=true`、`orderbooks=false`、两盘顺序完成和错误日期被拒绝。本地服务烟测确认 `/api/health` 和 `/api/history-sync/status` 返回成功、任务初态为 `idle`、`do_not_auto_trade=true`。
- 历史回放：使用现有 `extended_model_v4.json` 在 2026-02-10 至 2026-08-09 运行 `hourly-test`，50 个市场全部完成、生成 8,348 条小时预测，输出为新增且不覆盖原文件的 `data\hourly_history_sync_verify_20260816*.csv`。
- 风险和限制：XTracker 对历史删除帖文仍可能不完整；历史赔率采集耗时并受 CLOB 限流、可用范围影响；单盘源错误会保留在任务结果中，不能据此宣称历史已完整或模型精度提高。

## M-20260816-FIXED-REPORT-AUTOMATION-AND-NEWS-SYNC

- 操作目的：将前端历史复盘从“选择上传三个 CSV”改为“服务端固定路径生成并自动加载”，并为 AI 提供不绕过来源审核的本地新闻抓取入口。
- 三份固定文件含义：公开预测 `hourly_extended_v4_fullprob_public.csv` 不含结算答案；私有结算明细 `hourly_extended_v4_fullprob.csv` 包含逐小时真实结算字段，只在用户点击“揭晓结算”后显示；市场汇总 `hourly_extended_v4_fullprob_summary.csv` 是逐市场 MAE、Brier、LogLoss 等复盘汇总。
- 代码变更：历史抓取任务新增“抓取后生成三份固定 CSV”；生成前将旧 CSV 复制到 `data\report_backups\`，再更新固定路径。前端不再显示文件上传控件，启动时自动读取公开报告；私有与汇总仍须显式载入。新增 `POST/GET /api/news-sync` 和仅内存保存的公开联系邮箱请求头。
- 新闻边界：新闻任务只读取固定的 `news_sources.example.json` 白名单；Google News 发现源仍不可进入 AI 语料。联系邮箱仅拼入本次 HTTP User-Agent 发送给来源服务器，随后清空，不保存到 SQLite、日志、CSV、Git 或 AI 配置。没有合规语料时 AI 继续拒绝分析，而不是降级使用聚合转载。
- 可观测性：新闻任务状态会显示最多三条来源、错误类型和截断错误摘要；完整审计仍保存在 SQLite 的 `crawl_runs` 与 `crawl_errors`，便于区分“没有新闻”与“代理/订阅源失败”。
- 验证：新增新闻联系邮箱注入拒绝测试和固定报告备份测试；共 37 项单元测试通过。Python 编译、前端 JavaScript 解析、`git diff --check` 与本地 HTTP 烟测通过；烟测确认固定报告、历史抓取、新闻抓取状态接口返回成功，自动交易禁用状态不变。
- 已知问题：本机代理下只读检查 Tesla IR 与 SEC 公开订阅地址时发生 TLS 连接关闭，未写入数据库、未调用 AI。用户实际环境中可在页面输入 Proton 联系邮箱后点击“抓取合规新闻语料”；若仍失败，页面和 `crawl_errors` 会保留来源错误，需再核查代理、来源订阅地址和 User-Agent。

## M-20260816-XCODE-PROVIDER-VERIFY

- 操作目的：核实用户提供的 Xcode OpenAI-compatible 配置是否能用于本项目的本地 AI 信息结构化层。
- 只读验证：`GET https://xcode.best/v1/models` 成功返回 13 个模型 ID；`Gemini` 不是有效精确 ID。用 `gemini-2.5-flash` 对 Chat Completions 进行一次极短连通性验证，接口正常返回。
- 本地配置：通过项目本机 API 保存 Provider=`compatible`、API 地址=`https://xcode.best/v1`、模型=`gemini-2.5-flash`、模式=`chat_completions`。状态回读确认配置存在，存储位置为 Windows Credential Manager；未把 Key 写入 SQLite、CSV、Git、前端响应或维护日志。
- 成本与边界：本次只读模型列表不调用推理；连通性测试只发送“返回 ok”，最大 8 token，不发送新闻、市场正文或个人数据。AI 仍被本地合规语料门控拦截，不能修改模型概率、建议或交易状态。
- 风险：用户曾在对话中暴露完整 Key，应在 Xcode 控制台尽快轮换。轮换后需在页面重新保存新 Key；当前正确模型配置并不能替代新闻来源审核。

## M-20260816-OFFICIAL-TITLE-ONLY-CORPUS

- 问题核查：新闻任务成功写入 300 条文档且无抓取错误，但 40 条 SEC Tesla 官方文件因 Atom 条目无摘要而 `corpus_eligible=0`；其余均为 Google News 发现源，按设计不可进入 AI。
- 代码变更：仅当来源配置明确设定 `allow_title_only=true` 且 `tier=1` 时，空摘要的官方 Atom 条目会获得“仅标题，未抓全文”的可审计说明。`news_sources.example.json` 仅为 SEC Tesla 官方来源开启该选项。
- 安全边界：非官方来源、B/C/D 级来源、未显式允许的官方来源仍不能因标题而升级；没有抓取全文，不得把标题视为完整事实证据。AI 仍只做信息结构化，不能改变预测概率、纸面建议或交易状态。
- 验证：新增官方标题允许与非官方拒绝两项解析测试。需重启新版服务后重新执行一次“抓取合规新闻语料”，已有 SEC 文档会以新摘要规则幂等更新并进入合规语料候选。

## M-20260811-MULTI-MARKET-LIVE

- 操作目的：支持同时跟踪周二盘和周五盘，解决单个 `--slug` 的实时范围限制。
- 代码变更：新增可重复的 `--extra-slug`、多市场 `/api/live-markets` 和前端“实时市场”选择器。
- 数据隔离：每个市场独立刷新 Gamma、XTracker、CLOB、预测和 `paper_trades`；不混合两个市场的计数、概率或盘口。
- 稳定性：多盘统一由一个后台线程顺序刷新，避免多个 SQLite 写入连接竞争锁。
- 兼容性：不传 `--extra-slug` 时，原有单盘命令和 `/api/live` 行为保持不变。
- 风险：第二个 slug 必须是实际存在的市场；市场结束、盘口缺失或数据过期时，该盘会单独显示“不交易”。

## M-20260811-LIVE-API-DIAGNOSTIC

- 问题：前端已更新为请求 `/api/live-markets`，而未重启的旧服务只会返回纯文本 `Not found`；浏览器因此显示难懂的 JSON 解析错误。
- 修复：前端先读取响应文本再解析 JSON；遇到 HTTP 404 时明确提示“本地服务版本过旧，请停止并重启”。
- 边界：旧服务只能单盘运行；双盘市场选择器必须搭配本次更新后的 `live_dashboard_server.py`。

## M-20260812-AITEST-BRANCH

- 分支核查：`main` 与 `paper/live-orderbook-forecast` 指向同一提交；`model` 是其祖先，现有模型实现已包含在 `main`，没有待合并的独立提交。
- 分支操作：从当前 `main` 创建 `AItest`，并保留全部未提交工作区修改。
- 当前状态：尚未接入外部 AI、尚未读取或创建 API key、尚未改变本地模型、纸面交易或自动交易边界。
- 待讨论：先确定 AI Provider、隐私边界、预算上限和样本外验收标准，再实施服务端结构化事件层。

## M-20260811-LIVE-PORT-ALIGNMENT

- 问题：实时地址输入框原先固定为 8765；当用户为了避开旧进程改用新端口时，前端仍可能连接旧服务。
- 修复：页面由本地 HTTP 服务打开时，自动把实时地址设为当前页面的同源 `/api/live`。
- 排查：可用 `python -B` 跳过 Python 字节码缓存，并临时使用 8766 等空闲端口验证新版服务。

## M-20260811-START-SCRIPTS

- 操作目的：减少手动输入长命令造成的启动失败和模式混淆。
- 文件变更：新增 `start_live_dashboard.ps1` 和 `run_hourly_replay.ps1`。
- 使用方式：前者启动本地实时研究台；后者生成历史猜谜/复盘 CSV。
- 文档同步：`RUNBOOK-CH.md` 增加最小使用流程，明确“历史模式要上传 CSV，实时模式不需要上传文件”。
- 风险：脚本只是调用本地 Python CLI，不改变 `do_not_auto_trade`，也不新增任何真实交易能力。

## M-20260811-FULL-PROBABILITY-REPORT

- 问题：旧历史 CSV 只导出最高预测区间概率，前端无法展示完整的区间概率分布。
- 修复：新增 `bin_probabilities_json` 公共字段，导出每个结算区间的模型概率；前端增加完整区间概率表。
- 盘口解释：历史回放中 `price_probability_mass=0` 表示该预测时点没有本地历史盘口快照，不等于实时 CLOB 接口读取失败。
- 兼容性：保留旧 CSV 字段和猜谜/私有揭晓机制；旧 CSV 仍可加载，但完整概率表会提示重新生成 fullprob 报告。
- 安全：完整模型概率不包含真实结算答案，不会破坏猜谜模式。

## M-20260811-LIVE-VERSION-AND-CHART

- 诊断：8765 端口上的旧服务 API 没有 `model_probability` 字段，证明实时服务进程未随代码更新重启。
- 修复：新增 `service_version` 和 `/api/health` 版本输出；实时页面显示服务版本，便于识别旧进程。
- 前端：新增历史市场折线图，展示模型预测最终数、当时已观察累计数；真实最终观测线仅在私有数据揭晓后显示。
- 数值口径：模型预测是计数分布的连续期望值，因此可能为小数；页面同时显示四舍五入后的约整数值，结算区间概率仍由完整分布计算。
- 实时性口径：当前采用 REST/RSS 轮询，默认约每 30 秒刷新一次，加上外部 API 响应延迟，不是 WebSocket 毫秒级实时行情。
- 验证：新服务实例 `/api/health` 和 `/api/live` 返回 200；`/api/live` 的 23 个区间均包含模型概率字段；16/16 单元测试通过。

## M-20260818-MODEL-V6-REPLAY-RESIDUAL-CHART

- 操作目的：为历史回放增加可审计的误差时间序列，便于查看模型在一个七日市场的不同预测时点相对官方结算区间的偏高或偏低，而不把官方区间伪装为精确计数。
- 分支核查：`main`、`AItest` 与 `paper/live-orderbook-forecast` 指向同一提交，`model` 是其祖先；没有可合并的新提交。基于现有未提交工作区创建并继续使用 `model-v6`，未清理或回退任何文件。
- 文件变更：`forecast_dashboard.html` 新增“回溯预测与官方结算的残差”Canvas 曲线。仅在固定私有结算文件已经显式载入并点击“揭晓结算”后显示；猜谜模式继续隐藏官方区间和残差。
- 数值口径：每个小时点的代理残差 = `model_forecast_final_count - (resolved_lower + resolved_upper) / 2`。正值表示相对官方区间中点高估，负值表示低估；开放尾部或缺少上下界的官方区间没有可靠中点，因此该点留空。此指标不等同于相对精确官方最终计数的误差，也不能替代 MAE、Brier 或 LogLoss。
- 重叠窗口规则：实时抓取、展示和纸面研究必须保留两个同时未结算的市场，并按 slug 隔离数据；训练和样本外评估则对训练侧执行 purge，避免同一小时的发推行为与外部信息同时进入训练样本和测试标签而造成泄漏。purge 只影响训练/评估样本选择，不删除市场、推文、赔率或实时数据。
- 数据范围与指标：未重训模型、未改变 V4/V5 特征、概率、事件影响、纸面交易、自动交易开关或历史 CSV。没有新的 MAE、RMSE、Brier、LogLoss、命中率或收益指标；不能据此宣称预测能力或盈利能力改善。
- 风险与待办：官方结算通常是区间而非精确数量，区间中点代理会引入表示误差；历史本地发推计数也可能遗漏删除帖子。后续模型比较应报告 purged walk-forward 的样本数和被排除窗口，而不是删除重叠市场。

## M-20260818-GLOBAL-SETTLEMENT-RESIDUAL-CHART

- 操作目的：在保留单市场逐小时残差图的基础上，增加全部历史已结算市场的市场级残差总览，观察最终预测误差是否随结算时间、市场批次或样本顺序发生变化。
- 文件变更：`forecast_dashboard.html` 在整体复盘面板新增“全局结算预测残差”图。每个市场取最后一个回放时点，按结算时间排序；实线为最终预测减去本地最终观测，虚线为最终预测减去官方结算区间中点代理。
- 答案隔离：全局图只有在私有结算文件显式载入并点击“揭晓结算”后绘制；公开 CSV、竞猜模式和实时模式不会读取或显示市场级实际残差。
- 数据口径：市场必须同时拥有官方结算区间、最终模型预测和本地最终观测才进入图表；官方开放尾部区间没有可靠中点，虚线对应点留空。实线的本地最终观测仍可能受历史删除帖文影响，不能自动等同于官方精确结算数。
- 指标变化：无。本轮没有重训、校准或修改正式概率模型；MAE、Brier、LogLoss、命中率和纸面收益不因新增图表而改变，也不能据此声称模型盈利。
