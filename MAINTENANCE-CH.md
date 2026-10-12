# 中文维护日志

## M-20261012-GITHUB-README-PREP

- 实际修改：`README.md` 增加项目阶段性描述日志、前端 slug 功能分区、启动命令、离线检查、数据发布边界和安全约束；保留原有英文组件说明作为兼容参考。
- 发布排除：`.gitignore` 增加 `dist_dashboard_launcher/`，构建生成的 exe 不随源码提交，可由 `build_dashboard_launcher.ps1` 重建；既有 `data/`、SQLite、日志、环境文件和缓存排除规则保持不变。
- 版本审计：确认 `6bac487` 是当前 `7a1dd15` 之前的纸面预测提交；动态/波动研究文件主要来自之后的工作区修改。尚未创建提交、添加 GitHub remote 或推送，待远程空仓库状态和版本发布范围复核后决定。
- 范围与安全：本轮未访问外部接口，未启动服务，未写入 SQLite/CSV/模型，未重训或运行生产回放；`model-update.txt` 未更新；`do_not_auto_trade=true` 保持。

## M-20261012-HISTORY-RETENTION-500-ROUTING

- 实际修改：`history_prices.py` 增加 Polymarket 历史价格保留期路由和请求行数保护。5 分钟目标在最近 60 天优先请求 `bucket_seconds=300`；60 至 90 天请求 `1800`；超过 90 天请求 `10800`，并保留实际 `resolution_seconds`，不会把粗粒度样本登记为五分钟完整覆盖。
- 请求拆分：每个请求按安全上限约 450 行规划，仍遵守显式 `start/end` 不超过 15 天、`end` 排他；返回行数达到 450 条时记录 `split_required` 并自动二分窗口。每次请求审计保存目标粒度、实际粒度、保留期策略、安全上限、返回行数和拆分原因。
- 失败回退：五分钟空响应、稀疏响应或可拆分失败会尝试 30 分钟及更粗长期档；回退结果带真实分辨率和审计证据，不能进入五分钟安全特征、评分或 edge。
- 若回退接口返回了价格点但缺少源分辨率，会在首个有记录的较粗请求后停止继续扩大粒度；精度仍保留为未知，不根据请求参数猜测。
- 重复请求控制：保留期路由成功的窗口可进入现有短期 `history_price_checks` 抓取回执，避免同一粗粒度窗口每轮重复请求；该回执只表示“已按当前路由检查”，`target_complete` 仍根据源分辨率单独计算，粗粒度不会因此变成五分钟完整数据。
- 测试：新增近期五分钟窗口拆分和 75 天窗口 30 分钟路由回归；`test_history_price_window` 定向测试通过，Python 编译和 `git diff --check` 通过。
- 范围与安全：未启动服务，未访问外部接口，未写入 SQLite/CSV/模型，未重训或运行生产回放；`model-update.txt` 未更新；`do_not_auto_trade=true` 保持。
- 尚未完成：尚未对生产市场执行补采，真实覆盖率需在用户明确安排外联和任务窗口后再核对。

## M-20261006-DATA-ORGANIZER-ERROR-DIAGNOSTICS

- 数据审计：`data/` 当前约 17.1 GB，其中 SQLite 约 16.5 GB、`.bak` 约 546 MB、CSV 约 523 MB；整理器只扫描文本文件并保护 SQLite、备份、模型和实时来源，因此不能用重复文件清理释放主数据库空间。
- 原因定位：前端整理按钮此前使用 `window.location.origin` 请求接口。页面从启动控制台 8760 或旧后端打开时，响应为纯文本 `Not found`，代码直接调用 `response.json()`，于是出现 `Unexpected token 'N'`。
- 修复：整理请求统一复用 `liveUrl` 后端地址；页面位于 8760 时默认指向研究台 8765；响应先读取文本并校验 JSON，非 JSON 或 HTTP 错误现在会显示状态码、返回片段、请求地址及重启提示。
- 交接：`CONTINUATION-CONTEXT-CH.md` 已同步本次空间审计、`Not found` 根因和修复后的 API 地址规则。
- 安全与范围：未删除或改写任何 `data/` 文件，未写入 SQLite/CSV/模型，未启动服务或访问外部接口，未重训或运行生产回放；`do_not_auto_trade=true` 保持。
- 验证计划：执行前端脚本解析、Python 编译、`git diff --check`，并用临时本地 HTTP 响应覆盖 JSON 成功与 `Not found` 错误路径。

## M-20261004-DATA-COMPLETENESS-FILTER

- 实际变更：新增只读历史完整性评估。每个市场现在返回 `data_completeness_tag`、`data_completeness_reasons`、五分钟价格覆盖率、前 12 个策略检查点的点时盘口覆盖率、结算状态和 `eligible_for_profitability_analysis`。完整标签要求市场已结束并结算、所有 YES token 的五分钟价格窗口完整、每个建仓检查点都有带源时间的历史盘口；当前盘口或无源时间盘口不会被当作历史数据。
- 失败定位：当前 `data/extended.sqlite` 的 71 个市场均为 `incomplete_data`；61 个存在五分钟目标价格缺失，10 个价格目标仍稀疏，全部缺少可证明的前 12 个点时历史盘口，另有 2 个尚未结束。HTTP 404 的 `clob_book` 被记录为盘口资源缺失，不再误判为价格限流。
- 实际变更文件：`history_replay.py`、`live_dashboard_server.py`、`market_dynamics_research.py`、`forecast_dashboard.html`。
- 获利能力过滤：批量持有研究和固定时点/ walk-forward 研究只接受 `complete_for_profitability`，排除项输出原因和数量，缺失数据不计作零收益。
- 范围与安全：本阶段只读 SQLite 审计和代码验证；没有启动或重启服务，没有访问外部接口，没有写入 SQLite/CSV/模型，没有重训或生产回放；`do_not_auto_trade=true` 保持。
- 验证：Python 目标模块编译通过；完整性目录读取 71 个市场并输出标签/原因；既有测试契约继续保留，待下一阶段补充完整性单元测试和前端回归测试。

## M-20261004-DATA-COMPLETENESS-FILTER-FINAL

- 优化只读评估：目录优先使用最新历史归档中的五分钟覆盖证据，盘口仅扫描前 12 个策略检查点附近的带源时间记录，避免对大 SQLite 做重复全表计数；`/api/backtest-review` 同样复用该证据并返回请求市场、合格市场、排除市场及原因。
- 研究过滤：`market_dynamics_research.py` 的固定时点和 walk-forward 输出输入行、合格行、排除行及排除原因；旧的无标签 JSONL 仍保持兼容。
- 最终验证：全套 `python -m unittest -q` 为 191 项通过；Python 编译通过；目录只读核验仍为 71 个市场、0 个获利分析合格市场；未启动项目服务或访问外部接口，未写 SQLite/CSV/模型，未重训或运行生产回放，`do_not_auto_trade=true` 保持。

## M-20261004-DYNAMICS-AND-ADAPTIVE-RESEARCH

- 实施自适应历史抓取：公共 v2 历史价格默认精度统一为5分钟；请求窗口增加15天上限保护，查询/连接失败只做一次受控的12小时拆分，429/503继续由客户端按Retry-After处理；指定精度空/稀疏时依次记录省略精度、30分钟和60分钟回退，但粗精度不会进入5分钟安全特征。
- 新增 `market_dynamics_research.py`：提供实时快照特征扁平化、未来检查点标签生成、可解释多目标 ridge 映射矩阵、固定建仓时点收益比较和按市场 walk-forward 节点选择；全部纯内存/JSONL，不访问数据库、网络或模型。
- `live_dashboard_server.py` 的归档快照增加 `dynamics_features` 描述字段，不改变预测、纸面执行或安全决策路径；`RUNBOOK-CH.md` 增加离线研究命令和数据边界。
- 新增 `test_market_dynamics_research.py`，覆盖缺失字段、前向标签时间门控、系数/区间输出、固定节点和样本外节点选择；专项和全套回归均已完成。
- 本阶段未启动服务、未访问市场数据接口、未写生产SQLite/CSV/模型、未重训或运行生产回放；`model-update.txt` 未改，`do_not_auto_trade=true`。
- 验证完成：历史/采集/动态研究专项36项通过，全套 `python -m unittest discover` 共191项通过；目标模块编译和既有无服务前端渲染检查通过。仅有测试中模拟HTTPError的既有ResourceWarning，不影响断言。
- 实时界面补充“市场变化研究特征”区域，显示开盘后分钟、近5/15分钟发帖、发帖速率变化、模型-市场预期差和来源年龄；仅展示归档特征，不参与实时决策。`node test_history_price_display.cjs` 与 `node test_history_workspace.cjs` 均通过。

## M-20261004-HISTORY-VALIDATION-CLOSE

- 复核上一阶段实现：官方文档只读核对完成，`/v2/prices-history` 与 `/v2/trades` 的窗口、精度、cursor、错误和限速约束已与代码/测试对应；未请求市场数据接口。
- 最终验证维持 `python -m unittest discover` 186项通过、前端合成渲染通过、目标模块编译通过、`git diff --check` 无新增空白错误。工作区仍为 dirty，原有 tracked/未跟踪文件全部保留；未切分支、未回退或清理。
- 未启动/停止/重启服务，8765无监听；未写生产SQLite、CSV或模型，未重训或运行生产回放，`model-update.txt` 未改，`do_not_auto_trade=true`。

## M-20261003-HISTORY-ACQUISITION-VERIFIED

- 收尾修正 `test_history_price_window.py` 的小时导出汇总预期：00:00 检查点不能使用 00:00 开始、01:00 结束的桶，结果应为 `available=1`、`missing=1`、`rows=2`。`hourly_price_research.py` 的方法说明同步明确桶结束条件和未知精度排除，未放宽时间安全规则。
- 核对官方文档子页：`/v2/prices-history` 使用 `token_id` 与三选一时间形式（`start`/`end`、`interval`、`as_of`），绝对窗口最多15天、`start`含且`end`不含；指定 `bucket_seconds` 可能空/稀疏，省略时才自动选择可服务精度；响应为 `data` 加 cursor `pagination`，必须跟随 `next_cursor` 至 `null` 并使用每条 `resolution_seconds`。`/v2/trades` 使用 `condition`/`event_id`、每页游标且不得用 offset；429/503按`Retry-After`重试，保留`code`和`trace_id`。
- 现有 `history_prices.py` 已按真实窗口拆分、严格分页状态、保留请求证据并在指定精度为空时省略精度回退；`PublicDataClient.get_trades` 已使用 v2 condition 游标并拒绝不完整分页。前端对无样本显示“已尝试但缺失”等状态，不将缺失写成0或成功。
- 验证：`python -m unittest discover` 共186项通过（仅既有模拟 HTTPError ResourceWarning）；`node test_history_price_display.cjs` 通过；`python -m py_compile history_acquisition.py history_prices.py polymarket_pipeline.py live_dashboard_server.py hourly_price_research.py` 通过。未启动服务或外联，测试只用模拟接口/临时库。
- 本阶段未写生产 SQLite、CSV 或模型，未重训或生产回放，未更新 `model-update.txt`，`do_not_auto_trade=true` 保持。真实覆盖率、限流改善和运行中服务加载效果仍未验证。

## M-20261003-HISTORY-ACQUISITION-EVIDENCE

- 收尾定位：19项历史窗口测试中仅小时导出汇总断言失败；CSV已正确将00:00开始、01:00结束的桶排除于00:00节点。修正测试为available=1、missing=1、rows=2，并更新导出methodology说明桶结束和未知精度排除；不放宽安全规则。准备重新运行全套合成验证，尚未记为通过。未启动服务或外联，未写生产SQLite/CSV/模型、未重训或生产回放，model-update.txt未改，do_not_auto_trade=true。

- 首轮98项Python检查：新证据/请求测试通过，旧测试出现6失败2错误，均涉及旧模拟响应省略分页、未知精度覆盖或小时桶提前可知；已增量更新相应fixture/预期，并保留缺分页失败专测。无服务前端状态/转义检查通过。继续验证，不将首轮失败记为通过。

- 新增test_history_acquisition.py合成回归：缺失证据、旧核验/归档、缓存过期、失败恢复、读取异常、存储失败、桶结束、安全旧样本、成交指纹、游标和Retry-After；扩展无服务前端状态/转义检查。按用户已确认计划准备执行验证，仅模拟接口与临时库。

- 时间安全补充：通用latest_prices改用本地已捕获且桶已结束的prices_at，保留YES token过滤及安全旧样本回退；小时研究导出同样按桶结束后可知处理，不再将NULL精度用于小时收益。此变更可能减少旧未知精度的可用特征，未重训/未生成生产报告；验证待完成。

- 第三阶段复盘DTO和前端接入采集状态/目标精度/证据；旧库缺新审计表仍可只读使用旧成功核验，查询错误继续作为读取失败传播。前端显示“已尝试但缺失”等状态及时间，悬停显示范围/数量/错误；价格与盘口缺失独立。开始排除跨检查点聚合桶、未知精度和未分类零精度记录的安全价格用途，验证待完成。

- 第二阶段新增history_acquisition.py：按市场批量加载新审计、旧成功核验及归档失败页；逐窗口区分有样本、已尝试但缺失、失败、未尝试与证据不足。采集代码先保存价格再追加审计，存储失败不保存成功核验。旧NULL精度不计明确覆盖，独立小时采集也接入0.2秒请求门控。生产库未写入；验证待完成。

- 请求客户端已接入共享Retry-After等待及JSON错误code/trace证据；公共成交改为v2完整游标遍历，强校验condition，成交指纹不再仅用交易哈希。新增追加式审计表建表代码，未执行生产迁移。验证待完成。

- 用户确认实施缺失诊断方案，保留分支及所有既有修改。第一阶段修改history_prices.py：严格v2分页状态、逐请求时间/错误证据和逐窗口审计；未知精度不满足目标精度补查判断。验证待完成，其余接入进行中。
- 本阶段未启动服务、未访问外部接口、未写生产数据库/CSV/模型、未重训或运行回放，do_not_auto_trade=true保持。

## M-20261003-HISTORY-COVERAGE-AUDIT

