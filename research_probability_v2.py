"""Experimental shifted count distributions; legacy model semantics stay intact.

SciPy is required only when this explicitly opted-in module is called.
"""
from __future__ import annotations

from datetime import datetime
import math

from research_features_v2 import nonnegative, utc


def predict_probabilities_v2(*, slug: str, as_of: datetime, observed_count: int,
                             predicted_final: float,
                             ranges: list[tuple[str, int, int | None]],
                             variance: dict | None = None) -> dict:
    from scipy.stats import nbinom, poisson

    as_of = utc(as_of)
    if not slug or type(observed_count) is not int or observed_count < 0:
        raise ValueError("slug and nonnegative integer observed_count required")
    if not math.isfinite(predicted_final):
        raise ValueError("finite predicted_final required")
    mean = max(float(predicted_final) - observed_count, 0.0)
    requested, source, flags = None, "poisson_baseline", []
    if variance is not None:
        if variance.get("semantics") != "remaining_count_variance":
            flags.append("variance_semantics_rejected")
        elif utc(variance["available_at"]) > as_of or utc(variance["trained_through"]) > as_of:
            flags.append("future_variance_rejected")
        else:
            requested = nonnegative(variance["value"], "variance")
            source = str(variance["source"])
    if requested is None:
        flags.append("variance_missing_poisson_baseline")
    effective = mean if requested is None else requested
    if mean == 0:
        kind, effective, distribution = "point_mass", 0.0, None
        if requested not in (None, 0):
            flags.append("zero_mean_variance_reset")
    elif effective <= mean + 1e-9:
        if effective < mean:
            flags.append("underdispersion_poisson_fallback")
        kind, effective, distribution = "poisson", mean, poisson(mu=mean)
    else:
        shape = mean * mean / (effective - mean)
        kind = "negative_binomial"
        distribution = nbinom(n=shape, p=shape / (shape + mean))
    ordered = sorted(ranges, key=lambda item: item[1])
    labels = set()
    previous = -1
    for label, lower, upper in ordered:
        if (not label or label in labels or type(lower) is not int or lower < 0
                or (upper is not None and (type(upper) is not int or upper < lower))
                or lower <= previous):
            raise ValueError("distinct labels and disjoint nonnegative integer ranges required")
        labels.add(label)
        previous = upper if upper is not None else math.inf
    probabilities = {}
    for label, lower, upper in ranges:
        left = max(lower - observed_count, 0)
        right = upper - observed_count if upper is not None else None
        if right is not None and right < 0:
            probability = 0.0
        elif distribution is None:
            probability = float(left == 0)
        elif right is None:
            probability = float(distribution.sf(left - 1))
        elif left > mean:
            probability = float(distribution.sf(left - 1) - distribution.sf(right))
        else:
            probability = float(distribution.cdf(right) - distribution.cdf(left - 1))
        if not math.isfinite(probability):
            raise ValueError("distribution parameters exceed supported numerical range")
        probabilities[label] = max(0.0, min(1.0, probability))
    # Structural coverage matters even when omitted mass underflows numerically.
    next_count, complete = observed_count, False
    for _, lower, upper in ordered:
        if upper is not None and upper < next_count:
            continue
        if lower > next_count:
            break
        if upper is None:
            complete = True
            break
        next_count = upper + 1
    if not complete:
        flags.append("outcome_coverage_incomplete")
    return {"schema_version": "research_probability_v2", "slug": slug,
            "as_of_utc": as_of.isoformat(), "observed_count": observed_count,
            "remaining_mean": mean, "distribution": kind,
            "variance_source": source, "requested_variance": requested,
            "effective_variance": effective, "bin_probabilities": probabilities,
            "uncovered_probability": max(0.0, 1.0 - sum(probabilities.values())),
            "coverage_complete": complete, "safe_edge_allowed": complete,
            "quality_flags": flags, "do_not_auto_trade": True}


def prediction_for_context_v2(*, context: str, **inputs) -> dict:
    """One opt-in adapter for both live and review callers, with no I/O."""
    if context not in ("live", "backtest_review"):
        raise ValueError("unknown prediction context")
    return predict_probabilities_v2(**inputs)
