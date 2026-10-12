# 持续开发上下文

# 当前交接快照：历史价格保留期路由与请求上限（2026-10-12）

- `history_prices.py` 已加入粒度路由：默认五分钟目标在最近 60 天用 300 秒，60 至 90 天用 1800 秒，超过 90 天用 10800 秒长期档；失败或稀疏的近期五分钟窗口优先尝试 30 分钟，再尝试更粗精度。每条数据保留真实 `resolution_seconds`，粗粒度不能计入五分钟覆盖或安全特征。
- 请求按每次约 450 行安全上限划窗；API 实际返回达到 450 条时会留下 `split_required` 审计并继续二分窗口，持续遵守 15 天绝对窗口和排他 `end`。请求审计区分目标精度、请求/实际精度、保留策略、行数和拆分原因。
- 成功的保留期路由窗口会使用现有短期抓取回执避免每轮重复下载；回执只表示该窗口按当前精度已检查。`target_complete` 独立按源精度判定，所以 30 分钟/3 小时数据仍不算五分钟完整覆盖。
- 回退若返回没有 `resolution_seconds` 的记录，只在首次有记录的档位停止继续扩大粒度，并保持精度未知；不会依据请求参数伪造源精度。
- 定向验证：`python -m unittest test_history_price_window`、Python 编译与 `git diff --check` 通过。测试只使用模拟接口和临时数据。
- 未启动服务或抓取任务，未访问外部接口，未写生产 SQLite/CSV/模型，未重训或运行回放；`model-update.txt` 未更新，`do_not_auto_trade=true`。后端修改需按 RUNBOOK 提示重启本地服务/抓取任务后生效。
- 尚未做真实 API 覆盖核对或生产补采；需另行明确执行外联和生产数据写入阶段。

# 当前交接快照：研究采样写入隔离（2026-10-10）

- 用户已委托继续开发：保留全部既有修改和未跟踪文件，不切分支，不使用 reset/checkout/restore/clean；`do_not_auto_trade=true` 永久保持。
- 本轮修复 `live_dashboard_server.py`：30 秒研究采样不再写正式事件、帖子、盘口或市场活动表。Gamma 窗口、outcome token 和盘口在内存解析，唯一持久化为独立 `research_market_snapshots`；该链路不进入五分钟预测、评分、edge、纸面交易或历史特征。
- 前端已有功能 slug 锚点：`#live-capture`、`#history-acquisition`、`#prediction-review`、`#paper-research`、`#diagnostics`、`#backtest`、`#data-organization`、`#local-reports`。30 秒研究状态通过现有研究接口展示。
- 验证：`python -m py_compile live_dashboard_server.py polymarket_pipeline.py`、临时 SQLite 隔离验证与 `git diff --check` 已通过；已在隔离端口 `18768` 实际复核研究采样。生产服务旧实例未终止，既有 dirty 工作区和未跟踪文件全部保留。
- 当前运行中的本轮服务为 `http://127.0.0.1:18768/`；主控制台 8760/8765 不接管。后续可继续审计锁竞争、合并/弃置入口及数据完整性；不得把研究快照混入五分钟策略。

## 运行复核补充（2026-10-10）

- 已在隔离端口 `18768` 启动新版服务并实际访问 Gamma/CLOB/XTracker/RSS；动态发现两个活动市场。研究接口最新快照成功落入独立表，目标间隔 30 秒、市场开盘对齐，37 条帖子返回、窗口内计数 29、12 条新闻、25 个 outcome 盘口。
- 首次并发时出现 `database is locked`，已加研究表写入最多 4 次短重试并重启本轮实例复核；其他旧服务没有终止。CLOB 对一个已结算 outcome 返回 404，已保留为 `closed_outcome_expected`，不是限流或历史回填。
- 本轮确实写入生产 SQLite 的 `research_market_snapshots` 独立表；未写正式五分钟表、CSV 或模型，未重训/回放；`do_not_auto_trade=true`。隔离服务当前地址 `http://127.0.0.1:18768/`，后端代码更新后若切回主控制台需重启。
- `RUNBOOK-CH.md` 已补充功能合并/弃置审计：实时盘口与 30 秒研究采样、历史抓取与回测、浏览器缓存与本地文件整理继续分开；顶部 slug 导航作为后续功能固定入口。
- SQLite 锁来源已通过命令行审计确认是多个旧控制台/后端实例共同写 `data/extended.sqlite`；仅本轮 18768 实例被重启，其他 PID 未终止。共享连接已设置 30 秒 busy timeout，研究保存保留 4 次短重试；最新健康检查仍有 2 个活动市场，研究状态 `partial` 且只有已结算 outcome 的 404 证据。

## 启动器打开按钮修复（2026-10-10）