- 恢复交接并只读核对 Git：分支 backtest-audit-v1，HEAD 7a1dd15342e8e0ee0ffeaa2485bb57145ca90e33；12 个既有已修改文件及全部未跟踪文件保留，未切分支。git diff --check 无空白错误，只有既有换行提示。
- 历史提交及维护记录确认：旧请求使用相对 interval；当前未提交实现已使用实际市场窗口、v2 分页、源精度回退、按天/半天拆分及六小时核验缓存。官方本地快照支持 bucket_seconds=3600，不能把空响应认定为不支持一小时。
- 8765 未发现监听，GET 本机 /api/health 连接拒绝；只读进程查询因权限限制申请后完成，仅看到 Jupyter 相关 Python 进程。未启动、停止或重启服务。
- 新增 audit_history_coverage.py：直接 SQLite mode=ro、query_only、只读事务，输出表数、逐市场价格/盘口覆盖、精度和近期归档摘要；不构造 MarketStore，不调用网络或模型。data 下 7 个 SQLite quick_check 均 ok；生产 extended.sqlite 为 16,214,396,928 字节，journal=delete、locking=normal；表名已核对。
- 生产行数：market_events=68、market_outcomes=1872、market_prices=965637、market_orderbooks=982895、posts=7799、prediction_archive=16111、history_price_checks=26341、source_events=0。价格精度 NULL/0/300/1800/3600/10800 秒分别 322422/9125/570188/4871/48401/10630 行；三小时精度涉及 40 个市场，不能视为完整五分钟历史。
- 按真实市场区间计：68 市场中 2 个无区间内价格，49 个完全无盘口，7 个仅有区间外捕获盘口。捕获在区间内也不等于所有检查点都有安全/可成交盘口。本次结果更正旧快照“55市场无价格”，但不证明所有 token/时间桶完整。
- 独立 data/hourly_research/source_prices.sqlite 为 54,423,552 字节，prices=146180、checks=0，quick_check=ok、delete/normal；页面默认不读此库。最近六条生产归档为 2026-09-22，price_errors=0、price_quality=sparse；partial 中有已结束 token 的 clob_book HTTP 404，不能据此断言历史价格发生限流。
- 已发现 request_delay 在并发任务完成后才休眠，不能限制实际请求节奏；独立小时研究库并非页面默认读库。开始增加 JsonClient 共享请求门控，后续接入采集配置并验证，不自动补采。
- 门控已接入历史价格/CLOB 的实际 GET/公开批量 POST（包括分页和重试）；非零间隔时锁持有至响应头返回，保证实际发出顺序，可能降低吞吐但不会一次同时发出全部请求。采集前校验间隔，移除结果保存后的休眠；历史同步默认每客户端至少 0.2 秒，CLI 默认仍 0.1 秒。未运行真实采集，不能声称拒绝/限流已消除。
- 历史展示查询/DTO 增加源精度、来源与粗于五分钟标记；前端显示采样分钟数，空白改为“当前 5 分钟区间无价格样本”。不扩大价格窗口、不插值、不接入独立研究库、不改模型/策略/安全 edge；测试待完成。
- 增加合成请求限速/重试/非法参数/事后数据隔离测试及无服务前端渲染脚本；粗精度仅通过独立展示字段标记，不改变既有 quality_status、成交门控或评分。全部验证仅使用模拟响应和临时数据库，不启动测试服务。
- 首轮 85 项相关 Python 测试、Python 编译、无服务前端语法/唯一 DOM id/合成渲染和 git diff --check 通过（既有换行提示及模拟 HTTPError 清理警告）；继续补充历史同步默认限速与 DTO 不改变安全 edge、评分和动作的回归验证。
- 最终全套 python -m unittest discover：174 项通过；新增 DTO 回归确认事后展示价格不改变安全 edge、市场评分或持仓动作。Python 编译及 node test_history_price_display.cjs 通过；未启动浏览器/测试 HTTP 服务，因此未声称本轮桌面/移动真实页面验收。磁盘变更未部署到服务，启动控制台 EXE 未重建。
- 交接快照、QA-027和运行说明已同步本次实际数字、限速配置及未完成边界，旧历史记录保留；修正交接文字错字，不涉及程序或数据变更。
- 本轮实际文件：新增 audit_history_coverage.py、test_history_price_display.cjs；增量修改 polymarket_pipeline.py、live_dashboard_server.py、forecast_dashboard.html、test_history_price_window.py、test_polymarket_pipeline.py 及维护/交接/Q&A/运行文档。当前缺口未补采；后续需明确允许外联后小范围核对参数、源响应、缓存命中及耗时，再决定补采。不能用当前盘口补历史，也不将独立小时导出当作订单簿收益模拟。
- 本阶段未访问外部接口、未写数据库/CSV/模型、未重训或运行回放，model-update.txt 未修改，do_not_auto_trade=true。

## M-20260921-HISTORY-DOCUMENTATION-EVIDENCE

- 回答核验提示和小时采样来源。沙箱DNS失败后按环境要求申请外联，实际读取官方market-data/prices-order-books.md，确认Price History/API含bucket_seconds=3600示例、60至86400秒参数范围、显式范围最多15天，以及指定精度空/稀疏时省略精度的说明。详细位置与文档口径限制见QA-026。
- 核对polymarket_pipeline.py：核验为无未恢复请求错误，空响应也可缓存6小时，不代表数据完整或永久缺失。一天/半天分段及缓存期限是项目选择。当前用户具体token响应与运行版本未核对，不把旧实测当作本次空数据的根因。
- 本轮只修改Q&A.txt、MAINTENANCE-CH.md和CONTINUATION-CONTEXT-CH.md；没有修改程序、生产补采、写生产库或重启服务。文档变更未重复运行程序测试。

## M-20260921-DAILY-HISTORY-REQUESTS

- 用户要求5分钟采样、按一天/半天多次请求，并核对每日数据及市场标签。取消history_prices.py将每日缺口合并为7天的逻辑；保留HTTP默认最多3次尝试，范围过大/超时/断流失败时将超过12小时的窗口拆为半天及剩余片段，最多一层。自动精度回退同样适用；限流、权限、DNS等不拆分。成功子片段独立缓存，合法记录及父失败审计保留，已恢复父错误不计最终失败。
- 核对现有表定义：market_prices按(event_slug,market_id,token_id,price_time_utc)保存，不是每天单独文件；posts按post_id去重，没有直接的市场标签，count_samples保留tracking_id及时间窗口。本轮不迁移表、不把共享帖子复制进各市场。
- 实际修改：history_prices.py、test_history_price_window.py、RUNBOOK-CH.md、Logic.txt、MAINTENANCE-CH.md、CONTINUATION-CONTEXT-CH.md、Q&A.txt。原有1小时默认/5分钟选项不变，不插值、不改交易算法。
- 验证：14项历史价格专项及全套169项Python测试通过，覆盖整天失败半天恢复、自动精度补查、失败片段不缓存、成功片段保留、尾部覆盖、限流/权限/DNS不拆分。全套有HTTPError资源清理警告，不影响断言结果。全部为合成响应/临时数据库；没有真实接口速度或覆盖率验收。
- 边界：保留既有修改；未外联、未运行真实补采、未写生产库、未启动/终止/重启用户服务；运行进程版本未确认，需重载后端才能应用。EXE未重建。此前真实历史缺口没有因本轮代码修改而自动补齐。

## M-20260920-HOLD-SUMMARY-DISPLAY

- 用户先报告9月11–18日“没有检查点”，后优先要求修复全历史持有统计。只读核对8765/8778该市场均有2015个检查点，尚未取得当时完整日期参数，不能确定原始空结果唯一原因。
- forecast_dashboard.html新增独立“统计全部历史市场”按钮，共用单盘参数校验，不依赖当前单盘成功或响应run_config；目录去重后先展示全部待统计行，再逐市场更新，单盘失败保留并继续，按slug匹配结果。未成交/未结算/失败不显示0收益，明确数量、未成交原因、实际建仓顺延，保持揭晓锁。
- live_dashboard_server.py跳过无安全盘口节点的无用价格查询，补充未成交诊断与建仓延迟。65市场只读同条件（建仓60分钟、覆盖30%、资金100、持有）优化前17.706秒、后2.68秒，均9成交/56未成交；56项均带原因。没有把小时价格假设收益冒充订单簿成交。
- 保留并完成检查点修复：切市场清除旧日期范围、默认无日期截止，新增“完整市场范围”；后端区分市场不存在/未结束/日期不重叠/无有效检查点。MultiLiveService无实时子服务时通过history_replay.local_review读取本地模型与只读库，不再直接返回空回放，不迁移数据库。
- 实际修改forecast_dashboard.html、live_dashboard_server.py、history_replay.py、test_history_workspace.cjs、test_polymarket_pipeline.py、test_history_replay.py及维护/交接/Q&A/RUNBOOK。166项Python通过，合成浏览器覆盖单盘空但全历史正常、单市场失败不阻塞、独立重试、完整范围恢复及既有桌面移动流程；git diff --check待收尾核对。新测试首次因临时SQLite连接未关闭失败，已修复并重跑通过。
- 收尾：git diff --check通过（仅既有换行提示）；只读GET确认8765实际提供的新HTML包含独立统计和完整范围按钮。未外联、未生产采集、未写生产库、未重启或结束用户服务，EXE未重建。页面刷新可载入新按钮；后端诊断与查询优化需重载后端后生效。

## M-20260920-HOURLY-V2-BACKFILL

- 实际补采完成：首轮67市场/1846 token，3420个应用层请求，908秒，返回145964条源记录；2个连接中断。定向重试保存216条小时记录，但自动精度补查TLS超时，后续缩小缺口重试遇DNS失败，未宣称全部请求成功。最终离线重导出305544节点、72956可用（28市场）、232588缺失，69130有结算毛收益。35241节点有同刻记录、37715使用此前1小时内观测；46401可用节点来源v2、26555来自精度未知的旧归档。因用户旧抓取仍运行且当前市场继续推进，最终节点数与初始基线略不同，不能把所有增量全归因于本轮补采。
- 交付：data/hourly_research/hourly_prices.csv（70680426字节）、hourly_coverage.csv、summary.json、source_prices.sqlite、requests.jsonl、retry_requests.jsonl及README-CH.md；网络首轮摘要另存backfill_summary.json，离线重导出不覆盖网络审计。全部真实小时缺口未恢复，用户未选择估算版，未进行插值或跨小时粗价格伪装。
- 验证：165项Python全套通过（最终工具输出核对），合成浏览器验证1h/5m选项、稀疏/失败区别、进度/禁用态及既有移动桌面回归通过，编译及git diff --check通过。逐行检查CSV无未来时间、价格年龄>1h、源精度>1h冒充小时或毛收益公式错误。临时库测试断言生产输入文件字节不变、已有请求审计不覆盖、分页/坏数据/缓存/零价与结算公式正确。

- 用户先要求质量/速度修复，随后明确优先1小时研究数据。恢复协议并检查既有修改；只读近期归档发现多个partial均仅clob_history_coverage，非HTTP错误。读生产库曾遇锁，未停止用户进行中的抓取。
- 真实外联核实官方market-data/prices-order-books及用户提供concepts/prices-orderbook。新v2支持分页及不同精度保留；同token相同一天旧接口5m/1m和新接口显式1h均空，自动精度有289条（含结束边界）；无效token负例全部空。保留文档与响应副本，不能从文档推断每市场完整。
- 实际修改：新增history_prices.py、hourly_price_research.py、audit_history_source.py；修改polymarket_pipeline.py、live_dashboard_server.py、forecast_dashboard.html、test_history_price_window.py、test_polymarket_pipeline.py、test_history_workspace.cjs；同步RUNBOOK-CH.md、README-CH.md、Logic.txt、Q&A.txt及交接。
- 新采集采用v2分页/源精度回退，增量缺口与6小时成功核验缓存，错误不缓存，强制重查可选；保留已成功页、拒绝畸形记录及重复游标；请求审计含响应哈希与耗时。价格分500条小事务写入，页面每盘口更新进度；稀疏/空源记录与请求失败分开，固定报告保护保留。
- 独立小时导出取节点前<=1小时且源精度<=1小时观测，未知旧精度明确标注，零价/无结算不计算收益；不按模型选盘、不计费用/深度、不伪造缺失。用户对粗精度是否另做延用估算尚未回答，默认不估算。
- 生产库仅读；真实补采写独立目录data/hourly_research，初始只读基线data/hourly_research_before：67市场、1846 token、305492个小时-盘口节点，26818可用、278674缺失。最终补采结果后附。未重启/结束生产进程、未重建EXE、未训练模型。

## M-20260920-HISTORY-EXACT-WINDOW

- 最终验证：python -m unittest discover共158项通过；node test_history_workspace.cjs合成浏览器回归通过；git diff --check通过（仅既有换行提示）。Python有既存HTTPError mock清理ResourceWarning，无测试失败，不代表访问真实接口。

- 用户要求修复历史抓取不完整。已核实旧默认include_prices=false及相对interval=1w不能保证请求旧市场实际区间，缺历史价格也未判partial。
- polymarket_pipeline.py：按真实市场开始至min(结束,采集时间)使用startTs/endTs按天请求；保留成功分块，记录错误；保存后统计已有与新增数据的5分钟桶覆盖，空/部分覆盖明确上报，不插值。删除不能修复历史且未保存的当前批量报价请求。
- live_dashboard_server.py：默认开启价格采集，返回逐市场覆盖及错误；部分失败不再覆盖固定报告。forecast_dashboard.html：固定完整时间范围、默认勾选价格、展开全部覆盖明细，返回数量与不完整数量分开显示。
- 测试文件test_history_price_window.py新增历史窗口、分块失败、空/畸形响应、临时SQLite重复补采覆盖验证；test_polymarket_pipeline.py增加部分数据报告保护，并更新默认采集参数断言。同步RUNBOOK-CH.md、README-CH.md、Logic.txt、Q&A.txt及交接快照。
- 未访问真实接口、未补采生产库、未启动或重启服务、未重建EXE。历史价格补采不等于历史订单簿恢复，也不证明所有市场可成交；上游稀疏采样同样会显示覆盖不足，并不一律代表接口请求失败。

## M-20260920-NO-FILL-AUDIT

