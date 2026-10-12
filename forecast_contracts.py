#!/usr/bin/env python3
"""Stable contracts between storage, forecasting, decisions and the UI.

The existing pipeline remains the source of calculations and persistence.  This
module only adds versioned, JSON-safe envelopes so each layer can evolve
independently while older dashboard fields continue to work.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping


FEATURE_SCHEMA = "feature_snapshot_v1"
PREDICTION_SCHEMA = "prediction_output_v1"
DECISION_SCHEMA = "decision_output_v1"
DISPLAY_SCHEMA = "display_snapshot_v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _utc(value: Any) -> str | None:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return str(value) if value else None


def build_feature_snapshot(
    *,
    slug: str,
    as_of_utc: Any,
    features: Mapping[str, Any] | None = None,
    feature_names: list[str] | tuple[str, ...] | None = None,
    sources: Mapping[str, Any] | None = None,
    quality_flags: list[str] | None = None,
    market_coordinates: Mapping[str, Any] | None = None,
    calendar_coordinates: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a leakage-auditable feature envelope without changing values."""
    values = _mapping(features)
    names = list(feature_names or values.keys())
    return {
        "schema_version": FEATURE_SCHEMA,
        "slug": str(slug),
        "as_of_utc": _utc(as_of_utc),
        "available_at_utc": _utc(as_of_utc),
        "features": values,
        "feature_names": names,
        "sources": _mapping(sources),
        "quality_flags": _list(quality_flags),
        "market_coordinates": _mapping(market_coordinates),
        "calendar_coordinates": _mapping(calendar_coordinates),
        "leakage_check": {"status": "not_run", "violations": []},
    }


def build_prediction_output(
    *,
    slug: str,
    as_of_utc: Any,
    prediction: Mapping[str, Any] | None,
    model_version: str | None = None,
    feature_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = _mapping(prediction)
    return {
        "schema_version": PREDICTION_SCHEMA,
        "slug": str(slug),
        "as_of_utc": _utc(as_of_utc),
        "model_version": model_version or result.get("model"),
        "feature_schema_version": _mapping(feature_snapshot).get("schema_version", FEATURE_SCHEMA),
        "result": result,
    }


def build_decision_output(
    *,
    slug: str,
    as_of_utc: Any,
    decision: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = _mapping(decision)
    # This is a permanent safety invariant, independent of model output.
    result["do_not_auto_trade"] = True
    return {
        "schema_version": DECISION_SCHEMA,
        "slug": str(slug),
        "as_of_utc": _utc(as_of_utc),
        "result": result,
    }


def build_display_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a live or legacy snapshot for the frontend.

    Existing top-level/nested keys are retained for backwards compatibility.
    Missing collection fields become empty lists and missing objects become
    readable empty objects instead of nulls.
    """
    source = dict(snapshot) if isinstance(snapshot, Mapping) else {}
    slug = str(source.get("slug") or "")
    generated = source.get("generated_at_utc")
    prediction = _mapping(source.get("prediction"))
    market = _mapping(source.get("market"))
    decision = _mapping(source.get("recommendation"))
    features = _mapping(source.get("features"))
    if not features:
        features = _mapping(source.get("feature_values"))
    feature_snapshot = source.get("feature_snapshot")
    if not isinstance(feature_snapshot, Mapping):
        feature_snapshot = build_feature_snapshot(
            slug=slug,
            as_of_utc=generated,
            features=features,
            sources=_mapping(source.get("sources")),
            quality_flags=_list(source.get("quality_flags")),
        )
    prediction_output = source.get("prediction_output")
    if not isinstance(prediction_output, Mapping):
        prediction_output = build_prediction_output(
            slug=slug,
            as_of_utc=generated,
            prediction=prediction,
            feature_snapshot=feature_snapshot,
        )
    decision_output = source.get("decision_output")
    if not isinstance(decision_output, Mapping):
        decision_output = build_decision_output(
            slug=slug,
            as_of_utc=generated,
            decision=decision,
        )
    normalized = dict(source)
    normalized.update({
        "schema_version": DISPLAY_SCHEMA,
        "slug": slug,
        "prediction": prediction,
        "market": {**market, "outcomes": _list(market.get("outcomes"))},
        "news": _list(source.get("news")),
        "approved_events": _list(source.get("approved_events")),
        "external_context": {
            **_mapping(source.get("external_context")),
            "flight": _mapping(_mapping(source.get("external_context")).get("flight")) or {"status": "not_configured", "items": []},
        },
        "sources": _mapping(source.get("sources")),
        "errors": _list(source.get("errors")),
        "recommendation": {**decision, "do_not_auto_trade": True},
        "feature_snapshot": dict(feature_snapshot),
        "prediction_output": dict(prediction_output),
        "decision_output": dict(decision_output),
    })
    flight = normalized["external_context"]["flight"]
    flight["status"] = str(flight.get("status") or "not_configured")
    flight["items"] = _list(flight.get("items"))
    return normalized