- 8760 启动器与 8765 研究台实际健康；旧“打开研究台”按钮调用服务端 `webbrowser.open()`，导致用户浏览器窗口未打开。`dashboard_launcher.html` 已改为浏览器端直接打开健康检查后的 `dashboard_url`，弹窗拦截时回退当前窗口。
- 这是纯静态前端修复：启动器服务无需重启，但已打开的控制台必须 `Ctrl+F5` 刷新页面后生效。当前 8760 `/api/status` 为 ready，8765 `/api/health` 为 200。

## 最新快照：前端刷新与重启提示协议（2026-10-09）

- `持续开发协议.md` 已更新为 1.1：纯静态前端变化的收尾必须明确写“只需刷新浏览器窗口”并提示 `Ctrl+F5`；涉及后端/API/启动器/脚本/端口/配置的变化必须明确写“需要重启前端/本地服务”并提供可复制的 PowerShell 启动指令；混合变化按重启处理。
- 标准完整启动命令为：`Set-Location "C:\Users\宋思远\Desktop\K\AIPRP\co爬虫测试"` 后执行 `powershell -ExecutionPolicy Bypass -File ".\start_dashboard_launcher.ps1"`。本轮未启动服务，因此没有新的运行地址或进程状态可报告。
- 本轮只修改协议、维护日志和交接文档，未访问外部接口、未写入数据库/CSV/模型、未重训或运行生产回放；`model-update.txt` 未更新；`do_not_auto_trade=true` 保持。

## 最新快照：数据整理接口错误诊断（2026-10-06）

- `data/` 只读审计：约 17.1 GB；SQLite 约 16.5 GB（其中 `extended.sqlite` 约 16.46 GB），`.bak` 约 546 MB，CSV 约 523 MB。`extended.sqlite` 可读，24 张表，`journal=delete`、`locking=normal`；未删除、压缩或改写任何数据文件。
- “数据整理失败：Unexpected token 'N'”根因是前端整理按钮请求 `window.location.origin`，落到启动控制台 8760 或旧后端时收到纯文本 `Not found`，随后直接 `response.json()`。这不是 SQLite 结构错误。
- `forecast_dashboard.html` 已修复：HTTP 页面在 8760 时默认把 API 指向研究台 8765；整理接口复用 `liveUrl`；响应先读文本并检查 JSON，错误显示 HTTP 状态、返回片段、请求地址和重启提示。
- 本阶段未启动服务、未访问外部接口、未写 SQLite/CSV/模型、未重训或运行生产回放；`model-update.txt` 未更新，`do_not_auto_trade=true` 保持。

## 最新快照：本地数据文件整理器（2026-10-05）

- 用户澄清需要整理本地迭代数据文件，而不是浏览器缓存。新增 `data_file_organizer.py`：只读扫描 `data/` 下 CSV/JSONL/JSON，计算哈希和结构，区分完全重复文件、可按完整行去重的 CSV 合并候选与受保护数据库/备份文件。
- `live_dashboard_server.py` 新增本机 `/api/data-organizer/status`、`POST /api/data-organizer/scan`、`POST /api/data-organizer/consolidate`；`forecast_dashboard.html` 新增扫描按钮和“生成合并副本（不删除原件）”按钮。副本与清单写入 `data/consolidated/`，源文件不删除、不移动、不覆盖。
- `data/extended.sqlite`（约16GB）、所有 SQLite/DB、`.bak`、压缩包、订单簿、原始帖子和模型输入都不参与合并。删除策略固定为 `never_delete_sources`；如需真正删除，必须先取得用户对现有禁止删除用户文件约束的明确解除，并逐文件审核。
- 验证：临时目录单元测试 1 项通过，Python 编译和 `git diff --check` 通过；未启动服务、未访问外部接口、未写生产 SQLite/CSV/模型、未重训或生产回放，`model-update.txt` 未修改，`do_not_auto_trade=true` 保持。

### 删除授权后的安全边界

- 用户明确授权为释放空间删除重复文件。实现仅删除同一 SHA-256 的完全重复文本副本，并排除归档目录、风险命名、数据库、备份、模型、订单簿、原始帖子和实时采集源；每组保留一个规范副本。
- 前端按钮先重新扫描，再列出候选并二次确认；后端执行时重新核验路径、文件存在性和哈希，异常只跳过并返回 skipped。接口为 `POST /api/data-organizer/delete`。
- 本轮没有对真实 `data/` 执行删除；只用临时目录验证删除 1 个重复副本、保留规范副本及释放字节统计。

### 真实目录首次安全清理结果（2026-10-05）

