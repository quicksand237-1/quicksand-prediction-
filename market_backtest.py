#!/usr/bin/env python3
"""
Offline backtest and first-stage training for social-post count markets.

The script replays already-ended market windows from stored XTracker posts. At
each checkpoint it only uses posts whose created_at timestamp is at or before the
checkpoint, then predicts the final count and compares it with the realized final
count for that window.

It uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Iterable
from zoneinfo import ZoneInfo


DEFAULT_DB = "data/musk_posts.sqlite"
DEFAULT_MODEL = "data/initial_model.json"
DEFAULT_REPORT = "data/backtest_predictions.csv"
DEFAULT_USER = "elonmusk"
DEFAULT_PLATFORM = "X"
DEFAULT_TZ = "America/New_York"
DEFAULT_POLYMARKET_ANCHOR = "2026-02-10T12:00:00"
DEFAULT_MARKET_URL_PREFIX = "https://polymarket.com/zh/event/elon-musk-of-tweets"
POLYMARKET_WINDOW_DAYS = 7
POLYMARKET_START_GAPS_DAYS = (3, 4)


@dataclass(frozen=True)
class MarketWindow:
    name: str
    start_utc: dt.datetime
    end_utc: dt.datetime


@dataclass(frozen=True)
class ReplayExample:
    window_name: str
    checkpoint_utc: dt.datetime
    window_start_utc: dt.datetime
    window_end_utc: dt.datetime
    current_count: int
    final_count: int
    elapsed_hours: float
    remaining_hours: float
    total_hours: float


@dataclass(frozen=True)
class ModelParams:
    blend_weight: float
    baseline_rate_per_hour: float


def parse_datetime(value: str, timezone: str = DEFAULT_TZ) -> dt.datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) == 10:
        parsed = dt.datetime.combine(dt.date.fromisoformat(text), dt.time.min, ZoneInfo(timezone))
    else:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed.astimezone(dt.timezone.utc)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def day_start(value: dt.datetime, timezone: str) -> dt.datetime:
    local = value.astimezone(ZoneInfo(timezone))
    return dt.datetime(local.year, local.month, local.day, tzinfo=ZoneInfo(timezone)).astimezone(dt.timezone.utc)


def week_start(value: dt.datetime, timezone: str) -> dt.datetime:
    local = value.astimezone(ZoneInfo(timezone))
    start_date = local.date() - dt.timedelta(days=local.weekday())
    return dt.datetime.combine(start_date, dt.time.min, ZoneInfo(timezone)).astimezone(dt.timezone.utc)


def month_start(value: dt.datetime, timezone: str) -> dt.datetime:
    local = value.astimezone(ZoneInfo(timezone))
    return dt.datetime(local.year, local.month, 1, tzinfo=ZoneInfo(timezone)).astimezone(dt.timezone.utc)


def add_month(value: dt.datetime, timezone: str) -> dt.datetime:
    local = value.astimezone(ZoneInfo(timezone))
    year = local.year + (1 if local.month == 12 else 0)
    month = 1 if local.month == 12 else local.month + 1
    return dt.datetime(year, month, 1, tzinfo=ZoneInfo(timezone)).astimezone(dt.timezone.utc)


def add_local_days(value: dt.datetime, days: int, timezone: str) -> dt.datetime:
    local = value.astimezone(ZoneInfo(timezone))
    return (local + dt.timedelta(days=days)).astimezone(dt.timezone.utc)


def polymarket_slug(start_utc: dt.datetime, end_utc: dt.datetime, timezone: str) -> str:
    start = start_utc.astimezone(ZoneInfo(timezone))
    end = end_utc.astimezone(ZoneInfo(timezone))
    return (
        f"{start.strftime('%B').lower()}-{start.day}-"
        f"{end.strftime('%B').lower()}-{end.day}"
    )


def load_post_times(db_path: Path, user: str, platform: str) -> list[dt.datetime]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            select created_at_utc
            from posts
            where user = ? and platform = ? and created_at_utc is not null
            order by created_at_utc
            """,
            (user, platform),
        ).fetchall()
    finally:
        conn.close()

    times: list[dt.datetime] = []
    for (raw_time,) in rows:
        try:
            times.append(parse_datetime(str(raw_time), DEFAULT_TZ))
        except ValueError:
            continue
    return times


