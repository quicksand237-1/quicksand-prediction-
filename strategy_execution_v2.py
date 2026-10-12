"""Pure opt-in paper execution against archived depth, without I/O or models."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math


def _utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(timezone.utc)


def _number(value, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(name + " must be finite and nonnegative")
    return result


def adapt_archived_book_v2(row: dict, *, slug: str, token_id: str,
                           as_of: datetime, max_age_seconds: float = 300.0) -> dict:
    """Accept an archive row, never fetch a book or infer a missing timestamp."""
    as_of = _utc(as_of)
    age_limit = _number(max_age_seconds, "max_age_seconds")
    result = {"schema_version": "paper_book_v2", "slug": slug, "token_id": token_id,
              "as_of_utc": as_of.isoformat(), "max_age_seconds": age_limit,
              "point_in_time_safe": False, "reason": "invalid_archive",
              "bids": [], "asks": [], "do_not_auto_trade": True}
    if not slug or not token_id or row.get("event_slug") != slug or str(row.get("token_id")) != token_id:
        result["reason"] = "market_identity_mismatch"
        return result
    try:
        source = _utc(row["source_timestamp_utc"])
        captured = _utc(row["captured_at_utc"])
        if source > as_of or captured > as_of or source > captured:
            result["reason"] = "unsafe_book_time"
            return result
        if (as_of - source).total_seconds() > age_limit:
            result["reason"] = "stale_book"
            return result
        raw = json.loads(row["raw_json"]) if isinstance(row["raw_json"], str) else row["raw_json"]
        for side in ("bids", "asks"):
            if not isinstance(raw.get(side), list):
                result["reason"] = "full_depth_missing"
                return result
            grouped = {}
            for level in raw[side]:
                price = _number(level["price"], "price")
                size = _number(level["size"], "size")
                if price > 1:
                    raise ValueError("price outside [0,1]")
                grouped[price] = grouped.get(price, 0.0) + size
            result[side] = [(price, size) for price, size in sorted(
                grouped.items(), reverse=side == "bids") if size > 0]
        if result["bids"] and result["asks"] and result["bids"][0][0] > result["asks"][0][0]:
            result["reason"] = "crossed_book"
            return result
        result.update(source_timestamp_utc=source.isoformat(), captured_at_utc=captured.isoformat(),
                      point_in_time_safe=True, reason="safe_archive")
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        result["point_in_time_safe"] = False
        result["reason"] = "invalid_archive"
    return result


def execute_paper_order_v2(*, book: dict, slug: str, token_id: str, as_of: datetime,
                           side: str, quantity: float, position_quantity: float,
                           remaining_cash: float, remaining_turnover: float,
                           consumed: dict[str, float] | None = None,
                           fee_rate: float = 0.02, extra_slippage_bps: float = 0.0) -> dict:
    """Return updated balances/depth without mutating the caller's state.

    Carry consumed across checkpoints and outcomes for the entire experiment;
    reset turnover only at the start of a new market checkpoint. Turnover is
    gross executed notional; buy fees additionally consume cash.
    """
    as_of = _utc(as_of)
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    quantity = _number(quantity, "quantity")
    position = _number(position_quantity, "position_quantity")
    cash = _number(remaining_cash, "remaining_cash")
    turnover = _number(remaining_turnover, "remaining_turnover")
    fee_rate = _number(fee_rate, "fee_rate")
    if fee_rate > 1:
        raise ValueError("fee_rate must be <= 1")
    slip = _number(extra_slippage_bps, "extra_slippage_bps") / 10000
    used = {key: _number(value, "consumed depth") for key, value in (consumed or {}).items()}
    result = {"schema_version": "paper_execution_v2", "slug": slug, "token_id": token_id,
              "as_of_utc": as_of.isoformat(), "action": "no_fill", "reason": "unsafe_book",
              "side": side, "requested_quantity": quantity, "filled_quantity": 0.0,
              "unfilled_quantity": quantity, "average_price": None, "fees": 0.0,
              "gross_notional": 0.0, "cash_delta": 0.0, "position_quantity": position,
              "remaining_cash": cash, "remaining_turnover": turnover, "consumed": used,
              "parameters": {"fee_rate": fee_rate, "extra_slippage_bps": extra_slippage_bps,
                             "price_band": 0.05}, "fills": [], "do_not_auto_trade": True}
    if book.get("slug") != slug or book.get("token_id") != token_id:
        result["reason"] = "market_identity_mismatch"
        return result
    if book.get("schema_version") != "paper_book_v2" or not book.get("point_in_time_safe"):
        result["reason"] = book.get("reason", "unsafe_book")
        return result
    # Recheck at execution time so a previously safe DTO cannot become immortal.
    source = _utc(book["source_timestamp_utc"])
    captured = _utc(book["captured_at_utc"])
    if (source > captured or max(source, captured, _utc(book["as_of_utc"])) > as_of
            or (as_of - source).total_seconds() > _number(book["max_age_seconds"], "max_age_seconds")):
        result["reason"] = "unsafe_execution_time"
        return result
    levels = book["asks" if side == "buy" else "bids"]
    if not levels:
        result["reason"] = "zero_depth"
        return result
    best = levels[0][0]
    desired = quantity if side == "buy" else min(quantity, position)
    filled, notional, fees = 0.0, 0.0, 0.0
    for price, size in levels:
        if abs(price - best) > 0.05 + 1e-12:
            break
        # Source identity, rather than capture/id, prevents refilling duplicate archives.
        key = json.dumps([slug, token_id, source.isoformat(), side, price], separators=(",", ":"))
        available = max(0.0, size - used.get(key, 0.0))
        executable = min(1.0, price + slip) if side == "buy" else max(0.0, price - slip)
        if executable <= 0:
            continue
        fee = executable * fee_rate
        amount = min(desired - filled, available, turnover / executable)
        if side == "buy":
            amount = min(amount, cash / (executable + fee))
        if amount <= 1e-12:
            continue
        level_notional, level_fee = amount * executable, amount * fee
        cash += (-level_notional - level_fee) if side == "buy" else (level_notional - level_fee)
        turnover = max(0.0, turnover - level_notional)
        used[key] = used.get(key, 0.0) + amount
        filled += amount
        notional += level_notional
        fees += level_fee
        result["fills"].append({"book_price": price, "execution_price": executable,
                                "quantity": amount, "fee": level_fee})
        if filled >= desired - 1e-12:
            break
    result.update(action=side if filled else "no_fill",
                  reason="filled" if filled >= quantity and filled else (
                      "partial_fill" if filled else "depth_or_budget_exhausted"),
                  filled_quantity=filled, unfilled_quantity=max(0.0, quantity - filled),
                  average_price=notional / filled if filled else None,
                  fees=fees, gross_notional=notional, cash_delta=cash - remaining_cash,
                  position_quantity=position + (filled if side == "buy" else -filled),
                  remaining_cash=max(0.0, cash), remaining_turnover=turnover)
    return result