- 只读扫描真实 `data/` 后，删除 3 个 SHA-256 完全相同的低风险文本副本，共释放 2,877,237 字节；每组保留 `hourly_extended_events_gate*` 规范文件。
- 模型/验证命名文件、备份目录、SQLite/DB、订单簿、原始帖子、模型和实时采集文件均未删除。删除后扫描显示 109 个文件、0 个可删除重复组；`data/extended.sqlite` 仍存在。
- 服务未启动、未访问外部接口、未写数据库/模型、未重训或回放；`do_not_auto_trade=true` 保持。

## 最新快照：实时记录状态与安全缓存清理（2026-10-04）

- 后续复核确认仓库没有使用浏览器持久化缓存保存生产采集数据；清理按钮只删除 `aiprp:`/`aiprp-` 命名空间项目。没有匹配项时报告“当前没有可清理的应用缓存”，不会全量清理同源缓存，也不会触碰 SQLite、CSV/JSONL、订单簿、原始帖子或模型文件。
- 启动缺口审计不会在首个五分钟检查点尚未完成时提前登记窗口；开盘后首个检查点会在后续刷新中继续审计，避免首段缺口被永久跳过。
- 在 `live_dashboard_server.py` 中将五分钟检查点锚定到市场 `window.start_utc`，并为每个市场窗口执行一次启动缺口审计。已结束但无本地快照的检查点追加 `missing_checkpoint` 证据，证据不含盘口、价格、预测或 edge，不用当前盘口回填。
- `forecast_dashboard.html` 增加“正在正常记录当前市场信息”与“已尝试但缺失”明文状态，并显示缺口数量；新增清理浏览器历史缓存按钮，仅处理本应用 localStorage、sessionStorage、Cache API，明确不处理 SQLite、CSV/JSONL、订单簿、原始帖子或模型文件。
- `持续开发协议.md` 增加网页启动控制台和每次更新后维护文件的长期要求，以及缓存清理边界。
- Python 编译、Node 脚本语法和 `git diff --check` 已通过；未启动服务、未访问外部接口、未写生产数据库/CSV/模型、未重训或运行生产回放。`do_not_auto_trade=true` 保持。
- 当前分支 `backtest-audit-v1`，HEAD `7a1dd15342e8e0ee0ffeaa2485bb57145ca90e33`，工作区仍有大量既有 dirty 修改和未跟踪文件，均保留。下一步应在临时数据库/合成窗口测试缺口审计去重、非整点开盘对齐及前端状态渲染；生产服务需用户明确要求后再重启。

## 最新快照：采集证据与时间安全验证收尾（2026-10-04）

- 已完成本阶段实现并保留全部既有 dirty 修改/未跟踪文件；当前分支 `backtest-audit-v1`，HEAD `7a1dd15342e8e0ee0ffeaa2485bb57145ca90e33`，未切换分支、未提交。
- `history_acquisition.py`、`history_prices.py`、`polymarket_pipeline.py`、`live_dashboard_server.py`、`forecast_dashboard.html` 已区分“有目标精度样本 / 已尝试但缺失 / 抓取失败 / 尚未尝试 / 证据不足”，保存分页、精度、请求时间、错误码、trace、Retry-After 和存储失败证据；事后补采只能复盘展示。
- 官方规范已核对：`/v2/prices-history` 的 `start`/`end` 绝对窗口最多15天，`end`排他；指定 `bucket_seconds` 可能空/稀疏，省略才由服务端选择可用精度；必须按 `resolution_seconds` 和游标分页判断结果。`/v2/trades` 使用 condition/event_id 与游标分页，429/503按Retry-After处理。文档要求已记录于 `MAINTENANCE-CH.md` 和 `Q&A.txt`；本次只读文档外联，不请求市场数据。
- 小时导出安全规则已明确为“桶结束后才可用”；修正过时测试预期后，`python -m unittest discover` 共186项通过，`node test_history_price_display.cjs`、5个Python模块编译和 `git diff --check` 通过。
- 未启动/重启服务、未访问市场数据接口或补采生产历史、未写生产 SQLite/CSV/模型、未重训或运行生产回放，`model-update.txt` 未改，`do_not_auto_trade=true`。
- 真实历史缺口仍需在用户明确允许外联后做小范围 token/时间窗口核对；不能用当前盘口填历史，也不能把稀疏/粗精度价格当作五分钟完整覆盖。

## 最新实现补充：自适应抓取与动态研究（2026-10-04）

