"""Leakage-safe market dynamics and fixed-entry paper research.

The functions in this module consume normalized snapshots or exported paper
rows.  They do not open databases, call networks, load models, or place trades.
"""
from __future__ import annotations

import datetime as dt
import argparse
import json
import math
from collections import defaultdict
from typing import Any, Iterable


HORIZONS_MINUTES = (5, 15, 30, 60)
DEFAULT_FEATURE_NAMES = (
    "elapsed_minutes",
    "current_count",
    "posts_5m",
    "posts_15m",
    "posts_30m",
    "posts_60m",
    "post_rate_5m",
    "post_acceleration",
    "market_expected_count",
    "model_forecast_final_count",
    "model_market_gap",
    "best_bid",
    "best_ask",
    "spread",
    "depth_5",
    "source_age_seconds",
)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _nested(snapshot: dict[str, Any], *path: str) -> Any:
    value: Any = snapshot
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _post_window(snapshot: dict[str, Any], minutes: int) -> float | None:
    candidates = (
        _nested(snapshot, "dynamics", f"posts_{minutes}m"),
        _nested(snapshot, "count", f"posts_{minutes}m"),
        _nested(snapshot, "count", "recent", f"{minutes}m"),
        snapshot.get(f"posts_{minutes}m"),
    )
    for value in candidates:
        number = _number(value)
        if number is not None:
            return number
    return None


def build_snapshot_features(snapshot: dict[str, Any]) -> dict[str, float | None]:
    """Flatten a live snapshot into point-in-time research features.

    Missing source fields remain ``None``.  The function never substitutes a
    later snapshot or treats stale data as zero.
    """
    window = snapshot.get("window") or {}
    generated = snapshot.get("generated_at_utc")
    start = window.get("start_utc")
    model_features = _nested(snapshot, "feature_snapshot", "features") or {}
    elapsed = _number(_nested(snapshot, "dynamics", "elapsed_minutes"))
    if elapsed is None:
        elapsed_hours = _number(model_features.get("elapsed_hours")) if isinstance(model_features, dict) else None
        elapsed = elapsed_hours * 60.0 if elapsed_hours is not None else None
    if elapsed is None and generated and start:
        try:
            parse = lambda value: dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            elapsed = max(0.0, (parse(generated) - parse(start)).total_seconds() / 60.0)
        except (TypeError, ValueError):
            elapsed = None
    posts_5 = _post_window(snapshot, 5)
    posts_15 = _post_window(snapshot, 15)
    post_rate = posts_5 / 5.0 if posts_5 is not None else _number(model_features.get("last_1h_rate")) if isinstance(model_features, dict) else None
    acceleration = (post_rate - posts_15 / 15.0) if post_rate is not None and posts_15 is not None else None
    expected = _number(_nested(snapshot, "market", "expected_count"))
    if expected is None and isinstance(model_features, dict):
        expected = _number(model_features.get("market_expected_count"))
    forecast = _number(_nested(snapshot, "prediction", "final_count"))
    best_bid = _number(_nested(snapshot, "market", "best_bid"))
    best_ask = _number(_nested(snapshot, "market", "best_ask"))
    spread = _number(_nested(snapshot, "market", "spread"))
    depth = _number(_nested(snapshot, "market", "depth_5"))
    return {
        "elapsed_minutes": elapsed,
        "current_count": _number(_nested(snapshot, "count", "current")),
        "posts_5m": posts_5,
        "posts_15m": posts_15,
        "posts_30m": _post_window(snapshot, 30),
        "posts_60m": _post_window(snapshot, 60),
        "post_rate_5m": post_rate,
        "post_acceleration": acceleration if acceleration is not None else _number(model_features.get("rate_acceleration")) if isinstance(model_features, dict) else None,
        "market_expected_count": expected,
        "model_forecast_final_count": forecast,
        "model_market_gap": forecast - expected if forecast is not None and expected is not None else None,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "depth_5": depth,
        "source_age_seconds": _number(_nested(snapshot, "count", "tracker_refresh_age_seconds")),
    }


def _complete_rows(rows: Iterable[dict[str, Any]], feature_names: tuple[str, ...], target_names: tuple[str, ...]):
    for row in rows:
        values = [_number(row.get(name)) for name in (*feature_names, *target_names)]
        if all(value is not None for value in values):
            yield row, [float(value) for value in values[:len(feature_names)]], [float(value) for value in values[len(feature_names):]]