def count_between(times: list[dt.datetime], start: dt.datetime, end: dt.datetime) -> int:
    return sum(1 for value in times if start <= value < end)


def build_windows(
    times: list[dt.datetime],
    window: str,
    start: dt.datetime | None,
    end: dt.datetime | None,
    timezone: str,
    polymarket_anchor: dt.datetime,
) -> list[MarketWindow]:
    if not times and (start is None or end is None):
        return []

    lower = start or times[0]
    upper = end or times[-1]
    now = utc_now()
    windows: list[MarketWindow] = []

    if window == "polymarket":
        cursor = polymarket_anchor
        gap_index = 0
        while cursor < lower:
            cursor = add_local_days(cursor, POLYMARKET_START_GAPS_DAYS[gap_index], timezone)
            gap_index = (gap_index + 1) % len(POLYMARKET_START_GAPS_DAYS)

        while cursor < upper:
            next_cursor = add_local_days(cursor, POLYMARKET_WINDOW_DAYS, timezone)
            if next_cursor <= now:
                windows.append(MarketWindow(polymarket_slug(cursor, next_cursor, timezone), cursor, next_cursor))
            cursor = add_local_days(cursor, POLYMARKET_START_GAPS_DAYS[gap_index], timezone)
            gap_index = (gap_index + 1) % len(POLYMARKET_START_GAPS_DAYS)
        return windows

    if window == "month":
        cursor = month_start(lower, timezone)
        while cursor < upper:
            next_cursor = add_month(cursor, timezone)
            if next_cursor <= now:
                local = cursor.astimezone(ZoneInfo(timezone))
                windows.append(MarketWindow(f"{local.year:04d}-{local.month:02d}", cursor, next_cursor))
            cursor = next_cursor
        return windows

    delta = dt.timedelta(days=7 if window == "week" else 1)
    cursor = week_start(lower, timezone) if window == "week" else day_start(lower, timezone)
    index = 1
    while cursor < upper:
        next_cursor = cursor + delta
        if next_cursor <= now:
            local = cursor.astimezone(ZoneInfo(timezone))
            if window == "week":
                name = f"{local.year:04d}-W{local.isocalendar().week:02d}"
            else:
                name = local.date().isoformat()
            windows.append(MarketWindow(name, cursor, next_cursor))
        cursor = next_cursor
        index += 1
    return windows


def replay_examples(
    times: list[dt.datetime],
    windows: Iterable[MarketWindow],
    step_hours: float,
    min_elapsed_hours: float,
    min_final_count: int,
) -> list[ReplayExample]:
    examples: list[ReplayExample] = []
    step = dt.timedelta(hours=step_hours)
    min_elapsed = dt.timedelta(hours=min_elapsed_hours)
    for window in windows:
        final_count = count_between(times, window.start_utc, window.end_utc)
        if final_count < min_final_count:
            continue

        checkpoint = window.start_utc + min_elapsed
        while checkpoint < window.end_utc:
            current_count = count_between(times, window.start_utc, checkpoint)
            elapsed_hours = (checkpoint - window.start_utc).total_seconds() / 3600
            remaining_hours = (window.end_utc - checkpoint).total_seconds() / 3600
            total_hours = (window.end_utc - window.start_utc).total_seconds() / 3600
            examples.append(
                ReplayExample(
                    window.name,
                    checkpoint,
                    window.start_utc,
                    window.end_utc,
                    current_count,
                    final_count,
                    elapsed_hours,
                    remaining_hours,
                    total_hours,
                )
            )
            checkpoint += step
    return examples