- `history_prices.py` 默认目标5分钟，增加15天窗口保护；请求/连接失败最多拆为受控12小时子窗口，429/503仍按Retry-After退避；5分钟空/稀疏时按省略精度、30分钟、60分钟顺序保留真实精度证据，粗精度不进入5分钟安全特征。
- 新增 `market_dynamics_research.py`：纯内存/JSONL 的实时快照特征、前向目标标签、可解释 ridge 映射矩阵、固定预测节点纸面收益和市场分组 walk-forward 选择；`live_dashboard_server.py` 归档快照增加 `dynamics_features`，不改变决策路径。
- `forecast_dashboard.html` 实时区域增加研究特征显示；仅展示开盘后时间、近期发帖、速率变化、预期差和来源年龄，不改变纸面动作。
- `RUNBOOK-CH.md` 增加离线研究命令；`test_market_dynamics_research.py` 和历史回退回归已加入。专项36项、全套191项通过，Python编译和前端合成显示通过。
- 未启动服务、未访问市场数据接口、未写生产SQLite/CSV/模型、未重训/生产回放；`do_not_auto_trade=true`。真实外联覆盖率和生产服务加载效果仍未验证。

## 最新快照：历史缺失审计与请求节奏修复（2026-10-03）

- 当前用户要求检查爬虫历次修改、SQL状态并解决前端大量缺失；不启动服务、不访问外部接口、不自动补采。分支 backtest-audit-v1、HEAD 7a1dd15342e8e0ee0ffeaa2485bb57145ca90e33；12 个既有 tracked 修改和全部未跟踪文件保留，未切分支或提交。
- 只读完成 Git/历史提交/进程/健康检查及 7 个 SQLite quick_check（全 ok）。8765 无监听，本机 /api/health 拒绝连接；Python 查询仅见 Jupyter 相关进程。生产库 delete/normal、68 市场、965637 条价格、982895 条盘口；2 市场无区间内价格、49 无盘口、7 仅区间外盘口。旧“55市场无价格”已不代表现状；当前统计不证明全部时间桶覆盖。详见 M-20261003-HISTORY-COVERAGE-AUDIT。
- 40 市场有三小时源价格，而复盘展示仅读当前五分钟；源稀疏造成大量空白。独立小时库 prices=146180，并未接入默认生产页面。一小时属于本地保存的官方支持范围；最近归档价格无请求错误但稀疏，盘口 404 不能当作价格限流证据。
- 现有按天/半天拆分、v2 分页、精度回退保留。本轮修复 request_delay 只在结果保存后 sleep 的缺陷：JsonClient 共享门控控制真实 GET/公开批量 POST 启动（含分页/重试），crawl_one 接入有限非负间隔，历史同步默认 0.2 秒/客户端。独立 hourly_price_research 的采集配置未改，本轮未运行任何采集。
- 历史展示新增源精度/来源/粗于当前窗口字段，页面显示采样分钟数和“当前 5 分钟区间无价格样本”；不扩窗口、不插值、不改模型/策略/安全评分、不混入小时研究库。strategy_simulator 纯计算边界及 do_not_auto_trade=true 保留。
- 174 项全套 Python、编译、无服务前端语法/唯一 DOM id/合成渲染验证通过；本轮未做真实浏览器/外联效果验证。新增 audit_history_coverage.py，可 mode=ro 重查覆盖；可选 --quick-check 对 16GB 生产库耗时较长。
- 未启动/停止/重启服务、未写生产 SQLite/CSV/模型、未重训或生产回放、model-update.txt 未改。测试仅临时库/模拟接口；EXE 未重建。下一步在用户明确允许外联后先做小范围请求核对和覆盖/耗时对照，再定补采；历史盘口缺口仍须真实历史档案，不能由当前 /book 重建。

## 最新说明：小时采样官方依据（2026-09-21）

- 本轮实时读取官方Prices and Order Books页面Price History/API部分，明确支持bucket_seconds=3600。不能把空响应归因为“不支持一小时”。请求无错误/6小时核验缓存不等于完整数据；提示为本项目生成，详见QA-026。
- 官方参数范围、保留描述及原文行号已记录；一天/半天分段属于本地策略。尚未核对用户这次具体token响应、缓存命中或后端版本。未生产补采/重启；仅更新三份记忆文档。

## 当前交接快照：按天请求与半天恢复（2026-09-21）

- 最新请求：5分钟采样、按天/半天分段多次请求，核对每日存储和市场标签。history_prices.py已取消7天合并，初始最多24小时；HTTP有限重试后针对范围过大/超时/断流拆到最多12小时，自动精度补查适用；限流/权限/DNS不拆分。成功子窗口缓存，失败数据和父审计保留，恢复父失败不再计最终错误。
- 存储定义仍是带event_slug等标识的逐时刻market_prices，可按日读取，不是每日文件；posts按post_id去重，不直接挂市场slug。本轮未迁移表。页面仍默认1小时、可选5分钟；不插值、不改变交易算法。
- 验证：14项专项和169项Python全套通过；没有实际外联补采/速度验收，没有写生产库或重启服务，运行版本未确认，EXE未重建。下一步加载新后端后用小范围真实请求比较覆盖/耗时，再决定补采范围；现有缺口未自动恢复。
- 当前HEAD 7a1dd15；保留大量既有未提交修改。详细文件清单和边界见M-20260921-DAILY-HISTORY-REQUESTS。

