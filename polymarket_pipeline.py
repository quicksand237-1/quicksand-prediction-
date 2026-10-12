#!/usr/bin/env python3
"""
Collect Polymarket tweet-count markets and train a leakage-aware regression.

The pipeline is intentionally dependency-free. It stores:
- Gamma event and outcome metadata;
- CLOB Yes-token price history;
- settlement-aligned XTracker posts;
- optional external events supplied as JSONL.

Example:
    python polymarket_pipeline.py crawl --slug elon-musk-of-tweets-july-28-august-4
    python polymarket_pipeline.py train --start 2026-02-10 --end 2026-08-09
    python polymarket_pipeline.py predict --slug elon-musk-of-tweets-july-28-august-4
"""

from __future__ import annotations

import argparse
import csv
import concurrent.futures
import datetime as dt
import http.client
import hashlib
import email.utils
import json
import math
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Iterable
from zoneinfo import ZoneInfo
from history_acquisition import price_known_by

from musk_tracker import (
    DEFAULT_PLATFORM,
    DEFAULT_TZ,
    DEFAULT_USER,
    Store as PostStore,
    XTrackerClient,
    normalize_post,
    parse_bound,
    parse_datetime,
)


DEFAULT_DB = "data/musk_posts.sqlite"
DEFAULT_MODEL = "data/market_ridge_model.json"
DEFAULT_REPORT = "data/market_predictions.csv"
MODEL_NAME = "standardized_ridge_count_v5_event_gated"
DEFAULT_GAMMA_URL = "https://gamma-api.polymarket.com"
DEFAULT_CLOB_URL = "https://clob.polymarket.com"
DEFAULT_PROXY = None
ELON_TWEETS_SERIES_ID = "10000"
DEFAULT_ANCHOR = "2026-02-10T12:00:00"
MARKET_GAPS_DAYS = (3, 4)
WINDOW_DAYS = 7
MIN_PRICE_PROBABILITY_MASS = 0.6
MAX_PRICE_PROBABILITY_MASS = 1.2
MAX_SOURCE_EVENT_IMPACT = 2.0
MIN_EVENT_RELEVANCE = 0.6
MIN_EVENT_CONFIDENCE = 0.45
DEFAULT_SOURCE_REGISTRY = (
    ("x_official", "X official", "x.com", 1, 0.95, "social"),
    ("tesla_ir", "Tesla investor relations", "tesla.com", 1, 0.95, "tesla"),
    ("spacex_official", "SpaceX official", "spacex.com", 1, 0.95, "spacex"),
    ("xai_official", "xAI official", "x.ai", 1, 0.95, "xai"),
    ("sec", "U.S. SEC", "sec.gov", 1, 0.98, "policy,finance"),
    ("whitehouse", "The White House", "whitehouse.gov", 1, 0.95, "policy"),
    ("reuters", "Reuters", "reuters.com", 2, 0.90, "news"),
    ("ap", "Associated Press", "apnews.com", 2, 0.90, "news"),
    ("bloomberg", "Bloomberg", "bloomberg.com", 2, 0.88, "news,finance"),
)
COUNT_MARKET_WINDOW_RE = re.compile(
    r"""
    \bfrom\s+
    (?P<start_month>[A-Za-z]+)\s+(?P<start_day>\d{1,2})\s+
    (?P<start_time>\d{1,2}(?::\d{2})?\s*[AP]M)\s+ET\s+
    to\s+
    (?P<end_month>[A-Za-z]+)\s+(?P<end_day>\d{1,2}),?\s+
    (?P<year>\d{4})\s+
    (?P<end_time>\d{1,2}(?::\d{2})?\s*[AP]M)\s+ET
    """,
    re.IGNORECASE | re.VERBOSE,
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def pick(record: dict[str, Any], names: Iterable[str]) -> Any:
    lowered = {key.lower(): key for key in record}
    for name in names:
        key = lowered.get(name.lower())
        if key is not None:
            return record[key]
    return None


def parse_json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value if value is not None else default


def extract_records(payload: Any, preferred_keys: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Normalize list/data/items envelopes returned by public APIs."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (*preferred_keys, "data", "result", "results", "items"):
        nested = payload.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        if isinstance(nested, dict):
            records = extract_records(nested, preferred_keys)
            if records:
                return records
    return []


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_float(value: Any) -> float | None:
    """Parse an optional numeric value without turning missing data into zero."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_bool(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(bool(value))
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return 1
    if text in {"false", "0", "no", "n", "off"}:
        return 0
    return None


def normalize_event_kind(value: Any) -> str:
    kind = str(value or "other").strip().lower()
    aliases = {
        "travel": "travel",
        "trip": "travel",
        "tesla": "tesla",
        "spacex": "spacex",
        "xai": "xai",
        "policy": "policy",
        "politics": "policy",
        "conflict": "conflict",
        "launch": "launch",
        "news": "news",
    }
    return aliases.get(kind, "other")


def normalize_event_direction(value: Any) -> str:
    direction = str(value or "unknown").strip().lower()
    aliases = {
        "positive": "up",
        "increase": "up",
        "higher": "up",
        "up": "up",
        "negative": "down",
        "decrease": "down",
        "lower": "down",
        "down": "down",
        "neutral": "neutral",
        "none": "neutral",
    }
    return aliases.get(direction, "unknown")


def host_matches_domain(url: str, domain: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    expected = domain.lower().strip().rstrip(".")
    return bool(
        parsed.scheme.lower() in {"http", "https"}
        and hostname
        and expected
        and (hostname == expected or hostname.endswith(f".{expected}"))
    )


def add_local_days(value: dt.datetime, days: int, timezone: str) -> dt.datetime:
    local = value.astimezone(ZoneInfo(timezone))
    return (local + dt.timedelta(days=days)).astimezone(dt.timezone.utc)


def market_slug(start: dt.datetime, end: dt.datetime, timezone: str) -> str:
    local_start = start.astimezone(ZoneInfo(timezone))
    local_end = end.astimezone(ZoneInfo(timezone))
    return (
        f"elon-musk-of-tweets-{local_start.strftime('%B').lower()}-{local_start.day}-"
        f"{local_end.strftime('%B').lower()}-{local_end.day}"
    )


def generate_slugs(
    start: str,
    end: str,
    timezone: str = DEFAULT_TZ,
    anchor: str = DEFAULT_ANCHOR,
) -> list[str]:
    lower = parse_bound(start, timezone)
    upper = parse_bound(end, timezone)
    cursor = parse_datetime(anchor, timezone)
    gap_index = 0
    while cursor < lower:
        cursor = add_local_days(cursor, MARKET_GAPS_DAYS[gap_index], timezone)
        gap_index = (gap_index + 1) % len(MARKET_GAPS_DAYS)
    result: list[str] = []
    while cursor < upper:
        result.append(market_slug(cursor, add_local_days(cursor, WINDOW_DAYS, timezone), timezone))
        cursor = add_local_days(cursor, MARKET_GAPS_DAYS[gap_index], timezone)
        gap_index = (gap_index + 1) % len(MARKET_GAPS_DAYS)
    return result


class JsonClient:
    def __init__(self, base_url: str, timeout: int, proxy: str | None = None, retries: int = 3) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(1, retries)
        self.request_delay = 0.0
        self._request_lock = threading.Lock()
        self._next_request_at = 0.0
        handlers: list[Any] = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler())
        self.opener = urllib.request.build_opener(*handlers)

    def _open_request(self, request: urllib.request.Request) -> Any:
        # All workers, pages and retries on this client share the same gate.
        with self._request_lock:
            remaining = self._next_request_at - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            self._next_request_at = time.monotonic() + self.request_delay
            try:
                return self.opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                self._record_http_error(exc)
                raise

    def _record_http_error(self, exc: urllib.error.HTTPError) -> None:
        headers = exc.headers or {}
        exc.trace_id = headers.get('x-trace-id')
        exc.retry_after_seconds = None
        retry_after = headers.get('Retry-After')
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    delay = (email.utils.parsedate_to_datetime(retry_after) - utc_now()).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    delay = 0.0
            if math.isfinite(delay):
                exc.retry_after_seconds = max(0.0, delay)
                if exc.code in {429, 503}:
                    self._next_request_at = max(self._next_request_at, time.monotonic() + max(0.0, delay))
        try:
            payload = json.loads(exc.read(8192).decode('utf-8'))
            if isinstance(payload, dict):
                exc.api_error_code = payload.get('code')
                exc.api_retryable = payload.get('retryable')
                exc.trace_id = payload.get('trace_id') or exc.trace_id
        except (ValueError, OSError, AttributeError):
            pass
        finally:
            exc.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urllib.parse.urlencode({key: value for key, value in (params or {}).items() if value is not None})
        url = f"{self.base_url}{path}"
        if query:
            url += f"?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "musk-market-pipeline/0.2",
            },
        )
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                with self._open_request(request) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    return json.loads(response.read().decode(charset))
            except urllib.error.HTTPError as exc:
                last_error = exc
                if (exc.code not in {408, 425, 429, 500, 502, 503, 504}
                        or getattr(exc, 'api_retryable', None) is False or attempt + 1 >= self.retries):
                    raise
            except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead, http.client.RemoteDisconnected) as exc:
                last_error = exc
                if attempt + 1 >= self.retries:
                    raise
            time.sleep(min(2.0, 0.25 * (2**attempt)))
        raise RuntimeError(f"HTTP request failed: {last_error}")

    def post_json(self, path: str, payload: Any) -> Any:
        """POST JSON for documented public batch endpoints."""
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "musk-market-pipeline/0.3",
            },
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                with self._open_request(request) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    return json.loads(response.read().decode(charset))
            except urllib.error.HTTPError as exc:
                last_error = exc
                if (exc.code not in {408, 425, 429, 500, 502, 503, 504}
                        or getattr(exc, 'api_retryable', None) is False or attempt + 1 >= self.retries):
                    raise
            except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead, http.client.RemoteDisconnected) as exc:
                last_error = exc
                if attempt + 1 >= self.retries:
                    raise
            time.sleep(min(2.0, 0.25 * (2**attempt)))
        raise RuntimeError(f"HTTP request failed: {last_error}")


class GammaClient(JsonClient):
    def get_event(self, slug: str) -> dict[str, Any]:
        payload = self.get(f"/events/slug/{urllib.parse.quote(slug)}")
        if not isinstance(payload, dict):
            raise ValueError(f"Gamma returned non-object for {slug}")
        return payload

    def list_events(self, *, limit: int = 100, offset: int = 0, closed: bool | None = None) -> list[dict[str, Any]]:
        payload = self.get("/events", {"limit": limit, "offset": offset, "closed": closed})
        return extract_records(payload, ("events", "data", "items", "results"))

    def discover_count_events(
        self,
        start_utc: dt.datetime,
        end_utc: dt.datetime,
        timezone: str,
        *,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """Find real Elon count events before falling back to generated slugs."""
        found: dict[str, dict[str, Any]] = {}
        # Query the dedicated recurring series instead of scanning all markets.
        def load_page(page: int, series: bool) -> list[dict[str, Any]]:
            params = {"limit": 100, "offset": page * 100, "closed": None,
                      "order": "endDate", "ascending": False}
            if series:
                params["series_id"] = ELON_TWEETS_SERIES_ID
            payload = self.get("/events", params)
            return extract_records(payload, ("events", "data", "items", "results"))
        use_series = True
        for page in range(max(1, min(max_pages, 20))):
            try:
                rows = load_page(page, use_series)
            except urllib.error.HTTPError as exc:
                if page != 0 or exc.code not in {400, 404, 422}:
                    raise
                use_series = False
                rows = load_page(page, use_series)
            # Some Gamma deployments ignore or reject series_id. Retry the
            # same bounded page against the general event listing and filter
            # locally instead of manufacturing date-based slugs.
            if page == 0 and not rows:
                use_series = False
                rows = load_page(page, use_series)
            if not rows:
                break
            for event in rows:
                identity = " ".join(str(event.get(key) or "") for key in ("title", "question", "slug")).lower()
                if "elon" not in identity or not any(word in identity for word in ("tweet", "post", "发帖")):
                    continue
                window = event_window(event, timezone)
                if window[0] is None or window[1] is None or window[1] <= start_utc or window[0] >= end_utc:
                    continue
                slug = str(event.get("slug") or event.get("ticker") or "").strip()
                if slug:
                    found[slug] = event
            if len(rows) < 100:
                break
        return list(found.values())


class ClobClient(JsonClient):
    def get_history_window(self, token_id: str, start: dt.datetime, end: dt.datetime, fidelity: int = 5,
                           *, known_history=(), checked=()) -> dict[str, Any]:
        from history_prices import fetch_history
        return fetch_history(self.get, token_id, start, end, fidelity, parse_timestamp, parse_float,
                             known_history, checked)

    def get_history(self, token_id: str, interval: str, fidelity: int) -> list[dict[str, Any]]:
        payload = self.get(
            "/prices-history",
            {"market": token_id, "interval": interval, "fidelity": fidelity},
        )
        history = payload.get("history", []) if isinstance(payload, dict) else []
        return [row for row in history if isinstance(row, dict)]

    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        payload = self.get("/book", {"token_id": token_id})
        if not isinstance(payload, dict):
            raise ValueError(f"CLOB returned non-object order book for {token_id}")
        return payload

    def get_orderbooks(self, token_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not token_ids:
            return {}
        payload = self.post_json("/books", [{"token_id": token_id} for token_id in token_ids])
        rows = extract_records(payload, ("books", "data", "items"))
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            token_id = row.get("asset_id") or row.get("token_id") or row.get("market")
            if token_id is not None:
                result[str(token_id)] = row
        return result

    def get_prices(self, token_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not token_ids:
            return {}
        payload = self.post_json(
            "/prices",
            [{"token_id": token_id, "side": side} for token_id in token_ids for side in ("BUY", "SELL")],
        )
        if isinstance(payload, dict) and isinstance(payload.get("prices"), dict):
            return {
                str(token_id): value if isinstance(value, dict) else {"price": value}
                for token_id, value in payload["prices"].items()
            }
        if isinstance(payload, dict) and any(str(token) in payload for token in token_ids):
            return {str(token): payload[str(token)] if isinstance(payload[str(token)], dict) else {"price": payload[str(token)]}
                    for token in token_ids if str(token) in payload}
        rows = extract_records(payload, ("prices", "data", "items"))
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            token_id = row.get("token_id") or row.get("asset_id") or row.get("market")
            if token_id is not None:
                result.setdefault(str(token_id), {}).update(row)
        return result


class PublicDataClient(JsonClient):
    def get_history_window(self, token_id, start, end, fidelity=5, *, known_history=(), checked=()):
        from history_prices import fetch_history
        return fetch_history(self.get, token_id, start, end, fidelity, parse_timestamp, parse_float,
                             known_history, checked, api='v2')

    """Public Data API reader; no account credentials or signing are used."""

    def get_trades(self, market_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        if not re.fullmatch(r'0x[0-9a-fA-F]{64}', market_id):
            raise ValueError('公共成交需要链上condition ID，不能使用Gamma market ID')
        if not 1 <= limit <= 1000:
            raise ValueError('v2公共成交每页limit必须为1至1000')
        params = {'condition': market_id, 'limit': limit, 'taker_only': 'true',
                  'filter_type': 'TOKENS', 'filter_amount': 0.01}
        rows, seen = [], set()
        for _ in range(1000):
            payload = self.get('/v2/trades', params)
            if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                raise ValueError('公共成交响应缺少data数组')
            if any(not isinstance(row, dict) for row in payload['data']):
                raise ValueError('公共成交响应包含无效记录')
            rows.extend(payload['data'])
            pagination = payload.get('pagination')
            if not isinstance(pagination, dict) or not isinstance(pagination.get('has_more'), bool):
                raise ValueError('公共成交响应缺少完整分页状态')
            cursor = pagination.get('next_cursor')
            if 'next_cursor' not in pagination or (not pagination['has_more'] and cursor is not None):
                raise ValueError('公共成交分页状态不一致')
            if not pagination['has_more']:
                return rows
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError('公共成交分页缺少或重复游标')
            seen.add(cursor)
            params = {**params, 'cursor': cursor}
        raise ValueError('公共成交超过1000页安全上限，未完成')


def parse_range(label: str | None) -> tuple[int | None, int | None]:
    if not label:
        return None, None
    normalized = label.strip().replace(" ", "")
    if normalized.startswith("<"):
        match = re.search(r"\d+", normalized)
        return 0, int(match.group()) - 1 if match else None
    if normalized.endswith("+"):
        match = re.search(r"\d+", normalized)
        return int(match.group()) if match else None, None
    match = re.match(r"(\d+)-(\d+)", normalized)
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def parse_timestamp(value: Any, timezone: str = DEFAULT_TZ) -> dt.datetime | None:
    try:
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value.strip()):
            value = float(value.strip())
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                return None
            if value > 10_000_000_000:
                value /= 1000
            return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
        return parse_datetime(str(value), timezone)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_count_market_window(description: str | None, timezone: str = DEFAULT_TZ) -> tuple[dt.datetime, dt.datetime] | None:
    """Extract the settlement interval from a count-market resolution rule."""
    match = COUNT_MARKET_WINDOW_RE.search(description or "")
    if match is None:
        return None

    values = match.groupdict()
    year = int(values["year"])
    try:
        start_month = dt.datetime.strptime(values["start_month"], "%B").month
        end_month = dt.datetime.strptime(values["end_month"], "%B").month
        start_year = year - 1 if start_month > end_month else year
        start = dt.datetime.strptime(
            f"{values['start_month']} {values['start_day']}, {start_year} {values['start_time'].upper()}",
            "%B %d, %Y %I:%M %p",
        ).replace(tzinfo=ZoneInfo(timezone))
        end = dt.datetime.strptime(
            f"{values['end_month']} {values['end_day']}, {year} {values['end_time'].upper()}",
            "%B %d, %Y %I:%M %p",
        ).replace(tzinfo=ZoneInfo(timezone))
    except ValueError:
        return None
    if end <= start:
        return None
    return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)


def event_window(event: dict[str, Any], timezone: str) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Resolve the trading window from structured Gamma fields first.

    Gamma descriptions are useful for legacy records, but they are mutable text
    and may contain an old rule/date.  Structured event/market timestamps are the
    authoritative identity for live selection; description parsing is fallback
    only when those fields are incomplete.
    """
    market_starts = [
        parsed
        for market in event.get("markets", [])
        if isinstance(market, dict)
        if (parsed := parse_timestamp(market.get("gameStartTime"), timezone)) is not None
    ]
    explicit_start = min(market_starts) if market_starts else parse_timestamp(event.get("gameStartTime"), timezone)
    end = parse_timestamp(event.get("endDate"), timezone)
    if explicit_start is not None and end is not None and end > explicit_start:
        return explicit_start, end
    from_description = parse_count_market_window(str(event.get("description") or ""), timezone)
    if from_description is not None:
        return from_description
    return explicit_start or parse_timestamp(event.get("startDate"), timezone), end


@dataclass(frozen=True)
class Window:
    slug: str
    start_utc: dt.datetime
    end_utc: dt.datetime


@dataclass(frozen=True)
class Example:
    window: Window
    as_of_utc: dt.datetime
    features: list[float]
    label: float
    current_count: int


@dataclass(frozen=True)
class PostObservation:
    """A post's event time and the first local time it was available."""

    created_at_utc: dt.datetime
    available_at_utc: dt.datetime | None


class MarketStore(PostStore):
    def migrate(self) -> None:
        super().migrate()
        self.conn.executescript(
            """
            create table if not exists market_events (
                slug text primary key,
                event_id text,
                title text,
                description text,
                start_date_utc text,
                end_date_utc text,
                game_start_utc text,
                close_date_utc text,
                resolution_date_utc text,
                active integer,
                closed integer,
                accepting_orders integer,
                liquidity real,
                volume real,
                open_interest real,
                tags_json text not null default '[]',
                series_json text not null default '{}',
                status_json text not null default '{}',
                fetched_at_utc text not null,
                raw_json text not null
            );

            create table if not exists market_outcomes (
                event_slug text not null,
                market_id text not null,
                label text not null,
                lower_count integer,
                upper_count integer,
                yes_token_id text,
                no_token_id text,
                outcome_index integer,
                token_ids_json text not null default '[]',
                resolved_yes integer,
                raw_json text not null,
                primary key (event_slug, market_id)
            );

            create table if not exists market_prices (
                event_slug text not null,
                market_id text not null,
                token_id text not null,
                price_time_utc text not null,
                price real not null,
                captured_at_utc text not null,
                primary key (event_slug, market_id, token_id, price_time_utc)
            );

            create table if not exists history_price_checks (
                event_slug text not null, market_id text not null, token_id text not null,
                source_url text not null, start_utc text not null, end_utc text not null,
                fidelity integer not null, checked_at_utc text not null,
                primary key (event_slug, market_id, token_id, source_url, start_utc, end_utc, fidelity)
            );

            create table if not exists history_price_attempts (
                id integer primary key autoincrement,
                event_slug text not null, market_id text not null, token_id text not null,
                source_url text not null, start_utc text not null, end_utc text not null,
                target_seconds integer not null, finished_at_utc text not null,
                status text not null, evidence_json text not null
            );
            create index if not exists idx_history_price_attempts_event
                on history_price_attempts(event_slug, market_id, token_id, start_utc);

            create table if not exists market_orderbooks (
                event_slug text not null,
                market_id text not null,
                token_id text not null,
                captured_at_utc text not null,
                source_timestamp_utc text,
                best_bid real,
                best_ask real,
                spread real,
                bid_depth_5 real,
                ask_depth_5 real,
                depth_imbalance real,
                last_trade_price real,
                last_trade_side text,
                raw_json text not null,
                primary key (event_slug, market_id, token_id, captured_at_utc)
            );

            create table if not exists external_events (
                event_key text primary key,
                occurred_at_utc text not null,
                available_at_utc text,
                observed_at_utc text,
                time_precision text not null default 'unknown',
                time_quality text not null default 'unverified',
                source_kind text not null default 'unspecified',
                kind text not null,
                impact real not null default 1,
                keywords text,
                raw_json text not null
            );

            create table if not exists source_registry (
                source_id text primary key,
                name text not null,
                domain text not null,
                tier integer not null,
                reliability real not null,
                enabled integer not null default 1,
                allowed_topics text,
                notes text,
                updated_at_utc text not null
            );

            create table if not exists event_evidence (
                evidence_id text primary key,
                event_key text not null,
                source_id text not null,
                url text not null,
                published_at_utc text not null,
                retrieved_at_utc text not null,
                title text,
                excerpt text,
                content_hash text not null,
                raw_json text not null,
                unique (event_key, source_id, content_hash),
                foreign key (source_id) references source_registry(source_id)
            );

            create table if not exists source_events (
                event_key text primary key,
                occurred_at_utc text not null,
                available_at_utc text not null,
                kind text not null,
                direction text not null,
                relevance_score real not null,
                ai_confidence real not null,
                confidence_cap real not null,
                bounded_impact real not null,
                source_quality real not null,
                evidence_count integer not null,
                cross_source_count integer not null,
                verification_mode text not null,
                status text not null,
                decision_reason text not null,
                keywords text,
                summary text,
                raw_json text not null
            );

            create table if not exists paper_forecasts (
                id integer primary key autoincrement,
                captured_at_utc text not null,
                event_slug text not null,
                window_start_utc text not null,
                window_end_utc text not null,
                current_count integer not null,
                model_forecast_final_count real not null,
                forecast_bin text,
                forecast_bin_probability real,
                market_expected_count real,
                price_probability_mass real,
                price_distribution_valid integer not null,
                recommendation text not null,
                paper_direction text,
                edge_proxy real,
                do_not_auto_trade integer not null,
                risk_reasons text not null,
                snapshot_json text not null
            );

            create table if not exists paper_trades (
                id integer primary key autoincrement,
                created_at_utc text not null,
                event_slug text not null,
                outcome_label text not null,
                side text not null,
                model_probability real not null,
                quoted_best_bid real,
                quoted_best_ask real,
                executable_price real,
                spread real,
                available_depth real,
                requested_size real not null,
                fill_fraction real not null default 0,
                fee_rate real not null,
                slippage_bps real not null,
                estimated_fee real not null default 0,
                status text not null,
                recommendation text not null,
                edge_after_cost real,
                resolved_yes integer,
                settlement_price real,
                gross_pnl real,
                net_pnl real,
                risk_reasons text not null,
                snapshot_json text not null
            );

            create table if not exists live_market_snapshots (
                id integer primary key autoincrement,
                captured_at_utc text not null,
                event_slug text not null,
                window_start_utc text,
                window_end_utc text,
                refresh_status text not null,
                current_count integer not null default 0,
                posts_returned integer not null default 0,
                tracker_refresh_age_seconds integer,
                latest_post_capture_age_seconds integer,
                model_forecast_final_count real,
                market_expected_count real,
                market_probability_mass real,
                market_distribution_valid integer not null default 0,
                source_status_json text not null,
                external_context_json text not null,
                errors_json text not null,
                snapshot_json text not null
            );

            create table if not exists research_market_snapshots (
                id integer primary key autoincrement,
                event_slug text not null,
                captured_at_utc text not null,
                bucket_start_utc text,
                target_interval_seconds integer not null,
                capture_status text not null,
                current_count integer,
                posts_returned integer not null default 0,
                tracker_refresh_age_seconds integer,
                source_status_json text not null,
                errors_json text not null,
                snapshot_json text not null
            );

            create table if not exists market_trades (
                event_slug text not null,
                market_id text,
                token_id text,
                trade_id text not null,
                trade_time_utc text,
                price real,
                size real,
                side text,
                source_timestamp_utc text,
                captured_at_utc text not null,
                raw_json text not null,
                primary key (event_slug, trade_id)
            );

            create table if not exists market_activity_snapshots (
                id integer primary key autoincrement,
                event_slug text not null,
                captured_at_utc text not null,
                data_time_utc text,
                volume_24h real,
                open_interest real,
                liquidity real,
                trade_count integer,
                raw_json text not null
            );

            create table if not exists prediction_archive (
                id integer primary key autoincrement,
                event_slug text not null,
                data_time_utc text,
                captured_at_utc text not null,
                refresh_status text not null,
                schema_version text not null,
                payload_json text not null
            );

            create index if not exists idx_market_prices_lookup
                on market_prices(event_slug, price_time_utc);
            create index if not exists idx_external_events_time
                on external_events(occurred_at_utc);
            create index if not exists idx_source_events_available
                on source_events(available_at_utc, occurred_at_utc);
            create index if not exists idx_event_evidence_event
                on event_evidence(event_key, published_at_utc);
            create index if not exists idx_paper_forecasts_event_time
                on paper_forecasts(event_slug, captured_at_utc);
            create index if not exists idx_paper_trades_event_time
                on paper_trades(event_slug, created_at_utc);
            create index if not exists idx_live_market_snapshots_event_time
                on live_market_snapshots(event_slug, captured_at_utc);
            create index if not exists idx_research_market_snapshots_event_time
                on research_market_snapshots(event_slug, captured_at_utc);
            create index if not exists idx_market_trades_event_time
                on market_trades(event_slug, trade_time_utc);
            create index if not exists idx_market_activity_event_time
                on market_activity_snapshots(event_slug, captured_at_utc);
            create index if not exists idx_prediction_archive_event_time
                on prediction_archive(event_slug, captured_at_utc);
            """
        )
        for table, columns in {
            "market_events": {
                "close_date_utc": "text", "resolution_date_utc": "text",
                "active": "integer", "closed": "integer", "accepting_orders": "integer",
                "liquidity": "real", "volume": "real", "open_interest": "real",
                "tags_json": "text not null default '[]'", "series_json": "text not null default '{}'",
                "status_json": "text not null default '{}'",
            },
            "market_outcomes": {"outcome_index": "integer", "token_ids_json": "text not null default '[]'"},
            "market_prices": {"resolution_seconds": "integer", "source": "text"},
            "market_orderbooks": {
                "depth_imbalance": "real", "last_trade_price": "real", "last_trade_side": "text",
            },
            "external_events": {
                "available_at_utc": "text", "observed_at_utc": "text",
                "time_precision": "text not null default 'unknown'",
                "time_quality": "text not null default 'unverified'",
                "source_kind": "text not null default 'unspecified'",
            },
        }.items():
            existing = {row[1] for row in self.conn.execute(f"pragma table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    self.conn.execute(f"alter table {table} add column {column} {definition}")
        self.conn.executemany(
            """
            insert or ignore into source_registry (
                source_id, name, domain, tier, reliability, enabled,
                allowed_topics, notes, updated_at_utc
            ) values (?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            [
                (
                    source_id,
                    name,
                    domain,
                    tier,
                    reliability,
                    topics,
                    "Built-in conservative whitelist; review before production use.",
                    utc_now().isoformat(),
                )
                for source_id, name, domain, tier, reliability, topics in DEFAULT_SOURCE_REGISTRY
            ],
        )
        self.conn.commit()

    def save_event(self, event: dict[str, Any], timezone: str) -> Window:
        slug = str(event.get("slug") or event.get("ticker"))
        start, end = event_window(event, timezone)
        if not slug or start is None or end is None or end <= start:
            raise ValueError(f"Cannot determine market window for {slug or 'unknown event'}")
        with self.conn:
            self.conn.execute(
                """
                insert into market_events (
                    slug, event_id, title, description, start_date_utc, end_date_utc,
                    game_start_utc, close_date_utc, resolution_date_utc, active, closed,
                    accepting_orders, liquidity, volume, open_interest, tags_json,
                    series_json, status_json, fetched_at_utc, raw_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(slug) do update set
                    event_id=excluded.event_id, title=excluded.title,
                    description=excluded.description, start_date_utc=excluded.start_date_utc,
                    end_date_utc=excluded.end_date_utc, game_start_utc=excluded.game_start_utc,
                    close_date_utc=excluded.close_date_utc, resolution_date_utc=excluded.resolution_date_utc,
                    active=excluded.active, closed=excluded.closed, accepting_orders=excluded.accepting_orders,
                    liquidity=excluded.liquidity, volume=excluded.volume, open_interest=excluded.open_interest,
                    tags_json=excluded.tags_json, series_json=excluded.series_json, status_json=excluded.status_json,
                    fetched_at_utc=excluded.fetched_at_utc, raw_json=excluded.raw_json
                """,
                (
                    slug,
                    str(event.get("id")) if event.get("id") is not None else None,
                    event.get("title"),
                    event.get("description"),
                    parse_timestamp(event.get("startDate"), timezone).isoformat()
                    if parse_timestamp(event.get("startDate"), timezone) else None,
                    end.isoformat(),
                    start.isoformat(),
                    parse_timestamp(event.get("closeDate"), timezone).isoformat()
                    if parse_timestamp(event.get("closeDate"), timezone) else None,
                    parse_timestamp(event.get("resolutionDate") or event.get("resolvedAt"), timezone).isoformat()
                    if parse_timestamp(event.get("resolutionDate") or event.get("resolvedAt"), timezone) else None,
                    parse_bool(event.get("active")),
                    parse_bool(event.get("closed")),
                    parse_bool(event.get("acceptingOrders")),
                    parse_float(event.get("liquidity")), parse_float(event.get("volume")),
                    parse_float(event.get("openInterest")),
                    json_dumps(event.get("tags") if isinstance(event.get("tags"), list) else []),
                    json_dumps(event.get("series") if isinstance(event.get("series"), (dict, list)) else {}),
                    json_dumps({key: event.get(key) for key in ("active", "closed", "acceptingOrders", "resolved") if key in event}),
                    utc_now().isoformat(),
                    json_dumps(event),
                ),
            )
            self.conn.execute("delete from market_outcomes where event_slug = ?", (slug,))
            for outcome_index, market in enumerate(event.get("markets", [])):
                if not isinstance(market, dict):
                    continue
                labels = parse_range(str(market.get("groupItemTitle") or ""))
                tokens = parse_json(market.get("clobTokenIds"), [])
                prices = parse_json(market.get("outcomePrices"), [])
                names = parse_json(market.get("outcomes"), [])
                if not isinstance(tokens, list):
                    tokens = []
                names = [str(name).strip().lower() for name in names] if isinstance(names, list) else []
                yes_index = names.index("yes") if "yes" in names else (0 if not names else None)
                no_index = names.index("no") if "no" in names else (1 if not names else None)
                resolved_yes: int | None = None
                if yes_index is not None and no_index is not None and len(prices) > max(yes_index, no_index):
                    yes_price, no_price = parse_float(prices[yes_index]), parse_float(prices[no_index])
                    if yes_price in (0, 1) and no_price in (0, 1) and yes_price != no_price:
                        resolved_yes = int(yes_price > no_price)
                self.conn.execute(
                    """
                    insert into market_outcomes (
                        event_slug, market_id, label, lower_count, upper_count,
                        yes_token_id, no_token_id, outcome_index, token_ids_json, resolved_yes, raw_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slug,
                        str(market.get("id")),
                        str(market.get("groupItemTitle") or ""),
                        labels[0],
                        labels[1],
                        str(tokens[yes_index]) if yes_index is not None and len(tokens) > yes_index else None,
                        str(tokens[no_index]) if no_index is not None and len(tokens) > no_index else None,
                        outcome_index,
                        json_dumps(tokens),
                        resolved_yes,
                        json_dumps(market),
                    ),
                )
        return Window(slug, start, end)

    def save_prices(self, slug: str, market_id: str, token_id: str, history: list[dict[str, Any]]) -> int:
        inserted = 0
        captured = utc_now().isoformat()
        rows = []
        # Parse outside the write transaction; commit small batches so readers can proceed.
        for row in history:
            timestamp = parse_timestamp(row.get("t", row.get("timestamp")))
            numeric_price = parse_float(row.get("p", row.get("price")))
            if timestamp is not None and numeric_price is not None and 0 <= numeric_price <= 1:
                rows.append((slug, market_id, token_id, timestamp.isoformat(), numeric_price, captured,
                             row.get('resolution_seconds'), row.get('source')))
        for offset in range(0, len(rows), 500):
            with self.conn:
                cursor = self.conn.executemany(
                    "insert or ignore into market_prices (event_slug, market_id, token_id, price_time_utc, price, captured_at_utc, resolution_seconds, source) values (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows[offset:offset+500])
                inserted += cursor.rowcount
        return inserted

    def save_history_attempts(self, slug, market_id, token_id, source_url, attempts):
        with self.conn:
            self.conn.executemany(
                'insert into history_price_attempts '
                '(event_slug,market_id,token_id,source_url,start_utc,end_utc,target_seconds,'
                'finished_at_utc,status,evidence_json) values (?,?,?,?,?,?,?,?,?,?)',
                [(slug, market_id, token_id, source_url, r['start'], r['end'], r['target_seconds'],
                  r['finished_at_utc'], r['status'], json_dumps(r)) for r in attempts])

    def save_orderbook(
        self,
        slug: str,
        market_id: str,
        token_id: str,
        book: dict[str, Any],
    ) -> int:
        def levels(name: str) -> list[tuple[float, float]]:
            result: list[tuple[float, float]] = []
            for row in book.get(name, []) or []:
                if not isinstance(row, dict):
                    continue
                try:
                    price = float(row.get("price"))
                    size = float(row.get("size"))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(price) and math.isfinite(size) and 0 <= price <= 1 and size > 0:
                    result.append((price, size))
            return result

        actual_token = book.get("asset_id") or book.get("token_id")
        if actual_token is not None and str(actual_token) != str(token_id):
            raise ValueError("orderbook token does not match requested token")
        bids = levels("bids")
        asks = levels("asks")
        best_bid = max((price for price, _ in bids), default=None)
        best_ask = min((price for price, _ in asks), default=None)
        spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
        bid_depth = sum(size for price, size in bids if best_bid is not None and price >= best_bid - 0.05)
        ask_depth = sum(size for price, size in asks if best_ask is not None and price <= best_ask + 0.05)
        depth_imbalance = (
            (bid_depth - ask_depth) / (bid_depth + ask_depth)
            if bid_depth + ask_depth > 0 else None
        )
        source_time = parse_timestamp(book.get("timestamp"))
        captured = utc_now().isoformat()
        with self.conn:
            cursor = self.conn.execute(
                """
                insert or ignore into market_orderbooks (
                    event_slug, market_id, token_id, captured_at_utc,
                    source_timestamp_utc, best_bid, best_ask, spread,
                    bid_depth_5, ask_depth_5, depth_imbalance,
                    last_trade_price, last_trade_side, raw_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slug,
                    market_id,
                    token_id,
                    captured,
                    source_time.isoformat() if source_time else None,
                    best_bid,
                    best_ask,
                    spread,
                    bid_depth,
                    ask_depth,
                    depth_imbalance,
                    parse_float(book.get("last_trade_price") or book.get("lastTradePrice")),
                    str(book.get("last_trade_side") or book.get("lastTradeSide") or "").upper() or None,
                    json_dumps(book),
                ),
            )
        return cursor.rowcount

    def save_market_trades(
        self,
        event_slug: str,
        market_id: str,
        rows: list[dict[str, Any]],
        captured_at_utc: dt.datetime | None = None,
    ) -> int:
        captured = (captured_at_utc or utc_now()).isoformat()
        inserted = 0
        with self.conn:
            for index, row in enumerate(rows):
                trade_id = str(row.get("id") or row.get("trade_id") or "").strip()
                if not trade_id:
                    identity = {key: row.get(key) for key in (
                        "transaction_hash", "transactionHash", "condition_id", "token_id", "asset",
                        "timestamp", "price", "size", "side", "outcome_index", "log_index", "sequence_id")}
                    trade_id = "fill:" + hashlib.sha256(json_dumps(identity).encode("utf-8")).hexdigest()
                trade_time = parse_timestamp(row.get("timestamp") or row.get("match_time") or row.get("created_at"))
                token_id = row.get("asset_id") or row.get("token_id") or row.get("asset")
                cursor = self.conn.execute(
                    """
                    insert or ignore into market_trades (
                        event_slug, market_id, token_id, trade_id, trade_time_utc,
                        price, size, side, source_timestamp_utc, captured_at_utc, raw_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_slug, market_id, str(token_id) if token_id is not None else None,
                        trade_id, trade_time.isoformat() if trade_time else None,
                        parse_float(row.get("price")), parse_float(row.get("size") or row.get("amount")),
                        str(row.get("side") or row.get("taker_side") or "").upper() or None,
                        trade_time.isoformat() if trade_time else None, captured, json_dumps(row),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def save_market_activity(
        self,
        event_slug: str,
        payload: dict[str, Any],
        *,
        captured_at_utc: dt.datetime | None = None,
        data_time_utc: dt.datetime | None = None,
    ) -> int:
        captured = (captured_at_utc or utc_now()).isoformat()
        cursor = self.conn.execute(
            """
            insert into market_activity_snapshots (
                event_slug, captured_at_utc, data_time_utc, volume_24h,
                open_interest, liquidity, trade_count, raw_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_slug, captured,
                data_time_utc.isoformat() if data_time_utc else None,
                parse_float(payload.get("volume24hr") or payload.get("volume_24h") or payload.get("volume")),
                parse_float(payload.get("openInterest") or payload.get("open_interest")),
                parse_float(payload.get("liquidity")),
                int(payload.get("tradeCount") or payload.get("trade_count") or 0),
                json_dumps(payload),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def save_prediction_archive(
        self,
        *,
        event_slug: str,
        captured_at_utc: dt.datetime,
        refresh_status: str,
        payload: dict[str, Any],
        data_time_utc: dt.datetime | None = None,
    ) -> int:
        """Append one frontend-ready, time-aligned crawl record."""
        record = {
            "schema_version": "prediction_archive_v1",
            "event_slug": event_slug,
            "data_time_utc": data_time_utc.isoformat() if data_time_utc else None,
            "captured_at_utc": captured_at_utc.isoformat(),
            "refresh_status": refresh_status,
            "do_not_auto_trade": True,
            **payload,
        }
        cursor = self.conn.execute(
            """
            insert into prediction_archive (
                event_slug, data_time_utc, captured_at_utc, refresh_status,
                schema_version, payload_json
            ) values (?, ?, ?, ?, ?, ?)
            """,
            (
                event_slug, data_time_utc.isoformat() if data_time_utc else None,
                captured_at_utc.isoformat(), refresh_status, "prediction_archive_v1", json_dumps(record),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def prediction_archive(self, event_slug: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(1000, int(limit)))
        if event_slug:
            rows = self.conn.execute(
                "select id, payload_json from prediction_archive where event_slug = ? order by captured_at_utc desc, id desc limit ?",
                (event_slug, bounded),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "select id, payload_json from prediction_archive order by captured_at_utc desc, id desc limit ?",
                (bounded,),
            ).fetchall()
        result = []
        for row in rows:
            payload = parse_json(row[1], {})
            if isinstance(payload, dict):
                payload.setdefault("archive_id", int(row[0]))
                result.append(payload)
        return result

    def history_review(
        self,
        event_slug: str,
        limit: int = 100,
        half_life_days: float = 0,
        max_age_days: float = 365,
    ) -> dict[str, Any]:
        """Return a read-only, slug-isolated review DTO from local archives.

        The time-decay result is an audit sensitivity only.  It deliberately
        does not alter model parameters, a prediction, or a trading decision.
        """
        slug = str(event_slug or "").strip()
        if not slug:
            raise ValueError("slug is required")
        bounded = max(1, min(1000, int(limit)))
        half_life = float(half_life_days)
        maximum_age = float(max_age_days)
        if not math.isfinite(half_life) or not math.isfinite(maximum_age) or half_life < 0 or maximum_age <= 0:
            raise ValueError("half_life_days must be non-negative and max_age_days must be positive")

        event = self.conn.execute(
            """
            select slug, title, game_start_utc, end_date_utc, close_date_utc,
                   resolution_date_utc, active, closed, accepting_orders
            from market_events where slug = ?
            """,
            (slug,),
        ).fetchone()
        market = {
            "slug": slug,
            "title": event[1] if event else None,
            "window_start_utc": event[2] if event else None,
            "window_end_utc": event[3] if event else None,
            "close_date_utc": event[4] if event else None,
            "resolution_date_utc": event[5] if event else None,
            "active": bool(event[6]) if event and event[6] is not None else None,
            "closed": bool(event[7]) if event and event[7] is not None else None,
            "accepting_orders": bool(event[8]) if event and event[8] is not None else None,
        }

        prediction_rows = self.conn.execute(
            """
            select id, data_time_utc, captured_at_utc, refresh_status, schema_version, payload_json
            from prediction_archive where event_slug = ?
            order by captured_at_utc asc, id asc limit ?
            """,
            (slug, bounded),
        ).fetchall()
        predictions: list[dict[str, Any]] = []
        for row in prediction_rows:
            payload = parse_json(row[5], {})
            payload = payload if isinstance(payload, dict) else {}
            prediction = payload.get("prediction") if isinstance(payload.get("prediction"), dict) else {}
            count = payload.get("count") if isinstance(payload.get("count"), dict) else {}
            predictions.append({
                "archive_id": int(row[0]), "data_time_utc": row[1], "captured_at_utc": row[2],
                "refresh_status": row[3], "schema_version": row[4],
                "current_count": count.get("current"),
                "forecast_final_count": prediction.get("final_count"),
                "forecast_bin": prediction.get("forecast_bin"),
                "source": "prediction_archive",
            })

        price_rows = self.conn.execute(
            """
            select p.market_id, p.token_id, p.price_time_utc, p.captured_at_utc, p.price,
                   o.label, o.lower_count, o.upper_count
            from market_prices p
            left join market_outcomes o on o.event_slug = p.event_slug and o.market_id = p.market_id
            where p.event_slug = ?
            order by p.price_time_utc asc, p.captured_at_utc asc limit ?
            """,
            (slug, bounded),
        ).fetchall()
        prices = [
            {
                "market_id": row[0], "token_id": row[1], "source_price_time_utc": row[2],
                "captured_at_utc": row[3], "price": row[4], "outcome_label": row[5],
                "lower_count": row[6], "upper_count": row[7],
                "time_basis": "source_price_time_and_local_capture",
                "point_in_time_safe": True,
            }
            for row in price_rows
        ]
        book_rows = self.conn.execute(
            """
            select b.market_id, b.token_id, b.source_timestamp_utc, b.captured_at_utc,
                   b.best_bid, b.best_ask, b.spread, b.bid_depth_5, b.ask_depth_5,
                   b.depth_imbalance, b.last_trade_price, b.last_trade_side, o.label
            from market_orderbooks b
            left join market_outcomes o on o.event_slug = b.event_slug and o.market_id = b.market_id
            where b.event_slug = ?
            order by b.captured_at_utc asc limit ?
            """,
            (slug, bounded),
        ).fetchall()
        orderbooks = [
            {
                "market_id": row[0], "token_id": row[1], "source_timestamp_utc": row[2],
                "captured_at_utc": row[3], "best_bid": row[4], "best_ask": row[5],
                "spread": row[6], "bid_depth_5": row[7], "ask_depth_5": row[8],
                "depth_imbalance": row[9], "last_trade_price": row[10], "last_trade_side": row[11],
                "outcome_label": row[12],
                "time_basis": "source_timestamp_and_local_capture" if row[2] else "local_capture_only",
                "point_in_time_safe": bool(row[2]),
            }
            for row in book_rows
        ]
        activity_rows = self.conn.execute(
            """
            select captured_at_utc, data_time_utc, volume_24h, open_interest, liquidity, trade_count
            from market_activity_snapshots where event_slug = ?
            order by captured_at_utc asc, id asc limit ?
            """,
            (slug, bounded),
        ).fetchall()
        activity = [
            {
                "captured_at_utc": row[0], "data_time_utc": row[1], "volume_24h": row[2],
                "open_interest": row[3], "liquidity": row[4], "trade_count": row[5],
                "time_basis": "source_data_time_and_local_capture" if row[1] else "local_capture_only",
            }
            for row in activity_rows
        ]

        target_start = parse_timestamp(market["window_start_utc"])
        references: list[dict[str, Any]] = []
        excluded = {"no_target_start": 0, "invalid_reference_time": 0, "future_or_overlapping": 0, "too_old": 0}
        if target_start is None:
            excluded["no_target_start"] = 1
        else:
            rows = self.conn.execute(
                """
                select event_slug, window_end_utc, captured_at_utc, model_forecast_final_count
                from paper_forecasts
                where event_slug <> ? and captured_at_utc <= window_end_utc
                order by event_slug asc, captured_at_utc desc, id desc
                """,
                (slug,),
            ).fetchall()
            seen: set[str] = set()
            for row in rows:
                reference_slug = str(row[0])
                if reference_slug in seen:
                    continue
                seen.add(reference_slug)
                reference_end = parse_timestamp(row[1])
                if reference_end is None:
                    excluded["invalid_reference_time"] += 1
                    continue
                age_days = (target_start - reference_end).total_seconds() / 86400
                if age_days < 0:
                    excluded["future_or_overlapping"] += 1
                    continue
                if age_days > maximum_age:
                    excluded["too_old"] += 1
                    continue
                weight = 1.0 if half_life == 0 else 2 ** (-age_days / half_life)
                references.append({
                    "slug": reference_slug, "reference_window_end_utc": row[1],
                    "captured_at_utc": row[2], "age_days": age_days, "weight": weight,
                    "archived_forecast_final_count": row[3],
                })
        total_weight = sum(item["weight"] for item in references)
        forecast_weight = sum(
            item["weight"] * float(item["archived_forecast_final_count"])
            for item in references if item["archived_forecast_final_count"] is not None
        )
        forecast_weight_total = sum(
            item["weight"] for item in references if item["archived_forecast_final_count"] is not None
        )
        return {
            "schema_version": "history_review_v1", "slug": slug, "do_not_auto_trade": True,
            "market": market,
            "coverage": {
                "market_found": event is not None, "prediction_records": len(predictions),
                "price_records": len(prices), "orderbook_records": len(orderbooks),
                "activity_records": len(activity),
                "source_notes": [
                    "prices retain source_price_time_utc and local captured_at_utc",
                    "orderbooks without source_timestamp_utc are local capture records, not historical orderbook replay",
                ],
            },
            "predictions": predictions, "prices": prices, "orderbooks": orderbooks, "activity": activity,
            "time_decay": {
                "status": "sensitivity_only_not_model_feature", "half_life_days": half_life,
                "max_age_days": maximum_age, "target_checkpoint_utc": market["window_start_utc"],
                "eligible_prior_markets": len(references), "effective_sample_size": total_weight,
                "weighted_archived_forecast_reference": (
                    forecast_weight / forecast_weight_total if forecast_weight_total else None
                ),
                "excluded": excluded, "references": references,
                "notice": "Read-only time-decay sensitivity; no retraining, calibration, live prediction, or decision change.",
            },
            "external_context": {
                "news": {"status": "stored_not_model_feature", "items": []},
                "flight": {"status": "not_configured", "items": []},
                "posts": {
                    "status": "metadata_only_not_model_feature",
                    "time_fields": ["created_at_utc", "available_at_utc", "captured_at_utc"],
                },
            },
        }

    def backfill_prediction_archive(self, limit: int | None = None) -> int:
        """Index existing paper forecasts without changing their source rows."""
        existing_ids: set[int] = set()
        for row in self.conn.execute("select payload_json from prediction_archive"):
            payload = parse_json(row[0], {})
            source = payload.get("source") if isinstance(payload, dict) else None
            if isinstance(source, dict) and source.get("table") == "paper_forecasts":
                try:
                    existing_ids.add(int(source.get("id")))
                except (TypeError, ValueError):
                    pass
        query = """
            select id, event_slug, captured_at_utc, window_start_utc, window_end_utc,
                   current_count, model_forecast_final_count, forecast_bin,
                   forecast_bin_probability, market_expected_count, price_probability_mass,
                   price_distribution_valid, recommendation, paper_direction, edge_proxy,
                   do_not_auto_trade, risk_reasons, snapshot_json
            from paper_forecasts order by captured_at_utc, id
        """
        params: tuple[Any, ...] = ()
        if limit is not None:
            query += " limit ?"
            params = (max(0, int(limit)),)
        inserted = 0
        for row in self.conn.execute(query, params):
            source_id = int(row[0])
            if source_id in existing_ids:
                continue
            captured = parse_timestamp(row[2]) or utc_now()
            payload = {
                "source": {"table": "paper_forecasts", "id": source_id},
                "market": {"slug": row[1]},
                "window": {"start_utc": row[3], "end_utc": row[4]},
                "count": {"current": row[5]},
                "prediction": {
                    "final_count": row[6], "forecast_bin": row[7],
                    "forecast_bin_probability": row[8],
                },
                "market_distribution": {
                    "expected_count": row[9], "probability_mass": row[10],
                    "distribution_valid": bool(row[11]),
                },
                "recommendation": {
                    "label": row[12], "paper_direction": row[13],
                    "edge_proxy": row[14], "do_not_auto_trade": bool(row[15]),
                    "risk_reasons": parse_json(row[16], []),
                },
                "snapshot": parse_json(row[17], {}),
                "legacy_source": True,
            }
            self.save_prediction_archive(
                event_slug=str(row[1]),
                captured_at_utc=captured,
                data_time_utc=captured,
                refresh_status="legacy",
                payload=payload,
            )
            inserted += 1
        return inserted

    def save_paper_forecast(
        self,
        *,
        captured_at_utc: dt.datetime,
        window: Window,
        current_count: int,
        forecast: float,
        forecast_bin: str | None,
        forecast_bin_probability: float | None,
        market_expected_count: float,
        probability_mass: float,
        price_distribution_valid: bool,
        recommendation: str,
        paper_direction: str | None,
        edge_proxy: float | None,
        risk_reasons: list[str],
        snapshot: dict[str, Any],
    ) -> int:
        cursor = self.conn.execute(
            """
            insert into paper_forecasts (
                captured_at_utc, event_slug, window_start_utc, window_end_utc,
                current_count, model_forecast_final_count, forecast_bin,
                forecast_bin_probability, market_expected_count,
                price_probability_mass, price_distribution_valid, recommendation,
                paper_direction, edge_proxy, do_not_auto_trade, risk_reasons,
                snapshot_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                captured_at_utc.isoformat(),
                window.slug,
                window.start_utc.isoformat(),
                window.end_utc.isoformat(),
                current_count,
                forecast,
                forecast_bin,
                forecast_bin_probability,
                market_expected_count,
                probability_mass,
                1 if price_distribution_valid else 0,
                recommendation,
                paper_direction,
                edge_proxy,
                1,
                json_dumps(risk_reasons),
                json_dumps(snapshot),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    @staticmethod
    def _archive_snapshot_payload(
        *,
        snapshot_id: int | None,
        captured_at_utc: str,
        event_slug: str,
        refresh_status: str,
        snapshot: dict[str, Any],
        origin: str,
    ) -> dict[str, Any]:
        """Return a stable archive shape for new and legacy live captures."""
        payload = snapshot if isinstance(snapshot, dict) else {}
        window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
        count = payload.get("count") if isinstance(payload.get("count"), dict) else {}
        prediction = payload.get("prediction") if isinstance(payload.get("prediction"), dict) else {}
        market = payload.get("market") if isinstance(payload.get("market"), dict) else {}
        stock = payload.get("stock") if isinstance(payload.get("stock"), dict) else {}
        recommendation = payload.get("recommendation") if isinstance(payload.get("recommendation"), dict) else {}
        sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
        external_context = payload.get("external_context") if isinstance(payload.get("external_context"), dict) else {}
        flight = external_context.get("flight")
        if not isinstance(flight, dict):
            flight = {"status": "not_configured", "items": []}
        items = flight.get("items")
        flight = {
            "status": str(flight.get("status") or "not_configured"),
            "items": items if isinstance(items, list) else [],
        }
        news = payload.get("news")
        events = payload.get("approved_events")
        errors = payload.get("errors")
        outcomes = market.get("outcomes")
        market_event = payload.get("market_event")
        capture = payload.get("capture")
        settlement = payload.get("official_settlement")
        return {
            "archive_schema": "live_market_snapshot_v1",
            "capture_origin": origin,
            "snapshot_id": snapshot_id,
            "captured_at_utc": captured_at_utc,
            "slug": event_slug,
            "refresh_status": refresh_status,
            "capture": capture if isinstance(capture, dict) else {
                "data_time_utc": captured_at_utc,
                "target_interval_seconds": 300,
                "checkpoint_utc": None,
                "mode": "five_minute_polling",
            },
            "official_settlement": settlement if isinstance(settlement, dict) else {
                "status": "pending",
                "checked_at_utc": captured_at_utc,
                "official_range": None,
                "outcomes": [],
                "source": "local_market_outcomes",
            },
            "window": {
                "start_utc": window.get("start_utc"),
                "end_utc": window.get("end_utc"),
                "outside": bool(window.get("outside", False)),
            },
            "count": {
                "current": int(count.get("current") or 0),
                "tracker_refresh_age_seconds": count.get("tracker_refresh_age_seconds"),
                "latest_post_capture_age_seconds": count.get("latest_post_capture_age_seconds"),
                "posts_returned": int(count.get("posts_returned") or 0),
                "stale": bool(count.get("stale", True)),
            },
            "prediction": prediction,
            "market": {**market, "outcomes": outcomes if isinstance(outcomes, list) else []},
            "market_event": market_event if isinstance(market_event, dict) else {},
            "stock": {
                "symbol": str(stock.get("symbol") or ""),
                "price": stock.get("price"),
                "previous_close": stock.get("previous_close"),
                "change": stock.get("change"),
                "change_pct": stock.get("change_pct"),
                "currency": stock.get("currency"),
                "as_of": stock.get("as_of"),
                "error": stock.get("error"),
            },
            "news": news if isinstance(news, list) else [],
            "approved_events": events if isinstance(events, list) else [],
            "external_context": {**external_context, "flight": flight},
            "sources": sources,
            "recommendation": {**recommendation, "do_not_auto_trade": True},
            "errors": errors if isinstance(errors, list) else [],
        }

    def save_live_snapshot(
        self,
        *,
        captured_at_utc: dt.datetime,
        event_slug: str,
        refresh_status: str,
        snapshot: dict[str, Any],
    ) -> int:
        """Append one local live capture without changing prior market records."""
        payload = self._archive_snapshot_payload(
            snapshot_id=None,
            captured_at_utc=captured_at_utc.isoformat(),
            event_slug=event_slug,
            refresh_status=refresh_status,
            snapshot=snapshot,
            origin="live_snapshot",
        )
        count = payload["count"]
        market = payload["market"]
        cursor = self.conn.execute(
            """
            insert into live_market_snapshots (
                captured_at_utc, event_slug, window_start_utc, window_end_utc,
                refresh_status, current_count, posts_returned,
                tracker_refresh_age_seconds, latest_post_capture_age_seconds,
                model_forecast_final_count, market_expected_count,
                market_probability_mass, market_distribution_valid,
                source_status_json, external_context_json, errors_json, snapshot_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                captured_at_utc.isoformat(), event_slug,
                payload["window"]["start_utc"], payload["window"]["end_utc"],
                refresh_status, count["current"], count["posts_returned"],
                count["tracker_refresh_age_seconds"], count["latest_post_capture_age_seconds"],
                parse_float(payload["prediction"].get("final_count")),
                parse_float(market.get("expected_count")),
                parse_float(market.get("probability_mass")),
                1 if market.get("distribution_valid") else 0,
                json_dumps(payload["sources"]), json_dumps(payload["external_context"]),
                json_dumps(payload["errors"]), json_dumps(payload),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def save_research_snapshot(
        self,
        *,
        captured_at_utc: dt.datetime,
        event_slug: str,
        target_interval_seconds: int,
        capture_status: str,
        snapshot: dict[str, Any],
    ) -> int:
        """Append a high-frequency descriptive capture outside the decision archive.

        These rows are deliberately separate from five-minute live snapshots:
        they can support short-horizon research without becoming historical
        prediction, edge, or paper-trade inputs.
        """
        count = snapshot.get("count") if isinstance(snapshot.get("count"), dict) else {}
        capture = snapshot.get("capture") if isinstance(snapshot.get("capture"), dict) else {}
        sources = snapshot.get("sources") if isinstance(snapshot.get("sources"), dict) else {}
        errors = snapshot.get("errors") if isinstance(snapshot.get("errors"), list) else []
        cursor = self.conn.execute(
            """
            insert into research_market_snapshots (
                event_slug, captured_at_utc, bucket_start_utc,
                target_interval_seconds, capture_status, current_count,
                posts_returned, tracker_refresh_age_seconds,
                source_status_json, errors_json, snapshot_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_slug,
                captured_at_utc.isoformat(),
                capture.get("bucket_start_utc"),
                int(target_interval_seconds),
                str(capture_status),
                count.get("current"),
                int(count.get("posts_returned") or 0),
                count.get("tracker_refresh_age_seconds"),
                json_dumps(sources),
                json_dumps(errors),
                json_dumps(snapshot),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def research_snapshot_archive(self, event_slug: str, limit: int = 120) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(2000, int(limit)))
        rows = self.conn.execute(
            """
            select id, captured_at_utc, capture_status, snapshot_json
            from research_market_snapshots
            where event_slug = ?
            order by captured_at_utc desc, id desc
            limit ?
            """,
            (event_slug, bounded_limit),
        ).fetchall()
        return [
            {
                "id": int(row[0]),
                "captured_at_utc": str(row[1]),
                "capture_status": str(row[2]),
                **parse_json(row[3], {}),
                "research_only": True,
                "do_not_auto_trade": True,
            }
            for row in rows
        ]

    def latest_research_snapshot(self, event_slug: str) -> dict[str, Any] | None:
        rows = self.research_snapshot_archive(event_slug, 1)
        return rows[0] if rows else None

    def live_snapshot_archive(self, event_slug: str, limit: int = 100) -> list[dict[str, Any]]:
        """Read current captures or normalize the older paper-forecast snapshots."""
        bounded_limit = max(1, min(1000, int(limit)))
        rows = self.conn.execute(
            """
            select id, captured_at_utc, refresh_status, snapshot_json
            from live_market_snapshots
            where event_slug = ?
            order by captured_at_utc desc, id desc
            limit ?
            """,
            (event_slug, bounded_limit),
        ).fetchall()
        if rows:
            return [
                self._archive_snapshot_payload(
                    snapshot_id=int(row[0]), captured_at_utc=str(row[1]),
                    event_slug=event_slug, refresh_status=str(row[2]),
                    snapshot=parse_json(row[3], {}), origin="live_snapshot",
                )
                for row in rows
            ]
        legacy_rows = self.conn.execute(
            """
            select id, captured_at_utc, snapshot_json
            from paper_forecasts
            where event_slug = ?
            order by captured_at_utc desc, id desc
            limit ?
            """,
            (event_slug, bounded_limit),
        ).fetchall()
        return [
            self._archive_snapshot_payload(
                snapshot_id=int(row[0]), captured_at_utc=str(row[1]),
                event_slug=event_slug, refresh_status="legacy",
                snapshot=parse_json(row[2], {}), origin="legacy_paper_forecast",
            )
            for row in legacy_rows
        ]

    def save_paper_trade(
        self,
        *,
        created_at_utc: dt.datetime,
        event_slug: str,
        outcome_label: str,
        side: str,
        model_probability: float,
        quoted_best_bid: float | None,
        quoted_best_ask: float | None,
        executable_price: float | None,
        spread: float | None,
        available_depth: float | None,
        requested_size: float,
        fill_fraction: float,
        fee_rate: float,
        slippage_bps: float,
        estimated_fee: float,
        status: str,
        recommendation: str,
        edge_after_cost: float | None,
        risk_reasons: list[str],
        snapshot: dict[str, Any],
    ) -> int:
        cursor = self.conn.execute(
            """
            insert into paper_trades (
                created_at_utc, event_slug, outcome_label, side,
                model_probability, quoted_best_bid, quoted_best_ask,
                executable_price, spread, available_depth, requested_size,
                fill_fraction, fee_rate, slippage_bps, estimated_fee, status,
                recommendation, edge_after_cost, risk_reasons, snapshot_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at_utc.isoformat(),
                event_slug,
                outcome_label,
                side,
                model_probability,
                quoted_best_bid,
                quoted_best_ask,
                executable_price,
                spread,
                available_depth,
                requested_size,
                max(0.0, min(1.0, fill_fraction)),
                fee_rate,
                slippage_bps,
                estimated_fee,
                status,
                recommendation,
                edge_after_cost,
                json_dumps(risk_reasons),
                json_dumps(snapshot),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def settle_paper_trades(self, event_slug: str) -> int:
        """Settle filled paper YES entries when the local market outcome is known."""
        rows = self.conn.execute(
            """
            select id, outcome_label, executable_price, requested_size,
                   fill_fraction, estimated_fee
            from paper_trades
            where event_slug = ? and status = 'paper_filled' and resolved_yes is null
            """,
            (event_slug,),
        ).fetchall()
        updated = 0
        with self.conn:
            for row in rows:
                outcome = self.conn.execute(
                    """
                    select resolved_yes from market_outcomes
                    where event_slug = ? and label = ?
                    """,
                    (event_slug, row[1]),
                ).fetchone()
                if outcome is None or outcome[0] is None:
                    continue
                settlement = 1.0 if int(outcome[0]) else 0.0
                filled = float(row[3]) * float(row[4])
                gross = filled * (settlement - float(row[2]))
                net = gross - float(row[5])
                self.conn.execute(
                    """
                    update paper_trades
                    set resolved_yes = ?, settlement_price = ?, gross_pnl = ?, net_pnl = ?, status = 'settled'
                    where id = ?
                    """,
                    (int(outcome[0]), settlement, gross, net, row[0]),
                )
                updated += 1
        return updated

    def save_external_events(self, path: Path, timezone: str) -> int:
        inserted = 0
        with path.open(encoding="utf-8") as file:
            rows = [json.loads(line) for line in file if line.strip()]
        with self.conn:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                occurred = parse_timestamp(pick(row, ("timestamp", "occurred_at", "occurredAt")), timezone)
                if occurred is None:
                    continue
                kind = str(row.get("kind") or row.get("type") or "unknown")
                impact = float(row.get("impact", 1) or 1)
                raw_key = str(row.get("id") or f"{occurred.isoformat()}|{kind}|{json_dumps(row)}")
                cursor = self.conn.execute(
                    """
                    insert or ignore into external_events (
                        event_key, occurred_at_utc, kind, impact, keywords, raw_json
                    ) values (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        raw_key,
                        occurred.isoformat(),
                        kind,
                        impact,
                        json_dumps(row.get("keywords", [])),
                        json_dumps(row),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def save_source_registry(self, path: Path, timezone: str) -> int:
        inserted = 0
        with path.open(encoding="utf-8") as file:
            rows = [json.loads(line) for line in file if line.strip()]
        with self.conn:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                source_id = str(row.get("source_id") or row.get("id") or "").strip()
                name = str(row.get("name") or source_id).strip()
                domain = str(row.get("domain") or "").strip().lower()
                if not source_id or not name or not domain:
                    continue
                try:
                    tier = max(1, min(3, int(row.get("tier", 3))))
                    reliability = clamp(float(row.get("reliability", 0.5)), 0.0, 1.0)
                except (TypeError, ValueError):
                    continue
                cursor = self.conn.execute(
                    """
                    insert into source_registry (
                        source_id, name, domain, tier, reliability, enabled,
                        allowed_topics, notes, updated_at_utc
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(source_id) do update set
                        name=excluded.name, domain=excluded.domain, tier=excluded.tier,
                        reliability=excluded.reliability, enabled=excluded.enabled,
                        allowed_topics=excluded.allowed_topics, notes=excluded.notes,
                        updated_at_utc=excluded.updated_at_utc
                    """,
                    (
                        source_id,
                        name,
                        domain,
                        tier,
                        reliability,
                        1 if row.get("enabled", True) else 0,
                        json_dumps(row.get("allowed_topics", [])),
                        str(row.get("notes") or ""),
                        utc_now().isoformat(),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def save_source_events(self, path: Path, timezone: str) -> dict[str, int]:
        """Import AI-structured events after applying non-LLM validation gates."""
        inserted = 0
        rejected = 0
        evidence_inserted = 0
        with path.open(encoding="utf-8") as file:
            rows = [json.loads(line) for line in file if line.strip()]
        source_rows = {
            str(row["source_id"]): row
            for row in self.conn.execute(
                "select source_id, domain, tier, reliability, enabled from source_registry"
            ).fetchall()
        }
        with self.conn:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                occurred = parse_timestamp(
                    pick(row, ("timestamp", "occurred_at", "occurredAt")), timezone
                )
                if occurred is None:
                    rejected += 1
                    continue
                event_key = str(row.get("id") or "").strip()
                if not event_key:
                    event_key = hashlib.sha256(json_dumps(row).encode("utf-8")).hexdigest()[:32]
                direction = normalize_event_direction(row.get("direction"))
                kind = normalize_event_kind(row.get("kind") or row.get("type"))
                relevance = clamp(
                    safe_float(row.get("relevance", row.get("relevance_score", 0.0))),
                    0.0,
                    1.0,
                )
                ai_confidence = clamp(
                    safe_float(row.get("ai_confidence", row.get("confidence", 0.0))),
                    0.0,
                    1.0,
                )
                try:
                    raw_impact = clamp(abs(float(row.get("impact", 1.0) or 1.0)), 0.0, 3.0)
                except (TypeError, ValueError):
                    raw_impact = 0.0
                evidence = row.get("evidence", [])
                if isinstance(evidence, dict):
                    evidence = [evidence]
                if not isinstance(evidence, list) or not evidence:
                    evidence = [row]

                valid_evidence: list[tuple[dict[str, Any], dict[str, Any], dt.datetime, dt.datetime, str]] = []
                evidence_directions: set[str] = set()
                for item in evidence:
                    if not isinstance(item, dict):
                        continue
                    source_id = str(item.get("source_id") or row.get("source_id") or "").strip()
                    source = source_rows.get(source_id)
                    url = str(item.get("url") or row.get("url") or "").strip()
                    if source is None or not int(source["enabled"]) or not url:
                        continue
                    if not host_matches_domain(url, str(source["domain"])):
                        continue
                    published = parse_timestamp(
                        pick(item, ("published_at", "publishedAt", "timestamp")), timezone
                    ) or occurred
                    retrieved = parse_timestamp(
                        pick(item, ("retrieved_at", "retrievedAt")), timezone
                    ) or published
                    title = str(item.get("title") or row.get("title") or "")
                    excerpt = str(item.get("excerpt") or item.get("text") or row.get("excerpt") or "")
                    content_hash = hashlib.sha256(
                        f"{title}\n{excerpt}\n{url}".encode("utf-8")
                    ).hexdigest()
                    item_direction = normalize_event_direction(
                        item.get("direction", row.get("direction"))
                    )
                    if item_direction in {"up", "down"}:
                        evidence_directions.add(item_direction)
                    valid_evidence.append(
                        (source, item, published, retrieved, content_hash)
                    )

                source_quality = (
                    sum(float(source["reliability"]) for source, *_ in valid_evidence)
                    / len(valid_evidence)
                    if valid_evidence
                    else 0.0
                )
                source_ids = {str(source["source_id"]) for source, *_ in valid_evidence}
                cross_source_count = len(source_ids)
                evidence_count = len(valid_evidence)
                has_official_source = any(int(source["tier"]) == 1 for source, *_ in valid_evidence)
                confidence_cap = clamp(
                    0.40
                    + 0.35 * source_quality
                    + (0.10 if cross_source_count >= 2 else 0.0)
                    + (0.05 if has_official_source else 0.0),
                    0.0,
                    0.90,
                )
                effective_confidence = min(ai_confidence, confidence_cap)
                conflict = len(evidence_directions) > 1
                approved = (
                    direction in {"up", "down", "neutral"}
                    and relevance >= MIN_EVENT_RELEVANCE
                    and effective_confidence >= MIN_EVENT_CONFIDENCE
                    and evidence_count >= 1
                    and (cross_source_count >= 2 or has_official_source)
                    and not conflict
                )
                verification_mode = (
                    "cross_source"
                    if cross_source_count >= 2
                    else "official_single_source"
                    if has_official_source
                    else "unverified"
                )
                reason = (
                    "approved"
                    if approved
                    else "conflicting_evidence"
                    if conflict
                    else "insufficient_source_quality_or_confidence"
                )
                sign = 1.0 if direction == "up" else -1.0 if direction == "down" else 0.0
                bounded_impact = (
                    sign
                    * min(
                        MAX_SOURCE_EVENT_IMPACT,
                        raw_impact * relevance * effective_confidence,
                    )
                    if approved
                    else 0.0
                )
                available_at = max(
                    (retrieved for _, _, _, retrieved, _ in valid_evidence),
                    default=occurred,
                )
                cursor = self.conn.execute(
                    """
                    insert into source_events (
                        event_key, occurred_at_utc, available_at_utc, kind, direction,
                        relevance_score, ai_confidence, confidence_cap, bounded_impact,
                        source_quality, evidence_count, cross_source_count,
                        verification_mode, status, decision_reason, keywords, summary, raw_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(event_key) do update set
                        occurred_at_utc=excluded.occurred_at_utc,
                        available_at_utc=excluded.available_at_utc,
                        kind=excluded.kind, direction=excluded.direction,
                        relevance_score=excluded.relevance_score,
                        ai_confidence=excluded.ai_confidence,
                        confidence_cap=excluded.confidence_cap,
                        bounded_impact=excluded.bounded_impact,
                        source_quality=excluded.source_quality,
                        evidence_count=excluded.evidence_count,
                        cross_source_count=excluded.cross_source_count,
                        verification_mode=excluded.verification_mode,
                        status=excluded.status,
                        decision_reason=excluded.decision_reason,
                        keywords=excluded.keywords, summary=excluded.summary,
                        raw_json=excluded.raw_json
                    """,
                    (
                        event_key,
                        occurred.isoformat(),
                        available_at.isoformat(),
                        kind,
                        direction,
                        relevance,
                        ai_confidence,
                        confidence_cap,
                        bounded_impact,
                        source_quality,
                        evidence_count,
                        cross_source_count,
                        verification_mode,
                        "approved" if approved else "rejected",
                        reason,
                        json_dumps(row.get("keywords", [])),
                        str(row.get("summary") or row.get("title") or ""),
                        json_dumps(row),
                    ),
                )
                inserted += cursor.rowcount
                if not approved:
                    rejected += 1
                for source, item, published, retrieved, content_hash in valid_evidence:
                    evidence_id = hashlib.sha256(
                        f"{event_key}|{source['source_id']}|{content_hash}".encode("utf-8")
                    ).hexdigest()[:40]
                    evidence_cursor = self.conn.execute(
                        """
                        insert or ignore into event_evidence (
                            evidence_id, event_key, source_id, url, published_at_utc,
                            retrieved_at_utc, title, excerpt, content_hash, raw_json
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            evidence_id,
                            event_key,
                            source["source_id"],
                            str(item.get("url") or row.get("url")),
                            published.isoformat(),
                            retrieved.isoformat(),
                            str(item.get("title") or row.get("title") or ""),
                            str(item.get("excerpt") or item.get("text") or ""),
                            content_hash,
                            json_dumps(item),
                        ),
                    )
                    evidence_inserted += evidence_cursor.rowcount
        return {
            "source_events_seen": len(rows),
            "source_events_written": inserted,
            "source_events_rejected": rejected,
            "evidence_inserted": evidence_inserted,
        }

    def windows(self, start: dt.datetime | None = None, end: dt.datetime | None = None) -> list[Window]:
        rows = self.conn.execute(
            """
            select slug, game_start_utc, end_date_utc from market_events
            where (? is null or game_start_utc >= ?)
              and (? is null or end_date_utc <= ?)
            order by game_start_utc
            """,
            (
                start.isoformat() if start else None,
                start.isoformat() if start else None,
                end.isoformat() if end else None,
                end.isoformat() if end else None,
            ),
        ).fetchall()
        return [Window(row[0], parse_timestamp(row[1]), parse_timestamp(row[2])) for row in rows]

    def post_times(self, user: str, platform: str) -> list[dt.datetime]:
        rows = self.conn.execute(
            """
            select created_at_utc from posts
            where user = ? and platform = ? and created_at_utc is not null
            order by created_at_utc
            """,
            (user, platform),
        ).fetchall()
        return [parsed for row in rows if (parsed := parse_timestamp(row[0])) is not None]

    def post_observations(self, user: str, platform: str) -> list[PostObservation]:
        """Load event and availability times without rewriting legacy rows."""
        columns = {row[1] for row in self.conn.execute("pragma table_info(posts)")}
        availability = "available_at_utc" if "available_at_utc" in columns else "captured_at_utc"
        rows = self.conn.execute(
            f"""
            select created_at_utc, {availability}
            from posts
            where user = ? and platform = ? and created_at_utc is not null
            order by created_at_utc
            """,
            (user, platform),
        ).fetchall()
        observations: list[PostObservation] = []
        for created_raw, available_raw in rows:
            created = parse_timestamp(created_raw)
            if created is None:
                continue
            available = parse_timestamp(available_raw) if available_raw else None
            observations.append(PostObservation(created, available))
        return observations

    def final_count(self, window: Window, user: str, platform: str) -> int:
        return self.conn.execute(
            """
            select count(*) from posts
            where user = ? and platform = ? and created_at_utc >= ? and created_at_utc < ?
            """,
            (user, platform, window.start_utc.isoformat(), window.end_utc.isoformat()),
        ).fetchone()[0]

    def resolved_range(self, slug: str) -> tuple[int | None, int | None] | None:
        row = self.conn.execute(
            """
            select lower_count, upper_count
            from market_outcomes
            where event_slug = ? and resolved_yes = 1
            """,
            (slug,),
        ).fetchone()
        return (row[0], row[1]) if row is not None else None

    def outcome_ranges(self, slug: str) -> list[tuple[str, int | None, int | None]]:
        rows = self.conn.execute(
            """
            select label, lower_count, upper_count
            from market_outcomes
            where event_slug = ?
            order by lower_count
            """,
            (slug,),
        ).fetchall()
        return [(str(row[0]), row[1], row[2]) for row in rows]

    def final_count_is_verified(self, window: Window, user: str, platform: str) -> bool | None:
        """Return None before resolution, otherwise whether captured posts fit the resolved bin."""
        resolved = self.resolved_range(window.slug)
        if resolved is None:
            return None
        count = self.final_count(window, user, platform)
        lower, upper = resolved
        return (lower is None or count >= lower) and (upper is None or count <= upper)

    def latest_prices(self, slug: str, as_of: dt.datetime, max_age_hours: float) -> list[tuple[int | None, int | None, float]]:
        return [(row['lower_count'], row['upper_count'], float(row['price']))
                for row in self.prices_at(slug, as_of, max_age_hours)]

    def prices_at(self, slug: str, as_of: dt.datetime, max_age_hours: float) -> list[dict[str, Any]]:
        """Return the latest locally archived price per token at a checkpoint."""
        rows = self.conn.execute(
            """
            select p.market_id, p.token_id, p.price_time_utc, p.captured_at_utc, p.price,
                   o.label, o.lower_count, o.upper_count, p.resolution_seconds
            from market_prices p
            left join market_outcomes o on o.event_slug = p.event_slug and o.market_id = p.market_id
            where p.event_slug = ? and p.price_time_utc <= ?
              and p.captured_at_utc <= ?
              and p.price_time_utc >= ?
              and (o.yes_token_id is null or p.token_id = o.yes_token_id)
            order by p.market_id, p.token_id, p.price_time_utc desc
            """,
            (
                slug,
                as_of.isoformat(),
                as_of.isoformat(),
                (as_of - dt.timedelta(hours=max_age_hours)).isoformat(),
            ),
        ).fetchall()
        result = []
        seen = set()
        for row in rows:
            source_time = parse_timestamp(row[2])
            key = (row[0], row[1])
            if key in seen or not price_known_by(source_time, parse_timestamp(row[3]), row[8], as_of):
                continue
            seen.add(key)
            result.append({
                "market_id": row[0], "token_id": row[1], "source_price_time_utc": row[2],
                "captured_at_utc": row[3], "price": row[4], "outcome_label": row[5],
                "lower_count": row[6], "upper_count": row[7],
                "resolution_seconds": row[8], "point_in_time_safe": True,
                "age_minutes": round(max(0.0, (as_of - source_time).total_seconds() / 60), 3)
                if source_time else None,
            })
        return result

    def orderbooks_at(self, slug: str, as_of: dt.datetime, max_age_hours: float) -> list[dict[str, Any]]:
        """Return point-in-time-safe orderbook observations available at a checkpoint."""
        rows = self.conn.execute(
            """
            select b.market_id, b.token_id, b.source_timestamp_utc, b.captured_at_utc,
                   b.best_bid, b.best_ask, b.spread, b.bid_depth_5, b.ask_depth_5,
                   b.depth_imbalance, b.last_trade_price, b.last_trade_side, o.label
            from market_orderbooks b
            left join market_outcomes o on o.event_slug = b.event_slug and o.market_id = b.market_id
            where b.event_slug = ? and b.captured_at_utc <= ?
              and b.captured_at_utc >= ?
              and b.captured_at_utc = (
                select max(b2.captured_at_utc) from market_orderbooks b2
                where b2.event_slug = b.event_slug
                  and b2.market_id = b.market_id
                  and b2.token_id = b.token_id
                  and b2.captured_at_utc <= ?
                  and b2.captured_at_utc >= ?
              )
            order by b.captured_at_utc desc
            """,
            (
                slug,
                as_of.isoformat(),
                (as_of - dt.timedelta(hours=max_age_hours)).isoformat(),
                as_of.isoformat(),
                (as_of - dt.timedelta(hours=max_age_hours)).isoformat(),
            ),
        ).fetchall()
        result = []
        for row in rows:
            source_time = parse_timestamp(row[2])
            captured = parse_timestamp(row[3])
            result.append({
                "market_id": row[0], "token_id": row[1], "source_timestamp_utc": row[2],
                "captured_at_utc": row[3], "best_bid": row[4], "best_ask": row[5],
                "spread": row[6], "bid_depth_5": row[7], "ask_depth_5": row[8],
                "depth_imbalance": row[9], "last_trade_price": row[10], "last_trade_side": row[11],
                "outcome_label": row[12],
                "source_age_minutes": round(max(0.0, (as_of - source_time).total_seconds() / 60), 3)
                if source_time else None,
                "capture_age_minutes": round(max(0.0, (as_of - captured).total_seconds() / 60), 3)
                if captured else None,
                "point_in_time_safe": bool(source_time),
            })
        return result

    def prices_in_window(self, slug: str, as_of: dt.datetime, max_age_hours: float, window_minutes: int = 5) -> list[dict[str, Any]]:
        """Aggregate locally captured prices in the backward-looking checkpoint window.

        Only rows whose source/capture timestamps are no later than ``as_of`` are
        considered.  The latest row per token is retained for pricing while
        sample count and range statistics describe the whole window.
        """
        start = max(as_of - dt.timedelta(hours=max_age_hours), as_of - dt.timedelta(minutes=max(1, window_minutes)))
        rows = self.conn.execute(
            """
            select p.market_id, p.token_id, p.price_time_utc, p.captured_at_utc, p.price,
                   o.label, o.lower_count, o.upper_count, p.resolution_seconds
            from market_prices p
            left join market_outcomes o on o.event_slug = p.event_slug and o.market_id = p.market_id
            where p.event_slug = ? and p.captured_at_utc >= ? and p.captured_at_utc < ?
              and p.captured_at_utc <= ? and p.price_time_utc <= ?
              and (o.yes_token_id is null or p.token_id = o.yes_token_id)
            order by p.market_id, p.token_id, p.captured_at_utc desc
            """,
            (slug, start.isoformat(), as_of.isoformat(), as_of.isoformat(), as_of.isoformat()),
        ).fetchall()
        grouped: dict[tuple[Any, Any], list[Any]] = {}
        for row in rows:
            if not price_known_by(parse_timestamp(row[2]), parse_timestamp(row[3]), row[8], as_of):
                continue
            grouped.setdefault((row[0], row[1]), []).append(row)
        result: list[dict[str, Any]] = []
        for values in grouped.values():
            latest = values[0]
            numeric = [float(item[4]) for item in values if item[4] is not None]
            source_time = parse_timestamp(latest[2])
            captured = parse_timestamp(latest[3])
            result.append({
                "market_id": latest[0], "token_id": latest[1], "source_price_time_utc": latest[2],
                "captured_at_utc": latest[3], "price": latest[4], "outcome_label": latest[5],
                "lower_count": latest[6], "upper_count": latest[7],
                "age_minutes": round(max(0.0, (as_of - source_time).total_seconds() / 60), 3) if source_time else None,
                "window_sample_count": len(numeric),
                "window_price_min": min(numeric) if numeric else None,
                "window_price_max": max(numeric) if numeric else None,
                "window_price_mean": (sum(numeric) / len(numeric)) if numeric else None,
                "aggregation_window_start_utc": start.isoformat(),
                "aggregation_window_end_utc": as_of.isoformat(),
                "aggregation_method": "latest_per_token_with_window_stats",
                "resolution_seconds": latest[8],
                "point_in_time_safe": price_known_by(source_time, captured, latest[8], as_of),
            })
        return result

    def historical_prices_in_window(
        self, slug: str, as_of: dt.datetime, window_minutes: int = 5
    ) -> list[dict[str, Any]]:
        """Return source-timed prices for review, including later local backfills.

        These rows are display-only when captured after ``as_of`` and must not
        be used as point-in-time model features or executable quotes.
        """
        start = as_of - dt.timedelta(minutes=max(1, window_minutes))
        rows = self.conn.execute(
            """
            select p.market_id, p.token_id, p.price_time_utc, p.captured_at_utc, p.price,
                   o.label, o.lower_count, o.upper_count, p.resolution_seconds, p.source
            from market_prices p
            left join market_outcomes o on o.event_slug = p.event_slug and o.market_id = p.market_id
            where p.event_slug = ? and p.price_time_utc >= ? and p.price_time_utc < ?
              and (o.yes_token_id is null or p.token_id = o.yes_token_id)
            order by p.market_id, p.token_id, p.price_time_utc desc, p.captured_at_utc desc
            """,
            (slug, start.isoformat(), as_of.isoformat()),
        ).fetchall()
        grouped: dict[tuple[Any, Any], list[Any]] = {}
        for row in rows:
            grouped.setdefault((row[0], row[1]), []).append(row)
        result: list[dict[str, Any]] = []
        for values in grouped.values():
            latest = values[0]
            numeric = [float(item[4]) for item in values if item[4] is not None]
            source_time = parse_timestamp(latest[2])
            captured = parse_timestamp(latest[3])
            point_in_time_safe = price_known_by(source_time, captured, latest[8], as_of)
            result.append({
                "market_id": latest[0], "token_id": latest[1],
                "source_price_time_utc": latest[2], "captured_at_utc": latest[3],
                "price": latest[4], "outcome_label": latest[5],
                "lower_count": latest[6], "upper_count": latest[7],
                "age_minutes": round(max(0.0, (as_of - source_time).total_seconds() / 60), 3) if source_time else None,
                "window_sample_count": len(numeric),
                "window_price_min": min(numeric) if numeric else None,
                "window_price_max": max(numeric) if numeric else None,
                "window_price_mean": sum(numeric) / len(numeric) if numeric else None,
                "aggregation_window_start_utc": start.isoformat(),
                "aggregation_window_end_utc": as_of.isoformat(),
                "aggregation_method": "source_timed_history_for_review",
                "resolution_seconds": latest[8],
                "observation_end_utc": (source_time + dt.timedelta(seconds=latest[8])).isoformat()
                    if source_time and latest[8] is not None and latest[8] >= 0 else None,
                "price_source": latest[9] or "price_history",
                "coarser_than_review_window": bool(latest[8] and latest[8] > max(1, window_minutes) * 60),
                "point_in_time_safe": point_in_time_safe,
                "backfilled_after_checkpoint": bool(captured and captured > as_of),
                "review_only": not point_in_time_safe,
            })
        return result

    def orderbooks_in_window(self, slug: str, as_of: dt.datetime, max_age_hours: float, window_minutes: int = 5) -> list[dict[str, Any]]:
        """Aggregate locally captured order-book snapshots in a safe 5-minute window."""
        start = max(as_of - dt.timedelta(hours=max_age_hours), as_of - dt.timedelta(minutes=max(1, window_minutes)))
        token_cache = getattr(self, "_replay_book_tokens", None)
        prefix, token_join, token_filter, token_params = "", "", "", []
        if token_cache is not None:
            if slug not in token_cache:
                token_cache[slug] = self.conn.execute(
                    "select distinct market_id, token_id from market_orderbooks where event_slug = ?", (slug,)
                ).fetchall()
            pairs = token_cache[slug]
            if not pairs:
                return []
            # The existing PK is (slug, market_id, token_id, captured_at).
            # Bind its leading columns instead of scanning every snapshot for
            # the slug at every checkpoint. Retain NO tokens for audit too.
            prefix = "with replay_tokens(market_id,token_id) as (values " + ",".join("(?,?)" for _ in pairs) + ") "
            token_join = "replay_tokens t cross join "
            token_filter = "and b.market_id = t.market_id and b.token_id = t.token_id"
            token_params = [value for pair in pairs for value in pair]
        rows = self.conn.execute(
            prefix + f"""
            select b.market_id, b.token_id, b.source_timestamp_utc, b.captured_at_utc,
                   b.best_bid, b.best_ask, b.spread, b.bid_depth_5, b.ask_depth_5,
                   b.depth_imbalance, b.last_trade_price, b.last_trade_side, o.label, b.raw_json, o.yes_token_id
            from {token_join}market_orderbooks b
            left join market_outcomes o on o.event_slug = b.event_slug and o.market_id = b.market_id
            where b.event_slug = ? and b.captured_at_utc >= ? and b.captured_at_utc < ?
              and b.captured_at_utc <= ?
              {token_filter}
            order by b.market_id, b.token_id, b.captured_at_utc desc
            """,
            (*token_params, slug, start.isoformat(), as_of.isoformat(), as_of.isoformat()),
        ).fetchall()
        grouped: dict[tuple[Any, Any], list[Any]] = {}
        for row in rows:
            grouped.setdefault((row[0], row[1]), []).append(row)
        result: list[dict[str, Any]] = []
        for values in grouped.values():
            def assess(item):
                saved = parse_timestamp(item[2])
                try:
                    raw = json.loads(item[13])
                    source = parse_timestamp(raw.get("timestamp")) if raw.get("timestamp") is not None else saved
                except (ValueError, TypeError, AttributeError):
                    source = None
                captured = parse_timestamp(item[3])
                reason = "safe"
                if item[1] != item[14]:
                    reason = "wrong_yes_token"
                elif source is None or captured is None or source.year < 2000:
                    reason = "invalid_source_time"
                elif source > as_of or captured > as_of:
                    reason = "future_book"
                elif (as_of - source).total_seconds() > min(300, max_age_hours * 3600):
                    reason = "stale_book"
                elif any(item[i] is None or not math.isfinite(float(item[i])) for i in (4, 5, 7, 8)):
                    reason = "invalid_quote"
                elif not 0 <= item[4] <= item[5] <= 1 or min(item[7], item[8]) < 0:
                    reason = "invalid_quote"
                return source, captured, reason, source is not None and source != saved

            latest = next((item for item in values if assess(item)[2] == "safe"), values[0])
            source_time, captured, reason, recovered = assess(latest)
            spreads = [float(item[6]) for item in values if item[6] is not None]
            asks = [float(item[5]) for item in values if item[5] is not None]
            depths = [float(item[8]) for item in values if item[8] is not None]

            result.append({
                "market_id": latest[0], "token_id": latest[1], "source_timestamp_utc": source_time.isoformat() if source_time else None,
                "stored_source_timestamp_utc": latest[2], "source_time_recovered": recovered,
                "book_rejection_reason": reason,
                "captured_at_utc": latest[3], "best_bid": latest[4], "best_ask": latest[5], "spread": latest[6],
                "bid_depth_5": latest[7], "ask_depth_5": latest[8], "depth_imbalance": latest[9],
                "last_trade_price": latest[10], "last_trade_side": latest[11], "outcome_label": latest[12],
                "source_age_minutes": round(max(0.0, (as_of - source_time).total_seconds() / 60), 3) if source_time else None,
                "capture_age_minutes": round(max(0.0, (as_of - captured).total_seconds() / 60), 3) if captured else None,
                "point_in_time_safe": reason == "safe",
                "window_snapshot_count": len(values),
                "window_spread_min": min(spreads) if spreads else None,
                "window_spread_max": max(spreads) if spreads else None,
                "window_ask_depth_min": min(depths) if depths else None,
                "window_ask_depth_max": max(depths) if depths else None,
                "window_ask_mean": (sum(asks) / len(asks)) if asks else None,
                "aggregation_window_start_utc": start.isoformat(),
                "aggregation_window_end_utc": as_of.isoformat(),
                "aggregation_method": "latest_snapshot_with_window_stats",
            })
        return result

    def events_before(self, start: dt.datetime, as_of: dt.datetime) -> list[tuple[str, float]]:
        rows = self.conn.execute(
            """
            select kind, impact
            from external_events
            where occurred_at_utc >= ? and occurred_at_utc < ?
              and coalesce(available_at_utc, occurred_at_utc) <= ?
            union all
            select kind, bounded_impact
            from source_events
            where status = 'approved'
              and occurred_at_utc >= ? and occurred_at_utc < ?
              and available_at_utc <= ?
            """,
            (
                start.isoformat(),
                as_of.isoformat(),
                as_of.isoformat(),
                start.isoformat(),
                as_of.isoformat(),
                as_of.isoformat(),
            ),
        ).fetchall()
        return [(str(row[0]), float(row[1])) for row in rows]

    def detailed_events_before(
        self,
        start: dt.datetime,
        as_of: dt.datetime,
    ) -> list[tuple[dt.datetime, str, float]]:
        rows = self.conn.execute(
            """
            select occurred_at_utc, kind, impact
            from external_events
            where occurred_at_utc >= ? and occurred_at_utc < ?
              and coalesce(available_at_utc, occurred_at_utc) <= ?
            union all
            select occurred_at_utc, kind, bounded_impact
            from source_events
            where status = 'approved'
              and occurred_at_utc >= ? and occurred_at_utc < ?
              and available_at_utc <= ?
            order by occurred_at_utc
            """,
            (
                start.isoformat(),
                as_of.isoformat(),
                as_of.isoformat(),
                start.isoformat(),
                as_of.isoformat(),
                as_of.isoformat(),
            ),
        ).fetchall()
        result: list[tuple[dt.datetime, str, float]] = []
        for occurred_at, kind, impact in rows:
            parsed = parse_timestamp(occurred_at)
            if parsed is not None:
                result.append((parsed, str(kind), float(impact)))
        return result

    def latest_sample(
        self,
        user: str,
        platform: str,
        window: Window,
        as_of: dt.datetime | None = None,
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            select sampled_at_utc, freshness_seconds, raw_posts_seen
            from count_samples
            where user = ? and platform = ?
              and window_start_utc = ? and window_end_utc = ?
              and (? is null or sampled_at_utc <= ?)
            order by sampled_at_utc desc
            limit 1
            """,
            (
                user,
                platform,
                window.start_utc.isoformat(),
                window.end_utc.isoformat(),
                as_of.isoformat() if as_of else None,
                as_of.isoformat() if as_of else None,
            ),
        ).fetchone()


def count_before(
    times: list[dt.datetime] | list[PostObservation],
    start: dt.datetime,
    end: dt.datetime,
    available_as_of: dt.datetime | None = None,
) -> int:
    count = 0
    for item in times:
        value = item.created_at_utc if isinstance(item, PostObservation) else item
        if not (start <= value < end):
            continue
        if available_as_of is not None and isinstance(item, PostObservation):
            if item.available_at_utc is not None and item.available_at_utc > available_as_of:
                continue
        count += 1
    return count


def outcome_midpoint(lower: int | None, upper: int | None, default_width: int = 20) -> float:
    if lower is None:
        return 0.0
    if upper is None:
        return lower + default_width / 2
    return (lower + upper) / 2


def training_label_for_window(
    store: MarketStore,
    window: Window,
    user: str,
    platform: str,
    require_verified_final_count: bool,
    use_resolved_bin_labels: bool,
) -> tuple[float | None, str]:
    observed = float(store.final_count(window, user, platform))
    verified = store.final_count_is_verified(window, user, platform)
    if verified is not False:
        return observed, "local_verified"
    resolved = store.resolved_range(window.slug)
    if use_resolved_bin_labels and resolved is not None:
        return outcome_midpoint(resolved[0], resolved[1]), "resolved_bin_midpoint"
    if require_verified_final_count:
        return None, "unverified_excluded"
    return observed, "local_unverified"


def market_distribution(prices: list[tuple[int | None, int | None, float]]) -> tuple[float, float, float, float]:
    """Return expected count, raw price mass, normalized entropy and max probability."""
    raw_mass = sum(max(0.0, probability) for _, _, probability in prices)
    if raw_mass <= 0:
        return 0.0, 0.0, 0.0, 0.0
    normalized = [
        (lower, upper, max(0.0, probability) / raw_mass)
        for lower, upper, probability in prices
    ]
    expected = sum(outcome_midpoint(lower, upper) * probability for lower, upper, probability in normalized)
    entropy = -sum(probability * math.log(probability) for _, _, probability in normalized if probability > 0)
    max_probability = max(probability for _, _, probability in normalized)
    return expected, raw_mass, entropy, max_probability


def price_distribution_is_valid(
    probability_mass: float,
    minimum: float = MIN_PRICE_PROBABILITY_MASS,
    maximum: float = MAX_PRICE_PROBABILITY_MASS,
) -> bool:
    return minimum <= probability_mass <= maximum


def outcome_label_for_count(
    value: float,
    ranges: list[tuple[str, int | None, int | None]],
) -> str | None:
    rounded = int(round(value))
    for label, lower, upper in ranges:
        if (lower is None or rounded >= lower) and (upper is None or rounded <= upper):
            return label
    return None


def poisson_pmf(count: int, mean_count: float) -> float:
    if count < 0 or mean_count <= 0:
        return 1.0 if count == 0 and mean_count <= 0 else 0.0
    return math.exp(-mean_count + count * math.log(mean_count) - math.lgamma(count + 1))


def negative_binomial_pmf(count: int, mean_count: float, variance: float) -> float:
    """PMF for an overdispersed count forecast, falling back to Poisson."""
    if count < 0 or mean_count <= 0:
        return 1.0 if count == 0 and mean_count <= 0 else 0.0
    if variance <= mean_count + 1e-9:
        return poisson_pmf(count, mean_count)
    dispersion = mean_count * mean_count / (variance - mean_count)
    success = dispersion / (dispersion + mean_count)
    return math.exp(
        math.lgamma(count + dispersion)
        - math.lgamma(dispersion)
        - math.lgamma(count + 1)
        + dispersion * math.log(success)
        + count * math.log1p(-success)
    )


def count_range_probability(
    mean_count: float,
    variance: float,
    lower: int | None,
    upper: int | None,
) -> float:
    if lower is None:
        return 0.0
    if upper is None:
        return max(0.0, 1.0 - sum(
            negative_binomial_pmf(count, mean_count, variance)
            for count in range(max(0, lower))
        ))
    return sum(
        negative_binomial_pmf(count, mean_count, variance)
        for count in range(max(0, lower), upper + 1)
    )


def count_distribution(
    mean_count: float,
    variance: float,
    ranges: list[tuple[str, int | None, int | None]],
) -> list[float]:
    probabilities = [
        count_range_probability(mean_count, variance, lower, upper)
        for _, lower, upper in ranges
    ]
    total = sum(probabilities)
    return [probability / total for probability in probabilities] if total > 0 else [0.0] * len(ranges)


def estimate_prediction_variance(
    model: RidgeModel,
    examples: list[Example],
    predictor: Any | None = None,
) -> float:
    if not examples:
        return 400.0
    score = predictor or model.predict
    residuals = [(score(example.features) - example.label) ** 2 for example in examples]
    mean_target = mean(example.label for example in examples)
    return max(mean_target * 1.25, mean(residuals) + mean_target)


def market_features(
    store: MarketStore,
    window: Window,
    as_of: dt.datetime,
    times: list[dt.datetime] | list[PostObservation],
    prior_rate: float,
    max_price_age_hours: float,
    live_prices: list[tuple[int | None, int | None, float]] | None = None,
) -> list[float]:
    total_hours = max((window.end_utc - window.start_utc).total_seconds() / 3600, 1)
    elapsed = max((as_of - window.start_utc).total_seconds() / 3600, 0)
    remaining = max((window.end_utc - as_of).total_seconds() / 3600, 0)
    current = count_before(times, window.start_utc, as_of, as_of)
    count_6h = count_before(times, max(window.start_utc, as_of - dt.timedelta(hours=6)), as_of, as_of)
    count_24h = count_before(times, max(window.start_utc, as_of - dt.timedelta(hours=24)), as_of, as_of)
    count_1h = count_before(times, max(window.start_utc, as_of - dt.timedelta(hours=1)), as_of, as_of)
    count_3h = count_before(times, max(window.start_utc, as_of - dt.timedelta(hours=3)), as_of, as_of)
    count_12h = count_before(times, max(window.start_utc, as_of - dt.timedelta(hours=12)), as_of, as_of)
    count_48h = count_before(times, max(window.start_utc, as_of - dt.timedelta(hours=48)), as_of, as_of)
    pace = current / elapsed * total_hours if elapsed else 0.0
    last_1h_rate = float(count_1h)
    last_3h_rate = count_3h / 3
    last_6h_rate = count_6h / 6
    last_24h_rate = count_24h / 24
    last_12h_rate = count_12h / 12
    last_48h_rate = count_48h / 48
    rate_acceleration = last_6h_rate - last_24h_rate
    prior_projection = current + prior_rate * remaining

    prices = (
        live_prices
        if live_prices is not None
        else store.latest_prices(window.slug, as_of, max_price_age_hours)
    )
    expected_count, probability_mass, entropy, max_probability = market_distribution(prices)
    if not price_distribution_is_valid(probability_mass):
        expected_count = 0.0
        entropy = 0.0
        max_probability = 0.0
    event_rows = store.events_before(window.start_utc, as_of)
    event_impact = clamp(sum(impact for _, impact in event_rows), -6.0, 6.0)
    event_count = len(event_rows)
    kind_counts: dict[str, int] = {}
    for kind, _ in event_rows:
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
    event_kinds = ("news", "launch", "conflict", "policy", "other")
    detailed_events = store.detailed_events_before(window.start_utc, as_of)
    event_decay_impact = 0.0
    event_recent_6h = 0
    event_recent_24h = 0
    for occurred_at, _, impact in detailed_events:
        age_hours = max(0.0, (as_of - occurred_at).total_seconds() / 3600)
        event_decay_impact += impact * math.exp(-age_hours / 24)
        event_recent_6h += age_hours <= 6
        event_recent_24h += age_hours <= 24
    event_decay_impact = clamp(event_decay_impact, -4.0, 4.0)
    local_hour = as_of.astimezone(ZoneInfo(DEFAULT_TZ)).hour
    daytime = 1.0 if 8 <= local_hour < 23 else 0.0
    return [
        current,
        elapsed,
        remaining,
        pace,
        last_6h_rate,
        last_24h_rate,
        prior_projection,
        expected_count,
        probability_mass,
        entropy,
        max_probability,
        event_count,
        event_impact,
        *[float(kind_counts.get(kind, 0)) for kind in event_kinds],
        last_1h_rate,
        last_3h_rate,
        last_12h_rate,
        last_48h_rate,
        rate_acceleration,
        daytime,
        event_decay_impact,
        float(event_recent_6h),
        float(event_recent_24h),
        *[float(kind_counts.get(kind, 0)) for kind in ("travel", "tesla", "spacex", "xai")],
    ]


FEATURE_NAMES = (
    "current_count", "elapsed_hours", "remaining_hours", "pace_projection",
    "last_6h_rate", "last_24h_rate", "prior_rate_projection",
    "market_expected_count", "market_probability_mass", "market_entropy",
    "market_max_probability", "event_count", "event_impact",
    "event_news", "event_launch", "event_conflict", "event_policy", "event_other",
    "last_1h_rate", "last_3h_rate", "last_12h_rate", "last_48h_rate",
    "rate_acceleration", "daytime", "event_decay_impact", "event_recent_6h",
    "event_recent_24h",
    "event_travel", "event_tesla", "event_spacex", "event_xai",
)


def build_examples(
    store: MarketStore,
    windows: list[Window],
    user: str,
    platform: str,
    step_hours: float,
    min_elapsed_hours: float,
    min_final_count: int,
    max_price_age_hours: float,
    require_verified_final_count: bool,
    use_resolved_bin_labels: bool = False,
) -> list[Example]:
    times = store.post_observations(user, platform)
    labels = {
        window.slug: training_label_for_window(
            store,
            window,
            user,
            platform,
            require_verified_final_count,
            use_resolved_bin_labels,
        )[0]
        for window in windows
    }
    examples: list[Example] = []
    for index, window in enumerate(windows):
        final = labels[window.slug]
        if final is None or final < min_final_count or window.end_utc > utc_now():
            continue
        prior_windows = [
            previous for previous in windows[:index]
            if previous.end_utc <= window.start_utc and labels.get(previous.slug) is not None
        ]
        prior_hours = sum((previous.end_utc - previous.start_utc).total_seconds() / 3600 for previous in prior_windows)
        prior_posts = sum(labels[previous.slug] or 0.0 for previous in prior_windows)
        prior_rate = prior_posts / prior_hours if prior_hours else 0.0
        checkpoint = window.start_utc + dt.timedelta(hours=min_elapsed_hours)
        while checkpoint < window.end_utc:
            features = market_features(store, window, checkpoint, times, prior_rate, max_price_age_hours)
            examples.append(
                Example(
                    window,
                    checkpoint,
                    features,
                    float(final),
                    int(features[0]),
                )
            )
            checkpoint += dt.timedelta(hours=step_hours)
    return examples


def split_examples_purged(
    examples: list[Example],
    train_ratio: float,
) -> tuple[list[Example], list[Example], list[str]]:
    """Chronologically split markets and remove training windows that overlap holdout."""
    windows = {example.window.slug: example.window for example in examples}
    ordered = sorted(windows.values(), key=lambda window: (window.start_utc, window.slug))
    if len(ordered) <= 1:
        return examples, [], []

    cutoff = max(1, min(len(ordered) - 1, round(len(ordered) * train_ratio)))
    test_windows = ordered[cutoff:]
    first_test_start = test_windows[0].start_utc
    candidate_train = ordered[:cutoff]
    train_slugs = {window.slug for window in candidate_train if window.end_utc <= first_test_start}
    purged_slugs = [window.slug for window in candidate_train if window.slug not in train_slugs]

    # With fewer than three overlapping seven-day windows, no clean holdout exists.
    # Fit the available data but make that limitation explicit in metadata.
    if not train_slugs:
        return examples, [], [window.slug for window in candidate_train]

    test_slugs = {window.slug for window in test_windows}
    train_examples = [example for example in examples if example.window.slug in train_slugs]
    test_examples = [example for example in examples if example.window.slug in test_slugs]
    return train_examples, test_examples, purged_slugs


def solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            continue
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor:
                augmented[row] = [
                    left - factor * right
                    for left, right in zip(augmented[row], augmented[column])
                ]
    return [augmented[index][-1] for index in range(size)]


@dataclass(frozen=True)
class RidgeModel:
    feature_names: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float
    ridge: float
    ensemble_weights: tuple[float, float, float] | None = None

    def predict(self, features: list[float]) -> float:
        normalized = [
            (value - mean_value) / scale
            for value, mean_value, scale in zip(features, self.means, self.scales)
        ]
        ridge_prediction = self.intercept + sum(
            coef * value for coef, value in zip(self.coefficients, normalized)
        )
        if self.ensemble_weights is None:
            return ridge_prediction
        ridge_weight, pace_weight, prior_weight = self.ensemble_weights
        return max(
            features[0],
            0.0,
            ridge_weight * ridge_prediction
            + pace_weight * features[3]
            + prior_weight * features[6],
        )


def fit_ridge(examples: list[Example], ridge: float) -> RidgeModel:
    if not examples:
        raise ValueError("No training examples. Crawl multiple ended markets first.")
    width = len(FEATURE_NAMES)
    columns = [[example.features[index] for example in examples] for index in range(width)]
    means = [mean(column) for column in columns]
    scales = [math.sqrt(mean((value - average) ** 2 for value in column)) or 1.0 for column, average in zip(columns, means)]
    x_rows = [[(value - means[index]) / scales[index] for index, value in enumerate(example.features)] for example in examples]
    y = [example.label for example in examples]
    normal = [[0.0] * (width + 1) for _ in range(width + 1)]
    rhs = [0.0] * (width + 1)
    for row, target in zip(x_rows, y):
        values = [1.0, *row]
        for left in range(width + 1):
            for right in range(width + 1):
                normal[left][right] += values[left] * values[right]
            normal[left][left] += ridge if left else 0.0
        for left in range(width + 1):
            rhs[left] += values[left] * target
    solution = solve_linear(normal, rhs)
    return RidgeModel(
        FEATURE_NAMES,
        tuple(means),
        tuple(scales),
        tuple(solution[1:]),
        solution[0],
        ridge,
    )


@dataclass(frozen=True)
class EnsembleParams:
    ridge_weight: float
    pace_weight: float
    prior_weight: float


def ensemble_predict(
    features: list[float],
    model: RidgeModel,
    params: EnsembleParams,
) -> float:
    prediction = (
        params.ridge_weight * model.predict(features)
        + params.pace_weight * features[3]
        + params.prior_weight * features[6]
    )
    return max(features[0], prediction, 0.0)


def fit_ensemble(
    examples: list[Example],
    model: RidgeModel,
    grid_step: float = 0.05,
) -> EnsembleParams:
    """Choose a simple convex blend to reduce small-sample Ridge overfit."""
    best = EnsembleParams(1.0, 0.0, 0.0)
    best_error = float("inf")
    steps = range(round(1 / grid_step) + 1)
    for ridge_index in steps:
        ridge_weight = ridge_index * grid_step
        for pace_index in steps:
            pace_weight = pace_index * grid_step
            prior_weight = 1.0 - ridge_weight - pace_weight
            if prior_weight < -1e-9:
                continue
            candidate = EnsembleParams(ridge_weight, pace_weight, max(0.0, prior_weight))
            error = mean(
                abs(ensemble_predict(example.features, model, candidate) - example.label)
                for example in examples
            ) if examples else float("inf")
            if error < best_error:
                best = candidate
                best_error = error
    return best


def model_payload(model: RidgeModel, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": MODEL_NAME,
        "feature_names": list(model.feature_names),
        "means": list(model.means),
        "scales": list(model.scales),
        "coefficients": list(model.coefficients),
        "intercept": model.intercept,
        "ridge": model.ridge,
        "ensemble_weights": list(model.ensemble_weights) if model.ensemble_weights else None,
        **metadata,
    }


def load_model(path: Path) -> RidgeModel:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return RidgeModel(
        tuple(payload["feature_names"]),
        tuple(float(value) for value in payload["means"]),
        tuple(float(value) for value in payload["scales"]),
        tuple(float(value) for value in payload["coefficients"]),
        float(payload["intercept"]),
        float(payload["ridge"]),
        tuple(float(value) for value in payload["ensemble_weights"])
        if payload.get("ensemble_weights") else None,
    )


def score_examples(model: RidgeModel, examples: list[Example]) -> tuple[float, float]:
    if not examples:
        return math.nan, math.nan
    errors = [model.predict(example.features) - example.label for example in examples]
    return mean(abs(error) for error in errors), math.sqrt(mean(error * error for error in errors))


def write_report(path: Path, model: RidgeModel, examples: list[Example]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["slug", "as_of_utc", "current_count", "actual_final_count", "predicted_final_count", "absolute_error"])
        writer.writeheader()
        for example in examples:
            prediction = model.predict(example.features)
            writer.writerow({
                "slug": example.window.slug,
                "as_of_utc": example.as_of_utc.isoformat(),
                "current_count": example.current_count,
                "actual_final_count": int(example.label),
                "predicted_final_count": round(prediction, 4),
                "absolute_error": round(abs(prediction - example.label), 4),
            })


def eligible_prior_windows(
    store: MarketStore,
    windows: list[Window],
    target: Window,
    user: str,
    platform: str,
    require_verified_final_count: bool,
    use_resolved_bin_labels: bool,
) -> list[Window]:
    prior = [window for window in windows if window.end_utc <= target.start_utc]
    if use_resolved_bin_labels:
        return [window for window in prior if store.resolved_range(window.slug) is not None]
    if not require_verified_final_count:
        return prior
    return [
        window for window in prior
        if store.final_count_is_verified(window, user, platform) is not False
    ]


def prior_rate_for_windows(
    store: MarketStore,
    windows: list[Window],
    user: str,
    platform: str,
    require_verified_final_count: bool,
    use_resolved_bin_labels: bool,
) -> float:
    total_hours = sum((window.end_utc - window.start_utc).total_seconds() / 3600 for window in windows)
    total_count = sum(
        training_label_for_window(
            store,
            window,
            user,
            platform,
            require_verified_final_count,
            use_resolved_bin_labels,
        )[0] or 0.0
        for window in windows
    )
    return total_count / total_hours if total_hours else 0.0


def hourly_checkpoints(
    window: Window,
    min_elapsed_hours: float = 1,
    step_minutes: float = 60,
    min_elapsed_minutes: float | None = None,
) -> list[dt.datetime]:
    """Generate sliding checkpoints; minute resolution is supported for replay."""
    if min_elapsed_minutes is None:
        min_elapsed_minutes = min_elapsed_hours * 60
    first = window.start_utc + dt.timedelta(minutes=max(0.0, min_elapsed_minutes))
    checkpoints: list[dt.datetime] = []
    step = dt.timedelta(minutes=max(0.1, step_minutes))
    while first < window.end_utc:
        checkpoints.append(first)
        first += step
    return checkpoints


def write_hourly_test_reports(
    report_path: Path,
    summary_path: Path,
    rows: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    public_report_path: Path | None = None,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    hourly_fields = [
        "slug", "checkpoint_utc", "elapsed_hours", "current_count",
        "remaining_hours", "checkpoint_step_minutes", "post_data_available_as_of",
        "model_forecast_final_count", "forecast_bin", "observed_local_final_count",
        "resolved_bin", "resolved_lower", "resolved_upper", "forecast_in_resolved_bin",
        "observed_count_verified", "absolute_error_to_observed_count",
        "market_expected_count", "price_probability_mass", "price_distribution_valid", "training_windows",
        "model_source", "prediction_variance", "forecast_bin_probability",
        "resolved_bin_probability", "bin_probabilities_json", "brier_score", "log_loss",
        "price_snapshot_time_utc", "price_snapshot_age_minutes", "price_snapshot_json",
        "orderbook_snapshot_time_utc", "orderbook_snapshot_age_minutes", "orderbook_snapshot_json",
        "model_market_edge", "decision_status", "data_quality_flags",
    ]
    summary_fields = [
        "slug", "status", "hourly_predictions", "training_windows", "model_source",
        "observed_local_final_count", "resolved_bin", "observed_count_verified",
        "hourly_mae_to_observed_count", "last_forecast_final_count", "last_forecast_bin",
        "last_forecast_in_resolved_bin", "mean_resolved_bin_probability",
        "brier_score", "log_loss", "note",
        "paper_candidate_checkpoints", "quality_limited_checkpoints",
        "mean_model_market_edge", "backtest_step_minutes",
    ]
    with report_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=hourly_fields)
        writer.writeheader()
        writer.writerows(rows)
    with summary_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(summaries)
    if public_report_path is not None:
        public_fields = [
            "slug", "checkpoint_utc", "elapsed_hours", "current_count",
            "model_forecast_final_count", "forecast_bin", "market_expected_count",
            "price_probability_mass", "price_distribution_valid", "training_windows",
            "model_source", "prediction_variance", "forecast_bin_probability",
            "bin_probabilities_json",
        ]
        public_report_path.parent.mkdir(parents=True, exist_ok=True)
        with public_report_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=public_fields)
            writer.writeheader()
            writer.writerows({key: row.get(key) for key in public_fields} for row in rows)


def hourly_test_command(args: argparse.Namespace) -> None:
    store = MarketStore(Path(args.db))
    try:
        all_windows = store.windows(
            parse_bound(args.start, args.timezone) if args.start else None,
            parse_bound(args.end, args.timezone) if args.end else None,
        )
        if args.slug:
            selected = {slug for slug in args.slug}
            windows = [window for window in all_windows if window.slug in selected]
            missing = selected - {window.slug for window in windows}
            if missing:
                raise SystemExit(f"Markets not found in database: {', '.join(sorted(missing))}")
        else:
            windows = all_windows
        windows = [window for window in windows if window.end_utc <= utc_now()]
        if not windows:
            raise SystemExit("No ended markets found in the requested local-data range.")

        fixed_model = load_model(Path(args.model)) if args.model else None
        times = store.post_observations(args.user, args.platform)
        hourly_rows: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []

        for window in windows:
            prior_windows = eligible_prior_windows(
                store, all_windows, window, args.user, args.platform,
                args.require_verified_final_count, args.use_resolved_bin_labels,
            )
            model = fixed_model
            model_source = "provided_model" if fixed_model else "walk_forward_ensemble"
            train_examples: list[Example] = []
            ensemble_params: EnsembleParams | None = None
            if model is None:
                train_examples = build_examples(
                    store,
                    prior_windows,
                    args.user,
                    args.platform,
                    args.training_step_hours,
                    args.training_min_elapsed_hours,
                    args.min_final_count,
                    args.max_price_age_hours,
                    args.require_verified_final_count,
                    args.use_resolved_bin_labels,
                )
                if not train_examples:
                    summaries.append({
                        "slug": window.slug,
                        "status": "skipped_no_prior_examples",
                        "hourly_predictions": 0,
                        "training_windows": len(prior_windows),
                        "model_source": model_source,
                        "observed_local_final_count": store.final_count(window, args.user, args.platform),
                        "resolved_bin": (store.outcome_ranges(window.slug) and
                                         next((label for label, lower, upper in store.outcome_ranges(window.slug)
                                               if store.resolved_range(window.slug) == (lower, upper)), None)),
                        "observed_count_verified": store.final_count_is_verified(window, args.user, args.platform),
                        "hourly_mae_to_observed_count": None,
                        "last_forecast_final_count": None,
                        "last_forecast_bin": None,
                        "last_forecast_in_resolved_bin": None,
                        "mean_resolved_bin_probability": None,
                        "brier_score": None,
                        "log_loss": None,
                        "note": "No earlier eligible windows were available for walk-forward training.",
                    })
                    continue
                model = fit_ridge(train_examples, args.ridge)
                ensemble_params = fit_ensemble(train_examples, model)

            prior_rate = prior_rate_for_windows(
                store,
                prior_windows,
                args.user,
                args.platform,
                args.require_verified_final_count,
                args.use_resolved_bin_labels,
            )
            predictor = (
                (lambda features: ensemble_predict(features, model, ensemble_params))
                if ensemble_params is not None else model.predict
            )
            prediction_variance = estimate_prediction_variance(model, train_examples, predictor)
            observed_final = store.final_count(window, args.user, args.platform)
            resolved = store.resolved_range(window.slug)
            ranges = store.outcome_ranges(window.slug)
            resolved_label = next(
                (label for label, lower, upper in ranges if resolved == (lower, upper)),
                None,
            )
            verified = store.final_count_is_verified(window, args.user, args.platform)
            market_rows: list[dict[str, Any]] = []
            for checkpoint in hourly_checkpoints(
                window,
                args.min_elapsed_hours,
                args.step_minutes,
                args.min_elapsed_minutes,
            ):
                features = market_features(
                    store, window, checkpoint, times, prior_rate, args.max_price_age_hours
                )
                forecast = predictor(features)
                forecast_bin = outcome_label_for_count(forecast, ranges)
                probabilities = count_distribution(forecast, prediction_variance, ranges)
                forecast_index = next(
                    (index for index, (label, _, _) in enumerate(ranges) if label == forecast_bin),
                    None,
                )
                resolved_index = next(
                    (index for index, (label, lower, upper) in enumerate(ranges)
                     if resolved == (lower, upper)),
                    None,
                )
                forecast_probability = probabilities[forecast_index] if forecast_index is not None else None
                resolved_probability = probabilities[resolved_index] if resolved_index is not None else None
                price_rows = store.prices_at(window.slug, checkpoint, args.max_price_age_hours)
                book_rows = store.orderbooks_at(window.slug, checkpoint, args.max_price_age_hours)
                price_source_times = [parse_timestamp(row["source_price_time_utc"]) for row in price_rows]
                book_capture_times = [parse_timestamp(row["captured_at_utc"]) for row in book_rows]
                price_time = max((value for value in price_source_times if value), default=None)
                book_time = max((value for value in book_capture_times if value), default=None)
                selected_book = next((row for row in book_rows if row.get("outcome_label") == forecast_bin), None)
                executable_ask = selected_book.get("best_ask") if selected_book else None
                edge = (
                    float(forecast_probability) - float(executable_ask)
                    if forecast_probability is not None and executable_ask is not None else None
                )
                quality_flags: list[str] = []
                if not price_rows:
                    quality_flags.append("missing_price_snapshot")
                if not book_rows:
                    quality_flags.append("missing_orderbook_snapshot")
                if book_rows and not any(row.get("point_in_time_safe") for row in book_rows):
                    quality_flags.append("orderbook_capture_time_only")
                if features[8] and not price_distribution_is_valid(features[8]):
                    quality_flags.append("invalid_price_probability_mass")
                decision_status = "no_trade_data_insufficient"
                if edge is not None and selected_book:
                    spread = float(selected_book.get("spread") or 0.0)
                    depth = float(selected_book.get("ask_depth_5") or 0.0)
                    if edge >= args.backtest_min_edge and spread <= args.backtest_max_spread and depth >= args.backtest_min_depth:
                        decision_status = "paper_candidate_only"
                    else:
                        decision_status = "no_trade_quality_or_edge"
                brier_score = None
                log_loss = None
                if resolved_index is not None:
                    brier_score = sum(
                        (probability - (1.0 if index == resolved_index else 0.0)) ** 2
                        for index, probability in enumerate(probabilities)
                    )
                    log_loss = -math.log(max(resolved_probability or 0.0, 1e-12))
                in_resolved = (
                    resolved is not None
                    and (resolved[0] is None or forecast >= resolved[0])
                    and (resolved[1] is None or forecast <= resolved[1])
                )
                market_rows.append({
                    "slug": window.slug,
                    "checkpoint_utc": checkpoint.isoformat(),
                    "elapsed_hours": round((checkpoint - window.start_utc).total_seconds() / 3600, 2),
                    "current_count": int(features[0]),
                    "remaining_hours": round((window.end_utc - checkpoint).total_seconds() / 3600, 2),
                    "checkpoint_step_minutes": args.step_minutes,
                    "post_data_available_as_of": checkpoint.isoformat(),
                    "model_forecast_final_count": round(forecast, 4),
                    "forecast_bin": forecast_bin,
                    "observed_local_final_count": observed_final,
                    "resolved_bin": resolved_label,
                    "resolved_lower": resolved[0] if resolved else None,
                    "resolved_upper": resolved[1] if resolved else None,
                    "forecast_in_resolved_bin": in_resolved,
                    "observed_count_verified": verified,
                    "absolute_error_to_observed_count": round(abs(forecast - observed_final), 4),
                    "market_expected_count": round(features[7], 4),
                    "price_probability_mass": round(features[8], 4),
                    "price_distribution_valid": price_distribution_is_valid(features[8]),
                    "training_windows": len(prior_windows),
                    "model_source": model_source,
                    "prediction_variance": round(prediction_variance, 4),
                    "forecast_bin_probability": round(forecast_probability, 6)
                    if forecast_probability is not None else None,
                    "resolved_bin_probability": round(resolved_probability, 6)
                    if resolved_probability is not None else None,
                    "bin_probabilities_json": json_dumps({
                        label: round(probability, 6)
                        for (label, _, _), probability in zip(ranges, probabilities)
                    }),
                    "brier_score": round(brier_score, 6) if brier_score is not None else None,
                    "log_loss": round(log_loss, 6) if log_loss is not None else None,
                    "price_snapshot_time_utc": price_time.isoformat() if price_time else None,
                    "price_snapshot_age_minutes": round((checkpoint - price_time).total_seconds() / 60, 3) if price_time else None,
                    "price_snapshot_json": json_dumps(price_rows),
                    "orderbook_snapshot_time_utc": book_time.isoformat() if book_time else None,
                    "orderbook_snapshot_age_minutes": round((checkpoint - book_time).total_seconds() / 60, 3) if book_time else None,
                    "orderbook_snapshot_json": json_dumps(book_rows),
                    "model_market_edge": round(edge, 6) if edge is not None else None,
                    "decision_status": decision_status,
                    "data_quality_flags": json_dumps(quality_flags),
                })
            hourly_rows.extend(market_rows)
            last_row = market_rows[-1] if market_rows else {}
            summaries.append({
                "slug": window.slug,
                "status": "scored",
                "hourly_predictions": len(market_rows),
                "training_windows": len(prior_windows),
                "model_source": model_source,
                "observed_local_final_count": observed_final,
                "resolved_bin": resolved_label,
                "observed_count_verified": verified,
                "hourly_mae_to_observed_count": round(
                    mean(row["absolute_error_to_observed_count"] for row in market_rows), 4
                ) if market_rows else None,
                "last_forecast_final_count": last_row.get("model_forecast_final_count"),
                "last_forecast_bin": last_row.get("forecast_bin"),
                "last_forecast_in_resolved_bin": last_row.get("forecast_in_resolved_bin"),
                "mean_resolved_bin_probability": round(mean(
                    row["resolved_bin_probability"] for row in market_rows
                    if row["resolved_bin_probability"] is not None
                ), 6) if any(row["resolved_bin_probability"] is not None for row in market_rows) else None,
                "brier_score": round(mean(
                    row["brier_score"] for row in market_rows
                    if row["brier_score"] is not None
                ), 6) if any(row["brier_score"] is not None for row in market_rows) else None,
                "log_loss": round(mean(
                    row["log_loss"] for row in market_rows
                    if row["log_loss"] is not None
                ), 6) if any(row["log_loss"] is not None for row in market_rows) else None,
                "paper_candidate_checkpoints": sum(
                    row.get("decision_status") == "paper_candidate_only" for row in market_rows
                ),
                "quality_limited_checkpoints": sum(
                    row.get("decision_status") != "paper_candidate_only" for row in market_rows
                ),
                "mean_model_market_edge": round(mean(
                    row["model_market_edge"] for row in market_rows
                    if row["model_market_edge"] is not None
                ), 6) if any(row["model_market_edge"] is not None for row in market_rows) else None,
                "backtest_step_minutes": args.step_minutes,
                "note": (
                    "Observed local count is outside the resolved bin; treat error metrics as exploratory."
                    if verified is False else ""
                ),
            })

        write_hourly_test_reports(
            Path(args.report),
            Path(args.summary),
            hourly_rows,
            summaries,
            Path(args.public_report),
        )
        print(json.dumps({
            "hourly_report": str(Path(args.report).resolve()),
            "summary_report": str(Path(args.summary).resolve()),
            "public_report": str(Path(args.public_report).resolve()),
            "markets_requested": len(windows),
            "markets_scored": sum(summary["status"] == "scored" for summary in summaries),
            "hourly_predictions": len(hourly_rows),
            "walk_forward": fixed_model is None,
        }, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        store.close()


def crawl_one(args: argparse.Namespace, slug: str) -> dict[str, Any]:
    try:
        request_delay = float(getattr(args, "request_delay", 0.1))
    except (TypeError, ValueError) as exc:
        raise ValueError("request_delay must be finite and nonnegative") from exc
    if not math.isfinite(request_delay) or request_delay < 0:
        raise ValueError("request_delay must be finite and nonnegative")
    gamma = GammaClient(args.gamma_url, args.timeout, args.proxy)
    clob = ClobClient(args.clob_url, args.timeout, args.proxy)
    data_client = PublicDataClient(getattr(args, "data_url", "https://data-api.polymarket.com"), args.timeout, args.proxy)
    clob.request_delay = request_delay
    data_client.request_delay = request_delay
    tracker = XTrackerClient(args.tracker_url, args.timeout, args.proxy)
    store = MarketStore(Path(args.db))
    try:
        price_fidelity_minutes = int(getattr(args, "price_fidelity", 5))
    except (TypeError, ValueError) as exc:
        store.close()
        raise ValueError("price_fidelity must be a positive number of minutes") from exc
    if price_fidelity_minutes <= 0:
        store.close()
        raise ValueError("price_fidelity must be a positive number of minutes")
    captured_at = utc_now()
    event: dict[str, Any] | None = None
    status = "failed"
    result: dict[str, Any] = {
        "slug": slug,
        "captured_at_utc": captured_at.isoformat(),
        "data_time_utc": None,
        "posts_returned": 0,
        "new_posts_inserted": 0,
        "new_price_rows": 0,
        "new_orderbook_rows": 0,
        "new_trade_rows": 0,
        "market_activity_archive_id": None,
        "batch_clob": bool(getattr(args, "batch_clob", True)),
        "price_errors": 0,
        "orderbook_errors": 0,
        "trade_errors": 0,
        "price_fidelity_minutes": price_fidelity_minutes,
        "source_errors": [],
    }
    try:
        event = gamma.get_event(slug)
        window = store.save_event(event, args.timezone)
        # The crawl record represents the latest source snapshot at capture time;
        # the logical market window remains in the nested metadata.
        result["data_time_utc"] = captured_at.isoformat()
        result["market_event"] = event
        result.update({
            "window_start_utc": window.start_utc.isoformat(),
            "window_end_utc": window.end_utc.isoformat(),
        })
        local_start = window.start_utc.astimezone(ZoneInfo(args.timezone))
        local_end = window.end_utc.astimezone(ZoneInfo(args.timezone))
        try:
            posts_raw = tracker.get_posts(
                args.user,
                args.platform,
                local_start.date().isoformat(),
                local_end.date().isoformat(),
                args.timezone,
            )
            posts = [normalize_post(row, args.user, args.platform) for row in posts_raw]
            result["posts_returned"] = len(posts_raw)
            result["new_posts_inserted"] = store.upsert_posts(posts)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            result["source_errors"].append({"source": "xtracker", "error": type(exc).__name__, "message": str(exc)})

        newest_capture = store.newest_capture(args.user, args.platform)
        freshness = int((utc_now() - newest_capture).total_seconds()) if newest_capture else None
        store.insert_sample(
            args.user,
            args.platform,
            slug,
            window.start_utc,
            window.end_utc,
            store.count_posts(args.user, args.platform, window.start_utc, window.end_utc),
            freshness,
            result["posts_returned"],
        )
        result["post_freshness_seconds"] = freshness
        result["source_age_seconds"] = {"xtracker": freshness, "gamma": 0}
        resolved_range = store.resolved_range(slug)
        if resolved_range is not None:
            confirmed = result["confirmed_count"] = store.final_count(window, args.user, args.platform)
            result["resolved_range"] = {"lower": resolved_range[0], "upper": resolved_range[1]}
            result["settlement_count_verified"] = store.final_count_is_verified(window, args.user, args.platform)
            if result["settlement_count_verified"] is False:
                result["source_errors"].append({
                    "source": "xtracker_history",
                    "error": "SettlementMismatch",
                    "message": f"captured_count={confirmed} does not fit resolved range {resolved_range}",
                })

        outcomes = store.conn.execute(
            "select market_id, yes_token_id from market_outcomes where event_slug = ? and yes_token_id is not null",
            (slug,),
        ).fetchall()
        token_ids = [str(row[1]) for row in outcomes]
        history_end = min(window.end_utc, captured_at)
        result['price_coverage'] = []
        result['price_requests'] = []
        result['quality_warnings'] = []
        result['price_window_start_utc'] = window.start_utc.isoformat()
        result['price_window_end_utc'] = history_end.isoformat()
        batch_books: dict[str, dict[str, Any]] = {}
        if getattr(args, "batch_clob", True) and token_ids and args.orderbooks:
            try:
                batch_books = clob.get_orderbooks(token_ids)
                result["batch_orderbooks_requested"] = len(token_ids)
                result["batch_orderbooks_received"] = len(batch_books)
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError) as exc:
                result["source_errors"].append({"source": "clob_batch_books", "error": type(exc).__name__, "message": str(exc)})
        if not args.skip_prices and not token_ids:
            result['source_errors'].append({'source':'clob_history','message':'没有可抓取的YES token，历史价格未完成'})

        prior_prices = {}
        prior_checks = {}
        force_prices = bool(getattr(args, 'force_prices', False))
        if not args.skip_prices:
            cutoff = (captured_at-dt.timedelta(hours=6)).isoformat()
            for market_id, token_id in outcomes:
                key = (str(market_id), str(token_id))
                prior_prices[key] = store.conn.execute(
                    'select price_time_utc from market_prices where event_slug=? and market_id=? and token_id=? and price_time_utc>=? and price_time_utc<? and resolution_seconds>0 and resolution_seconds<=?',
                    (slug, market_id, token_id, window.start_utc.isoformat(), history_end.isoformat(), price_fidelity_minutes*60)).fetchall()
                prior_checks[key] = [(parse_timestamp(r[0]), parse_timestamp(r[1])) for r in store.conn.execute(
                    'select start_utc,end_utc from history_price_checks where event_slug=? and market_id=? and token_id=? and source_url=? and fidelity=? and checked_at_utc>=?',
                    (slug, market_id, token_id, data_client.base_url, price_fidelity_minutes, cutoff))]

        def fetch_token_data(market_id: Any, token_id: Any) -> dict[str, Any]:
            payload: dict[str, Any] = {"market_id": str(market_id), "token_id": str(token_id)}
            if not args.skip_prices:
                try:
                    key = (str(market_id), str(token_id))
                    fetched = data_client.get_history_window(str(token_id), window.start_utc, history_end, price_fidelity_minutes,
                        known_history=() if force_prices else prior_prices.get(key, ()),
                        checked=() if force_prices else prior_checks.get(key, ()))
                    payload["history"] = fetched['history']
                    payload['history_errors'] = fetched['errors']
                    payload['history_requests'] = fetched.get('requests', [])
                    payload['verified_windows'] = fetched.get('verified_windows', [])
                    payload['history_attempts'] = fetched.get('attempts', [])
                    payload['cache_used'] = not force_prices and bool(prior_checks.get(key))
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    payload["price_error"] = str(exc)
            if args.orderbooks:
                payload["orderbook"] = batch_books.get(str(token_id))
                if payload["orderbook"] is None:
                    try:
                        payload["orderbook"] = clob.get_orderbook(str(token_id))
                    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, json.JSONDecodeError) as exc:
                        payload["orderbook_error"] = str(exc)
            return payload

        workers = max(1, min(args.price_workers, len(outcomes) or 1))
        price_started = time.monotonic()
        completed_tokens = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetch_token_data, market_id, token_id) for market_id, token_id in outcomes]
            for future in concurrent.futures.as_completed(futures):
                payload = future.result()
                if "price_error" in payload:
                    result["price_errors"] += 1
                    result['source_errors'].append({'source':'clob_history','token_id':payload['token_id'],'message':payload['price_error']})
                elif "history" in payload:
                    try:
                        result["new_price_rows"] += store.save_prices(
                            slug, payload["market_id"], payload["token_id"], payload["history"])
                    except sqlite3.Error as exc:
                        failure = dict(stage='storage', error=type(exc).__name__, message=str(exc))
                        payload.setdefault('history_errors', []).append(failure)
                        payload['verified_windows'] = []
                        for attempt in payload.get('history_attempts', []):
                            attempt.update(status='storage_failed', storage_complete=False,
                                           finished_at_utc=utc_now().isoformat())
                            attempt['errors'].append(failure)
                if not args.skip_prices:
                    attempts = payload.get('history_attempts', [])
                    if 'price_error' in payload:
                        attempts = [dict(start=window.start_utc.isoformat(), end=history_end.isoformat(),
                            target_seconds=price_fidelity_minutes*60, finished_at_utc=utc_now().isoformat(),
                            status='failed', pagination_complete=False,
                            errors=[dict(stage='fetch', message=payload['price_error'])])]
                    for attempt in attempts:
                        attempt.setdefault('storage_complete', attempt['status'] == 'ok')
                    store.save_history_attempts(slug, payload['market_id'], payload['token_id'],
                                                data_client.base_url, attempts)
                    result['price_requests'].extend(dict(token_id=payload['token_id'], **r) for r in payload.get('history_requests', []))
                    # Only save receipts after all returned prices have been committed.
                    with store.conn:
                        store.conn.executemany(
                            'insert or replace into history_price_checks values (?,?,?,?,?,?,?,?)',
                            [(slug,payload['market_id'],payload['token_id'],data_client.base_url,r['start'],r['end'],price_fidelity_minutes,captured_at.isoformat())
                             for r in payload.get('verified_windows', [])])
                    errors = payload.get('history_errors', [])
                    result['price_errors'] += len(errors)
                    result['source_errors'].extend({'source':'clob_history','token_id':payload['token_id'],**error} for error in errors)
                    saved = store.conn.execute('select price_time_utc from market_prices where event_slug=? and market_id=? and token_id=? and price_time_utc>=? and price_time_utc<? and resolution_seconds>0 and resolution_seconds<=?',
                        (slug,payload['market_id'],payload['token_id'],window.start_utc.isoformat(),history_end.isoformat(),price_fidelity_minutes*60)).fetchall()
                    seconds = price_fidelity_minutes * 60
                    expected = max(0, math.ceil((history_end-window.start_utc).total_seconds()/seconds))
                    buckets = {int((parse_timestamp(row[0])-window.start_utc).total_seconds()//seconds) for row in saved if parse_timestamp(row[0]) is not None}
                    coverage = dict(token_id=payload['token_id'],market_id=payload['market_id'],expected_buckets=expected,
                                    covered_buckets=len(buckets),status='complete' if expected>0 and len(buckets)==expected else 'sparse' if buckets else 'empty',
                                    request_status='failed' if errors or 'price_error' in payload else 'ok',
                                    request_count=len(payload.get('history_requests', [])), cache_used=payload.get('cache_used',False))
                    result['price_coverage'].append(coverage)
                    if coverage['status'] != 'complete':
                        result['quality_warnings'].append({'source':'clob_history_coverage','token_id':payload['token_id'],
                            'message':f"价格记录覆盖 {len(buckets)}/{expected} 个{price_fidelity_minutes}分钟区间；{'请求失败，尚待补查' if coverage['request_status']=='failed' else '已核验或复用近期核验，仍有稀疏/空记录'}，未填造价格"})
                if "orderbook_error" in payload:
                    result["orderbook_errors"] += 1
                    result['source_errors'].append({'source':'clob_book','token_id':payload['token_id'],'message':payload['orderbook_error']})
                elif "orderbook" in payload:
                    result["new_orderbook_rows"] += store.save_orderbook(
                        slug, payload["market_id"], payload["token_id"], payload["orderbook"]
                    )
                completed_tokens += 1
                progress = getattr(args, 'history_progress', None)
                if callable(progress):
                    progress(dict(completed_tokens=completed_tokens, total_tokens=len(outcomes),
                                  request_count=len(result['price_requests']), new_price_rows=result['new_price_rows'],
                                  elapsed_seconds=round(time.monotonic()-price_started,1)))
        if getattr(args, "public_trades", False):
            for market in event.get("markets", []):
                if not isinstance(market, dict):
                    continue
                market_id = str(market.get("conditionId") or market.get("condition_id") or "")
                try:
                    trades = data_client.get_trades(market_id, limit=int(getattr(args, "trade_limit", 500)))
                    trades = [row for row in trades if (trade_time := parse_timestamp(row.get('timestamp')))
                              is not None and window.start_utc <= trade_time < history_end]
                    result["new_trade_rows"] += store.save_market_trades(slug, market_id, trades, captured_at)
                except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError) as exc:
                    result["trade_errors"] += 1
                    result["source_errors"].append({"source": "data_trades", "market_id": market_id, "error": type(exc).__name__, "message": str(exc)})
        activity = {
            "volume": event.get("volume"),
            "volume_24h": event.get("volume24hr") or event.get("volume24h"),
            "open_interest": event.get("openInterest") or event.get("open_interest"),
            "liquidity": event.get("liquidity"),
            "trade_count": result.get("new_trade_rows", 0),
        }
        result["market_activity_archive_id"] = store.save_market_activity(
            slug,
            activity,
            captured_at_utc=captured_at,
        )
        result["source_age_seconds"].update({"clob": 0, "data_api": 0})
        result["outcomes"] = [dict(row) for row in store.conn.execute(
            "select market_id, label, lower_count, upper_count, yes_token_id, no_token_id, outcome_index from market_outcomes where event_slug = ? order by outcome_index",
            (slug,),
        ).fetchall()]
        result['price_quality'] = ('empty' if result['price_coverage'] and all(c['status']=='empty' for c in result['price_coverage'])
                                   else 'sparse' if result['quality_warnings'] else 'complete')
        result["status"] = "partial" if result["source_errors"] else result['price_quality']
        status = result["status"]
    except Exception as exc:
        # Keep fatal market/source failures searchable in the append-only
        # archive before the caller reports the crawl as failed.
        result["error"] = type(exc).__name__
        result["error_message"] = str(exc)[:300]
        result["failure_reason"] = (
            "market_not_found"
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 404
            else "crawl_error"
        )
        result["source_errors"].append({
            "source": "gamma" if isinstance(exc, urllib.error.HTTPError) else "crawl",
            "error": type(exc).__name__,
            "message": str(exc)[:300],
        })
        raise
    finally:
        try:
            store.save_prediction_archive(
                event_slug=slug,
                captured_at_utc=captured_at,
                data_time_utc=parse_timestamp(result.get("data_time_utc")),
                refresh_status=status,
                payload=result,
            )
        except sqlite3.Error as exc:
            result["source_errors"].append({"source": "prediction_archive", "error": type(exc).__name__, "message": str(exc)})
        store.close()
    return result


def crawl_command(args: argparse.Namespace) -> None:
    slugs = list(args.slug or [])
    if args.start and args.end:
        discovered: list[str] = []
        if getattr(args, "discover", True):
            try:
                lower = parse_bound(args.start, args.timezone)
                upper = parse_bound(args.end, args.timezone)
                if lower and upper:
                    discovered = [
                        str(row.get("slug") or row.get("ticker"))
                        for row in GammaClient(args.gamma_url, args.timeout, args.proxy).discover_count_events(
                            lower, upper, args.timezone
                        )
                        if row.get("slug") or row.get("ticker")
                    ]
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError) as exc:
                print(json.dumps({"discovery_error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
        if discovered:
            slugs.extend(discovered)
        elif getattr(args, "discover", True):
            raise SystemExit("Gamma 市场发现失败或日期范围内没有真实市场；未生成日期拼接 slug")
        else:
            slugs.extend(generate_slugs(args.start, args.end, args.timezone, args.anchor))
    if not slugs:
        raise SystemExit("Provide --slug one or more times, or both --start and --end.")
    for slug in dict.fromkeys(slugs):
        try:
            print(json.dumps(crawl_one(args, slug), ensure_ascii=False, sort_keys=True), flush=True)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
            print(json.dumps({"slug": slug, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)


def ingest_events_command(args: argparse.Namespace) -> None:
    store = MarketStore(Path(args.db))
    try:
        if args.sources:
            print(json.dumps({
                "sources_written": store.save_source_registry(Path(args.sources), args.timezone)
            }, ensure_ascii=False))
        result = store.save_source_events(Path(args.events), args.timezone)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        store.close()


def train_command(args: argparse.Namespace) -> None:
    store = MarketStore(Path(args.db))
    try:
        if args.sources:
            print(json.dumps({
                "sources_written": store.save_source_registry(Path(args.sources), args.timezone)
            }, ensure_ascii=False))
        if args.source_events:
            print(json.dumps({
                "source_events": store.save_source_events(Path(args.source_events), args.timezone)
            }, ensure_ascii=False))
        if args.events:
            print(json.dumps({"external_events_inserted": store.save_external_events(Path(args.events), args.timezone)}, ensure_ascii=False))
        windows = store.windows(
            parse_bound(args.start, args.timezone) if args.start else None,
            parse_bound(args.end, args.timezone) if args.end else None,
        )
        examples = build_examples(
            store, windows, args.user, args.platform, args.step_hours,
            args.min_elapsed_hours, args.min_final_count, args.max_price_age_hours,
            args.require_verified_final_count, args.use_resolved_bin_labels,
        )
        if not examples:
            raise SystemExit(
                "No training examples. Crawl multiple ended markets with verified XTracker counts, "
                "or use --allow-unverified-history only for exploratory work."
            )
        train_examples, test_examples, purged_slugs = split_examples_purged(examples, args.train_ratio)
        ridge_model = fit_ridge(train_examples, args.ridge)
        ensemble = fit_ensemble(train_examples, ridge_model)
        model = RidgeModel(
            ridge_model.feature_names,
            ridge_model.means,
            ridge_model.scales,
            ridge_model.coefficients,
            ridge_model.intercept,
            ridge_model.ridge,
            (ensemble.ridge_weight, ensemble.pace_weight, ensemble.prior_weight),
        )
        train_mae, train_rmse = score_examples(model, train_examples)
        test_mae, test_rmse = score_examples(model, test_examples)
        payload = model_payload(model, {
            "created_at_utc": utc_now().isoformat(),
            "db": str(Path(args.db).resolve()),
            "windows_total": len(windows),
            "examples_total": len(examples),
            "train_examples": len(train_examples),
            "test_examples": len(test_examples),
            "purged_overlapping_windows": purged_slugs,
            "require_verified_final_count": args.require_verified_final_count,
            "use_resolved_bin_labels": args.use_resolved_bin_labels,
            "train_mae": train_mae,
            "train_rmse": train_rmse,
            "test_mae": test_mae if not math.isnan(test_mae) else None,
            "test_rmse": test_rmse if not math.isnan(test_rmse) else None,
        })
        model_path = Path(args.model_out)
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        write_report(Path(args.report), model, test_examples or train_examples)
        print(json.dumps({
            "model": str(model_path.resolve()),
            "report": str(Path(args.report).resolve()),
            "windows_total": len(windows),
            "examples_total": len(examples),
            "train_mae": round(train_mae, 4),
            "test_mae": round(test_mae, 4) if not math.isnan(test_mae) else None,
            "purged_overlapping_windows": len(purged_slugs),
        }, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        store.close()


def predict_command(args: argparse.Namespace) -> None:
    store = MarketStore(Path(args.db))
    try:
        row = store.conn.execute(
            "select game_start_utc, end_date_utc from market_events where slug = ?",
            (args.slug,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"Market {args.slug!r} is not in the database. Run crawl first.")
        window = Window(args.slug, parse_timestamp(row[0]), parse_timestamp(row[1]))
        as_of = parse_timestamp(args.as_of) if args.as_of else utc_now()
        times = store.post_times(args.user, args.platform)
        model = load_model(Path(args.model))
        features = market_features(store, window, as_of, times, args.prior_rate, args.max_price_age_hours)
        forecast = max(0.0, model.predict(features))
        prices = store.latest_prices(args.slug, as_of, args.max_price_age_hours)
        sample = store.latest_sample(args.user, args.platform, window, as_of)
        sample_freshness = None
        if sample is not None:
            if args.as_of is None and sample["freshness_seconds"] is not None:
                sample_freshness = int(sample["freshness_seconds"])
            else:
                sampled_at = parse_timestamp(sample["sampled_at_utc"])
                if sampled_at is not None:
                    sample_freshness = max(0, int((as_of - sampled_at).total_seconds()))
        feed_stale = (
            sample_freshness is None
            or sample_freshness > args.max_sample_age_seconds
            or args.as_of is not None
        )
        outside_window = as_of < window.start_utc or as_of >= window.end_utc
        historical_evaluation = args.as_of is not None and as_of < utc_now() - dt.timedelta(seconds=1)
        incomplete_distribution = not price_distribution_is_valid(
            features[8], args.min_probability_mass, args.max_probability_mass
        )
        print(json.dumps({
            "slug": args.slug,
            "as_of_utc": as_of.isoformat(),
            "current_count": int(features[0]),
            "model_forecast_final_count": round(forecast, 2),
            "market_expected_count": round(features[7], 2),
            "price_probability_mass": round(features[8], 4),
            "price_outcome_coverage": len(prices),
            "stale_or_incomplete_price_data": incomplete_distribution,
            "post_feed_stale": feed_stale,
            "outside_market_window": outside_window,
            "historical_evaluation": historical_evaluation,
            "do_not_auto_trade": incomplete_distribution or feed_stale or outside_window or historical_evaluation,
        }, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        store.close()


def archive_command(args: argparse.Namespace) -> None:
    store = MarketStore(Path(args.db))
    try:
        inserted = store.backfill_prediction_archive(args.limit)
        print(json.dumps({
            "archive_schema": "prediction_archive_v1",
            "inserted": inserted,
            "do_not_auto_trade": True,
        }, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        store.close()


def discover_live_command(args: argparse.Namespace) -> None:
    """Print currently overlapping real Elon Musk count-market slugs.

    This is intentionally read-only and is used by the local launcher so it
    never has to manufacture a slug from a calendar date.
    """
    now = utc_now()
    from live_market_time import live_event_window
    candidates = GammaClient(args.gamma_url, args.timeout, args.proxy).discover_count_events(
        now - dt.timedelta(days=args.lookback_days),
        now + dt.timedelta(days=args.lookahead_days),
        args.timezone,
        max_pages=args.max_pages,
    )
    live: list[tuple[dt.datetime, str]] = []
    for event in candidates:
        start, end = live_event_window(event, args.timezone)
        slug = str(event.get("slug") or event.get("ticker") or "").strip()
        if not slug or start is None or end is None or start > now or end <= now:
            continue
        live.append((start, slug))
    live.sort(key=lambda item: (item[0], item[1]), reverse=True)
    payload = {
        "as_of_utc": now.isoformat(),
        "slugs": list(dict.fromkeys(slug for _, slug in live)),
        "count": len(live),
        "do_not_auto_trade": True,
    }
    print(json.dumps(payload, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crawl Polymarket tweet-count markets and train a ridge regression.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    crawl = subparsers.add_parser("crawl", help="Fetch Gamma metadata, CLOB prices and XTracker posts.")
    crawl.add_argument("--slug", action="append", help="Polymarket event slug; repeat for multiple markets.")
    crawl.add_argument("--start", help="Generate Tuesday/Friday slugs from this local date.")
    crawl.add_argument("--end", help="Exclusive upper bound for generated slugs.")
    crawl.add_argument("--anchor", default=DEFAULT_ANCHOR)
    crawl.add_argument("--db", default=DEFAULT_DB)
    crawl.add_argument("--user", default=DEFAULT_USER)
    crawl.add_argument("--platform", default=DEFAULT_PLATFORM)
    crawl.add_argument("--timezone", default=DEFAULT_TZ)
    crawl.add_argument("--gamma-url", default=DEFAULT_GAMMA_URL)
    crawl.add_argument("--clob-url", default=DEFAULT_CLOB_URL)
    crawl.add_argument("--data-url", default="https://data-api.polymarket.com", help="Public Data API base URL; no credentials are used.")
    crawl.add_argument("--tracker-url", default="https://xtracker.polymarket.com")
    crawl.add_argument("--proxy", default=DEFAULT_PROXY, help="HTTP proxy, e.g. http://127.0.0.1:7890.")
    crawl.add_argument("--timeout", type=int, default=30)
    crawl.add_argument("--price-interval", default="1w", choices=("1d", "1w", "1m", "max"))
    crawl.add_argument(
        "--price-fidelity", type=int, default=5,
        help="Price-history sampling fidelity in minutes (default: 5).",
    )
    crawl.add_argument(
        "--skip-prices",
        action="store_true",
        help="Only collect event metadata and XTracker posts; useful for fast historical backfill.",
    )
    crawl.add_argument(
        "--orderbooks",
        action="store_true",
        help="Capture current CLOB order books and best bid/ask/depth snapshots.",
    )
    crawl.add_argument("--no-batch-clob", action="store_false", dest="batch_clob", help="Disable public CLOB batch requests.")
    crawl.add_argument("--no-discover", action="store_false", dest="discover", help="Use generated slugs instead of Gamma discovery for date ranges.")
    crawl.add_argument("--public-trades", action="store_true", help="Save public market trades from the Data API.")
    crawl.add_argument("--trade-limit", type=int, default=500)
    crawl.add_argument("--request-delay", type=float, default=0.1)
    crawl.add_argument("--price-workers", type=int, default=4)
    crawl.add_argument("--force-prices", action="store_true", help="Ignore recent successful history checks and recheck the full price window.")
    crawl.set_defaults(func=crawl_command, batch_clob=True, discover=True)

    archive = subparsers.add_parser(
        "archive",
        help="Build the append-only frontend-ready prediction archive from existing paper forecasts.",
    )
    archive.add_argument("--db", default=DEFAULT_DB)
    archive.add_argument("--limit", type=int, help="Optional maximum number of source forecasts to inspect.")
    archive.set_defaults(func=lambda args: archive_command(args))

    discover_live = subparsers.add_parser(
        "discover-live",
        help="Read-only discovery of currently overlapping real Elon count-market slugs.",
    )
    discover_live.add_argument("--timezone", default=DEFAULT_TZ)
    discover_live.add_argument("--gamma-url", default=DEFAULT_GAMMA_URL)
    discover_live.add_argument("--proxy", default=DEFAULT_PROXY)
    discover_live.add_argument("--timeout", type=int, default=20)
    discover_live.add_argument("--lookback-days", type=int, default=8)
    discover_live.add_argument("--lookahead-days", type=int, default=8)
    discover_live.add_argument("--max-pages", type=int, default=20)
    discover_live.set_defaults(func=discover_live_command)

    ingest_events = subparsers.add_parser(
        "ingest-events",
        help="Validate AI-structured events, evidence and source whitelist entries.",
    )
    ingest_events.add_argument("--db", default=DEFAULT_DB)
    ingest_events.add_argument("--events", required=True, help="JSONL with event fields and evidence[] citations.")
    ingest_events.add_argument(
        "--sources",
        help="Optional JSONL source registry additions/updates; built-in whitelist is always seeded.",
    )
    ingest_events.add_argument("--timezone", default=DEFAULT_TZ)
    ingest_events.set_defaults(func=ingest_events_command)

    train = subparsers.add_parser("train", help="Train with chronological market-level split.")
    train.add_argument("--db", default=DEFAULT_DB)
    train.add_argument("--events", help="Optional JSONL external events file.")
    train.add_argument("--source-events", help="Optional AI-structured event JSONL with evidence citations.")
    train.add_argument("--sources", help="Optional JSONL source registry additions/updates.")
    train.add_argument("--start")
    train.add_argument("--end")
    train.add_argument("--user", default=DEFAULT_USER)
    train.add_argument("--platform", default=DEFAULT_PLATFORM)
    train.add_argument("--timezone", default=DEFAULT_TZ)
    train.add_argument("--step-hours", type=float, default=12)
    train.add_argument("--min-elapsed-hours", type=float, default=12)
    train.add_argument("--min-final-count", type=int, default=1)
    train.add_argument("--max-price-age-hours", type=float, default=24)
    train.add_argument(
        "--allow-unverified-history",
        action="store_false",
        dest="require_verified_final_count",
        help="Use ended windows whose fetched XTracker count does not fit the resolved Polymarket bin.",
    )
    train.set_defaults(require_verified_final_count=True)
    train.add_argument(
        "--resolved-bin-labels",
        action="store_true",
        dest="use_resolved_bin_labels",
        help="Use the official resolved-bin midpoint for unverified historical final counts.",
    )
    train.add_argument("--ridge", type=float, default=10.0)
    train.add_argument("--train-ratio", type=float, default=0.7)
    train.add_argument("--model-out", default=DEFAULT_MODEL)
    train.add_argument("--report", default=DEFAULT_REPORT)
    train.set_defaults(func=train_command)

    predict = subparsers.add_parser("predict", help="Score one already-crawled market.")
    predict.add_argument("--slug", required=True)
    predict.add_argument("--db", default=DEFAULT_DB)
    predict.add_argument("--model", default=DEFAULT_MODEL)
    predict.add_argument("--as-of")
    predict.add_argument("--user", default=DEFAULT_USER)
    predict.add_argument("--platform", default=DEFAULT_PLATFORM)
    predict.add_argument("--prior-rate", type=float, default=0.0)
    predict.add_argument("--max-price-age-hours", type=float, default=24)
    predict.add_argument("--min-probability-mass", type=float, default=0.6)
    predict.add_argument("--max-probability-mass", type=float, default=1.2)
    predict.add_argument("--max-sample-age-seconds", type=int, default=600)
    predict.set_defaults(func=predict_command)

    hourly_test = subparsers.add_parser(
        "hourly-test",
        help="Walk forward through local ended markets and write a time-aligned backtest record.",
    )
    hourly_test.add_argument("--db", default=DEFAULT_DB)
    hourly_test.add_argument("--slug", action="append", help="Limit testing to one or more stored market slugs.")
    hourly_test.add_argument("--start", help="Inclusive local market-start bound.")
    hourly_test.add_argument("--end", help="Exclusive local market-end bound.")
    hourly_test.add_argument(
        "--model",
        help="Optional pre-trained model. Omit to train a separate walk-forward model per market.",
    )
    hourly_test.add_argument("--user", default=DEFAULT_USER)
    hourly_test.add_argument("--platform", default=DEFAULT_PLATFORM)
    hourly_test.add_argument("--timezone", default=DEFAULT_TZ)
    hourly_test.add_argument("--min-elapsed-hours", type=float, default=1)
    hourly_test.add_argument(
        "--min-elapsed-minutes", type=float,
        help="Minimum elapsed time before the first checkpoint; overrides --min-elapsed-hours.",
    )
    hourly_test.add_argument(
        "--step-minutes", type=float, default=60,
        help="Sliding checkpoint interval in minutes; 5 is suitable for high-resolution local replay.",
    )
    hourly_test.add_argument("--training-step-hours", type=float, default=12)
    hourly_test.add_argument("--training-min-elapsed-hours", type=float, default=12)
    hourly_test.add_argument("--min-final-count", type=int, default=1)
    hourly_test.add_argument("--max-price-age-hours", type=float, default=24)
    hourly_test.add_argument("--backtest-min-edge", type=float, default=0.08)
    hourly_test.add_argument("--backtest-max-spread", type=float, default=0.04)
    hourly_test.add_argument("--backtest-min-depth", type=float, default=3.0)
    hourly_test.add_argument("--ridge", type=float, default=10.0)
    hourly_test.add_argument(
        "--allow-unverified-history",
        action="store_false",
        dest="require_verified_final_count",
        help="Include historical windows whose locally fetched count misses the settled Polymarket bin.",
    )
    hourly_test.set_defaults(require_verified_final_count=True)
    hourly_test.add_argument(
        "--resolved-bin-labels",
        action="store_true",
        dest="use_resolved_bin_labels",
        help="Use official resolved-bin midpoints in walk-forward training for unverified history.",
    )
    hourly_test.add_argument("--report", default="data/hourly_market_test.csv")
    hourly_test.add_argument("--summary", default="data/hourly_market_test_summary.csv")
    hourly_test.add_argument(
        "--public-report",
        default="data/hourly_market_predictions_public.csv",
        help="Prediction-only CSV for the frontend; excludes settlement answers.",
    )
    hourly_test.set_defaults(func=hourly_test_command)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
