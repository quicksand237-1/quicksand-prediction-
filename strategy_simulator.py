"""Pure paper-position simulation primitives.

This module intentionally has no database, network, model, or frontend imports.
It is safe to use from historical replay tests and future strategy experiments.
"""
from __future__ import annotations

from typing import Any

# Explicit experimental exports; legacy rebalance keeps its existing behavior.
from strategy_execution_v2 import adapt_archived_book_v2, execute_paper_order_v2


# Keep the paper strategy contract in one dependency-free module so callers
# cannot accidentally drift from the documented defaults.
DEFAULT_ENTRY_CHECKPOINTS = 12
DEFAULT_ENTRY_EDGE = 0.08
DEFAULT_RETAIN_EDGE = 0.04
DEFAULT_EXIT_EDGE = 0.0
DEFAULT_MAX_OUTCOME_WEIGHT = 0.40
DEFAULT_MAX_TURNOVER_FRACTION = 0.25
DEFAULT_FEE_RATE = 0.02
DEFAULT_SLIPPAGE_BPS = 50.0
DEFAULT_TAKE_PROFIT_RETURN = 0.20
DEFAULT_STOP_LOSS_RETURN = -0.15
DEFAULT_PROBABILITY_COVERAGE = 0.80


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def paper_buy_cost(best_ask: float | None, fee_rate: float, slippage_bps: float) -> tuple[float | None, float | None]:
    if best_ask is None or not 0 <= best_ask <= 1:
        return None, None
    executable = min(1.0, best_ask + max(0.0, slippage_bps) / 10000.0)
    return executable, best_ask * max(0.0, fee_rate)


def mandatory_probability_weights(
    outcomes: list[dict[str, Any]], *, probability_coverage: float = DEFAULT_PROBABILITY_COVERAGE,
    support_lower: float | None = None, support_upper: float | None = None,
    forecast_count: float | None = None,
) -> dict[str, float]:
    """Select a contiguous forecast-centred set and exclude unsupported tails."""
    eligible = []
    for row in sorted(outcomes, key=lambda r: float(r.get("lower_count") or 0)):
        probability = _number(row.get("model_probability"))
        lower = _number(row.get("lower_count"))
        upper = _number(row.get("upper_count"))
        intersects = (
            (support_upper is None or lower is None or lower <= support_upper)
            and (support_lower is None or upper is None or upper >= support_lower)
        )
        if probability is not None and intersects:
            eligible.append((row, max(0.0, probability)))
    if not eligible or sum(p for _, p in eligible) <= 0:
        return {}
    anchor = max(range(len(eligible)), key=lambda index: eligible[index][1])
    if forecast_count is not None:
        anchor = next((i for i, (r, _) in enumerate(eligible)
                       if (r.get("lower_count") is None or r["lower_count"] <= forecast_count)
                       and (r.get("upper_count") is None or r["upper_count"] >= forecast_count)), anchor)
    target = min(1.0, max(0.0, float(probability_coverage)))
    if target <= 0:
        return {}
    remaining = target
    weights = {}
    def allocate(indices):
        nonlocal remaining
        mass = sum(eligible[i][1] for i in indices)
        consumed = min(remaining, mass)
        if mass > 0:
            for i in indices:
                weights[str(eligible[i][0]["outcome_label"])] = consumed * eligible[i][1] / mass / target
        remaining -= consumed
    allocate([anchor])
    left, right = anchor - 1, anchor + 1
    while remaining > 1e-12 and (left >= 0 or right < len(eligible)):
        allocate(([left] if left >= 0 else []) + ([right] if right < len(eligible) else []))
        left -= 1
        right += 1
    return {label: weight for label, weight in weights.items() if weight > 0}


def select_mandatory_outcomes(outcomes, **kwargs):
    return list(mandatory_probability_weights(outcomes, **kwargs))