def _invert(matrix: list[list[float]]) -> list[list[float]]:
    size = len(matrix)
    augmented = [row[:] + [1.0 if i == j else 0.0 for j in range(size)] for i, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda index: abs(augmented[index][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("研究特征矩阵奇异，无法估计系数")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for index in range(size):
            if index == column:
                continue
            factor = augmented[index][column]
            augmented[index] = [a - factor * b for a, b in zip(augmented[index], augmented[column])]
    return [row[size:] for row in augmented]


def fit_mapping(rows: list[dict[str, Any]], feature_names=DEFAULT_FEATURE_NAMES,
                target_names=("short_change", "final_change"), alpha: float = 1.0) -> dict[str, Any]:
    """Fit an interpretable standardized ridge mapping ``X -> Y``.

    The caller must provide targets built only from timestamps after each
    checkpoint.  No future target is read or generated here.
    """
    feature_names = tuple(feature_names)
    target_names = tuple(target_names)
    usable = list(_complete_rows(rows, feature_names, target_names))
    if not usable:
        raise ValueError("没有同时具备完整特征和结果的研究样本")
    if alpha < 0 or not math.isfinite(alpha):
        raise ValueError("alpha必须是非负有限数")
    x_values = [item[1] for item in usable]
    y_values = [item[2] for item in usable]
    means = [sum(row[index] for row in x_values) / len(x_values) for index in range(len(feature_names))]
    scales = [math.sqrt(sum((row[index] - means[index]) ** 2 for row in x_values) / max(1, len(x_values) - 1)) or 1.0
              for index in range(len(feature_names))]
    design = [[1.0] + [(value - means[index]) / scales[index] for index, value in enumerate(row)] for row in x_values]
    size = len(feature_names) + 1
    gram = [[sum(row[i] * row[j] for row in design) for j in range(size)] for i in range(size)]
    for index in range(1, size):
        gram[index][index] += alpha
    inverse = _invert(gram)
    coefficients: dict[str, dict[str, float]] = {}
    standard_errors: dict[str, dict[str, float]] = {}
    intervals: dict[str, dict[str, tuple[float, float]]] = {}
    for target_index, target_name in enumerate(target_names):
        rhs = [sum(row[i] * y[target_index] for row, y in zip(design, y_values)) for i in range(size)]
        beta = [sum(inverse[i][j] * rhs[j] for j in range(size)) for i in range(size)]
        predictions = [sum(a * b for a, b in zip(row, beta)) for row in design]
        residual_variance = sum((y[target_index] - prediction) ** 2 for y, prediction in zip(y_values, predictions)) / max(1, len(design) - size)
        errors = [math.sqrt(max(0.0, residual_variance * inverse[i][i])) for i in range(size)]
        names = ("intercept",) + feature_names
        coefficients[target_name] = dict(zip(names, beta))
        standard_errors[target_name] = dict(zip(names, errors))
        intervals[target_name] = {name: (value - 1.96 * errors[index], value + 1.96 * errors[index])
                                  for index, (name, value) in enumerate(zip(names, beta))}
    return {
        "feature_names": list(feature_names), "target_names": list(target_names),
        "alpha": alpha, "rows": len(usable), "feature_means": dict(zip(feature_names, means)),
        "feature_scales": dict(zip(feature_names, scales)), "coefficients": coefficients,
        "standard_errors": standard_errors, "confidence_intervals_95": intervals,
        "methodology": "standardized_multitarget_ridge_point_in_time_rows",
    }


def _market_key(row: dict[str, Any]) -> str:
    return str(row.get("market_id") or row.get("slug") or "")


def _profitability_eligible(row: dict[str, Any]) -> bool:
    """Honor the crawler's completeness gate when it is present.

    Older exported research rows have no tag and remain usable for backwards
    compatibility.  Once a row carries an explicit eligibility value, false
    means the row is excluded instead of being counted as a zero return.
    """
    if "eligible_for_profitability_analysis" in row:
        return row.get("eligible_for_profitability_analysis") is True
    tag = row.get("data_completeness_tag")
    return tag in (None, "complete_for_profitability")


def _excluded_reason_counts(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        reasons = row.get("data_completeness_reasons") or ["incomplete_data"]
        for reason in reasons:
            counts[str(reason)] += 1
    return dict(sorted(counts.items()))


def attach_forward_targets(rows: list[dict[str, Any]], horizon_minutes: int = 15) -> list[dict[str, Any]]:
    """Attach forward-only short and final change labels to snapshot rows.

    Rows must contain ``market_id``/``slug``, ``checkpoint_utc`` and
    ``market_expected_count``.  The optional ``final_value`` is used only as a
    future label and is never copied into the feature fields.
    """
    if horizon_minutes <= 0:
        raise ValueError("horizon_minutes必须为正数")
    groups: dict[str, list[tuple[dt.datetime, dict[str, Any]]]] = defaultdict(list)
    for row in rows:
        raw = row.get("checkpoint_utc") or row.get("generated_at_utc")
        try:
            timestamp = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        groups[_market_key(row)].append((timestamp, row))
    result = []
    for values in groups.values():
        values.sort(key=lambda item: item[0])
        for index, (timestamp, row) in enumerate(values):
            current = _number(row.get("market_expected_count"))
            if current is None:
                continue
            future = None
            for future_timestamp, future_row in values[index + 1:]:
                if future_timestamp >= timestamp + dt.timedelta(minutes=horizon_minutes):
                    future = _number(future_row.get("market_expected_count"))
                    break
            if future is None:
                continue
            labeled = dict(row)
            labeled["short_change"] = future - current
            final_value = _number(row.get("final_value"))
            if final_value is not None:
                labeled["final_change"] = final_value - current
            result.append(labeled)
    return result


def evaluate_fixed_entry_times(rows: list[dict[str, Any]], candidate_minutes=range(5, 121, 5)) -> dict[str, Any]:
    """Evaluate equal-capital paper entries at fixed elapsed times."""
    excluded = [row for row in rows if not _profitability_eligible(row)]
    usable_rows = [row for row in rows if _profitability_eligible(row)]
    candidates = [int(value) for value in candidate_minutes]
    results = []
    for minute in candidates:
        eligible = [row for row in usable_rows
                    if int(row.get("entry_elapsed_minutes", -1)) == minute
                    and row.get("point_in_time_safe") is True
                    and row.get("paper_settled") is True
                    and _number(row.get("invested")) is not None
                    and _number(row.get("final_value")) is not None
                    and float(row.get("invested")) > 0]
        pnl = [float(row["final_value"]) - float(row["invested"]) for row in eligible]
        invested = sum(float(row["invested"]) for row in eligible)
        compounded = 1.0
        for row in eligible:
            compounded *= 1.0 + (float(row["final_value"]) - float(row["invested"])) / float(row["invested"])
        results.append({
            "entry_elapsed_minutes": minute, "markets": len(eligible), "invested": invested,
            "total_pnl": sum(pnl), "total_return": sum(pnl) / invested if invested else None,
            "compounded_return": compounded - 1.0 if eligible else None,
            "mean_return": sum((value / float(row["invested"]) for value, row in zip(pnl, eligible)), 0.0) / len(eligible) if eligible else None,
        })
    ranked = sorted((row for row in results if row["total_return"] is not None),
                    key=lambda row: (row["total_return"], row["markets"]), reverse=True)
    return {"candidates": results, "recommended_entry_elapsed_minutes": ranked[0]["entry_elapsed_minutes"] if ranked else None,
            "selection_metric": "total_return_equal_capital", "paper_only": True,
            "input_rows": len(rows), "eligible_rows": len(usable_rows), "excluded_rows": len(excluded),
            "excluded_reason_counts": _excluded_reason_counts(excluded),
            "profitability_filter": "complete_for_profitability"}


def walk_forward_entry_selection(rows: list[dict[str, Any]], candidate_minutes=range(5, 121, 5), min_training_markets: int = 10) -> dict[str, Any]:
    """Select an entry time from past markets and score it on later markets."""
    excluded = [row for row in rows if not _profitability_eligible(row)]
    rows = [row for row in rows if _profitability_eligible(row)]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_market_key(row)].append(row)
    ordered = sorted(grouped.items(), key=lambda item: min(str(row.get("market_end_utc") or "") for row in item[1]))
    selections = []
    evaluations = []
    for index, (market_id, market_rows) in enumerate(ordered):
        prior = [row for _, values in ordered[:index] for row in values]
        if len({ _market_key(row) for row in prior }) < min_training_markets:
            continue
        choice = evaluate_fixed_entry_times(prior, candidate_minutes).get("recommended_entry_elapsed_minutes")
        if choice is None:
            continue
        current = evaluate_fixed_entry_times(market_rows, [choice])["candidates"][0]
        selections.append({"market_id": market_id, "entry_elapsed_minutes": choice})
        evaluations.append(current)
    invested = sum(row["invested"] for row in evaluations)
    return {"selections": selections, "evaluations": evaluations,
            "total_pnl": sum(row["total_pnl"] for row in evaluations),
            "total_return": sum(row["total_pnl"] for row in evaluations) / invested if invested else None,
            "paper_only": True, "walk_forward": True, "min_training_markets": min_training_markets,
            "input_rows": len(rows) + len(excluded), "eligible_rows": len(rows),
            "excluded_rows": len(excluded), "excluded_reason_counts": _excluded_reason_counts(excluded),
            "profitability_filter": "complete_for_profitability"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline market dynamics and fixed-entry paper research")
    parser.add_argument("input_jsonl", help="JSONL of normalized research rows; no database or network access")
    parser.add_argument("--mode", choices=("entry", "walk-forward", "mapping"), default="entry")
    parser.add_argument("--min-training-markets", type=int, default=10)
    args = parser.parse_args()
    with open(args.input_jsonl, encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    if args.mode == "mapping":
        result = fit_mapping(rows)
    elif args.mode == "walk-forward":
        result = walk_forward_entry_selection(rows, min_training_markets=args.min_training_markets)
    else:
        result = evaluate_fixed_entry_times(rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