- 用户追问仅9市场成交。只读重算65个已结束市场（开盘后0分钟、30%概率覆盖、100份、持有结算）：9个成交且已结算，56个未成交。其中49个完全无订单簿，7个仅在市场结束数月后才采到1–2条盘口，均不可用于历史成交。
- 市场区间内价格表覆盖也不足：仅10个市场有价格记录，55个没有；有记录不代表指定节点或所选档具备完整价格。不存在单纯放宽概率覆盖就能修好的56个拒绝。
- 已成交市场部分实际建仓比指定时间晚数天，例如8月28日市场到9月1日23:35才成交；现有顺延口径不能等同严格同一时长入场收益。建议将严格节点与顺延样本分列，另设明确标注的历史价格收益研究模式，不冒充盘口执行。
- 未改模拟逻辑、未采集/外联/写生产数据；后续补价格需验证源历史覆盖，当前订单簿接口不能恢复历史深度。

## M-20260920-FRACTIONAL-COVERAGE-HISTORY

- 用户明确变更强制建仓资金分配：目标为绝对概率覆盖C，中心消耗min(p,C)，余量向左右相邻档成对扩展，每层按概率比分剩余量，预算权重=该档消耗概率/C。取消首档最低一份和正edge分配，legacy不变。35%中心/30%覆盖全投中心；25%中心、21%/14%邻档时权重为83.333%/10%/6.667%。支持范围、盘口安全和深度限制保留，不足概率或不可交易档预算留现金。
- 新增仅强制建仓+持有至结算可用的paper_hold_summary，只计算首次成交所需节点，再按官方结果兑付；拒绝带日期过滤。前端读取完成后按同一建仓时长/覆盖/资金参数顺序统计所有本地已结束市场，不套用单市场日期范围，显示进度、取消、失败/未成交/未结算、投入和结算收益率，结算数字受揭晓锁控制。
- 实际修改strategy_simulator.py、live_dashboard_server.py、forecast_dashboard.html、test_mandatory_paper.py、test_polymarket_pipeline.py、test_history_workspace.cjs及本日志、Logic.txt、RUNBOOK-CH.md、Q&A.txt、CONTINUATION-CONTEXT-CH.md。
- 验证：152项Python测试通过；合成浏览器验证全市场参数、日期隔离、揭晓、未结算排除和清空通过。真实SQLite mode=ro遍历65个已结束市场（开盘0分钟、30%、100份、持有），3.4秒无异常，9个有效已成交且已结算；总投入765.97552667，总盈亏-309.77114711，加权投入回报率-40.44138962%。不是所有市场都有可执行历史数据。
- 未联网、未写生产库、未重训、未重启生产或旧只读预览；新后端需重载后生效。

## M-20260920-HISTORY-CRAWL-FAILURE

- 修复市场发现失败退回日期拼接slug造成批量404：UI明确返回发现异常，CLI仅显式no-discover才拼接。Gamma系列列表空或400/404/422时回退通用列表并继续同一种分页，仍严格筛选Elon发帖市场。
- 所有市场失败不再标为成功且不覆盖报告；部分失败标partial。新增发现失败与全失败报告保护回归测试。
- 只读数据库最近12条归档均complete，尚未取得用户当前“全部失败”的具体异常，不能声称已定位实际线上根因。未外联、未抓取生产库、未重启服务。验证见后续补充。
- 验证：151项Python测试通过，包含新增两项回归；线上抓取尚未验证。

## M-20260920-DATA-COVERAGE

- 排查“未采集”显示：9月8–15市场有165132条订单簿但价格表仅23759条且从9月11日开始；历史页原先只看价格表，导致已有安全盘口被误报未采集。9月11–18市场价格43730条、盘口6898条，问题同样存在。
- 修改数据兼容：价格历史接受`t/ timestamp`与`p/ price`字段，批量价格响应兼容token顶层映射；市场outcomes按明确YES/NO名称映射token，旧无token归档保持兼容；订单簿校验请求token，过滤非有限数值和零深度档位；历史价格查询只取YES token。
- 修改回放DTO：当五分钟历史价格缺失但安全盘口存在时，增加display-only `archived_book_midpoint`，页面显示“盘口中间价代理”，不进入模型输入、成交价格或纸面交易；已有盘口但双边/深度无效显示独立状态。
- 验证：149项Python测试通过，合成Chrome回归通过，py_compile与git diff --check通过。未重新抓取、未访问外部接口、未写生产SQLite，生产采集未重启。

## M-20260920-FILLED-HISTORY

- 用户要求禁用止盈止损导致的交易驳回不要进入历史成立交易。forecast_dashboard.html历史表仅保留非零实际成交及已揭晓结算；筛选前paper_prior_actions同样过滤，零成交/持有仍在当前节点状态中显示，后端审计数据保留。
- 更新表标题、计数与空状态；test_history_workspace.cjs增加未成交不进入历史表断言。同步交接与问答。未改交易算法、未操作生产服务或数据。
- 验证：node test_history_workspace.cjs通过，包含成交过滤、结算揭晓和桌面/移动端回归。

## M-20260920-ELAPSED-HOLD

- 用户澄清建仓时间为市场已进行多久；改为小时+分钟输入，跨市场保留相对偏移，服务端按市场起点换算并在之后首个五分钟节点尝试，缺盘口继续顺延，等待时间超出市场拒绝。
- 新增paper_exit_policy=hold_to_settlement：首次建仓后仓位冻结，不止盈、不止损、不按edge减仓或增持，仅官方到期兑付；managed默认及legacy保持原行为。不是仅关闭止损。
- 修改strategy_simulator.py、live_dashboard_server.py、forecast_dashboard.html及三个相关测试；同步Logic、RUNBOOK、Q&A和交接。合成浏览器通过；Python148项通过。未外联、未写生产库、未启动生产采集；重叠市场概率迁移仅分析，未接入模型。

## M-20260920-MANDATORY-PAPER

- 在现有未提交的部分强制建仓实现上完成接入和修正；保留其他用户修改。实际修改 strategy_simulator.py、live_dashboard_server.py、forecast_dashboard.html、test_mandatory_paper.py（新增）、test_polymarket_pipeline.py、test_history_workspace.cjs，以及 Logic.txt、RUNBOOK-CH.md、Q&A.txt、CONTINUATION-CONTEXT-CH.md 和本日志。
- 支持UTC首次建仓节点、80%连续概率覆盖、100份资金、逐档20%止盈/15%止损；指定前不交易，缺盘口顺延，远端不支持档不选。API兼容模式不受新退出规则影响。时间/深度/现金安全不跳过，未接入v2逐档引擎。
- 逐笔净价格、费用、现金流、本笔/累计盈亏和触发阈值；新增资金摘要、成交导航、最近结束市场默认选择、完整官方结算与揭晓锁。缺官方结果不结算，截断不宣称市场结束。
- 验证：147项Python测试通过（包含新单测及日期过滤/顺延/无盘口/截断集成断言）；合成Chrome浏览器回归通过，覆盖首笔定位、时间控件、结算揭晓、桌面1440与移动390宽度无溢出。既有HTTPError mock有ResourceWarning，测试无失败。
- 两个真实市场已用SQLite mode=ro复放；9月8–15日强制模式15条非零动作，9月11–18日11条；后者缺官方结算，不给虚假最终收益。只读预览启动于127.0.0.1:8778，未启动生产采集、访问外部接口、修改生产SQLite/CSV/模型；未重建EXE、未重启生产服务。
- 真实只读Chrome验证通过：9月11–18日→9月8–15日→9月11–18日，分别34.3/19.0/32.7秒；取消、纸面成交、1440/390桌面移动检查通过。git diff --check通过（仅既有换行提示）。当前仍为汇总深度执行假设，标准化资金不是实际下单。

## M-20260919-LAUNCHER-EXE-DELIVERY

- 实际修改：`dashboard_launcher.py` 新增 `/api/restart`，仅重启当前控制台持有且仍在运行的后端子进程，并复用上次启动配置；未知或未托管进程不会被扫描或终止。`dashboard_launcher.html` 新增“重启后端服务”按钮及状态联动；`test_dashboard_launcher.py` 新增重启配置复用、未知进程拒绝和按钮状态测试。
- 构建结果：使用现有 `build_dashboard_launcher.ps1` 和 PyInstaller 6.22.2 重新构建成功，产物为 `dist_dashboard_launcher/AIPRP-DashboardLauncher.exe`，大小 10,130,469 字节，时间 2026-09-19 23:16:48。
- 验证：控制台专项测试 8 项通过；Python 编译、前端 JavaScript 语法检查和 `git diff --check` 完成。冻结 EXE 在隔离端口启动成功，`/api/status` 返回 `not_started`，无托管后端时 `/api/restart` 返回明确错误；测试进程已结束。三项生命周期操作以合成/Mock 子进程验证，未启动真实后端。
- 边界：8760、8765、8778 当前无监听；两个 Python 进程命令行归属无法确认，未终止。未访问外部接口，未启动生产采集，未写生产 SQLite/CSV/模型，未修改历史回放算法，`do_not_auto_trade=true` 保持。

## M-20260919-HANDOFF-LAUNCHER-EXE

- 交接记录：用户要求将本地启动控制台打包为 Windows 单文件 `.exe`，保留现有端口和命令，并提供一键结束当前服务、一键重启后端、一键启动前端。
- 当前审计发现目录中已存在 `dashboard_launcher.py`、`dashboard_launcher.html`、`AIPRP-DashboardLauncher.spec`、`build_dashboard_launcher.ps1` 和 `start_dashboard_launcher.ps1`。现有控制台可启动/停止由本实例管理的后端、查询健康状态并打开前端页面；是否已生成可用 `.exe`、重启按钮是否完整、冻结模式下项目根目录定位是否可用，均未在本阶段确认。
- 下一阶段验收：构建 `dist_dashboard_launcher/AIPRP-DashboardLauncher.exe`；启动控制台后验证停止当前服务、重启后端、启动/打开前端、状态和错误提示；确认不会终止不属于本实例的生产进程，`do_not_auto_trade=true` 保持。
- 本阶段仅更新交接文档，未构建、启动或终止服务，未访问外部接口，未写数据库/CSV/模型，未重训或生产回放。

## M-20260919-HANDOFF-CHECK

- 用户明确要求本阶段只维护持续开发协议和例行文件，不继续实现启动控制台功能。
- 已核对协议、交接快照、维护日志和 QA-012；协议中的单文件 exe 待开发目标及下一阶段验收条件均已写入。
- 进程核对结果：8760、8765、8778 无监听端口；发现两个 Python 进程，但由于当前权限无法读取命令行，未将其误认作项目服务，也未终止它们。没有执行重启、生产回放、外部访问或数据写入。

## M-20260916-LOGIC-READABILITY

- 改进 `Logic.txt` 全文组织：每个算法单独说明用途、局部变量、单位、计算步骤、数值例子及输出去向；中文解释优先，代码字段用于定位。
- 对照代码纠正文档：Brier 为全区间误差之和，换手上限按 outcome 应用；补充目标预算与现金约束的区别、概率分布降级、模型标准化和集成计算、实时与回测方差差异。
- 仅修改说明文档和本维护记录，保留既有程序及用户修改；未访问外部接口、启动服务、写入数据库/CSV/模型、重训或运行回放，`model-update.txt` 未修改，`do_not_auto_trade=true` 保持。

## M-20260916-LOGIC-DATA-TO-RESULTS

- 新增 `Logic.txt`，按当前实现说明从本地数据、时间门控、特征、固定模型、负二项概率、盘口成本、纸面持仓模拟、回测评分到 API/前端显示的完整逻辑链路。
- 文档明确历史数据防泄漏规则、缺失与 audit_only 的区别、纸面 PnL 的计算方式及永久 `do_not_auto_trade=true` 边界。
- 本轮仅新增说明文档和维护日志；未访问外部接口，未启动服务，未写入数据库/CSV/模型，未重训或运行生产回放。

## M-20260915-STRATEGY-ISOLATION-BUDGET-ADAPTER

- 实际改动：在 `strategy_simulator.py` 集中定义纸面策略默认参数（建仓检查点、edge 阈值、费用与滑点），函数默认值统一引用这些常量；`live_dashboard_server.py` 的方法论输出改为引用同一组常量。
- 接口适配：`/api/backtest-review` 现在转发 `paper_market_budget` 查询参数，使多 outcome 共用市场总预算可由回测调用方显式配置。
- 测试：增加策略默认值集中化回归测试；既有策略行为、数据时间边界和服务兼容导出保持不变。
- 安全与数据：`do_not_auto_trade=true` 保持；未访问外部接口，未启动服务，未写入 SQLite/CSV/模型，未重训或运行生产回放，`model-update.txt` 未修改。

## M-20260913-UNIFIED-REVIEW-ORDERBOOK-OPTIN

- 实际改动：历史同步增加显式 `include_orderbooks` 选项；默认仍关闭，开启时只从任务启动后追加当前可获得的未来订单簿快照，绝不恢复或冒充历史盘口。任务状态和结果增加订单簿采集说明与新增行数。
- 前端：主工作区移除“竞猜模式/复盘模式”双按钮，保留单一“历史回测与复盘”时间轴；历史同步增加“从现在起追加快照（不恢复历史）”勾选项，状态文案明确采集边界。回测汇总、逐检查点表、详情和校准中的 Brier/Log Loss 及派生差值在揭晓前统一显示“揭晓后显示”，点击揭晓后同步刷新。
- 文档：`RUNBOOK-CH.md` 补充该选项的 API/数据边界说明，强调订单簿只从任务启动后追加，不能恢复过去盘口。
- 测试：补充订单簿采集显式开关和前端统一工作区/重复 id/揭晓前隐藏评分回归测试；未启动服务、未访问外部接口、未抓取历史、未训练或回放模型。
- DTO 实测：使用生产 SQLite 临时副本直接复放 `elon-musk-of-tweets-august-28-september-4-2026`，5 分钟检查点 2015/2015、`truncated=false`，首点 `2026-08-28T16:05Z`、末点 `2026-09-04T15:55Z`；26 个 outcome 全部保留，21 个有历史价格，订单簿因来源时间缺失仅审计且成本后 edge 全为空。
- 数据与安全：未迁移、删除或覆盖 SQLite/CSV/模型；`do_not_auto_trade=true` 保持不变，无交易、下注、钱包、私钥、签名或下单 API。`model-update.txt` 未修改。

## M-20260828-LIVE-SNAPSHOT-VERIFY

