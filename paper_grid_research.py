"""Read-only paper research over entry time and probability coverage.

The module deliberately knows nothing about SQLite, HTTP, models, or the
frontend.  Callers provide a replay callback that returns one full-market
paper hold summary for a parameter combination.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, median, pstdev
from typing import Any, Callable, Iterable


DEFAULT_ENTRY_MINUTES = tuple(range(0, 361, 30))
# 0.01 is the smallest valid simulator coverage and selects the centre bin
# whenever its probability is non-zero.  It is exposed as a named centre mode.
DEFAULT_COVERAGE_GRID = (0.01, 0.30, 0.50, 0.70, 0.80, 1.00)
DEFAULT_MARKET_BUDGET = 100.0


@dataclass(frozen=True)
class GridConfig:
    entry_minutes: int
    probability_coverage: float

    @property
    def label(self) -> str:
        coverage = "center" if self.probability_coverage <= 0.01 else f"coverage_{self.probability_coverage:.0%}"
        return f"entry_{self.entry_minutes}m_{coverage}"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _market_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows = summary.get("markets")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _metrics(rows: Iterable[dict[str, Any]], budget: float) -> dict[str, Any]:
    row_list = [row for row in rows if isinstance(row, dict)]
    usable = []
    for row in row_list:
        pnl = _number(row.get("paper_final_pnl"))
        invested = _number(row.get("paper_total_buy_cost"))
        if row.get("paper_settled") and row.get("paper_entry_actual_utc") and pnl is not None and invested is not None and invested > 0:
            usable.append({"row": row, "pnl": pnl, "invested": invested, "return": pnl / invested})
    usable.sort(key=lambda item: (str(item["row"].get("window_end_utc") or item["row"].get("window_start_utc") or ""), str(item["row"].get("slug") or "")))
    pnls = [item["pnl"] for item in usable]
    returns = [item["return"] for item in usable]
    invested_total = sum(item["invested"] for item in usable)
    pnl_total = sum(pnls)
    equity = float(budget)
    peak = equity
    max_drawdown = 0.0
    equity_curve = [equity]
    for item in usable:
        equity += item["pnl"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        equity_curve.append(equity)
    drawdown_fraction = max_drawdown / budget if budget > 0 else None
    net_return = pnl_total / invested_total if invested_total > 0 else None
    # A transparent risk-adjusted score: return less the worst capital drawdown.
    risk_adjusted = net_return - drawdown_fraction if net_return is not None and drawdown_fraction is not None else None
    return {
        "markets_total": len(row_list),
        "markets_with_settlement": sum(bool(row.get("paper_settled")) for row in row_list),
        "filled_and_settled_markets": len(usable),
        "not_filled_markets": sum(not row.get("paper_entry_actual_utc") for row in row_list),
        "excluded_or_failed_markets": sum(bool(row.get("error")) or row.get("eligible_for_profitability_analysis") is False for row in row_list),
        "total_invested": round(invested_total, 8),
        "net_pnl": round(pnl_total, 8),
        "return_on_invested": round(net_return, 8) if net_return is not None else None,
        "mean_market_return": round(mean(returns), 8) if returns else None,
        "median_market_return": round(median(returns), 8) if returns else None,
        "market_return_stddev": round(pstdev(returns), 8) if len(returns) > 1 else None,
        "max_drawdown": round(max_drawdown, 8),
        "max_drawdown_fraction": round(drawdown_fraction, 8) if drawdown_fraction is not None else None,
        "risk_adjusted_score": round(risk_adjusted, 8) if risk_adjusted is not None else None,
        "equity_curve": [round(value, 8) for value in equity_curve],
        "market_results": [{"slug": item["row"].get("slug"), "pnl": round(item["pnl"], 8), "invested": round(item["invested"], 8), "return": round(item["return"], 8)} for item in usable],
    }


def _shared_pool_metrics(rows: list[dict[str, Any]], initial_capital: float) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (str(row.get("window_end_utc") or row.get("window_start_utc") or ""), str(row.get("slug") or "")))
    cash = float(initial_capital)
    peak = cash
    max_drawdown = 0.0
    invested = 0.0
    completed = 0
    blocked = 0
    curve = [cash]
    results = []
    for row in ordered:
        pnl = _number(row.get("paper_final_pnl"))
        deployed = _number(row.get("paper_total_buy_cost"))
        if not row.get("paper_settled") or not row.get("paper_entry_actual_utc") or pnl is None or deployed is None or deployed <= 0:
            continue
        if deployed > cash + 1e-9:
            blocked += 1
            continue
        cash -= deployed
        cash += deployed + pnl
        invested += deployed
        completed += 1
        peak = max(peak, cash)
        max_drawdown = max(max_drawdown, peak - cash)
        curve.append(cash)
        results.append({"slug": row.get("slug"), "pnl": round(pnl, 8), "invested": round(deployed, 8), "return": round(pnl / deployed, 8)})
    net_pnl = cash - initial_capital
    return {
        "initial_capital": round(initial_capital, 8),
        "final_capital": round(cash, 8),
        "filled_and_settled_markets": completed,
        "capital_blocked_markets": blocked,
        "total_invested": round(invested, 8),
        "net_pnl": round(net_pnl, 8),
        "return_on_initial_capital": round(net_pnl / initial_capital, 8) if initial_capital > 0 else None,
        "max_drawdown": round(max_drawdown, 8),
        "max_drawdown_fraction": round(max_drawdown / initial_capital, 8) if initial_capital > 0 else None,
        "equity_curve": [round(value, 8) for value in curve],
        "market_results": results,
    }


def _walk_forward(configs: dict[str, dict[str, Any]], ordered_slugs: list[str]) -> dict[str, Any]:
    n = len(ordered_slugs)
    if n < 4:
        return {"folds": [], "validated_markets": 0, "message": "样本少于4个已成交市场，暂不输出样本外选择"}
    validation_size = max(1, n // 5)
    train_size = max(3, n // 2)
    folds = []
    start = train_size
    while start < n:
        validation_slugs = ordered_slugs[start:min(n, start + validation_size)]
        if not validation_slugs:
            break
        train_slugs = set(ordered_slugs[max(0, start - train_size):start])
        ranking = []
        for label, data in configs.items():
            train_rows = [row for row in data["rows"] if row.get("slug") in train_slugs]
            metric = _metrics(train_rows, data["budget"])
            if metric.get("risk_adjusted_score") is not None:
                ranking.append((metric["risk_adjusted_score"], label))
        if not ranking:
            start += validation_size
            continue
        ranking.sort(reverse=True)
        selected = ranking[0][1]
        selected_data = configs[selected]
        validation_rows = [row for row in selected_data["rows"] if row.get("slug") in validation_slugs]
        folds.append({"train_markets": len(train_slugs), "validation_markets": len(validation_slugs), "selected_config": selected, "train_risk_adjusted_score": ranking[0][0], "validation": _metrics(validation_rows, selected_data["budget"])})
        start += validation_size
    return {"folds": folds, "validated_markets": sum(fold["validation_markets"] for fold in folds)}


def run_grid_research(
    replay: Callable[[int, float], dict[str, Any]],
    *, entry_minutes: Iterable[int] = DEFAULT_ENTRY_MINUTES,
    coverage_grid: Iterable[float] = DEFAULT_COVERAGE_GRID,
    market_budget: float = DEFAULT_MARKET_BUDGET,
    control: Any = None,
) -> dict[str, Any]:
    """Run the fixed grid through a supplied read-only replay callback."""
    budget = float(market_budget)
    if not budget > 0:
        raise ValueError("market_budget must be positive")
    configs: dict[str, dict[str, Any]] = {}
    entry_values = tuple(max(0, int(minutes)) for minutes in entry_minutes)
    coverage_values = tuple(float(coverage) for coverage in coverage_grid)
    if any(coverage <= 0 or coverage > 1 for coverage in coverage_values):
        raise ValueError("coverage_grid values must be in (0, 1]")
    combinations = [GridConfig(minutes, coverage) for minutes in entry_values for coverage in coverage_values]
    if not combinations:
        raise ValueError("research grid is empty")
    for index, config in enumerate(combinations, 1):
        if control is not None:
            control.check()
            control.completed = index - 1
            control.total = len(combinations)
        result = replay(config.entry_minutes, config.probability_coverage)
        rows = _market_rows(result)
        configs[config.label] = {"config": config, "rows": rows, "budget": budget, "summary": result.get("summary") or {}}
    first_rows = next(iter(configs.values()))["rows"]
    all_slugs = [str(row.get("slug")) for row in sorted(
        first_rows,
        key=lambda row: (str(row.get("window_end_utc") or row.get("window_start_utc") or ""), str(row.get("slug") or "")),
    ) if row.get("slug")]
    results = []
    for label, data in configs.items():
        fixed = _metrics(data["rows"], budget)
        shared = _shared_pool_metrics(data["rows"], budget)
        results.append({"config": {"label": label, "entry_minutes": data["config"].entry_minutes, "probability_coverage": data["config"].probability_coverage, "probability_policy": "center" if data["config"].probability_coverage <= 0.01 else "center_symmetric"}, "fixed_budget": fixed, "shared_pool": shared})
    results.sort(key=lambda item: (item["fixed_budget"].get("risk_adjusted_score") is not None, item["fixed_budget"].get("risk_adjusted_score") or float("-inf")), reverse=True)
    config_map = {item["config"]["label"]: configs[item["config"]["label"]] for item in results}
    walk_forward = _walk_forward(config_map, all_slugs)
    return {"schema_version": "paper_grid_research_v1", "do_not_auto_trade": True, "market_budget": budget, "entry_minutes": list(entry_values), "coverage_grid": list(coverage_values), "selection_metric": "return_on_invested - max_drawdown_fraction", "results": results, "best_fixed_budget": results[0]["config"]["label"] if results else None, "walk_forward": walk_forward}