## 当前交接快照：全历史持有统计显示（2026-09-20）

- 用户最新优先项为全历史其他市场结果不显示。已新增独立统计按钮、全部待统计行、逐市场更新/失败继续、明确未成交原因和建仓顺延；不依赖单盘有检查点及响应run_config，保持结算揭晓锁。未成交不伪装为0收益。
- 真实65市场只读复算9成交/56未成交，原因齐全；移除无安全盘口时多余价格查询，17.706秒降至2.68秒。166项Python及扩展浏览器回归通过。实际仍缺历史盘口，独立一小时CSV没有接入订单簿成交模拟。
- 同时保留前项修复：切市场清日期、完整市场范围按钮、空结果原因；无实时子服务时回放仍读本地库。9.11–9.18盘两个既有后端均报告2015检查点，用户当时完整日期条件未知，不能声称唯一根因确定。
- 服务未重启，刷新页面可用新按钮；后端优化/诊断需重载。未写生产库或外联。见M-20260920-HOLD-SUMMARY-DISPLAY。

## 当前交接快照：优先一小时研究补采（2026-09-20）

- 最新用户目标：先交付1小时实验价格数据，再保留5分钟模拟研究选项。已接入Data API v2价格分页及指定精度缺失时自动读取源可用精度；官方文档及真实请求已核对。旧接口/显式1h为空而自动精度有记录；负例无效token为空。数月前粗精度不能还原真实小时价。
- history_prices.py负责增量窗口、分页/去重/验证/失败保留/请求哈希；polymarket_pipeline新增源精度/来源、核验缓存、小批量写入；live_dashboard_server和前端默认60分钟、可选5分钟、区分sparse与partial，逐盘口进度。原交易执行算法未修改。
- hourly_price_research.py仅读生产库，真实补采到独立data/hourly_research/source_prices.sqlite，输出逐盘口逐相对小时价格及结算毛盈亏CSV。不插值、不用未来价，源精度>1h排除；非模型选盘/非订单簿成交。已完成67市场/1846 token补采与离线导出：305544节点，72956可用（28市场）、232588缺失，69130可算毛收益。两个盘口剩余补查遇TLS/DNS错误，审计保留；未证明所有数据补齐。
- 165项Python、合成浏览器及CSV逐行时间/公式检查通过；生产进程未重载，新增UI/API需后端重载生效，独立CSV已可直接使用。全套结果见M-20260920-HOURLY-V2-BACKFILL。
- 官方文档快照history_price_docs_audit.json、同token对照history_source_audit.json及无效token负例history_invalid_token_audit.json；audit_history_source.py可复核。无凭据。生产旧抓取正在进行，未重启/终止/写入生产库；EXE未重建。既有未提交修改保留。

## 当前交接快照：历史价格完整区间抓取（2026-09-20）

- 最终验证：158项Python测试、合成浏览器回归及git diff --check通过。

- 修复默认不抓历史价格、相对最近时间范围错过旧市场及空数据误报成功。现在默认请求市场实际起止区间（未结束市场截至采集时刻），按天分段、5分钟采样；成功片段保留，逐token返回覆盖与来源错误，重复补采计入已有覆盖。
- 部分数据缺失标partial，不覆盖固定报告；页面展示全部市场覆盖明细，不把partial计为完整成功。涉及polymarket_pipeline.py、live_dashboard_server.py、forecast_dashboard.html及测试，见M-20260920-HISTORY-EXACT-WINDOW。
- 验证使用模拟接口与临时数据库，未外联、未写生产数据、未重启服务；旧55市场价格缺失尚未实际补齐，历史深度仍不可由当前/book恢复。后续重载后端并运行历史抓取，再核对逐市场覆盖；不得宣称全部可交易。
- 分支backtest-audit-v1，HEAD 7a1dd15342e8e0ee0ffeaa2485bb57145ca90e33；保留大量既有修改。运行进程版本未确认，EXE未重建。

- 最新只读覆盖审计：65历史市场中9成交、56未成交；49无订单簿、7仅事后盘口。55市场无市场区间内价格。当前顺延可能晚数天，不等同严格指定时长对照。价格收益研究模式/严格与顺延分组仅建议，尚未实现，见QA-021。

## 最新补充：概率分数预算与全历史持有统计（2026-09-20）

- 强制模式已替换为中心+左右逐层概率份额分配，不再按edge/最低一份；30%覆盖、25%中心及21%/14%邻档，权重83.333%/10%/6.667%。不足覆盖或缺盘口预算留现金，历史预测与legacy不变。
- 持有模式载入后自动用同一相对建仓时长遍历本地已结束市场，分别显示状态与到期收益，加权统计只计有成交且官方已结算市场；揭晓锁保留。
- 152项Python及扩展合成Chrome回归通过，真实只读65市场统计无报错。见M-20260920-FRACTIONAL-COVERAGE-HISTORY。未重启服务；旧8778预览可能仍是旧后端。