- 分支与工作区审计：在 `model-v6`（`7a1dd15`）的既有 dirty 工作区完成只读核对后，创建 `model-v6-live-snapshot-verify-20260828`；`data-trans` 仍仅指向同一提交，不包含任何工作区未提交改动。未执行 reset、restore、clean、删除或覆盖操作。
- 差异归类：`README.md` 与未跟踪的 `README-CH.md` 是项目说明补充；`forecast_dashboard.html` 是既有全局残差图的坐标和悬停细节调整，未新增归档浏览入口。实时归档改动集中在 `polymarket_pipeline.py`、`live_dashboard_server.py`、`test_polymarket_pipeline.py` 及本日志/运行手册。
- SQLite 只读核对：通过 `mode=ro` URI 确认 `live_market_snapshots` 已存在；检查时表内已有 7,865 行。现有表、既有帖子、订单簿、纸面预测、CSV 和模型文件均未被迁移脚本回填、删除或覆盖。
- 本地服务验证：确认未发现 8765 监听后，仅启动一个 8765 实例。健康接口返回 `do_not_auto_trade=true`。启动脚本选择的 `elon-musk-of-tweets-august-25-september-1` 与 `elon-musk-of-tweets-august-21-august-28` 均收到 Gamma HTTP 404，服务按 slug 分别追加 `refresh_status=failed` 的最小快照并保持“不交易”；`/api/market-archive` 可读到各自 `live_snapshot` 记录、稳定字段和关闭状态。验证结束后已停止该实例，避免继续写入重复失败快照。
- 旧数据兼容：无新归档行的 `elon-musk-of-tweets-august-11-august-18` 通过同一只读 API 返回 `capture_origin=legacy_paper_forecast`、`refresh_status=legacy`，新闻、盘口结果、来源和航班上下文字段保持可读的稳定结构。
- 回归验证：`python -m unittest -q .\test_polymarket_pipeline.py .\test_news_event_crawler.py .\test_ai_provider.py` 通过 41 项；`polymarket_pipeline.py`、`live_dashboard_server.py`、`musk_tracker.py` 编译通过；按 UTF-8 提取的前端脚本通过 `node --check`。未重训、未运行回放、未生成或覆盖报告，因此未更新 `model-update.txt`，也没有新的模型或盈利结论。

## M-20260829-CRAWLER-ARCHIVE-V1

- 目标：在既有爬虫和 SQLite 基础上提高 Gamma 市场发现、CLOB 采集一致性，并建立前端可适配的统一预测历史归档；未删除、覆盖或回退任何用户文件、原始表、CSV 或模型。
- Gamma：增加公开 `/events` 列表发现，按 Elon Musk + tweet/post 文本和时间窗口筛选真实事件；日期生成 slug 仅作发现失败时的回退。`market_events` 幂等增加关闭/结算时间、active/closed/accepting_orders、流动性、成交量、未平仓量、tags/series/status JSON 字段；原始 Gamma JSON 始终保留。
- CLOB：增加公开批量 `/books` 和 `/prices` 请求，`crawl` 默认启用，可用 `--no-batch-clob` 关闭；批量请求只降低请求次数和同一采样内部时间差。`market_orderbooks` 摘要补充 `depth_imbalance`、最近成交价/方向；历史完整订单簿仍写入 `raw_json`，不把当前盘口伪装为历史盘口。
- 公开活动：增加 `market_trades` 和 `market_activity_snapshots` 追加表及 `--public-trades` 显式选项。由于本轮只执行既有历史预测回填，未调用 Data API，当前两表均为 0 行；成交活动尚未进入模型特征。
- 统一归档：增加 append-only `prediction_archive` 表、`prediction_archive_v1` JSON 结构和只读 `GET /api/prediction-archive`。已将既有 `paper_forecasts` 15,386 条记录幂等索引，覆盖 4 个 slug；每条记录保留来源表/ID、数据时间、采集时间、预测字段、风险原因和原始 snapshot，并固定 `do_not_auto_trade=true`。
- 低频边界：运行手册增加约 1 小时采集示例（`RefreshSeconds=3600`）；记录 data time/capture time/状态，缺失采样不当作 0 条发帖。未新增 WebSocket、航班、交易、付费或需授权 API。
- 验证：全套 43 项单元测试通过；`polymarket_pipeline.py`、`live_dashboard_server.py`、`musk_tracker.py` 编译通过；`git diff --check` 无空白错误。未训练或回放，模型指标无变化，无盈利结论。

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

## M-20260819-LIVE-SNAPSHOT-ARCHIVE

- 操作目的：解决市场结束后实时盘口与关联上下文无法稳定复盘的问题，将每次实时刷新追加归档到本地 SQLite，不覆盖任何既有市场、帖子、盘口、纸面记录、CSV 或模型文件。
- 代码变更：`MarketStore.migrate` 新增幂等 `live_market_snapshots` 表和按市场/时间索引；实时服务每轮保存累计发帖数、预测、盘口摘要、新闻、股价、已审核事件、来源延迟/错误、外部上下文和完整结构化快照。完整 CLOB 原始订单簿仍由 `market_orderbooks.raw_json` 逐 token 保存，逐帖原始记录仍位于 `posts`。Gamma 失败及后台刷新失败也写入 `refresh_status=failed` 的最小可读归档。
- 兼容性：`GET /api/market-archive?slug=...&limit=...` 只读返回统一归档结构；没有新表记录的往期市场从 `paper_forecasts.snapshot_json` 兼容读取并标记 `legacy_paper_forecast`。新闻、盘口结果、事件与航班字段使用稳定空列表/状态对象，避免旧数据缺字段导致读取失败。
- 数据范围：本机只读核对发现 `extended.sqlite` 已有 55 个市场、164855 条订单簿快照、7451 条纸面预测、6388 条帖子和 490 条来源文档；本轮未重训或替换模型，未改动既有数据库文件内容。
- 外部数据边界：仓库当前没有航班来源配置，因此仅保留 `flight.status=not_configured` 的可扩展上下文；未新增外部 API、未产生费用，后续接入前必须先确认条款、费用与隐私。自动交易、自动下注、钱包和真实资金操作继续永久关闭。

## M-20260819-GLOBAL-RESIDUAL-AXES-TOOLTIP

- 操作目的：使全局结算预测残差图可定位具体历史市场，避免只显示首尾日期而无法判断折线点对应盘。
- 前端变更：Canvas 新增横轴最终回放日期刻度、纵轴残差刻度与坐标轴；鼠标悬停绘图区会突出最近市场并显示完整 slug、最终回放时点、相对本地最终观测残差和相对官方区间中点代理残差。没有可靠官方中点时明确显示不可用。
- 答案隔离与数据口径：交互仍只在私有结算已显式载入并点击揭晓后才绘制；横轴采用各市场最后一个回放 checkpoint，不把它伪装为精确官方结算时刻。官方区间中点仍是代理，本地最终观测仍可能遗漏历史删除帖文。
- 指标变化：未重训、未校准、未修改模型概率、纸面交易或自动交易状态；图形可读性改进不构成准确率、收益或盈利能力证明。

## M-20260818-GLOBAL-SETTLEMENT-RESIDUAL-CHART

- 操作目的：在保留单市场逐小时残差图的基础上，增加全部历史已结算市场的市场级残差总览，观察最终预测误差是否随结算时间、市场批次或样本顺序发生变化。
- 文件变更：`forecast_dashboard.html` 在整体复盘面板新增“全局结算预测残差”图。每个市场取最后一个回放时点，按结算时间排序；实线为最终预测减去本地最终观测，虚线为最终预测减去官方结算区间中点代理。
- 答案隔离：全局图只有在私有结算文件显式载入并点击“揭晓结算”后绘制；公开 CSV、竞猜模式和实时模式不会读取或显示市场级实际残差。
- 数据口径：市场必须同时拥有官方结算区间、最终模型预测和本地最终观测才进入图表；官方开放尾部区间没有可靠中点，虚线对应点留空。实线的本地最终观测仍可能受历史删除帖文影响，不能自动等同于官方精确结算数。
- 指标变化：无。本轮没有重训、校准或修改正式概率模型；MAE、Brier、LogLoss、命中率和纸面收益不因新增图表而改变，也不能据此声称模型盈利。
- M-20260830-LAYER-CONTRACTS

  - 实际改动：新增 `forecast_contracts.py`，定义本地特征、模型输出、决策输出和前端显示四个版本化契约；实时服务在保存和返回前生成统一快照，同时保留旧字段兼容。
  - 数据与范围：未抓取外部数据，未修改 SQLite、CSV 或模型文件；未发生数据库迁移。特征快照记录 as_of、来源、质量标记、市场内坐标和日历坐标，航班字段继续为 not_configured。
  - 安全边界：决策契约强制 `do_not_auto_trade=true`，不新增交易、钱包、签名或外部付费 API。
  - 验证：`test_polymarket_pipeline.py` 30 项通过；Python 编译通过；`git diff --check` 通过。待完成项目规定的全套测试和前端 UTF-8 脚本检查。
  - 模型/回放：未改变模型、训练或回放，指标无变化。
## M-20260830-DYNAMIC-LIVE-MARKET-DISCOVERY

- 实际改动：新增只读 `discover-live` Gamma 市场发现命令；`start_live_dashboard.ps1` 未显式指定 slug 时按当前 UTC 时间发现真实 Elon Musk 发帖数市场，不再拼接日期 slug。服务端遇到旧 slug 404 时也会重新发现当前窗口市场并切换 slug。
- 故障核对：8765 原服务健康接口仍可用；日期拼接的两个 slug 被 Gamma 返回 404。当前 Gamma 无未结束目标市场时，不伪造盘口，明确保持“不交易/无法读取当前盘口”。
- 数据与覆盖：未删除、回写或覆盖 SQLite、CSV、模型及历史归档；未发生数据库迁移；没有将旧盘口当作当前盘口。
- 验证：46 项单元测试通过，Python 编译、前端 UTF-8 JavaScript 语法检查和 `git diff --check` 通过。
- 模型/回放：未训练、未回放，指标无变化。

## M-20260831-RUNTIME-MARKET-SET-SYNC

- 实际改动：自动发现模式下，实时服务每轮重新查询 Gamma；即使旧 slug 返回 HTTP 200，只要窗口已结束或尚未开始，也会按当前 UTC 窗口选择真实市场。多市场服务会加入新 slug、停止刷新已结束 slug，并把退役服务保留用于历史归档读取；显式 `-Slug` 历史复盘不启用切换。
- 审计记录：复用 `live_market_snapshots.snapshot_json`，新增 `market_discovery`（查询时间、原因、候选数、选中 slug、窗口）字段，没有新建表或删除/覆盖归档。
- 启动约束：`start_live_dashboard.ps1` 在自动发现模式传递 `--dynamic-discovery`，并在启动前检查目标端口是否已有监听，避免并发服务。
- 实际数据核对：SQLite 只读 `quick_check=ok`、锁模式 `normal`；当前 `live_market_snapshots` 9277 行、`prediction_archive` 15444 行；本轮未抓取当前盘口，未发生数据库迁移。
- 验证：`python -m unittest -q .\\test_polymarket_pipeline.py .\\test_news_event_crawler.py .\\test_ai_provider.py` 49 项通过；Python 编译、前端 UTF-8 JavaScript 检查和 `git diff --check` 通过。Gamma discover-live 本轮在代理下超时，未读取到当前盘口。
- 模型/回放：未改变模型、训练或回放，指标无变化；`do_not_auto_trade=true` 保持不变。

## M-20260902-FIX-CRAWL-ACTIVITY-CALL

- 原因：历史抓取 58 个市场全部失败并非市场逐盘不兼容；`crawl_one()` 按位置传入 `captured_at`，而 `MarketStore.save_market_activity()` 的该参数在关键字参数区（`*`）之后，统一触发 `TypeError`。
- 修复：调用改为 `captured_at_utc=captured_at`；新增临时 SQLite 回归测试验证活动快照写入、采集时间和成交量字段。
- 范围：未修改表结构、既有 SQLite、CSV、模型、训练或回放；之前失败任务已结束，未继续启动抓取。
- 验证：修复后运行全套单元测试、Python 编译、前端脚本语法检查和 `git diff --check`。

## M-20260831-POWERSHELL-ENCODING-FIX

- 故障：Windows PowerShell 5.1 按系统 ANSI 代码页读取无 BOM 的 UTF-8 启动脚本，中文 `throw` 文本被误解析为字符串终止，服务未启动，网页不可访问。
- 修复：`start_live_dashboard.ps1` 的启动错误提示改为 ASCII；未改用 cmd，保留原有参数、Gamma 动态发现、8765 端口检查和 `--dynamic-discovery` 行为。
- 验证：Windows PowerShell 解析器检查通过；全套 49 项单元测试、Python 编译和 `git diff --check` 通过。未启动服务，未写入数据库、CSV 或模型。
- 后续网络故障：用户再次启动时 Python 在 Gamma 分块响应中收到 `http.client.IncompleteRead`，不是 PowerShell 缩进；HTTP 客户端已将该异常纳入有限重试。当前 Gamma 发现命令仍可能因代理超时而无法返回，服务会保持未启动，不伪造盘口。

## M-20260902-AUTO-DISCOVERY-BACKFILL

- 自动识别：动态服务使用独立 Gamma 客户端持续查询最近 8 天至未来 8 天的真实事件；当前服务集合为空时仍继续发现，不依赖旧 slug 或日期拼接。
- 自动补档：发现已结束且本地 `market_events` 没有记录的真实 slug 后，加入顺序补档队列，复用 `crawl_one` 写入 Gamma 元数据、XTracker、可获得的价格/盘口和 `prediction_archive`；每个 slug 6 小时内不重复尝试，失败保留状态。
- 隔离与状态：新 active slug 自动加入实时监控，结束 slug 停止刷新但保留归档服务；`/api/health` 与实时 DTO 增加市场发现/补档状态，前端显示当前市场数量、无市场、发现失败和待补档数量。
- 边界：补档不训练、不生成报告、不删除重叠市场；无法恢复的历史盘口仍标记为来源失败，不以当前盘口冒充历史盘口；`do_not_auto_trade=true` 保持不变。
- 运行态核对：8765 当前健康接口仍返回旧版本 `2026-08-12-ai-assist-v1`，必须停止旧进程并重新启动脚本后才会加载本轮代码；本轮未主动终止用户进程。实时服务在核对期间可能追加写入 SQLite，属于既有服务行为，不是代码迁移。

## M-20260902-ELON-SERIES-DT-DISCOVERY

