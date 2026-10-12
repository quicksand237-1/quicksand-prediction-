"""Opt-in research features. No storage, network or fitted-model dependency."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(timezone.utc)


def nonnegative(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(name + " must be finite and nonnegative")
    return value


@dataclass(frozen=True)
class Post:
    key: str
    occurred_at: datetime
    available_at: datetime


@dataclass(frozen=True)
class Coverage:
    """A verified observation interval [start, end), not inferred from posts."""
    start: datetime
    end: datetime
    available_at: datetime


@dataclass(frozen=True)
class Event:
    key: str
    occurred_at: datetime
    available_at: datetime
    approved_at: datetime
    impact: float


def _posts_at(posts: list[Post], cutoff: datetime) -> list[tuple[str, datetime]]:
    unique = {}
    for post in posts:
        occurred, available = utc(post.occurred_at), utc(post.available_at)
        if occurred >= cutoff or available > cutoff:
            continue
        if not post.key:
            raise ValueError("post identity required for deduplication")
        if post.key in unique and unique[post.key] != occurred:
            raise ValueError("conflicting post timestamps")
        unique[post.key] = occurred
    return list(unique.items())


def _coverage_at(coverage: list[Coverage], start: datetime, end: datetime,
                 cutoff: datetime) -> list[tuple[datetime, datetime]]:
    intervals = []
    for row in coverage:
        left, right = utc(row.start), utc(row.end)
        if right <= left:
            raise ValueError("coverage must have positive duration")
        if utc(row.available_at) > cutoff or right > cutoff:
            continue
        left, right = max(left, start), min(right, end)
        if left < right:
            intervals.append((left, right))
    merged = []
    for left, right in sorted(intervals):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
        else:
            merged.append((left, right))
    return merged


def _hour_parts(start: datetime, end: datetime, zone: ZoneInfo):
    # Walk absolute time so repeated/missing DST hours have actual exposure.
    cursor = start
    while cursor < end:
        local = cursor.astimezone(zone)
        local_remainder = timedelta(minutes=60 - local.minute, seconds=-local.second,
                                    microseconds=-local.microsecond)
        stop = min(end, cursor + local_remainder,
                   cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        yield local.weekday() * 24 + local.hour, (stop - cursor).total_seconds() / 3600
        cursor = stop


def historical_profile(posts: list[Post], coverage: list[Coverage], *,
                       cutoff: datetime, timezone_name: str = "America/New_York",
                       shrink_hours: float = 24.0) -> dict:
    cutoff = utc(cutoff)
    shrink_hours = nonnegative(shrink_hours, "shrink_hours")
    if shrink_hours == 0:
        raise ValueError("shrink_hours must be positive")
    zone = ZoneInfo(timezone_name)
    intervals = _coverage_at(coverage, datetime.min.replace(tzinfo=timezone.utc), cutoff, cutoff)
    hours, counts = [0.0] * 168, [0] * 168
    for left, right in intervals:
        for slot, duration in _hour_parts(left, right, zone):
            hours[slot] += duration
    for _, occurred in _posts_at(posts, cutoff):
        if any(left <= occurred < right for left, right in intervals):
            local = occurred.astimezone(zone)
            counts[local.weekday() * 24 + local.hour] += 1
    exposure = sum(hours)
    baseline = sum(counts) / exposure if exposure else None
    rates = [(count + shrink_hours * baseline) / (duration + shrink_hours)
             for count, duration in zip(counts, hours)] if baseline is not None else [None] * 168
    return {"schema_version": "historical_profile_v2", "do_not_auto_trade": True,
            "cutoff_utc": cutoff.isoformat(), "timezone": timezone_name,
            "global_rate": baseline, "slot_rates": rates, "slot_hours": hours,
            "slot_counts": counts, "shrink_hours": shrink_hours,
            "quality_flags": [] if exposure else ["history_coverage_missing"]}


def build_features_v2(*, slug: str, start: datetime, end: datetime, as_of: datetime,
                      posts: list[Post], coverage: list[Coverage], events: list[Event],
                      prior_hours: float = 6.0, shrink_hours: float = 24.0,
                      timezone_name: str = "America/New_York") -> dict:
    start, end, as_of = utc(start), utc(end), utc(as_of)
    if not slug or not start <= as_of <= end or start == end:
        raise ValueError("slug and valid market/checkpoint boundaries required")
    prior_hours = nonnegative(prior_hours, "prior_hours")
    if prior_hours == 0:
        raise ValueError("prior_hours must be positive")
    # Freeze historical reference at market start, independent of later labels.
    profile = historical_profile(posts, coverage, cutoff=start,
                                 timezone_name=timezone_name, shrink_hours=shrink_hours)
    usable = [t for _, t in _posts_at(posts, as_of) if start <= t < as_of]
    observed = _coverage_at(coverage, start, as_of, as_of)
    observed_hours = sum((r - l).total_seconds() / 3600 for l, r in observed)
    covered_count = sum(any(l <= t < r for l, r in observed) for t in usable)
    elapsed = (as_of - start).total_seconds() / 3600
    prior = profile["global_rate"]
    smooth = ((covered_count + prior * prior_hours) / (observed_hours + prior_hours)
              if prior is not None else (covered_count / observed_hours if observed_hours else None))
    expected = (sum(profile["slot_rates"][slot] * duration
                    for slot, duration in _hour_parts(as_of, end, ZoneInfo(timezone_name)))
                if prior is not None else None)
    flags = list(profile["quality_flags"])
    if observed_hours + 1e-9 < elapsed:
        flags.append("current_coverage_incomplete")
    if prior is None:
        flags.append("smoothing_prior_unavailable")
    values = {"current_count": len(usable), "total_hours": (end - start).total_seconds() / 3600,
              "elapsed_hours": elapsed, "remaining_hours": (end - as_of).total_seconds() / 3600,
              "observed_hours": observed_hours,
              "coverage_fraction": observed_hours / elapsed if elapsed else 0.0,
              "smoothed_rate": smooth, "seasonal_remaining_count": expected,
              "seasonal_final_count": len(usable) + expected if expected is not None else None}
    for hours in (1, 3, 6, 12, 24, 48):
        left = max(start, as_of - timedelta(hours=hours))
        intervals = [(max(l, left), r) for l, r in observed if r > left]
        exposure = sum((r - l).total_seconds() / 3600 for l, r in intervals)
        count = sum(any(l <= t < r for l, r in intervals) for t in usable)
        values[f"recent_{hours}h_rate"] = count / exposure if exposure else None
        values[f"recent_{hours}h_coverage"] = exposure / hours
    seen_events = {}
    for event in events:
        occurred = utc(event.occurred_at)
        if max(occurred, utc(event.available_at), utc(event.approved_at)) > as_of:
            continue
        if not event.key or not math.isfinite(event.impact):
            raise ValueError("valid event identity and impact required")
        signature = (occurred, event.impact)
        if event.key in seen_events and seen_events[event.key] != signature:
            raise ValueError("conflicting event revisions require as-of resolution")
        seen_events[event.key] = signature
    for tau in (3, 12, 24, 72):
        for sign in ("positive", "negative"):
            values[f"event_{sign}_tau_{tau}h"] = sum(
                impact * math.exp(-(as_of - occurred).total_seconds() / (3600 * tau))
                for occurred, impact in seen_events.values()
                if (impact >= 0) == (sign == "positive"))
    return {"schema_version": "research_features_v2", "slug": slug,
            "as_of_utc": as_of.isoformat(), "history_cutoff_utc": start.isoformat(),
            "parameters": {"prior_hours": prior_hours, "shrink_hours": shrink_hours,
                           "timezone": timezone_name, "event_tau_hours": [3, 12, 24, 72]},
            "features": values, "history_profile": profile, "quality_flags": flags,
            "model_ready": False, "do_not_auto_trade": True}