- 最新：历史抓取发现失败不再退回猜测slug，Gamma列表增加有界回退；全失败保留报告并标失败。当前用户失败根因仍缺实际错误证据，数据库最近12条归档complete；未外联或重启，见M-20260920-HISTORY-CRAWL-FAILURE。

## 最新补充（2026-09-20，行情数据覆盖与兼容）

- 已修复历史页把“有安全订单簿、无价格表”误报为未采集：增加仅展示用盘口中间价代理；不作为历史价格、模型输入或成交依据。
- 采集/存储兼容价格字段和批量响应，明确YES token，过滤零深度/非有限档位；页面区分未采集、无有效双边报价、深度无效和中间价代理。见M-20260920-DATA-COVERAGE。
- 149项Python与合成浏览器验证通过；未重新采集或重启生产服务。真实历史库旧记录不回写，需下次明确授权采集后获得更完整价格历史。

- 2026-09-20补充：历史成立纸面交易表仅显示非零实际成交及已揭晓结算，包含日期过滤前成交；零成交/持有/拒绝不再混入，仍保留后端审计和当前节点状态。见M-20260920-FILLED-HISTORY。

## 最新补充（2026-09-20，相对时间与持有至结算）

- 建仓改为“市场开始后小时+分钟”，API paper_entry_elapsed_minutes，非检查点向后取下一检查点。旧绝对时间参数仍兼容但不能与相对时间同时使用。
- 新增退出策略hold_to_settlement，首次建仓后不提前出售、不增持，仅官方结算；managed保留策略管理，页面默认仍managed。
- 148项Python测试及合成浏览器通过；重叠市场预测迁移尚未实现，需以同一时刻可知预测做样本外对照，不能宣称已验证准确率。

## 当前交接快照（2026-09-20，强制建仓与成交审计）

- 本节优先于以下旧快照。用户已授权实施强制建仓计划：动态UTC节点、连续80%概率覆盖、100份资金按正edge分配、逐档+20%/-15%退出、官方到期结算；保留时间安全，不生产采集/外联/写生产库。
- 已完成前后端接入与兼容边界修正；API legacy默认，页面mandatory_entry默认；首次节点前不建仓，无安全盘口顺延，首次部分成交后余额保留，不强制补齐。缺官方结果明确未结算。
- 实际变更及验证见 M-20260920-MANDATORY-PAPER。147项Python测试和合成Chrome桌面/移动回归通过；两个9月市场只读真实复放成功。预览 http://127.0.0.1:8778/ 是隔离只读服务，未重启生产。
- 分支backtest-audit-v1，HEAD 7a1dd15342e8e0ee0ffeaa2485bb57145ca90e33；大量既有未提交修改保留。EXE未重建，历史模型/生产CSV未修改。
- 真实只读Chrome市场A→B→A、取消、成交显示和桌面/移动检查通过，单次约19–34秒；只读预览进程PID 64320。生产进程未检查/未操作。
- 限制：仍为汇总深度模拟；缺盘口不能虚构交易，9月11–18日缺官方结果无法最终兑付；原全局图隔离问题仍不在本次范围。


## 当前交接快照（2026-09-19，启动控制台 EXE 交付后更新）

入口：先读 `持续开发协议.md`，再按任务阅读 `Q&A.txt` 与维护日志。以下快照优先于后面的旧交接内容；旧内容保留作为历史，不代表全部当前事实。

### 当前目标与已实现

- 用户准备将工作移交到下一次对话。新增待开发目标：把 AIPRP 本地启动控制台打包为 Windows 单文件 `.exe`，提供一键结束当前服务、一键重启后端服务、一键启动前端，并沿用现有端口与命令。
- 当前目录已有控制台实现和打包入口：`dashboard_launcher.py`、`dashboard_launcher.html`、`AIPRP-DashboardLauncher.spec`、`build_dashboard_launcher.ps1`、`start_dashboard_launcher.ps1`，输出目录为 `dist_dashboard_launcher`。本轮新增 `/api/restart` 和“重启后端服务”按钮；停止、重启只作用于本控制台持有的后端子进程，打开前端仍要求健康就绪。
- 已实际构建 `dist_dashboard_launcher/AIPRP-DashboardLauncher.exe`（10,130,469 字节，2026-09-19 23:16:48）。冻结 EXE 冒烟启动和状态接口验证通过；完整三项生命周期用合成/Mock 子进程验证，未启动真实后端。
- 本轮核对：8760、8765、8778 均无监听端口；两个 Python 进程命令行归属无法确认，未终止任何进程。