- 根因：Gamma 全站 `/events` 默认排序会让 Elon 发帖市场埋在大量短周期市场之后，发现命令可能超时或停留在本地旧 slug；不是 PowerShell 缩进，也不是手动修改时间戳。
- 修复：`GammaClient.discover_count_events` 改为查询固定 Elon Tweets 系列 `series_id=10000`，按 `endDate` 倒序返回真实 slug；随后用 `datetime` 的 UTC 窗口严格执行 `start <= now < end`，保留周二/周五重叠市场。
- 实测：2026-09-01T19:13:57Z（北京时间 9 月 2 日）Gamma 返回 `elon-musk-of-tweets-september-1-september-8-2026` 与 `elon-musk-of-tweets-august-28-september-4-2026` 两个当前重叠市场，`do_not_auto_trade=true`。
- 边界：显式 `-Slug` 仍是历史复盘模式；必须不带 `-Slug` 运行启动脚本才能启用自动发现。未训练、未回放、未修改模型；未新增数据库迁移。

## M-20260902-RECONCILE-SETTLEMENT-AND-CLOB-STATUS

- 启动修正：`live_dashboard_server.py --dynamic-discovery` 现在允许省略 `--slug`，服务端直接按当前 UTC `datetime` 窗口发现两个重叠市场；显式单 slug 仍保持历史复盘语义。
- CLOB 状态：已结算子盘口返回 HTTP 404 时标记 `closed_outcome_expected`，前端显示“已结算盘口无可用 CLOB”；未将已结算盘口误报为当前可交易盘口。
- 8 月 25 日结算盘核对：官方区间为 `320-339`，本地严格窗口观测为 `317`。边界（2026-08-18 16:00 UTC 至 2026-08-25 16:00 UTC）正确；差异来自 XTracker 历史缺失/删除帖子不可恢复，不补写虚假帖子，快照新增 `settlement_reconciliation` 明确记录不一致。

## M-20260902-HISTORY-SYNC-DATE-AND-SLUG-FIX

- 实际数据核对：历史任务共 58 个候选，成功 55、失败 3；失败 slug 为 `elon-musk-of-tweets-august-21-august-28`、`elon-musk-of-tweets-august-25-september-1`、`elon-musk-of-tweets-august-28-september-4`，均为 Gamma `HTTP 404 Not Found`。本地 SQLite `quick_check=ok`，已有成功数据写入 `market_activity_snapshots`；不是新信息存储格式不兼容，也不是数据库损坏。
- 根因与修复：历史任务此前直接用日期规则生成无年份 slug，市场身份更新后产生 404。默认历史范围现在先查询 Gamma Elon Tweets 系列并使用真实 slug（包括年份）；仅 Gamma 请求失败时才回退日期生成。空发现结果不再制造一批必然 404 的任务。
- 日期修复：结束日期按服务时区解释且为排他边界；允许比服务时区当前日期晚一天，兼容中国本地日期早于纽约跨日的情况。浏览器默认值改用本地日期，因此中国本地当天无需手动改成前一天。
- 范围与验证：未迁移数据库，未改动 SQLite、CSV 或模型，未训练/回放，未启动并发服务；`do_not_auto_trade=true` 保持不变。新增 Gamma slug 回归测试；全套单元测试、Python 编译、前端 UTF-8 JavaScript 语法检查和 `git diff --check` 均通过。
- 可检索性：致命 Gamma/抓取异常现在也会写入 `prediction_archive` 的 `error`、`error_message`、`failure_reason` 和 `source_errors` 字段；网页历史状态同步显示前 3 个失败 slug 及“市场不存在（Gamma 404）”等原因。

## M-20260902-HISTORY-REVIEW-TIME-DECAY-AUDIT

- 实际改动：新增只读 `history_review_v1` DTO 和 `/api/history-review`。它严格按完整 `slug` 隔离读取本地 `prediction_archive`、`market_prices`、`market_orderbooks` 与 `market_activity_snapshots`；前端新增本地历史归档表和时间衰减敏感性控件，不直接访问外部 API。
- 时间口径：价格同时显示 `source_price_time_utc` 与 `captured_at_utc`。订单簿没有 `source_timestamp_utc` 时显式标为“仅本地归档时间，不能还原历史盘口”；不把当前盘口或本地最终帖数伪装成历史精确答案。
- 时间衰减边界：公式为 `2 ** (-age_days / half_life_days)`；只引用目标市场开始前已经结束的历史市场。未来、重叠、超龄或时间无效参考记录被排除。输出仅为敏感性审计，不重训、不校准、不修改当前预测、决策或纸面交易结论。
- 时间分析准备层：帖子、新闻文档/版本/候选事件与 `external_events` 增加可用时间、时间精度和质量字段；新闻、航班、帖子内容当前仍不是模型特征。航班保持 `not_configured`，未接入航班 API、付费 API、云服务或授权数据源。
- 数据库与运行态：迁移均为 `ALTER TABLE ADD COLUMN` 的增量兼容迁移；本轮没有启动、停止或重启 8765 服务，因此当前运行实例尚未执行这些迁移。未抓取外部数据，未删除、覆盖或回退 SQLite、CSV、模型或既有归档。
- 模型/回放/安全：未改变模型、训练或回放，未更新 `model-update.txt`；`do_not_auto_trade=true` 保持不变，没有下单、钱包、私钥、签名或真实资金操作。
- 参数边界：`half_life_days` 与 `max_age_days` 拒绝非有限数，避免把异常查询参数转换成无意义权重。

## M-20260902-POSTS-TIME-COLUMN-MIGRATION-ORDER

- 启动故障修复：旧 `posts` 表缺少 `available_at_utc` 时，迁移此前会在 `ALTER TABLE ADD COLUMN` 前创建依赖该列的索引，SQLite 因而报 `no such column: available_at_utc`。现在先补齐增量时间列，再创建 `idx_posts_available`。
- 数据边界：只读核对 `data/extended.sqlite` 的 `quick_check=ok`，确认是旧表结构的迁移顺序问题而非数据库损坏；修复仅执行无损的新增列/索引，不删除、覆盖或回退既有 SQLite 数据。
- 验证：59 项单元测试、Python 编译和 `git diff --check` 通过；未启动服务、未抓取、未训练或回放，`do_not_auto_trade=true` 不变，未更新 `model-update.txt`。

## M-20260902-BACKTEST-REVIEW-POINT-IN-TIME

- 回测接口：新增只读 `/api/backtest-review` 与 `backtest_review_v1` DTO；前端新增历史回测记录、5 分钟步长选择、滑动检查点和选中记录详情。
- 时间边界：检查点只使用当时已经可获取的帖子（`available_at_utc`）；价格同时限制源时间与本地 `captured_at_utc`，订单簿按检查点前每个 token 的最新本地快照读取，避免把后来归档的数据带入过去。
- 记录内容：输出模型区间概率、市场隐含概率、best bid/ask、spread、depth、价格/盘口年龄、edge、质量标记、决策状态、训练窗口及结算后 Brier/Log Loss；完整 slug 隔离周二/周五重叠市场。
- 方法声明：当前是固定模型的时间点复放，不是 walk-forward；时间衰减不修改模型参数，Brier/Log Loss 与纸面 edge 不能作为盈利证明。未抓取、未重训、未回放生产数据，未更新 `model-update.txt`。
- 运行与安全：未启动或停止 8765，未执行生产数据库迁移；回测只读本地已归档盘口/价格，不读取当前外部盘口。`do_not_auto_trade=true` 保持不变，无交易、下注、钱包、私钥、签名或下单 API。
- 文档：`RUNBOOK-CH.md` 与 `README-CH.md` 补充 `/api/backtest-review` 参数、5 分钟默认步长、滑动检查点和固定模型复放口径。

## M-20260902-BACKTEST-EVENT-AVAILABILITY-GATE

- 时间点修复：回测特征读取 `external_events` 时，优先以 `available_at_utc`、缺失时以 `occurred_at_utc` 作为可用时间门控；事件只有在检查点前可用才会进入特征，避免后来归档事件提前泄漏。
- 范围：仅调整本地查询条件，保留完整事件原始记录；未删除、覆盖或迁移生产 SQLite、CSV、模型，未改变模型参数、训练、回放或交易开关。
- 验证：全套 61 项单元测试与 Python 编译检查通过；未抓取外部数据，未启动或停止 8765，`do_not_auto_trade=true` 保持不变。

## M-20260905-BACKTEST-RUN-IDENTITY-AND-COSTS

- 实际改动：`backtest_review_v1` 为每次只读本地回测输出确定性的 `run_config`、`backtest_run_id`、模型 JSON 的 SHA-256 与检查点 `record_id`；市场级汇总新增 Brier、Log Loss 和成本后净 edge。前端显示运行身份、模型哈希、纸面成本假设、净 edge、模拟成交价、手续费、滑点和预估可填充比例。
- 纸面执行边界：净 edge = 模型区间概率 - best ask - 固定滑点 - 以 ask 计的固定费率；候选还要求价差、深度和 100% 预估可填充比例满足门槛。它只用于检查本地历史快照下的可执行性，绝不是实际成交、实际手续费、盈利或套利证明。
- 数据边界：只读取已有 SQLite 归档的价格与订单簿；未抓取 Gamma、CLOB、XTracker、RSS 或当前盘口，未写入生产 SQLite、CSV 或模型文件，未做数据库迁移。
- 模型与安全：固定已加载模型复放，未训练、重训、校准或生产回放；`do_not_auto_trade=true` 不变，无交易、下注、钱包、私钥、签名或下单 API。未更新 `model-update.txt`。
- 回归保护：新增临时 FakeStore 回归测试，验证相同配置的运行 ID 稳定、成本后净 edge 小于毛 edge、深度不足不成为纸面候选；同时修复市场级评分汇总遗漏 `mean` 依赖导致回测运行失败的问题。

## M-20260906-BACKTEST-MARKET-WEIGHTED-CALIBRATION

- 实际改动：回测 DTO 新增按市场等权的 Brier/Log Loss 汇总，以及按模型选中区间概率分桶的校准诊断（预测概率、实际命中率、绝对校准差距、样本数）。长市场不再仅凭检查点数量主导总体评分；空概率桶也保留以便审计。
- 解释边界：校准仅使用本地存在结算区间的检查点，命中定义为模型选中区间等于本地记录的已解析区间；它用于检查概率惯性/过度自信，不是重新校准模型，也不是盈利证明。
- 前端：回测区显示已结算检查点数量和非空概率桶的预测/命中/差距；继续显示 run id、模型哈希和纸面成本说明。
- 范围与验证：仅修改回测计算、前端 DTO 展示和临时 SQLite/FakeStore 回归测试；未写生产 SQLite、CSV 或模型，未抓取外部数据，未启动 8765，未训练、重训或生产回放，`do_not_auto_trade=true` 保持不变。

## M-20260906-LOCAL-LAUNCHER-CONSOLE

- 实际改动：新增 `dashboard_launcher.py` 与 `dashboard_launcher.html`，提供绑定 `127.0.0.1` 的本地启动控制台。用户可通过网页选择自动发现或固定 slug，配置数据库、模型、代理、研究台端口、刷新间隔和请求超时，并打开原有研究台。
- 并发与安全：启动前检查目标端口，已有监听时拒绝启动第二个后端；停止操作只作用于本控制台创建的子进程。控制台和研究台均保持 `do_not_auto_trade=true`，不增加交易、下注、钱包、私钥、签名或下单能力。
- 数据边界：启动控制台本身不抓取外部数据、不读写 SQLite/CSV/模型；后端是否抓取仍完全由用户选择的研究台模式决定。未修改生产数据、未训练/重训/回放。
- 便捷入口：新增 `start_dashboard_launcher.ps1`，仅负责定位 Python 并启动控制台，可通过 `-LauncherPort`、`-DashboardPort` 和 `-NoBrowser` 调整本地入口。
- 回测隔离：新增“本地回测/历史只读”模式，启动研究台时传递 `--no-background`，只提供本地回测/归档 API，不启动周期抓取线程。
- 默认选择与打包：启动控制台默认选择自动发现当前两个市场；新增 `/api/local-markets`，历史归档、历史回测和竞猜初始市场按本地最新采集时间选择最近市场。新增 `build_dashboard_launcher.ps1` 生成单文件 exe 的可复现构建入口；本轮不自动下载依赖。
- 打包结果：已在 `C:\Study program\Python` 环境启用 pip、安装 PyInstaller 6.22.2，并生成 `dist_dashboard_launcher\AIPRP-DashboardLauncher.exe`。exe 内置控制台 HTML，运行目录仍需放置 `live_dashboard_server.py`、`forecast_dashboard.html` 和 `data` 目录；未打包数据库、模型或用户数据。
- 部署修正：exe 采用无控制台窗口模式，并会从自身目录向上探测包含 `live_dashboard_server.py` 的项目根目录，因此放在 `dist_dashboard_launcher` 子目录中也可运行；已实测 exe 控制台页面 HTTP 200。
- 启动故障修正：控制台不再丢弃后端 stdout/stderr；研究台异常退出时状态会显示退出码和最近日志，`打开研究台` 不再只返回笼统的 `dashboard service is not running`。超时输入按原值传递给后端，并标注为外部 REST 请求超时。

## M-20260905-BACKTEST-PAIRED-BASELINES

- 分支：在保留全部既有未提交修改的前提下，从 `model-v6-live-snapshot-verify-20260828` 创建 `backtest-audit-v1`；未执行 reset、restore、clean、删除或覆盖操作。
- 实际改动：回测为每个已结算检查点增加纯本地速度基准。它只将检查点前、已归档可用的帖子计数按当前速度外推至窗口末尾，不使用价格、订单簿、外部事件或先前市场特征。模型、纯本地速度和同期本地归档市场概率都输出 Brier/Log Loss，并提供模型减基准的配对差；差为负仅表示该配对样本的模型评分更低。
- 市场评分质量门控：市场基准要求所有结果区间均有本地归档价格，且归一化前概率质量有效；缺失或不完整盘口不评分，不以零概率补全。前端显示模型评分、相对本地速度/市场的 Brier 差及每种配对的样本数。
- 解释边界：市场对照不是独立基准，因为当前模型的结构化特征中也包含本地归档价格统计；它用于诊断模型是否明显弱于同期市场，不可据此证明增量预测能力、可成交性或盈利。Brier/Log Loss 只在本地存在结算区间时计算。
- 数据与安全：只读已有 SQLite 归档，未抓取外部数据、未启动或停止 8765、未修改生产 SQLite/CSV/模型，未训练、重训或生产回放；`do_not_auto_trade=true` 保持不变，无交易、下注、钱包、私钥、签名或下单 API。未更新 `model-update.txt`。