def baseline_rate(examples: list[ReplayExample]) -> float:
    totals: dict[str, tuple[int, float]] = {}
    for example in examples:
        totals[example.window_name] = (example.final_count, example.total_hours)
    total_posts = sum(count for count, _ in totals.values())
    total_hours = sum(hours for _, hours in totals.values())
    return total_posts / total_hours if total_hours > 0 else 0.0


def pace_projection(example: ReplayExample) -> float:
    if example.elapsed_hours <= 0:
        return float(example.current_count)
    rate = example.current_count / example.elapsed_hours
    return example.current_count + rate * example.remaining_hours


def baseline_projection(example: ReplayExample, rate_per_hour: float) -> float:
    return example.current_count + rate_per_hour * example.remaining_hours


def predict(example: ReplayExample, params: ModelParams) -> float:
    pace = pace_projection(example)
    base = baseline_projection(example, params.baseline_rate_per_hour)
    return params.blend_weight * pace + (1 - params.blend_weight) * base


def mae(examples: list[ReplayExample], params: ModelParams) -> float:
    if not examples:
        return math.nan
    return mean(abs(predict(example, params) - example.final_count) for example in examples)


def rmse(examples: list[ReplayExample], params: ModelParams) -> float:
    if not examples:
        return math.nan
    return math.sqrt(mean((predict(example, params) - example.final_count) ** 2 for example in examples))


def train(examples: list[ReplayExample]) -> ModelParams:
    base_rate = baseline_rate(examples)
    best = ModelParams(0.0, base_rate)
    best_error = mae(examples, best)
    for i in range(0, 101):
        candidate = ModelParams(i / 100, base_rate)
        error = mae(examples, candidate)
        if error < best_error:
            best = candidate
            best_error = error
    return best


def split_by_window(examples: list[ReplayExample], train_ratio: float) -> tuple[list[ReplayExample], list[ReplayExample]]:
    window_names = sorted({example.window_name for example in examples})
    if len(window_names) <= 1:
        return examples, []
    cutoff = max(1, min(len(window_names) - 1, round(len(window_names) * train_ratio)))
    train_windows = set(window_names[:cutoff])
    train_examples = [example for example in examples if example.window_name in train_windows]
    test_examples = [example for example in examples if example.window_name not in train_windows]
    return train_examples, test_examples


def write_report(path: Path, examples: list[ReplayExample], params: ModelParams) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "window_name",
                "checkpoint_utc",
                "window_start_utc",
                "window_end_utc",
                "current_count",
                "final_count",
                "elapsed_hours",
                "remaining_hours",
                "pace_prediction",
                "baseline_prediction",
                "blended_prediction",
                "absolute_error",
            ],
        )
        writer.writeheader()
        for example in examples:
            blended = predict(example, params)
            writer.writerow(
                {
                    "window_name": example.window_name,
                    "checkpoint_utc": example.checkpoint_utc.isoformat(),
                    "window_start_utc": example.window_start_utc.isoformat(),
                    "window_end_utc": example.window_end_utc.isoformat(),
                    "current_count": example.current_count,
                    "final_count": example.final_count,
                    "elapsed_hours": round(example.elapsed_hours, 4),
                    "remaining_hours": round(example.remaining_hours, 4),
                    "pace_prediction": round(pace_projection(example), 4),
                    "baseline_prediction": round(baseline_projection(example, params.baseline_rate_per_hour), 4),
                    "blended_prediction": round(blended, 4),
                    "absolute_error": round(abs(blended - example.final_count), 4),
                }
            )