def paper_position_rebalance(
    outcomes: list[dict[str, Any]], positions: dict[str, dict[str, float]], *,
    checkpoint_index: int, paper_budget: float,
    account: dict[str, float] | None = None,
    entry_checkpoints: int = DEFAULT_ENTRY_CHECKPOINTS,
    entry_edge: float = DEFAULT_ENTRY_EDGE,
    retain_edge: float = DEFAULT_RETAIN_EDGE,
    exit_edge: float = DEFAULT_EXIT_EDGE,
    max_outcome_weight: float = DEFAULT_MAX_OUTCOME_WEIGHT,
    max_turnover_fraction: float = DEFAULT_MAX_TURNOVER_FRACTION,
    fee_rate: float = DEFAULT_FEE_RATE,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    mandatory_entry: bool = False,
    probability_coverage: float = DEFAULT_PROBABILITY_COVERAGE,
    support_lower: float | None = None,
    support_upper: float | None = None,
    take_profit_return: float = DEFAULT_TAKE_PROFIT_RETURN,
    stop_loss_return: float = DEFAULT_STOP_LOSS_RETURN,
    risk_exits: bool = False,
    hold_to_settlement: bool = False,
    forecast_count: float | None = None,
) -> tuple[list[dict[str, Any]], float, float]:
    """Rebalance in-memory YES positions using only safe historical quotes."""
    budget = max(0.0, float(paper_budget))
    if budget <= 0 or not outcomes:
        return [], 0.0, 0.0
    if account is None:
        account = {"cash": budget + sum(p.get("realized_pnl", 0.0) - p.get("quantity", 0.0) * p.get("avg_cost", 0.0) for p in positions.values())}
    account.setdefault("cash", budget)
    account.setdefault("total_buy_cost", 0.0)
    account.setdefault("total_sell_proceeds", 0.0)
    account.setdefault("fees_paid", 0.0)
    eligible = []
    for row in outcomes:
        edge = _number(row.get("paper_net_edge_after_cost")); ask = _number(row.get("best_ask"))
        if row.get("entry_allowed", True) and edge is not None and edge >= entry_edge and ask is not None and ask > 0 and row.get("point_in_time_safe"):
            eligible.append((row, edge))
    total_edge = sum(max(0.0, edge) for _, edge in eligible)
    targets = {
        str(row["outcome_label"]): budget * min(max(0.0, max_outcome_weight), edge / total_edge) / float(row["best_ask"])
        for row, edge in eligible
    } if total_edge > 0 else {}
    mandatory_weights = mandatory_probability_weights(
        outcomes, probability_coverage=probability_coverage,
        support_lower=support_lower, support_upper=support_upper,
        forecast_count=forecast_count,
    ) if mandatory_entry else {}
    mandatory_labels = list(mandatory_weights)
    mandatory_rows = [row for row in outcomes if str(row.get("outcome_label") or "") in mandatory_labels]
    tradable_mandatory = [row for row in mandatory_rows if row.get("point_in_time_safe") and (_number(row.get("best_ask")) or 0) > 0 and (_number(row.get("ask_depth_5")) or 0) > 0]
    mandatory_allocations: dict[str, float] = {}
    if mandatory_entry:
        targets = {}
    if tradable_mandatory:
        for row in tradable_mandatory:
            label = str(row["outcome_label"])
            allocation = budget * mandatory_weights[label]
            executable, fee = paper_buy_cost(_number(row.get("best_ask")), fee_rate, slippage_bps)
            if executable is not None and fee is not None:
                targets[label] = allocation / max(executable + fee, 1e-9)
                mandatory_allocations[label] = allocation
    cap = budget * (1.0 if mandatory_entry else max(0.0, float(max_turnover_fraction))); realized_delta = 0.0; actions = []
    initial_cap = cap
    # Sell first, then allocate the shared remaining cash and turnover proportionally.
    buy_demands = {}
    for row in outcomes:
        label = str(row.get("outcome_label") or "")
        current = positions.get(label, {}).get("quantity", 0.0)
        if label in targets and (mandatory_entry or current > 0 or checkpoint_index < entry_checkpoints):
            buy_demands[label] = min(max(0.0, targets[label] - current), max(0.0, _number(row.get("ask_depth_5")) or 0.0))
    buy_scale = None
    def sell_priority(row):
        position = positions.get(str(row.get("outcome_label")), {})
        bid = _number(row.get("best_bid"))
        cost = position.get("avg_cost", 0)
        net = max(0.0, (bid or 0) - slippage_bps / 10000) - (bid or 0) * fee_rate
        ratio = net / cost - 1 if cost > 0 else 0
        exits = risk_exits and (ratio <= stop_loss_return or ratio >= take_profit_return)
        return (0 if position.get("quantity", 0) > 0 and (exits or (_number(row.get("paper_net_edge_after_cost")) or 0) < retain_edge) else 1, str(row.get("outcome_label")))
    ordered = sorted(outcomes, key=sell_priority)
    for row in ordered:
        label = str(row.get("outcome_label") or "")
        if not label: continue
        state = positions.setdefault(label, {"quantity": 0.0, "avg_cost": 0.0, "realized_pnl": 0.0})
        current = max(0.0, float(state.get("quantity", 0.0))); edge = _number(row.get("paper_net_edge_after_cost"))
        ask = _number(row.get("best_ask")); bid = _number(row.get("best_bid")); safe = bool(row.get("point_in_time_safe"))
        desired, reason = current, (row.get("book_rejection_reason") or "hold_data_insufficient") if not safe else ("entry_window_closed" if current <= 0 and checkpoint_index >= entry_checkpoints else "edge_below_entry")
        if safe and not row.get("entry_allowed", True):
            reason = row.get("entry_block_reason") or "probability_risk_guard"
        if mandatory_entry and label in mandatory_labels:
            if label in targets:
                desired, reason = targets[label], "mandatory_entry"
            else:
                reason = row.get("book_rejection_reason") or "mandatory_entry_no_valid_book"
        elif not mandatory_entry and current <= 0 and label in targets and checkpoint_index < max(0, int(entry_checkpoints)):
            desired, reason = targets[label], "enter"
        elif current > 0 and hold_to_settlement:
            desired, reason = current, "hold_to_settlement"
        elif current > 0:
            avg_cost = float(state.get("avg_cost", 0.0))
            sell_price = max(0.0, bid - max(0.0, slippage_bps) / 10000.0) if bid is not None else None
            sell_fee = bid * max(0.0, fee_rate) if bid is not None else None
            holding_return = ((sell_price - sell_fee) / avg_cost - 1.0) if sell_price is not None and sell_fee is not None and avg_cost > 0 else None
            if not safe: reason = "hold_data_insufficient"
            elif risk_exits and holding_return is not None and holding_return <= stop_loss_return: desired, reason = 0.0, "stop_loss"
            elif risk_exits and holding_return is not None and holding_return >= take_profit_return: desired, reason = 0.0, "take_profit"
            elif edge is None: reason = "hold_data_insufficient"
            elif edge <= exit_edge: desired, reason = 0.0, "exit_edge_below_zero"
            elif edge < retain_edge: desired, reason = current * max(0.0, edge / max(retain_edge, 1e-9)), "reduce_below_retain"
            elif edge >= entry_edge and label in targets: desired, reason = max(current, targets[label]), "increase_high_edge"
            else: reason = "hold_hysteresis"
        desired_delta = desired - current; executed = 0.0; execution_unit = None; action_fee = 0.0
        if desired_delta > 0 and safe and ask is not None:
            if buy_scale is None:
                gross = costs = 0.0
                for candidate in outcomes:
                    quantity = buy_demands.get(str(candidate.get("outcome_label")), 0.0)
                    price, charge = paper_buy_cost(_number(candidate.get("best_ask")), fee_rate, slippage_bps)
                    if price is not None:
                        gross += quantity * price
                        costs += quantity * (price + charge)
                buy_scale = min(1.0, cap / gross if gross else 0.0, max(0.0, account["cash"]) / costs if costs else 0.0)
            quantity = buy_demands.get(label, 0.0) * buy_scale
            executable, fee = paper_buy_cost(ask, fee_rate, slippage_bps)
            if quantity > 0 and executable is not None and fee is not None:
                quantity = min(quantity, cap / max(executable, 1e-9), max(0.0, account["cash"]) / max(executable + fee, 1e-9))
                cap -= quantity * executable
                cost = quantity * (executable + fee)
                account["cash"] -= cost
                account["total_buy_cost"] += cost
                account["fees_paid"] += quantity * fee
                execution_unit = executable + fee
                action_fee = quantity * fee
                old = current * float(state.get("avg_cost", 0.0)); state["quantity"] = current + quantity
                state["avg_cost"] = (old + quantity * (executable + fee)) / state["quantity"]; executed = quantity
        elif desired_delta < 0 and safe and bid is not None:
            quantity = min(-desired_delta, max(0.0, _number(row.get("bid_depth_5")) or 0.0), cap / max(bid, 1e-9))
            if quantity > 0:
                sell = max(0.0, bid - max(0.0, slippage_bps) / 10000.0); fee = bid * max(0.0, fee_rate)
                cap -= quantity * sell
                proceeds = quantity * (sell - fee)
                account["cash"] += proceeds
                account["total_sell_proceeds"] += proceeds
                account["fees_paid"] += quantity * fee
                execution_unit = sell - fee
                action_fee = quantity * fee
                pnl = quantity * (sell - fee - float(state.get("avg_cost", 0.0))); state["quantity"] = max(0.0, current - quantity)
                state["realized_pnl"] = float(state.get("realized_pnl", 0.0)) + pnl; realized_delta += pnl; executed = -quantity
        action = reason if executed else ("hold" if desired_delta == 0 else "no_fill")
        mark = bid if bid is not None and safe else None
        unrealized = 0.0 if state["quantity"] == 0 else (state["quantity"] * (mark - float(state.get("avg_cost", 0.0))) if mark is not None else None)
        avg_cost = float(state.get("avg_cost", 0.0))
        net_sell = max(0.0, bid - max(0.0, slippage_bps) / 10000.0 - bid * max(0.0, fee_rate)) if bid is not None else None
        holding_return = (net_sell / avg_cost - 1.0) if net_sell is not None and avg_cost > 0 else None
        action_pnl = (-executed * (execution_unit - avg_cost)) if executed < 0 and execution_unit is not None else 0.0
        cash_flow = (-executed * execution_unit) if executed < 0 and execution_unit is not None else (-(executed * execution_unit) if executed > 0 and execution_unit is not None else 0.0)
        actions.append({"outcome_label": label, "action": action, "position_before": round(current, 8), "position_after": round(float(state["quantity"]), 8), "quantity_delta": round(executed, 8), "edge_after_cost": edge, "best_bid": bid, "best_ask": ask, "average_cost": round(avg_cost, 8), "execution_price": round(execution_unit, 8) if execution_unit is not None else None, "fee_paid": round(action_fee, 8), "cash_flow": round(cash_flow, 8), "realized_pnl_delta": round(action_pnl, 8), "realized_pnl": round(float(state.get("realized_pnl", 0.0)), 8), "holding_return": round(holding_return, 8) if holding_return is not None else None, "unrealized_pnl": round(unrealized, 8) if unrealized is not None else None, "point_in_time_safe": safe, "reason": reason, "trigger_code": reason, "take_profit_return": take_profit_return, "stop_loss_return": stop_loss_return, "mandatory_selected": label in mandatory_labels, "mandatory_allocation": round(mandatory_allocations.get(label, 0.0), 8), "do_not_auto_trade": True})
        actions[-1]["requested_quantity_delta"] = round(desired_delta, 8)
        actions[-1]["probability_budget_weight"] = mandatory_weights.get(label, 0.0) if mandatory_entry else None
        actions[-1]["partial_fill"] = 0 < abs(executed) < abs(desired_delta) - 1e-8
        if mandatory_entry and label in mandatory_labels and not safe:
            actions[-1]["action"] = "no_fill"
            actions[-1]["mandatory_attempt"] = True
    account["turnover"] = initial_cap - cap
    if mandatory_entry and any(item["quantity_delta"] > 0 for item in actions):
        account["mandatory_entry_complete"] = True
    return actions, realized_delta, sum(item["unrealized_pnl"] or 0.0 for item in actions)


