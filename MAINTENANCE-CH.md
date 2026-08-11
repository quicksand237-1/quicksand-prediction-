# 中文维护日志

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
