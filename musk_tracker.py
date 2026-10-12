#!/usr/bin/env python3
"""
Poll XTracker for Polymarket-style social post count markets.

This script treats XTracker as the settlement-aligned source of truth:
- fetch posts for a tracked user
- store raw records in SQLite
- deduplicate by platform post id
- count posts inside a market time window
- emit freshness metadata so stale data can be blocked upstream

It uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


DEFAULT_BASE_URL = "https://xtracker.polymarket.com"
DEFAULT_PLATFORM = "X"
DEFAULT_USER = "elonmusk"
DEFAULT_TZ = "America/New_York"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_datetime(value: Any, default_tz: str = DEFAULT_TZ) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Accept seconds or milliseconds since epoch.
        if value > 10_000_000_000:
            value = value / 1000
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)

    text = str(value).strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        # Date-only values are interpreted as midnight in the market timezone.
        try:
            parsed_date = dt.date.fromisoformat(text[:10])
        except ValueError:
            return None
        parsed = dt.datetime.combine(parsed_date, dt.time.min)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(default_tz))
    return parsed.astimezone(dt.timezone.utc)


def parse_bound(value: str | None, default_tz: str, *, end_of_day: bool = False) -> dt.datetime | None:
    if value is None:
        return None
    stripped = value.strip()
    if len(stripped) == 10:
        local_date = dt.date.fromisoformat(stripped)
        local_time = dt.time.max if end_of_day else dt.time.min
        return dt.datetime.combine(local_date, local_time, ZoneInfo(default_tz)).astimezone(dt.timezone.utc)
    return parse_datetime(stripped, default_tz)


def to_api_date(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:10]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def pick(record: dict[str, Any], names: Iterable[str]) -> Any:
    lowered = {k.lower(): k for k in record.keys()}
    for name in names:
        actual = lowered.get(name.lower())
        if actual is not None:
            return record[actual]
    return None


def extract_records(payload: Any, preferred_keys: Iterable[str]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for key in preferred_keys:
        item = payload.get(key)
        if isinstance(item, list):
            return [row for row in item if isinstance(row, dict)]
        if isinstance(item, dict):
            nested = extract_records(item, preferred_keys)
            if nested:
                return nested

    for key in ("data", "result", "results", "items"):
        item = payload.get(key)
        nested = extract_records(item, preferred_keys)
        if nested:
            return nested
    return []


@dataclass(frozen=True)
class NormalizedPost:
    post_id: str
    user: str
    platform: str
    created_at_utc: dt.datetime | None
    captured_at_utc: dt.datetime
    post_type: str | None
    text: str | None
    raw: dict[str, Any]


class XTrackerClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: int = 20, proxy: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        if proxy:
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        else:
            self.opener = urllib.request.build_opener()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": "musk-market-tracker/0.1"})
        with self.opener.open(request, timeout=self.timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset))

    def get_posts(
        self,
        user: str,
        platform: str,
        start_date: str | None = None,
        end_date: str | None = None,
        timezone: str = DEFAULT_TZ,
    ) -> list[dict[str, Any]]:
        payload = self.get_json(
            f"/api/users/{urllib.parse.quote(user)}/posts",
            {
                "platform": platform,
                "startDate": to_api_date(start_date),
                "endDate": to_api_date(end_date),
                "timezone": timezone,
            },
        )
        return extract_records(payload, ("posts", "tweets", "records"))

    def get_trackings(self, user: str, platform: str, active_only: bool = True) -> list[dict[str, Any]]:
        payload = self.get_json(
            f"/api/users/{urllib.parse.quote(user)}/trackings",
            {"platform": platform, "activeOnly": str(active_only).lower()},
        )
        return extract_records(payload, ("trackings", "markets", "records"))


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # The live dashboard serves HTTP requests from worker threads; all
        # shared writes are serialized by the live service lock.
        # Several local dashboard instances may briefly overlap a write. Wait
        # for the active transaction instead of failing immediately with a
        # misleading capture error; callers still retain their own bounded
        # retry/error evidence.
        self.conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma busy_timeout=30000")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        self.conn.executescript(
            """
            create table if not exists posts (
                post_id text primary key,
                user text not null,
                platform text not null,
                created_at_utc text,
                captured_at_utc text not null,
                available_at_utc text,
                time_precision text not null default 'unknown',
                time_quality text not null default 'unverified',
                post_type text,
                text text,
                raw_json text not null,
                first_seen_utc text not null,
                last_seen_utc text not null
            );

            create table if not exists count_samples (
                id integer primary key autoincrement,
                sampled_at_utc text not null,
                user text not null,
                platform text not null,
                tracking_id text,
                window_start_utc text,
                window_end_utc text,
                confirmed_count integer not null,
                freshness_seconds integer,
                raw_posts_seen integer not null
            );

            create index if not exists idx_posts_user_created
                on posts(user, platform, created_at_utc);
            """
        )
        existing = {row[1] for row in self.conn.execute("pragma table_info(posts)")}
        for column, definition in {
            "available_at_utc": "text",
            "time_precision": "text not null default 'unknown'",
            "time_quality": "text not null default 'unverified'",
        }.items():
            if column not in existing:
                self.conn.execute(f"alter table posts add column {column} {definition}")
        # Existing databases need the column before this index can be created.
        self.conn.execute(
            "create index if not exists idx_posts_available "
            "on posts(user, platform, available_at_utc)"
        )
        self.conn.commit()

    def upsert_posts(self, posts: list[NormalizedPost]) -> int:
        inserted = 0
        now_text = utc_now().isoformat()
        with self.conn:
            for post in posts:
                existing = self.conn.execute(
                    "select 1 from posts where post_id = ?",
                    (post.post_id,),
                ).fetchone()
                if existing is None:
                    inserted += 1
                self.conn.execute(
                    """
                    insert into posts (
                        post_id, user, platform, created_at_utc, captured_at_utc,
                        available_at_utc, time_precision, time_quality,
                        post_type, text, raw_json, first_seen_utc, last_seen_utc
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(post_id) do update set
                        user = excluded.user,
                        platform = excluded.platform,
                        created_at_utc = excluded.created_at_utc,
                        captured_at_utc = excluded.captured_at_utc,
                        available_at_utc = case
                            when posts.available_at_utc is null then excluded.available_at_utc
                            when excluded.available_at_utc < posts.available_at_utc then excluded.available_at_utc
                            else posts.available_at_utc end,
                        time_precision = excluded.time_precision,
                        time_quality = excluded.time_quality,
                        post_type = excluded.post_type,
                        text = excluded.text,
                        raw_json = excluded.raw_json,
                        last_seen_utc = excluded.last_seen_utc
                    """,
                    (
                        post.post_id,
                        post.user,
                        post.platform,
                        post.created_at_utc.isoformat() if post.created_at_utc else None,
                        post.captured_at_utc.isoformat(),
                        (post.created_at_utc or post.captured_at_utc).isoformat(),
                        "second" if post.created_at_utc else "unknown",
                        "source_timestamp" if post.created_at_utc else "capture_only",
                        post.post_type,
                        post.text,
                        json_dumps(post.raw),
                        now_text,
                        now_text,
                    ),
                )
        return inserted

    def count_posts(
        self,
        user: str,
        platform: str,
        start_utc: dt.datetime | None,
        end_utc: dt.datetime | None,
    ) -> int:
        clauses = ["user = ?", "platform = ?", "created_at_utc is not null"]
        values: list[Any] = [user, platform]
        if start_utc is not None:
            clauses.append("created_at_utc >= ?")
            values.append(start_utc.isoformat())
        if end_utc is not None:
            clauses.append("created_at_utc < ?")
            values.append(end_utc.isoformat())
        query = f"select count(*) from posts where {' and '.join(clauses)}"
        return int(self.conn.execute(query, values).fetchone()[0])

    def newest_capture(self, user: str, platform: str) -> dt.datetime | None:
        row = self.conn.execute(
            "select max(captured_at_utc) from posts where user = ? and platform = ?",
            (user, platform),
        ).fetchone()
        return parse_datetime(row[0]) if row and row[0] else None

    def insert_sample(
        self,
        user: str,
        platform: str,
        tracking_id: str | None,
        start_utc: dt.datetime | None,
        end_utc: dt.datetime | None,
        confirmed_count: int,
        freshness_seconds: int | None,
        raw_posts_seen: int,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                insert into count_samples (
                    sampled_at_utc, user, platform, tracking_id, window_start_utc,
                    window_end_utc, confirmed_count, freshness_seconds, raw_posts_seen
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now().isoformat(),
                    user,
                    platform,
                    tracking_id,
                    start_utc.isoformat() if start_utc else None,
                    end_utc.isoformat() if end_utc else None,
                    confirmed_count,
                    freshness_seconds,
                    raw_posts_seen,
                ),
            )


def normalize_post(record: dict[str, Any], user: str, platform: str) -> NormalizedPost:
    raw_id = pick(
        record,
        (
            "post_id",
            "postId",
            "platform_post_id",
            "platformPostId",
            "tweet_id",
            "tweetId",
            "id",
        ),
    )
    raw_json = json_dumps(record)
    if raw_id is None:
        raw_id = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()

    created_at = parse_datetime(
        pick(record, ("created_at", "createdAt", "posted_at", "postedAt", "timestamp", "date"))
    )
    captured_at = (
        parse_datetime(pick(record, ("captured_at", "capturedAt", "imported_at", "importedAt", "seen_at", "seenAt")))
        or utc_now()
    )
    post_type = pick(record, ("type", "post_type", "postType", "kind"))
    text = pick(record, ("text", "content", "body", "full_text", "fullText"))
    return NormalizedPost(
        post_id=str(raw_id),
        user=user,
        platform=platform,
        created_at_utc=created_at,
        captured_at_utc=captured_at,
        post_type=str(post_type) if post_type is not None else None,
        text=str(text) if text is not None else None,
        raw=record,
    )


def tracking_id(record: dict[str, Any]) -> str | None:
    value = pick(record, ("id", "tracking_id", "trackingId", "slug"))
    return str(value) if value is not None else None


def tracking_bounds(record: dict[str, Any], timezone: str) -> tuple[dt.datetime | None, dt.datetime | None]:
    start_value = pick(record, ("startDate", "start_date", "windowStart", "periodStart", "start"))
    end_value = pick(record, ("endDate", "end_date", "windowEnd", "periodEnd", "end"))
    return parse_datetime(start_value, timezone), parse_datetime(end_value, timezone)


def print_trackings(client: XTrackerClient, user: str, platform: str) -> None:
    trackings = client.get_trackings(user, platform, active_only=True)
    if not trackings:
        print("No active trackings returned.")
        return
    print(json.dumps(trackings, ensure_ascii=False, indent=2, sort_keys=True))


def collect_once(args: argparse.Namespace) -> dict[str, Any]:
    client = XTrackerClient(args.base_url, args.timeout, args.proxy)
    store = Store(Path(args.db))
    try:
        start_utc = parse_bound(args.start, args.timezone)
        end_utc = parse_bound(args.end, args.timezone, end_of_day=False)
        tracking = None

        if args.use_active_tracking:
            trackings = client.get_trackings(args.user, args.platform, active_only=True)
            if trackings:
                tracking = trackings[0]
                tracking_start, tracking_end = tracking_bounds(tracking, args.timezone)
                start_utc = start_utc or tracking_start
                end_utc = end_utc or tracking_end

        posts_raw = client.get_posts(args.user, args.platform, args.start, args.end, args.timezone)
        posts = [normalize_post(row, args.user, args.platform) for row in posts_raw]
        inserted = store.upsert_posts(posts)

        confirmed_count = store.count_posts(args.user, args.platform, start_utc, end_utc)
        newest_capture = store.newest_capture(args.user, args.platform)
        freshness = int((utc_now() - newest_capture).total_seconds()) if newest_capture else None
        stale = freshness is None or freshness > args.stale_after
        tid = tracking_id(tracking) if tracking else args.tracking_id

        store.insert_sample(
            args.user,
            args.platform,
            tid,
            start_utc,
            end_utc,
            confirmed_count,
            freshness,
            len(posts_raw),
        )

        return {
            "sampled_at_utc": utc_now().isoformat(),
            "user": args.user,
            "platform": args.platform,
            "tracking_id": tid,
            "window_start_utc": start_utc.isoformat() if start_utc else None,
            "window_end_utc": end_utc.isoformat() if end_utc else None,
            "raw_posts_returned": len(posts_raw),
            "new_posts_inserted": inserted,
            "confirmed_count": confirmed_count,
            "freshness_seconds": freshness,
            "stale": stale,
            "db": str(Path(args.db).resolve()),
        }
    finally:
        store.close()


def run_poll(args: argparse.Namespace) -> None:
    while True:
        try:
            result = collect_once(args)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        except KeyboardInterrupt:
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, sqlite3.Error) as exc:
            error = {
                "sampled_at_utc": utc_now().isoformat(),
                "error": type(exc).__name__,
                "message": str(exc),
            }
            print(json.dumps(error, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
        time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch settlement-aligned X post counts from XTracker.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=int, default=20)

    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--user", default=DEFAULT_USER)
    common.add_argument("--platform", default=DEFAULT_PLATFORM)
    common.add_argument("--timezone", default=DEFAULT_TZ)
    common.add_argument("--db", default="data/musk_posts.sqlite")
    common.add_argument("--proxy", help="HTTP proxy, e.g. http://127.0.0.1:7890.")
    common.add_argument("--start", help="Market window start, e.g. 2026-08-01 or ISO datetime.")
    common.add_argument("--end", help="Market window end, exclusive, e.g. 2026-09-01 or ISO datetime.")
    common.add_argument("--tracking-id")
    common.add_argument("--use-active-tracking", action="store_true")
    common.add_argument("--stale-after", type=int, default=600, help="Mark sample stale after this many seconds.")

    once = subparsers.add_parser("once", parents=[common], help="Fetch once, store posts, print count sample.")
    once.set_defaults(func=lambda args: print(json.dumps(collect_once(args), ensure_ascii=False, indent=2, sort_keys=True)))

    poll = subparsers.add_parser("poll", parents=[common], help="Continuously fetch and print count samples.")
    poll.add_argument("--interval", type=int, default=60)
    poll.set_defaults(func=run_poll)

    active = subparsers.add_parser("active-trackings", help="Print active XTracker markets for a user.")
    active.add_argument("--base-url", default=DEFAULT_BASE_URL)
    active.add_argument("--timeout", type=int, default=20)
    active.add_argument("--proxy", help="HTTP proxy, e.g. http://127.0.0.1:7890.")
    active.add_argument("--user", default=DEFAULT_USER)
    active.add_argument("--platform", default=DEFAULT_PLATFORM)
    active.set_defaults(
        func=lambda args: print_trackings(
            XTrackerClient(args.base_url, args.timeout, args.proxy), args.user, args.platform
        )
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