def settle_paper_positions(
    positions: dict[str, dict[str, float]], account: dict[str, float], resolved_label: str | None,
) -> list[dict[str, Any]]:
    """Settle remaining YES positions after an ended market is revealed."""
    actions = []
    if resolved_label is None:
        return actions
    for label, state in positions.items():
        quantity = max(0.0, float(state.get("quantity", 0.0)))
        if quantity <= 0:
            continue
        payout_per_share = 1.0 if resolved_label is not None and label == resolved_label else 0.0
        proceeds = quantity * payout_per_share
        avg_cost = float(state.get("avg_cost", 0.0))
        pnl = proceeds - quantity * avg_cost
        account["cash"] = float(account.get("cash", 0.0)) + proceeds
        account["total_sell_proceeds"] = float(account.get("total_sell_proceeds", 0.0)) + proceeds
        state["quantity"] = 0.0
        state["realized_pnl"] = float(state.get("realized_pnl", 0.0)) + pnl
        actions.append({
            "outcome_label": label, "action": "settlement", "reason": "settlement",
            "trigger_code": "settlement", "position_before": round(quantity, 8), "position_after": 0.0,
            "quantity_delta": round(-quantity, 8), "requested_quantity_delta": round(-quantity, 8),
            "execution_price": payout_per_share, "average_cost": round(avg_cost, 8),
            "cash_flow": round(proceeds, 8), "realized_pnl_delta": round(pnl, 8),
            "fee_paid": 0.0,
            "realized_pnl": round(float(state["realized_pnl"]), 8), "holding_return": None,
            "unrealized_pnl": 0.0, "point_in_time_safe": True, "partial_fill": False,
            "do_not_auto_trade": True,
        })
    return actions
