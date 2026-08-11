# 本地运行手册

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

## 纸面成本参数

纸面服务默认使用 `--paper-fee-rate 0.02` 和 `--paper-slippage-bps 50`，它们是保守的研究假设，不是官方费用声明。可按实际规则重新设置。`--paper-size` 默认 1 份，最低可用深度默认要求为目标纸面数量的 3 倍。

## API

- `GET /api/health`：服务健康状态。
- `GET /api/live`：当前市场、计数、模型概率、盘口、事件、新闻和建议。
- `GET /api/paper-history?limit=100`：历史纸面预测。
- `GET /api/paper-trades?limit=100`：区间级纸面记录及结算结果。

## 停止、备份和排查

- 在运行服务的 PowerShell 窗口按 `Ctrl+C` 停止；不会删除数据库。
- 备份前先停止服务，然后复制 `data\extended.sqlite` 到用户明确指定的备份位置。
- `不交易` 的原因会显示在前端并写入 `paper_forecasts.risk_reasons` 与 `paper_trades.risk_reasons`。
- 重点检查 XTracker 新鲜度、盘口快照年龄、盘口概率质量、价差、深度和来源审核状态。
- 没有 API key 时系统仍可运行；可选 AI 只能结构化新闻，不能输出概率或交易指令。本轮服务没有自动调用外部 AI。

## 明确禁止

本项目永久禁止自动交易、自动下注、钱包签名和真实资金操作。所有建议最多是“纸面方向研究”，数据不足时必须是“不交易”。