## M-20260905-BACKTEST-PAIRED-BASELINES-DOCS

- 文档：`README-CH.md` 与 `RUNBOOK-CH.md` 明确三类评分对象、市场评分的完整盘口质量门槛、配对差方向及市场对照非独立性的解释边界。
- 范围：本记录只补充说明，不改变回测算法、模型、训练、回放、数据库架构或现有数据；未启动服务、未访问外部接口，`do_not_auto_trade=true` 不变。
# M-20260906-DYNAMIC-LIVE-MARKET-SETTLEMENT-FIX

- 修复自动发现实时市场残留问题：动态发现成功但没有活动市场时，当前服务集合会清空；Gamma 查询失败/超时时也不再继续把旧的已结算市场作为实时市场，旧实例仅保留在历史退役集合中供本地归档查询。
- 修复历史补档完整性判断：已存在 `market_events` 但缺少成功 `prediction_archive` 的已结束市场仍会进入补档队列；补档按完整 slug 隔离并仅追加/幂等写入本地数据，不删除旧失败记录。
- 修复窗口判定优先级：优先使用 Gamma 结构化 `gameStartTime` 与 `endDate`；结构化字段缺失时才回退到描述规则，避免可变描述中的旧日期把当前盘误判为已结束，同时兼容历史记录。
- 动态模式在启动时 Gamma 暂时失败也会正常启动并显示发现错误/无实时市场状态，等待后台 REST 轮询重试，不回退到日期拼接 slug。
- 验证：`test_polymarket_pipeline.py` 52 项通过；未启动/停止 8765，未抓取外部数据，未迁移或删除 SQLite，未训练、重训或回放模型；`do_not_auto_trade=true` 保持不变。
- 动态补档窗口：实时活动筛选仍严格使用 `start <= now < end`，但候选扫描回看最近 35 天、向前 8 天，避免已结算市场在 8 天后脱离发现范围；只对本地归档不完整的真实 slug 排队补档。
- 启动时序：动态模式不再同步阻塞等待首次 Gamma 请求，服务先以空实时集合监听本地端口，由后台 REST 轮询完成首次发现和重试；外部代理慢或失败时前端显示等待/错误状态，不展示旧盘。
# M-20260906-HISTORY-SYNC-REPORT-ARGS-FIX

- 根因：历史抓取的 63 个市场全部成功（`succeeded=63`、`failed=0`），但收尾生成固定公开/私有/汇总 CSV 时，`HistorySyncManager._report_args()` 未提供回测函数新增的 `step_minutes`、`min_elapsed_minutes`、`backtest_min_edge`、`backtest_max_spread`、`backtest_min_depth` 参数，导致报告阶段抛出 `AttributeError`，前端错误地将整个任务显示为失败。
- 修复：补齐报告生成参数，固定复盘仍按 60 分钟检查点生成，并增加回归测试验证参数完整性。不会把抓取失败计数改成成功，也不删除或覆盖旧归档。
- 验证：全套 67 项单元测试、Python 编译和 `git diff --check` 通过。未重新抓取外部数据，未重训/回放/修改模型，未迁移或删除 SQLite；`do_not_auto_trade=true` 保持不变。
- 长期注意事项：已将“格式/契约变更必须同步适配全部调用方”写入 `RUNBOOK-CH.md` 与 `README-CH.md`，特别要求检查 `HistorySyncManager._report_args()`、`hourly_test_command()`、后台任务、API DTO 和前端字段，并分别验证抓取阶段与报告收尾阶段。
- 复核：隔离执行当前 `hourly-test`（临时报告路径）成功生成 61 个市场、10,185 条检查点，确认磁盘代码的报告参数修复有效；此前 63/63 的 `AttributeError` 来自旧服务进程内存代码。历史任务失败状态现在额外保存 `error_type` 和 `error_detail`，便于区分抓取与报告收尾错误。
- 沟通标准：已将“申请权限/外部或高风险操作前说明计划与影响；阶段性任务完成后说明实际动作、验证结果、未完成项和下一步；严格区分计划、代码测试与外部实测”写入 `RUNBOOK-CH.md` 和 `README-CH.md`。
# M-20260906-HISTORY-FORMAT-LOCK-AND-EMPTY-FIELDS

- 历史抓取任务启动后，前端会锁定日期、是否抓取价格、价格间隔和是否生成固定报告等格式相关选项，直到任务结束；任务开始时后台保存 `format_locked=true` 和本次实际配置，避免中途修改造成界面与工作线程参数不一致。
- 未请求价格/盘口时继续使用空值和明确缺失状态，不用 `0` 伪装成真实赔率或盘口；已有归档字段保持兼容读取。
- 任务结束或启动失败后自动解锁控件；本轮未重新抓取、未修改模型、未训练/回放、未删除或迁移数据库。`do_not_auto_trade=true` 保持不变。
# M-20260906-BACKTEST-UX-AND-UNDEFINED-PARAM-FIX

- 根因：前端详情面板和采样步长下拉框重复使用 `id=backtestStep`，`getElementById` 取到文本元素后，其 `.value` 为 `undefined`，请求参数因此变成 `step_minutes=undefined`，后端 `int()` 抛出异常。
- 修复：详情字段改为 `backtestRecordStep`；后端数值参数对空字符串、`undefined`、`null` 和 `none` 使用安全默认值。回测页面改为从 `/api/local-markets` 自动加载并选择最近的本地已结束市场，支持“全部已结束本地市场”；日期、步长、最小运行时间和记录上限移入默认折叠的高级设置。
- 设计边界：回测默认只读本地归档，不抓取、不训练、不修改模型；价格/盘口缺失仍显示为空或质量不足，不补零。`do_not_auto_trade=true` 保持不变。
- 验证：67 项单元测试、Python 编译、前端 JavaScript 语法检查和 `git diff --check` 通过。
# M-20260906-BACKTEST-INFO-AND-LAYOUT-CLEANUP

- 前端将“只读本地 SQLite、固定模型按检查点复放、不触发抓取/训练”的方法说明移入回测区信息图标悬浮提示，主标题只保留简短用途说明。
- 回测界面整理为：市场选择与读取、核心结果卡片、时间轴、逐条记录表；运行身份/校准诊断与当前检查点详情改为可展开区域，减少技术字段堆叠。
- 验证：前端 JavaScript 语法检查和 `git diff --check` 通过；未修改模型、训练、回放或数据库，`do_not_auto_trade=true` 保持不变。

# M-20260906-BACKTEST-REVIEW-5MIN-OUTCOME-MARKET-ROWS

- 实际改动：统一历史回测与复盘主 DTO 为 5 分钟半开区间 `[interval_start_utc, interval_end_utc)`；首个检查点默认在市场开始后 5 分钟，报告路径同步使用 5 分钟步长。窗口行情按本地 `captured_at_utc` 归属，并要求价格源时间、订单簿来源时间不晚于检查点；订单簿缺来源时间仅保留审计状态，不计算成本后 edge。
- DTO：每个检查点新增区间起止、区间新增帖子、累计帖子、总/已载入检查点与截断标记，并为完整 slug 的每个结果区间输出模型概率、价格范围、bid/ask、价差、买卖深度、纸面成本后 edge、年龄和质量 flags；缺失行情保持 `null`。保留旧顶层选中区间字段兼容旧调用方。
- 前端：旧本地历史归档入口收进隐藏兼容区，主界面保留单一历史回测/复盘选择器和 5 分钟时间轴；当前检查点详情直接显示所有结果区间行情，逐检查点表保留完整历史列表，滑动时间轴同步显示该点 Brier/Log Loss。
- 复盘定位修正：加载单一市场后默认跳到该市场第一个存在本地价格或订单簿的 5 分钟区间，避免页面初始停在市场开始后的空数据区间；时间轴仍可回看所有未采集区间。订单簿来源时间缺失时仍只显示归档 bid/ask/深度审计信息，不生成安全成本后 edge。
- 顶部工作区合并：原“竞猜模式/复盘模式”时间轴现优先读取同一市场的 `/api/backtest-review` 5 分钟记录，并直接显示逐 outcome 历史价格、市场隐含概率、bid/ask、价差、深度和成本后 edge；私有 CSV 仍只作为揭晓后的结算答案与评分来源，揭晓前不显示官方区间、本地最终数、Brier 或 Log Loss。
- 五分钟历史价格采集：历史同步 API、CLI 与前端统一传递 `price_fidelity_minutes=5` / `--price-fidelity 5`，非 5 分钟的历史同步请求会被拒绝；任务状态记录实际采样步长。事后补采价格按 `price_time_utc` 显示于对应复盘区间并标记仅供复盘，但因 `captured_at_utc` 晚于检查点，不进入当时模型特征、市场基准或成本后 edge。未执行实际历史抓取、未重训或修改模型。
- 验证与边界：新增半开窗口和完整 outcome 回归测试；代码测试阶段未写入生产 SQLite/CSV/模型，未训练/重训/生产回放。为实测新 DTO，按规范停止两个重复旧实例并只启动一个 8765；动态服务恢复后执行了正常 REST 发现/订单簿轮询并追加本地订单簿归档，未由回测接口触发抓取。`do_not_auto_trade=true` 保持不变。历史价格/订单簿覆盖仍取决于本地归档，当前盘口未用于填补历史空缺。

# M-20260915-OUTCOME-MISSING-MARKET-STATUS

- 前端统一历史回测与复盘行情表的缺失值显示：价格缺失显示“未采集价格”，订单簿、价差或深度缺失显示“未采集订单簿”，保留 null DTO 与“仅审计/事后补采”质量状态，不以 0 或当前盘口填补历史空缺。
- 验证：将执行单元测试、Python 编译、前端 JavaScript 语法检查和 `git diff --check`；本轮未启动服务、未访问外部接口、未写入 SQLite/CSV、未修改模型或训练。`do_not_auto_trade=true` 保持不变。

# M-20260915-PAPER-POSITION-REPLAY

- 实际改动：在现有 `/api/backtest-review` 固定模型复放中加入内存纸面 YES 持仓状态机；每个完整 `event_slug` 单独维护仓位，不写入生产 SQLite，不改变旧 `paper_trades` 接口。
- 选项含义：初期定义为市场开始后的前 12 个 5 分钟检查点（60 分钟）；一个市场允许多个 outcome，但共用市场总预算，正 edge 按比例分配并限制单 outcome 40%；默认单次调仓不超过总预算 25%。
- edge 与动作：唯一触发值是成本后可成交 edge（模型概率减 ask、滑点和手续费）。edge >= 8% 进入或高 edge 增持，4% 至 8% 保持，0% 至 4% 按比例减持，<= 0% 出清；阈值形成滞回，减少轮询抖动。
- 成交与安全：买入按 ask、卖出按 bid，按买卖深度模拟部分成交；缺少检查点前安全来源时间、bid/ask 或深度时不调仓。所有动作、仓位、成交量、费用、实现/未实现纸面 PnL 和原因写入回测 DTO，并固定 `do_not_auto_trade=true`。
- 前端：回测详情同步显示各 outcome 的纸面动作、当前持仓和 PnL；状态仍明确为纸面估算，不代表真实成交、稳定盈利或自动交易。
- 验证边界：新增状态机单元测试；未启动服务、未访问 Gamma/CLOB/RSS、未抓取历史、未训练/重训/生产回放、未修改模型或生产数据库；`model-update.txt` 未更新。
- 边界修正：盘口深度为零或无法成交时，动作明确标记为 `no_fill`，成交数量保持 0，不把目标仓位误报为已建立。

# M-20260915-STRATEGY-MODULE-ISOLATION

- 实际改动：新增 `strategy_simulator.py` 作为纯纸面策略模块，集中实现成本计算和多 outcome 持仓再平衡；模块不导入数据库、网络、模型或前端。
- 兼容边界：`live_dashboard_server.py` 继续导出原有 `paper_buy_cost` 和 `paper_position_rebalance` 名称，但仅作为兼容别名，实际调用转至隔离模块；旧 API、CSV、数据库表和实时服务接口保持不变。
- 数据边界：回测仍在内存中维护每个完整 slug 的持仓，不新增生产数据库写入；检查点后数据、当前盘口和不安全订单簿仍不能触发历史动作。
- 验证：新增模块身份回归测试；未启动服务、未访问外部接口、未抓取历史、未训练/重训/生产回放，`do_not_auto_trade=true` 保持不变。

# M-20260918-ALGORITHM-V2

## M-20260918-LIVE-WINDOW-ROLLOVER

- 新增 live_market_time.py 实时严格窗口解析，完整时间/时区/DST 验证，结构化及规则冲突排除，禁止创建时间代替统计起点；历史解析不变。
- CLI 与动态发现/替换共用严格窗口；动态服务按开始时间重排，子实例不自行换 slug，单市场接口不回退退役市场，边界缩短下次发现等待，响应前再次检查有效窗口。
- 既有分支和 dirty 修改保留；只读审计及测试结果后续追加，未重启服务、未访问外部接口、未写生产数据或模型，do_not_auto_trade=true。
- 审计完成：data/extended.sqlite mode=ro quick_check=ok，journal=delete、locking=normal；市场 66、outcome 1820、订单簿 806877 条（运行中服务可继续自行采集）。本机 health 正常，仍运行旧版本；本轮未重启。原有 119 项测试通过。
- 第二批增加前端北京时间/纽约时间、倒计时、最近发现时间和到期清屏/选择切换；新增 test_live_market_time.py 合成时间、发现失败恢复及 Node 前端测试。父级管理子服务 slug，子服务 404 不跨市场替换。版本号更新用于区分磁盘与运行中代码。
- 首轮新增测试定位到 Windows 测试管道默认编码与 Node UTF-8 不一致，显式指定 UTF-8 后重测；生产代码无需为该测试编码问题变更。
- 新增 7 项测试后全套 126 项通过；最终补充初始子服务与运行中新增子服务采用相同 parent_managed_slug 隔离规则及测试。Logic.txt 追加实时严格窗口、时区换算和切换限制说明。最终回归结果随后记录。
- 最终验证：全套 127 项测试通过（本轮新增 8 项），Python 编译、前端完整 JavaScript 语法检查通过；git diff --check 无空白错误，仅既有换行提示。本轮修改 live_market_time.py、polymarket_pipeline.py、live_dashboard_server.py、forecast_dashboard.html、test_live_market_time.py、Logic.txt 和本日志。未重启现有服务，未主动访问外部接口，测试仅用临时合成数据，未写生产数据库/CSV/模型、未重训/生产回放，model-update.txt 未改，do_not_auto_trade=true。下一步由用户重启后观察新版本；线上真实切换尚未验证。