def save_model(path: Path, params: ModelParams, metadata: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": "baseline_pace_blend_v1",
        "blend_weight": params.blend_weight,
        "baseline_rate_per_hour": params.baseline_rate_per_hour,
        **metadata,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def window_rows(windows: list[MarketWindow], timezone: str, url_prefix: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for window in windows:
        rows.append(
            {
                "window_name": window.name,
                "start_local": window.start_utc.astimezone(ZoneInfo(timezone)).isoformat(),
                "end_local": window.end_utc.astimezone(ZoneInfo(timezone)).isoformat(),
                "start_utc": window.start_utc.isoformat(),
                "end_utc": window.end_utc.isoformat(),
                "url": f"{url_prefix.rstrip('/')}-{window.name}",
            }
        )
    return rows


def run_backtest(args: argparse.Namespace) -> None:
    times = load_post_times(Path(args.db), args.user, args.platform)
    windows = build_windows(
        times,
        args.window,
        parse_datetime(args.start, args.timezone) if args.start else None,
        parse_datetime(args.end, args.timezone) if args.end else None,
        args.timezone,
        parse_datetime(args.polymarket_anchor, args.timezone),
    )
    if args.list_windows:
        print(json.dumps(window_rows(windows, args.timezone, args.market_url_prefix), ensure_ascii=False, indent=2))
        return

    examples = replay_examples(times, windows, args.step_hours, args.min_elapsed_hours, args.min_final_count)
    train_examples, test_examples = split_by_window(examples, args.train_ratio)

    if not train_examples:
        raise SystemExit("No train examples. Collect more historical posts or lower --min-final-count.")

    params = train(train_examples)
    scored_examples = test_examples or train_examples
    report_path = Path(args.report)
    model_path = Path(args.model_out)
    write_report(report_path, scored_examples, params)

    metadata = {
        "created_at_utc": utc_now().isoformat(),
        "db": str(Path(args.db).resolve()),
        "user": args.user,
        "platform": args.platform,
        "window_mode": args.window,
        "step_hours": args.step_hours,
        "min_elapsed_hours": args.min_elapsed_hours,
        "train_ratio": args.train_ratio,
        "windows_total": len(windows),
        "examples_total": len(examples),
        "train_examples": len(train_examples),
        "test_examples": len(test_examples),
        "train_mae": mae(train_examples, params),
        "train_rmse": rmse(train_examples, params),
        "test_mae": mae(test_examples, params) if test_examples else None,
        "test_rmse": rmse(test_examples, params) if test_examples else None,
        "report": str(report_path.resolve()),
    }
    save_model(model_path, params, metadata)

    print(
        json.dumps(
            {
                "model": str(model_path.resolve()),
                "report": str(report_path.resolve()),
                "blend_weight": params.blend_weight,
                "baseline_rate_per_hour": round(params.baseline_rate_per_hour, 6),
                "windows_total": len(windows),
                "examples_total": len(examples),
                "train_examples": len(train_examples),
                "test_examples": len(test_examples),
                "train_mae": round(metadata["train_mae"], 4),
                "test_mae": round(metadata["test_mae"], 4) if metadata["test_mae"] is not None else None,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay ended post-count windows and train an initial predictor.")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--platform", default=DEFAULT_PLATFORM)
    parser.add_argument("--timezone", default=DEFAULT_TZ)
    parser.add_argument("--start", help="Historical replay start, e.g. 2026-01-01.")
    parser.add_argument("--end", help="Historical replay end, e.g. 2026-08-01.")
    parser.add_argument("--window", choices=("polymarket", "day", "week", "month"), default="polymarket")
    parser.add_argument(
        "--polymarket-anchor",
        default=DEFAULT_POLYMARKET_ANCHOR,
        help="First known Polymarket count window start in --timezone.",
    )
    parser.add_argument("--market-url-prefix", default=DEFAULT_MARKET_URL_PREFIX)
    parser.add_argument("--list-windows", action="store_true", help="Print generated windows and exit.")
    parser.add_argument("--step-hours", type=float, default=24)
    parser.add_argument("--min-elapsed-hours", type=float, default=24)
    parser.add_argument("--min-final-count", type=int, default=1)
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--model-out", default=DEFAULT_MODEL)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.set_defaults(func=run_backtest)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