- 用户最新要求：修复切换历史市场无限加载、纸面模拟异常500+ YES；允许启动前端实际调试。本轮已完成代码修复及真实本地只读页面验证。
- 新增history_replay.py：独立只读事务、单worker任务、进度/取消、120秒截止；前端15秒请求/130秒总等待、请求代次隔离。盘口查询按缓存token组合利用既有复合索引，不写生产库。
- 新增replay_probability.py：24小时先验稳定开盘速度、剩余数量分布、10%概率和3标准差范围新增仓位门控；保留原始预测，明确未经校准，主预测/评分使用受约束输出。Logic.txt已记录公式与原有执行约束。
- 历史工作区已合并为一套日期/市场/五分钟游标，三个标签共享检查点；CSV 流式读取与私有字段裁剪已实现。
- 旧版纸面模拟已修复数字字符串来源时间解析、从 raw_json 读取时恢复时间、YES token 和五分钟来源年龄检查；共享现金与25%调仓限额已接入。首小时开仓、8%/4%/0%阈值不变，v2未接入。
- 三图共用悬停逻辑，结果残差受揭晓锁控制。全局图已移到分市场结算表上方；表默认折叠，展开限高360px。

### 未完成项（切勿当作已修复）

1. 历史加载代码与隔离页面已修复验证；原生产后端是否重载未确认，本轮未重启采集服务。后台任务方案已实现，QA-006/007的“仅建议”已由QA-011更正。较慢市场仍需约19–32秒，页面显示进度；不是即时计算。
2. 全局残差数据与单市场 `state.privateRows` 耦合；切市场清空全局，只有一个点时没有常驻标记。图的位置/表折叠已改，但这两项未修复。见 QA-008/009。
3. 概率保护是工程风险约束，未重训、未拟合校准或证明收益提升；真实高计数支持的500+不封禁，缺少安全盘口时仍不能模拟成交。不得为了出现交易取消时间安全规则。

### 后续入口与验收

- 历史任务入口：history_replay.py、LiveService.backtest_review、loadUnifiedHistory；测试test_history_replay.py和test_history_workspace.cjs。后续若优化速度，保留只读事务、取消和检查点预热；无需修改数据库索引。
- 全局图入口：privateRows/setPrivateRows/clearHistory/updateReview/drawGlobalResidualChart/drawInteractiveChart；验收多个市场全局数据不因单市场切换丢失，单点可见，揭晓锁与空状态正确。
- 不自动启动这些待办；依据用户当轮明确任务执行。

### 验证与运行边界

- 启动控制台本轮验证：`python -m unittest test_dashboard_launcher.py` 共 8 项通过，Python 编译和内联 JavaScript 语法检查通过，PyInstaller 构建成功；EXE 在隔离端口可启动并返回 `not_started`，无托管服务重启返回明确错误。未验证真实后端重启/打开浏览器流程，避免启动生产采集。

- 本轮Python新增任务隔离/取消/超时、早期爆发/结算收敛/不完整区间、保护门控和索引路径等价测试。最终数量及结果见MAINTENANCE-CH.md最新条目。
- 真实本地只读验证：9月11–18日与9月8–15日均2015检查点，24/58条成交动作，安全盘口覆盖46/548检查点，首小时4/12和12/12。后者优化前98.9秒、后17.7秒；浏览器A→B→A约31.9/18.8/31.9秒，取消旧任务与桌面/手机检查通过。
- 常用命令：`python -m unittest discover`；`node test_history_workspace.cjs`；`git diff --check`。浏览器脚本需本机 Chrome，仅使用合成响应和临时服务，截图在系统临时目录；沙箱拒绝时按权限规则处理，不能假称已经运行。
- 真实验证使用debug_history_readonly.py独立8778服务和临时Chrome；原8765服务状态未确认，磁盘变更不能视为生产进程更新。本轮未重启/终止生产采集、未访问外部API、未写生产SQLite/CSV/模型、未重训。
- 分支 `backtest-audit-v1`，HEAD `7a1dd15342e8e0ee0ffeaa2485bb57145ca90e33`（本轮只读确认）；大量既有未提交修改及未跟踪文件必须保留，不能凭 git diff 把全部修改归为本轮。

## 以下为旧交接记录（历史保留）

## 本轮新增：预测结果获利网格研究（2026-10-09）