## M-20260918-LAUNCHER-FIX

- 修复控制台三秒轮询覆盖编辑中的参数：仅首次且未编辑时恢复配置，区分待启动表单与上次启动配置，并显示未应用修改。
- 后端增加未启动/启动中/就绪/停止/失败状态、退出码、项目目录及入口；健康检查仅访问本机且不通过代理，打开页面要求健康就绪。超时严格校验 1 至 300 整数，旧进程日志不污染新进程。
- 未启动或重启服务，未访问外部接口、未写数据库/CSV/模型、未训练或回放；保留既有修改，do_not_auto_trade=true。待模拟测试验证。
- 新增 test_dashboard_launcher.py：模拟进程、健康响应、启动异常和超时传参；使用 Node 执行页面脚本，验证首次响应与输入竞争、多次轮询不覆盖编辑及就绪按钮状态，不启动实际服务。
- 验证完成：全套 119 项测试通过（控制台新增 6 项），Python 编译与 git diff --check 通过，页面 JavaScript 已在 Node 模拟 DOM 中执行。未实际重启控制台/后端；加载 Python 状态修复需要用户重启控制台。测试仅使用临时数据，不写生产数据库/CSV/模型，未访问外部接口、未重训或生产回放，do_not_auto_trade=true。

- 审计：分支 backtest-audit-v1，HEAD 7a1dd15342e8e0ee0ffeaa2485bb57145ca90e33；保留全部既有修改。生产库 data/extended.sqlite 使用 SQLite mode=ro，quick_check=ok，journal=delete，locking=normal；订单簿 806549 条、市场 65 条、outcome 1794 条、source_events 0 条。订单簿抽查 100 条均包含买卖档位，不代表全量安全覆盖。未发现 8765 监听或 Python 服务，本机 health 不可达，未启动服务。
- 第一批实现：新增 research_features_v2.py，使用显式覆盖时段、帖子去重、市场开始前历史、168 时段收缩速度和正负分开的多尺度事件特征；新增 research_probability_v2.py，以 SciPy 分布实现剩余数量概率、方差语义/时间门控和未覆盖概率。新模块显式调用，未接入默认生产路径，旧特征顺序和模型不变。
- 验证待后续合成测试及回归测试完成。未访问外部接口、未写生产数据库/CSV/模型、未重训或生产回放；model-update.txt 不变，do_not_auto_trade=true。
- 第二批实现：新增纯模块 strategy_execution_v2.py，并从 strategy_simulator.py 显式导出实验接口；归档适配器校验 slug/token、来源/捕获时间、过期及档位完整性。逐档模拟返回费用、均价、部分成交、剩余现金/换手和已消耗深度；相同来源时间盘口不会因重复归档恢复深度。默认额外滑点 0，手续费 2%，价格带绝对差 0.05。旧 rebalance 和服务默认行为不变；待合成测试验证。
- 第三批实现：新增 test_research_v2.py，覆盖早期平滑、短市场、缺失/零历史、去重与覆盖重叠、时间门控、时段收缩、夏令时、事件多尺度、概率分支/质量守恒、实时/复盘共享入口，以及逐档成交、现金/换手、快照重复、slug 隔离和旧导出兼容。测试全部使用合成数据，验证结果将在执行后追加。
- 首轮验证：新增 33 项测试通过，全套 unittest discover 共 111 项通过（使用临时测试数据，不是生产回放）。第四批补充半小时时区切分、时长增加的平滑收敛测试，修正跨市场拒绝原因，并对非有限概率显式报错；新增可选实验依赖 requirements-research-v2.txt（本机已有 scipy 1.17.0，未安装依赖）。Logic.txt 新增第十章，逐项解释 v2 变量、公式、调用边界和未启用状态；最终验证待运行。
- 最终验证：python -m unittest discover 共 113 项通过，其中 v2 新增 35 项；新 Python 模块编译通过，原前端内联 JavaScript 语法检查通过，git diff --check 无空白错误（仅既有 LF/CRLF 提示）。一次回归运行出现既有 HTTPError mock 清理的 ResourceWarning，不影响测试通过，不代表进行了外部 HTTP 请求。
- 本轮交付：research_features_v2.py、research_probability_v2.py、strategy_execution_v2.py、test_research_v2.py、requirements-research-v2.txt，以及 strategy_simulator.py 的实验导出、Logic.txt 第十章和本日志。未切换分支、未删除/覆盖用户数据、未改旧模型或默认预测入口。测试仅使用临时数据库/CSV 等合成工件，未写生产 SQLite/CSV/模型，未启动服务或访问外部接口，未重训或生产回放，model-update.txt 保持不变，do_not_auto_trade=true。
- 后续边界：真实预测收益未评估；168 时段依赖明确的历史观测覆盖，不能由帖子稀疏程度推定。事件动态参数训练、时间顺序样本外比较和默认服务启用不在本轮执行范围，必须后续明确授权。逐档执行器返回余额/深度状态，由调用方跨 outcome 和检查点传递，不替代完整策略账户和结算系统。

## M-20260919-UNIFIED-HISTORY-WORKSPACE

- 只读检查既有修改后，合并历史复盘与纸面交易：一套市场选择、日期范围、读取入口和五分钟游标，三个标签共享完整 slug 与检查点记录；删除重复回测请求、行情表及独立游标。
- 纸面页显示逐 outcome 买卖、成交份数、持仓和盈亏，累计动作包含日期筛选前的成交；明确标注汇总深度模拟。零成交、缺少安全盘口、部分成交、缺失估值不再混淆。
- 日期之前从市场首个检查点预计算，返回值按日期过滤；缺失持仓估值时汇总盈亏返回 null。旧模拟仅新增请求数量和部分成交标识，未替换为 v2。
- 使用请求序号拒绝迟到响应；更换市场、日期、失败或 CSV 数据源时清理不匹配状态。揭晓锁保留，诊断和固定 CSV 入口迁移并保留。
- 验证：64 项流水线单元测试通过，新增日期截取与完整回放持仓一致、范围前动作和截断断言；本机合成浏览器回归通过，覆盖单游标、前后拖动、标签、盘口缺失、零成交、部分成交、估值缺失、揭晓、竞争、失败和 CSV。检查 1440px / 390px 截图无页面溢出。最终全套回归结果后续追加。
- 边界：保留全部既有修改；未删除用户数据，未启动/重启生产采集服务，未访问外部 API，未写生产数据库/CSV/模型，未重训或生产回放；do_not_auto_trade=true 不变。仅启动并关闭合成测试 HTTP 服务和临时浏览器。

### 新上下文收尾验证

- 重新只读审计现有未提交实现，保留全部既有修改；确认单市场、单游标、三个标签、日期前仓位延续及旧版模拟已实现。
- 修复固定公开 CSV 加载失败残留旧结果和迟到失败覆盖状态；私有 CSV 与市场列表的迟到失败也不再覆盖新数据源。空工作区禁用滑块，API 拒绝无效检查点时间；修正诊断页沿用的 CSV 专属盈亏说明。
- 验证：全套 Python 单元测试 127 项通过；扩展本地合成浏览器测试通过，新增无效时间、CSV 失败清空及迟到失败断言。检查 1440px / 390px 截图无页面溢出或控件重叠，移动宽表可横向滚动。首次浏览器运行受沙箱 Chrome 子进程权限限制，获准在沙箱外重跑后通过。
- 边界不变：未启动生产采集、未访问外部接口、未写生产数据库/CSV/模型、未重训或生产回放；do_not_auto_trade=true；v2 逐档执行未接入。合成 HTTP 服务及临时浏览器已关闭。

## M-20260919-PRIVATE-CSV-LOAD

- 用户报告点击“载入固定私有结算与汇总”后前端崩溃。只读检查文件大小与表头：私有 CSV 264,722,327 字节，公开 CSV 80,833,855 字节；原实现一次性 response.text 后同步构建全部单元格与对象，保留大型盘口字段，造成主线程阻塞和高峰内存压力。诊断新增的质量标记 join 也不兼容 CSV 字符串。
- 改为响应流分块 CSV 解析，跨块保留引号/转义/换行状态；私有报告只保留身份与结算所需列，大型盘口和概率字段在读取时丢弃；读取过程中显示 MB 进度并让出主线程。公开 CSV 也使用分块读取，保持原有列。生产 CSV 不变。
- 私有结算按完整 slug 与检查点建立 Map 索引，替代图表逐点全表 find；质量标记兼容字符串/数组。较早请求在数据源变更后取消读取或丢弃结果，坏 CSV 显示错误并恢复按钮，结算仍需显式揭晓。
- 合成浏览器测试通过：实际点击公开/私有按钮，约 4 MB 含大量转义引号和跨行字段的 CSV 分块响应，验证大型字段未保留、UI 定时器仍运行、质量标记渲染、揭晓值、坏引号恢复及日期切换竞争。未加载完整生产 CSV 做回放，未写生产数据，未重启服务。
- 最终回归：127 项 Python 单元测试通过，合成浏览器按钮回归与 JavaScript 语法检查通过，git diff --check 无空白错误（仅既有换行提示）。

## M-20260919-PAPER-TIME-CASH-CHARTS

- 布局调整：全局结算预测残差图移到分市场结算细则表上方；细则表使用默认折叠的 details，展开后限制高度 360px 并允许滚动，继续受结算揭晓锁控制。表头“小时数”更正为“检查点数”。合成浏览器回归通过，未重启生产服务或修改数据。

- 修复数字字符串 epoch 秒/毫秒解析；历史五分钟盘口从原始 JSON 恢复来源时间，返回原始存储时间、恢复标记和拒绝原因，不回写旧数据库。校验 YES token、报价/深度、未来时间与五分钟来源年龄，在候选窗口内选择最新有效快照。
- 旧版汇总深度模拟增加市场现金账户及多个 outcome 共享的 25% 调仓限额；卖出先执行，买入按需求比例使用剩余现金及限额，费用/滑点计入现金；高 edge 分支不再反向减仓。保留首小时开仓与 8%/4%/0% 阈值，未接入 v2。
- DTO 增加现金、调仓金额、首小时覆盖和实际执行状态；页面区分候选和成交、过期/无效盘口、错过开仓窗口及无持仓无需估值。
- 三个 Canvas 图统一按真实时间定位，悬停显示北京/UTC 时间、坐标值和揭晓后的结果残差。单市场残差同时绘制本地最终观测误差与官方中点代理；揭晓锁、空数据和切换数据清除提示，支持触摸结束隐藏及窗口缩放。
- 验证：131 项 Python 测试通过，新增时间恢复、最新有效盘口、只读归档、共享资金限额、首小时限制回归；本机合成浏览器验证三图悬停和揭晓锁、CSV 与工作区原有回归通过。未运行生产回放、未启动或重启生产采集、未修改生产数据库/CSV/模型；do_not_auto_trade=true。

## M-20260919-CONTINUOUS-DEVELOPMENT-PROTOCOL

- 新建根目录“持续开发协议.md”和 AGENTS.md 入口，规定“调用持续开发协议”的读取/核对流程、中文沟通、约束优先级、日志/问答维护及交接收尾。
- 原 Q&A.txt 为空，本轮按可见对话整理10条问答，明确区分用户确认、事实、推测、已修复及仅建议；不编造更早历史。把持续维护 Q&A 列为以后每阶段要求。
- CONTINUATION-CONTEXT-CH.md 顶部新增当前交接快照，保留旧记录并标为历史；记录超时后台任务和全局图数据隔离仍未实现、Logic.txt尚需同步后续算法变更，防止新上下文误报完成。
- 验证：已核对文档入口和所引用文件、问答编号及当前分支/HEAD；git diff --check无空白错误，仅既有换行提示。纯文档任务未运行程序测试、启动服务、访问外部接口或写生产数据，model-update.txt不变。
# 2026-10-04：实时双市场五分钟归档基线

- 将 `live_dashboard_server.py` 与 `start_live_dashboard.ps1` 的实时刷新默认值统一为 300 秒（5 分钟）。`--refresh-seconds` 仍可显式调整；本次未启动服务、未访问外部接口、未写入数据库/CSV/模型，也未重训或运行回放。
- 现有 `live_market_snapshots` 继续按完整 `event_slug` 追加保存两个市场的盘口、价格、概率、来源状态和纸面研究字段；后续补充 5 分钟检查点及逐 outcome 官方结算状态，保持 `do_not_auto_trade=true`。
- 已实现 `capture` 与 `official_settlement` 快照字段：`capture` 记录实际 `data_time_utc`、5 分钟 `checkpoint_utc` 和 `target_interval_seconds=300`；`official_settlement` 逐 outcome 保存 `resolved_yes`、官方区间、检查时间与来源。未结算保持 `pending`/`null`，不使用当前盘口或模型结果补齐。仅完成代码与合成验证，尚未启动服务或访问外部接口。
- `RUNBOOK-CH.md` 已同步说明默认 300 秒轮询和五分钟归档字段；手动刷新仍可按需触发，但生产后台默认节奏保持五分钟。
- `forecast_dashboard.html` 的实时监控选项已改为 5/10/15 分钟，前端最小轮询间隔固定为 300 秒，避免界面默认高频请求覆盖后端五分钟采集策略。
- `live_dashboard_server.py` 在解析 CLI 后将 `refresh_seconds` 钳制为至少 300 秒，防止误传更短参数导致公开接口高频轮询；`do_not_auto_trade=true` 未改变。
- `RUNBOOK-CH.md` 中旧的 30 秒实时示例已改为 300 秒，并说明 600 秒以上低频选项，避免文档命令绕过五分钟采集边界。
- 快照 `capture` 现在额外记录 `configured_interval_seconds` 与 `interval_status`；显式使用更长间隔时会标记为 `coarser_than_target`，不冒充完整五分钟覆盖。
- 前端实时状态栏显示五分钟检查点及“官方结算已核验/待定”，让两个市场的采集时间和结算可用性可直接审计。
- `dashboard_launcher.html` 的网页启动控制台默认刷新间隔已同步为 300 秒，最小值也固定为 300 秒；控制台地址仍为 `http://127.0.0.1:8760/`。
## M-20261004-BROWSER-CACHE-NAMESPACE