- 已新增纯聚合模块 `paper_grid_research.py`，比较 0 至 360 分钟、每 30 分钟建仓节点与中心/对称概率覆盖。输出固定市场预算、共享资金池、最大回撤、风险调整分数和滚动 walk-forward；只使用已有本地只读回放结果。
- 后端已接入 `/api/paper-grid-research` 和 `/api/paper-grid-jobs/{id}`，前端纸面交易区增加手动运行网格研究按钮和结果表。单次最多 64 个组合，默认不自动运行。
- 新增 `test_paper_grid_research.py`。本轮未启动服务、未访问外部接口、未写入生产 SQLite/CSV/模型、未重训或运行生产回放；`model-update.txt` 未修改；`do_not_auto_trade=true` 保持。
- 已完成离线验证：网格单元测试 4/4、Python 编译、前端 JavaScript `node --check` 和 `git diff --check` 均通过。前端按钮默认不运行，必须手动提交且单次最多 64 个组合。

## 当前仓库

- 项目目录：`C:\Users\宋思远\Desktop\K\AIPRP\co爬虫测试`
- 分支：`backtest-audit-v1`
- HEAD：`7a1dd15342e8e0ee0ffeaa2485bb57145ca90e33`
- 工作区存在用户既有 dirty 修改和未跟踪文件，必须全部保留。
- 禁止使用 `git reset`、`git checkout --`、`git restore`、`git clean`，不要切换分支。

## 永久安全约束

- `do_not_auto_trade=true` 永久保持。
- 不访问钱包、私钥、签名、下单 API 或真实资金。
- 不自动交易、自动下注。
- 实时数据只能称为 REST/RSS 低频轮询，继续支持约 1 小时采集。
- 周二/周五重叠市场按完整 slug 隔离。
- 检查点之后的数据不能进入过去的预测特征、评分或安全历史 edge。
- 当前盘口不能填补历史盘口空缺。
- 小样本指标、paper edge 和 paper PnL 不是盈利证明。
- 当前没有航班数据源，`flight` 保持 `not_configured`。
- 未明确要求不得重训、重新抓取历史或运行生产回放。
- 修改文件必须使用 `apply_patch`；每次实际修改后更新 `MAINTENANCE-CH.md`。
- 没有重训或模型回放时不得更新 `model-update.txt`。

## 已完成能力

- `forecast_contracts.py`：版本化 feature/prediction/decision/display DTO。
- `polymarket_pipeline.py`：Gamma、XTracker、CLOB 价格/订单簿归档、历史窗口、5 分钟检查点和 slug 隔离。
- `live_dashboard_server.py`：只读 `/api/backtest-review`，完整 outcome 行情 DTO，固定模型历史复放。
- `forecast_dashboard.html`：统一“历史回测与复盘”5 分钟时间轴，逐 outcome 价格、盘口、edge 和质量状态。
- 缺失行情显示明确中文状态，不用 0 填充。

## 最近完成的策略模拟隔离

- 新增 `strategy_simulator.py`，纯策略模块，不导入数据库、网络、模型或前端。
- 实现：
  - 前 12 个 5 分钟检查点允许建仓，覆盖市场开始后 60 分钟；
  - 多 outcome 共用市场总预算；正成本后 edge 按比例分配；单 outcome 上限 40%；
  - 成本后 edge：模型概率减 ask、滑点和手续费；
  - edge >= 8% 进入/增持；4% 到 8% 保持；0% 到 4% 减持；<= 0% 出清；
  - 买入按 ask、卖出按 bid；依据买卖深度模拟部分成交；
  - 零深度或不可成交时动作是 `no_fill`，数量为 0；
  - 输出动作、仓位、成交量、费用、已实现/未实现纸面 PnL；
  - 完整 slug 独立维护内存持仓。
- `live_dashboard_server.py` 保留 `paper_buy_cost` 和 `paper_position_rebalance` 兼容导出，实际实现来自 `strategy_simulator.py`。
- `forecast_dashboard.html` 详情显示纸面动作、持仓和 PnL。
- `MAINTENANCE-CH.md` 已记录上述选项的具体含义。

## 验证基线

- 全套测试：77 项通过。
- Python 编译：通过，包括 `strategy_simulator.py`、`polymarket_pipeline.py`、`live_dashboard_server.py`、`musk_tracker.py`、`forecast_contracts.py`。
- 前端 UTF-8 JavaScript `node --check`：通过。
- HTML 重复 DOM id：无。
- `git diff --check`：无空白错误，仅有既有 CRLF 警告。
- SQLite `mode=ro` `quick_check`：`ok`。
- 未启动 8765、未访问 Gamma/CLOB/RSS、未写入生产 SQLite/CSV、未重训或修改模型。

## 后续开发建议

优先继续把 `strategy_simulator.py` 扩展为可独立测试和配置的策略实验模块，再由 `/api/backtest-review` 适配其 DTO。保持策略计算与模型、数据采集、决策安全限制、前端展示四层分离。任何新增参数或字段都要同步检查 CLI、后台任务、API DTO、前端读取和回归测试，并更新 `MAINTENANCE-CH.md`。