- 复核发现仓库没有使用 `localStorage`、`sessionStorage` 或 Cache API 保存生产采集数据；前端清理按钮因此改为只处理明确的 `aiprp:`（Storage）与 `aiprp-`（Cache API）命名空间。
- 清理确认框明确列出可清理对象和不可清理对象。没有匹配命名空间时只显示“当前没有可清理的应用缓存”，不调用全量 `.clear()`，也不删除 SQLite、`live_market_snapshots`、CSV/JSONL、订单簿、原始帖子或模型文件。
- 本次仅修改前端代码和维护日志；未启动服务、未访问外部接口、未写入生产 SQLite/CSV/模型、未重训或运行生产回放，`do_not_auto_trade=true` 保持。

## M-20261004-GAP-AUDIT-FIRST-CHECKPOINT

- 修正 `live_dashboard_server.py` 启动缺口审计：市场开盘后但首个五分钟检查点尚未完成时，不登记“一次性审计已完成”；后续刷新会继续检查首个检查点，避免首段缺口被永久跳过。
- 仍按市场 `window.start_utc` 对齐，缺口证据不填充价格、盘口、预测或 edge；本次未启动服务、未访问外部接口、未写入生产 SQLite/CSV/模型、未重训或运行生产回放，`do_not_auto_trade=true` 保持。

## M-20261004-LIVE-RECORDING-STATUS

- 后端 `live_dashboard_server.py` 将五分钟检查点从 Unix 整点改为按市场 `window.start_utc` 锚定，快照标记 `alignment=market_start_utc`，避免开盘时间非整点时错位或重复。
- 每个市场窗口的服务生命周期只执行一次启动缺口审计。对已经过去且本地没有快照的检查点追加 `refresh_status=missing_checkpoint` 证据；证据不含价格、盘口、预测或 edge，并明确记录“历史行情重建未尝试”，不会用当前盘口填补过去。
- `forecast_dashboard.html` 新增明文记录状态：无缺口时显示“正在正常记录当前市场信息”，有缺口时显示“已尝试但缺失”；新增浏览器缓存清理按钮，仅清理本应用 localStorage、sessionStorage 和 Cache API，并在确认框中明确不删除 SQLite、CSV/JSONL、订单簿、原始帖子和模型文件。
- `持续开发协议.md` 新增网页启动控制台、每次更新同步维护文件、缓存清理边界和 `do_not_auto_trade=true` 的长期要求。
- 验证范围：尚未启动服务、未访问外部接口、未写入生产数据库/CSV/模型、未重训或运行生产回放；待完成 Python/JavaScript 语法与合成测试。

## M-20261004-DATA-FILE-INVENTORY

- 只读盘点发现若干内容相同或高度相似的 CSV、汇总 CSV 与 `report_backups` 文件；仅凭哈希无法证明它们可安全删除，可能分别承担历史版本、报告备份或审计用途。
- `data/extended.sqlite` 约 16GB，属于一手采集数据库，明确排除在前端“清除历史缓存”范围之外。未删除、移动、覆盖或压缩任何 CSV/JSONL、订单簿、原始帖子、模型、备份或 SQLite 文件。
- 本轮未启动服务、未访问外部接口、未写入生产数据库/CSV/模型、未重训或运行生产回放；`model-update.txt` 未修改，`do_not_auto_trade=true` 保持。

## M-20261005-DATA-ORGANIZER

- 新增 `data_file_organizer.py`，只读扫描 `data/` 下 CSV/JSONL/JSON 与受保护数据库/备份文件，记录大小、SHA-256、CSV 表头、行数、完全重复组和可按完整行去重的 CSV 合并候选。`data/extended.sqlite`、其他 SQLite/DB、`.bak` 和压缩包标记为受保护源，不参与合并。
- `live_dashboard_server.py` 新增本机接口 `/api/data-organizer/status`、`POST /api/data-organizer/scan` 和 `POST /api/data-organizer/consolidate`；前端新增“扫描本地重复数据文件”和“生成合并副本（不删除原件）”按钮。合并结果写入 `data/consolidated/`，附带 `consolidation_manifest.json`；源文件不删除、不移动、不覆盖。
- 删除策略固定为 `never_delete_sources`，用于避免把一手五分钟数据、报告备份或模型输入误判为冗余。若以后确需删除，必须先由用户明确解除现有“不得删除用户文件”约束，并逐文件审核清单。
- 验证：临时目录单元测试 1 项通过；`data_file_organizer.py` 与 `live_dashboard_server.py` 编译通过；`git diff --check` 无新增空白错误。未启动服务、未访问外部接口、未写生产数据、未重训或生产回放；`model-update.txt` 未修改，`do_not_auto_trade=true` 保持。

## M-20261005-DATA-DELETE-SAFETY

- 根据用户为“缓解内存/磁盘压力”明确授权，删除能力限定为：同一 SHA-256 的完全重复文本文件，且文件不在 `report_backups`、`hourly_research_before`、`consolidated` 等归档目录，文件名不含 before/backup/smoke/verify/test/model/retry/initial/baseline 等风险标记。每组保留一个稳定规范副本。
- 删除接口 `POST /api/data-organizer/delete` 在执行前重新扫描并逐文件复核路径归属、文件存在性和 SHA-256；哈希变化、路径越界或受保护对象会跳过。前端先展示候选数量并二次确认；SQLite/DB、`.bak`、压缩包、订单簿、原始帖子、模型和一手实时采集文件不参与删除。
- 删除只记录实际文件大小和释放字节数，返回 deleted/skipped 清单；不删除合并副本或源数据组中唯一文件。
- 验证：数据整理器单元测试 2 项通过，Python 编译、Node 语法检查和 `git diff --check` 通过。当前未对真实 `data/` 执行扫描或删除，未启动服务、未访问外部接口、未写生产数据库/CSV/模型，未重训或生产回放；`model-update.txt` 未修改，`do_not_auto_trade=true` 保持。

## M-20261009-PAPER-GRID-RESEARCH

- 新增 `paper_grid_research.py`，以本地只读历史回放比较建仓后每 30 分钟节点（0 至 360 分钟）与中心/对称概率覆盖（中心、30%、50%、70%、80%、100%）。固定每市场预算与共享资金池同时统计净 PnL、投入收益率、最大回撤、风险调整分数和滚动 walk-forward 验证；未成交、未结算、失败或不完整市场不计入收益。
- `live_dashboard_server.py` 新增 `/api/paper-grid-research` 及独立 `/api/paper-grid-jobs/{id}` 异步任务队列，限制单次最多 64 个组合；`forecast_dashboard.html` 增加手动触发的网格研究表、共享资金池和 walk-forward 状态。所有结果明确 `do_not_auto_trade=true`。
- 新增 `test_paper_grid_research.py`，覆盖迭代器输入、资金不足、中心模式、非法预算/覆盖率和不完整样本排除。
- 本轮仅离线修改与测试，未启动服务、未访问外部接口、未写入 SQLite/CSV/模型，未重训或运行生产回放；`model-update.txt` 未修改。

## M-20261009-PAPER-GRID-VERIFY

- 修正网格统计对迭代器和市场时间顺序的处理；补充前端异步状态轮询、组合上限和脚本语法校验；修正共享资金池离线测试样例。网格单元测试 4/4、Python 编译、前端 JavaScript `node --check`、`git diff --check` 均通过。
- 异步回放与网格任务状态响应统一显式返回 `do_not_auto_trade=true`。

## M-20261009-FRONTEND-RESTART-PROMPTS

- 更新 `持续开发协议.md` 至 1.1，固定前端更新收尾提示：仅静态页面变化必须明确写“只需刷新浏览器窗口”并提示 `Ctrl+F5`；后端、API、启动器、启动脚本、端口或配置变化必须明确写“需要重启前端/本地服务”并给出可复制的 PowerShell 启动指令。
- 规定静态页面与服务端同轮变化按重启处理，重启后再提示 `Ctrl+F5`；服务未启动时必须明确报告未启动及原因，服务已启动时必须报告地址、命令、端口和状态。
- 本轮实际修改：`持续开发协议.md`、`MAINTENANCE-CH.md`、`CONTINUATION-CONTEXT-CH.md`。仅文档检查，未启动服务、未访问外部接口、未写入 SQLite/CSV/模型、未重训或运行生产回放；`model-update.txt` 未修改；`do_not_auto_trade=true` 保持。

## M-20261005-DATA-DELETE-EXECUTED

- 按用户对“缓解内存/磁盘压力”的明确授权，先对真实 `data/` 只读扫描，再按 SHA-256、路径归属和风险命名复核。删除 3 个逐字节完全重复且低风险的文本副本：`hourly_extended_v4.csv`、`hourly_extended_v4_public.csv`、`hourly_extended_v4_summary.csv`；每组规范副本分别保留为 `hourly_extended_events_gate.csv`、`hourly_extended_events_gate_public.csv`、`hourly_extended_events_gate_summary.csv`。
- 实际释放 2,877,237 字节。`hourly_model_v6_residual_verify_summary.csv` 等含模型/验证语义的重复文件、report_backups、hourly_research_before、SQLite/DB、`.bak`、订单簿、原始帖子、模型及一手实时文件均未删除。
- 删除后只读复核：可扫描文件 112→109，完全重复组 10→8，可删除组 3→0；`data/extended.sqlite` 仍存在，大小约 16.4GB。未启动服务、未访问外部接口、未写 SQLite/模型、未重训或生产回放；`model-update.txt` 未修改，`do_not_auto_trade=true` 保持。
## M-20261010-RESEARCH-CAPTURE-ISOLATION

- 修复 30 秒研究采样的写入边界：`live_dashboard_server.py` 不再在研究路径调用 `MarketStore.save_event`、`upsert_posts`、`save_orderbook`、`save_market_activity`。Gamma 窗口和 outcome token 仅在内存中组装，盘口只解析当前 REST 响应，研究快照唯一持久化目标为 `research_market_snapshots`。
- 正式五分钟 `market_events`、`market_outcomes`、`posts`、`market_orderbooks`、`market_activity_snapshots` 不会因研究轮询新增记录；正式实时预测/评分/edge/纸面交易路径未改变。研究快照继续带 `research_only=true` 与 `do_not_auto_trade=true`，不进入历史特征或策略。
- 验证：Python 编译与 `git diff --check` 已通过；待用临时 SQLite 和模拟 Gamma/CLOB/XTracker 做一次端到端写入计数验证。尚未启动服务、未访问外部接口、未写生产 SQLite/CSV/模型、未重训或生产回放，`model-update.txt` 未修改。

## M-20261010-RESEARCH-RUNTIME-CHECK

- 在不影响既有 8760/8765 实例的前提下，于隔离端口 `18768` 启动新版服务，动态发现到两个活动市场：`elon-musk-of-tweets-october-9-october-16-2026` 与 `elon-musk-of-tweets-october-6-october-13-2026`。
- 首次轮询遇到同一 SQLite 被其他实例占用，研究失败证据明确记录为 `database is locked`；新增研究表写入短重试（最多 4 次）后重启隔离实例复核成功。最新状态为 30 秒目标、按市场开盘对齐、帖子返回 37/窗口内计数 29、新闻 12、盘口 25 个 outcome，唯一错误为已结算 outcome 的 CLOB 404，标记 `closed_outcome_expected`。
- 本次实际访问 Gamma、CLOB、XTracker 和 RSS，并向生产 `data/extended.sqlite` 的独立 `research_market_snapshots` 写入研究快照；没有写正式五分钟表、CSV 或模型，没有钱包/私钥/签名/下单，没有重训或生产回放。隔离服务当前仍运行于 `http://127.0.0.1:18768/`，`do_not_auto_trade=true`。

## M-20261010-FUNCTION-ENTRY-AUDIT

- `RUNBOOK-CH.md` 补充前端功能合并/弃置审计：顶部 slug 分区分别承载实时采集、历史抓取、预测复盘、纸面研究、诊断、回测、数据整理和本地报告。实时盘口与 30 秒研究采样保留为不同状态；历史抓取与回测、浏览器缓存与本地文件整理不合并，以免误把事后数据或可再生缓存当作正式输入。
- 本阶段只更新说明文件；未新增服务接口，未访问外部接口，未写数据库/CSV/模型，未重训或运行回放，`model-update.txt` 未修改，`do_not_auto_trade=true` 保持。

## M-20261010-SQLITE-CONTENTION

- 只读进程命令行审计确认锁来源：主动态服务 PID 39744（8765）及多个历史 18760/18765 实例同时使用 `data/extended.sqlite`，本轮服务为 PID 49660（18768）。没有终止其他实例。
- `musk_tracker.Store` 的 SQLite 连接新增 `timeout=30` 与 `PRAGMA busy_timeout=30000`，降低多实例短写入的即时锁失败；研究保存仍保留独立的 4 次重试。重启本轮 18768 后，健康检查显示 2 个活动市场，30 秒研究状态恢复为 `partial`（25 个盘口成功，唯一错误为 `closed_outcome_expected`）。
- 生产库 `PRAGMA quick_check` 在其他实例持续写入时仍可能被 SQLite 锁阻塞；本轮未强行中止进程或修改 journal 文件。隔离服务继续运行于 `http://127.0.0.1:18768/`，`do_not_auto_trade=true`。
## M-20261010-LAUNCHER-OPEN-FIX

- 复核发现 8760 启动器和 8765 研究台实际均为健康状态；“打开研究台”失败的原因是旧按钮调用服务端 `webbrowser.open()`，服务端进程无法稳定控制当前用户浏览器窗口。
- 修改 `dashboard_launcher.html`：按钮现在使用浏览器点击事件直接打开启动器状态中的已健康检查 `dashboard_url`；若弹窗被浏览器拦截，则回退到当前窗口跳转。后端接口仍保留兼容。
- 验证：8760 `/api/status` 显示 `ready`、后端 PID 73336、8765 `/api/health` 返回 200；代码修改后未重启服务、未访问外部接口、未写数据库/CSV/模型、未重训或回放，`do_not_auto_trade=true`。
