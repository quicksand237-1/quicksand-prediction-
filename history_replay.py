"""Bounded, cancellable read-only historical replay; no collector lock or writes."""
import copy
import datetime as dt
import json
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path


COMPLETENESS_TAG_COMPLETE = "complete_for_profitability"
COMPLETENESS_TAG_INCOMPLETE = "incomplete_data"


def _parse_utc(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _table_names(conn):
    return {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}


def _market_completeness(conn, slug, start_raw, end_raw, tables, now=None, archive_payload=None):
    """Derive a conservative, read-only completeness label for one market.

    A market is eligible for profitability research only when it is ended and
    settled, every YES token has the requested five-minute history for the
    whole market window, and every one of the first twelve strategy checkpoints
    has a point-in-time-safe order book for every outcome.  No fallback price
    resolution or current order book is treated as historical evidence.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    start = _parse_utc(start_raw)
    end = _parse_utc(end_raw)
    reasons = []
    ended = bool(end and end <= now)
    if not ended:
        reasons.append("market_not_ended")
    outcomes = []
    if "market_outcomes" in tables:
        outcomes = conn.execute(
            "select market_id, yes_token_id, resolved_yes from market_outcomes "
            "where event_slug = ? order by outcome_index, market_id", (slug,)
        ).fetchall()
    outcome_count = len(outcomes)
    token_count = sum(bool(row[1]) for row in outcomes)
    if not outcomes:
        reasons.append("missing_outcomes")
    elif token_count != outcome_count:
        reasons.append("missing_yes_token")
    settled = bool(ended and any(row[2] == 1 for row in outcomes))
    if ended and not settled:
        reasons.append("unsettled")

    expected_buckets = 0
    price_ratios = []
    if start and end and end > start:
        expected_buckets = max(1, math.ceil((end - start).total_seconds() / 300))
    if isinstance(archive_payload, dict) and archive_payload.get("price_fidelity_minutes") == 5:
        coverage_rows = archive_payload.get("price_coverage") or []
        for row in coverage_rows:
            try:
                expected = float(row.get("expected_buckets"))
                covered = float(row.get("covered_buckets"))
            except (AttributeError, TypeError, ValueError):
                continue
            if expected > 0:
                price_ratios.append(min(1.0, covered / expected))
        if coverage_rows:
            expected_buckets = max(expected_buckets, max(
                int(row.get("expected_buckets") or 0) for row in coverage_rows
                if isinstance(row, dict)
            ))
    elif "market_prices" in tables and outcomes and expected_buckets:
        covered_rows = conn.execute(
            "select market_id, token_id, count(distinct price_time_utc) from market_prices "
            "where event_slug = ? and resolution_seconds = 300 "
            "and price_time_utc >= ? and price_time_utc < ? group by market_id, token_id",
            (slug, start.isoformat(), end.isoformat()),
        ).fetchall()
        covered_by_token = {(row[0], row[1]): row[2] for row in covered_rows}
        for market_id, token_id, _resolved in outcomes:
            price_ratios.append(min(1.0, float(covered_by_token.get((market_id, token_id), 0)) / expected_buckets))
    price_ratio = min(price_ratios) if price_ratios else 0.0
    if not price_ratios:
        reasons.append("missing_five_minute_prices")
    elif price_ratio < 1.0:
        reasons.append("sparse_five_minute_prices")

    # The strategy only opens during the first twelve five-minute checkpoints.
    # A book is safe only when both capture and source timestamps are no later
    # than the checkpoint.  This deliberately excludes current / untimestamped
    # books and cannot be repaired from the present market.
    entry_checkpoints = 12 if start and outcome_count else 0
    safe_entry_checkpoints = 0
    if "market_orderbooks" in tables and entry_checkpoints and not any(
        reason in reasons for reason in ("unsettled", "sparse_five_minute_prices",
                                         "missing_five_minute_prices", "missing_yes_token")
    ):
        book_rows = conn.execute(
            "select token_id, captured_at_utc, source_timestamp_utc, best_bid, best_ask "
            "from market_orderbooks where event_slug = ? and captured_at_utc >= ? "
            "and captured_at_utc <= ? and source_timestamp_utc is not null "
            "and best_bid is not null and best_ask is not null",
            (slug, (start - dt.timedelta(hours=24)).isoformat(),
             (start + dt.timedelta(minutes=5 * entry_checkpoints)).isoformat()),
        ).fetchall()
        safe_rows = []
        for token_id, captured_raw, source_raw, _bid, _ask in book_rows:
            captured = _parse_utc(captured_raw)
            source = _parse_utc(source_raw)
            if captured is not None and source is not None:
                safe_rows.append((str(token_id), captured, source))
        expected_tokens = {str(row[1]) for row in outcomes if row[1]}
        for index in range(1, entry_checkpoints + 1):
            checkpoint = start + dt.timedelta(minutes=5 * index)
            safe_tokens = {
                token for token, captured, source in safe_rows
                if token in expected_tokens and captured <= checkpoint and source <= checkpoint
                and captured >= checkpoint - dt.timedelta(hours=24)
            }
            if len(safe_tokens) == outcome_count:
                safe_entry_checkpoints += 1
    orderbook_ratio = (safe_entry_checkpoints / entry_checkpoints) if entry_checkpoints else 0.0
    if orderbook_ratio < 1.0:
        reasons.append("missing_point_in_time_orderbooks")

    eligible = not reasons
    return {
        "data_completeness_tag": COMPLETENESS_TAG_COMPLETE if eligible else COMPLETENESS_TAG_INCOMPLETE,
        "data_completeness_status": "complete" if eligible else "incomplete",
        "data_completeness_reasons": reasons,
        "eligible_for_profitability_analysis": eligible,
        "ended": ended,
        "settled": settled,
        "outcome_count": outcome_count,
        "yes_token_count": token_count,
        "expected_five_minute_buckets": expected_buckets,
        "price_coverage_ratio": round(price_ratio, 6),
        "orderbook_entry_coverage_ratio": round(orderbook_ratio, 6),
        "safe_entry_checkpoints": safe_entry_checkpoints,
        "expected_entry_checkpoints": entry_checkpoints,
    }


def _catalog_from_connection(conn, limit=500):
    tables = _table_names(conn)
    rows = conn.execute(
        "select slug, game_start_utc, end_date_utc from market_events "
        "order by end_date_utc desc, slug limit ?", (max(1, min(10000, int(limit))),)
    ).fetchall()
    catalog = []
    for row in rows:
        archive_payload = None
        if "prediction_archive" in tables:
            archive = conn.execute(
                "select payload_json from prediction_archive where event_slug = ? "
                "order by captured_at_utc desc, id desc limit 1", (row[0],)
            ).fetchone()
            if archive:
                try:
                    parsed = json.loads(archive[0])
                    archive_payload = parsed if isinstance(parsed, dict) else None
                except (TypeError, ValueError):
                    archive_payload = None
        catalog.append({
            "slug": row[0],
            "window_start_utc": row[1],
            "window_end_utc": row[2],
            **_market_completeness(conn, row[0], row[1], row[2], tables,
                                   archive_payload=archive_payload),
        })
    return catalog


def read_history_catalog(database, limit=500):
    """Small metadata query, isolated from the collector connection and lock."""
    conn = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        conn.execute("PRAGMA query_only=ON")
        return _catalog_from_connection(conn, limit)
    finally:
        conn.close()


class ReplayControl:
    def __init__(self, seconds=120):
        self.cancelled = threading.Event()
        self.deadline = time.monotonic() + seconds
        self.completed = 0
        self.total = 0

    def check(self):
        if self.cancelled.is_set():
            raise RuntimeError("历史读取已取消")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("历史计算超过 120 秒，请缩小日期范围或检查本地磁盘")


def local_review(args, **kwargs):
    """Historical replay does not require an active live market or collector."""
    from live_dashboard_server import LiveService
    from polymarket_pipeline import MarketStore, load_model
    reader = LiveService.__new__(LiveService)
    reader.args = args
    reader.lock = threading.RLock()
    reader.model = load_model(Path(args.model))
    reader.model_payload = reader._read_model_payload(Path(args.model))
    reader.store = MarketStore.__new__(MarketStore)
    reader.store.conn = sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    try:
        return reader.backtest_review(**kwargs)
    finally:
        reader.store.conn.close()


def isolated_review(service, control=None, **kwargs):
    control = control or ReplayControl()
    # Lightweight fake stores used by unit tests retain their existing contract.
    if not isinstance(getattr(service.store, "conn", None), sqlite3.Connection):
        return service._backtest_review(_control=control, **kwargs)
    database = Path(service.args.db).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(database, uri=True, timeout=2)
    try:
        def interrupt():
            return int(control.cancelled.is_set() or time.monotonic() >= control.deadline)
        conn.set_progress_handler(interrupt, 10000)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        reader = copy.copy(service)
        reader.store = copy.copy(service.store)
        reader.store.conn = conn
        reader.store._replay_book_tokens = {}
        reader.lock = threading.RLock()
        return reader._backtest_review(_control=control, **kwargs)
    except sqlite3.OperationalError:
        control.check()
        raise
    finally:
        conn.close()


class ReplayJobs:
    """One worker, bounded retained jobs; switching explicitly cancels the old job."""
    def __init__(self, review):
        self.review = review
        self.jobs = {}
        self.condition = threading.Condition()
        threading.Thread(target=self._work, daemon=True, name="history-review").start()

    def submit(self, params, client_id=None, generation=0):
        with self.condition:
            now = time.monotonic()
            for key, job in list(self.jobs.items()):
                if now - job["created"] > 180:
                    job["control"].cancelled.set()
                    del self.jobs[key]
            if client_id:
                previous = [j for j in self.jobs.values() if j.get("client_id") == client_id]
                if any(j.get("generation", 0) > generation for j in previous):
                    raise ValueError("已忽略较早的历史请求")
                for job in previous:
                    job["control"].cancelled.set()
                    job["state"] = "cancelled"
                    job.pop("result", None)
            if sum(j["state"] in {"queued", "running"} and not j["control"].cancelled.is_set()
                   for j in self.jobs.values()) >= 3:
                raise ValueError("历史任务繁忙，请取消旧任务后重试")
            # Keep at most two completed payloads (often several MB each).
            finished = [key for key, j in self.jobs.items() if j["state"] not in {"queued", "running"}]
            for key in finished[:-1]:
                del self.jobs[key]
            key = uuid.uuid4().hex
            self.jobs[key] = dict(state="queued", control=ReplayControl(), params=params, created=now,
                                  client_id=client_id, generation=generation)
            self.condition.notify()
            return self.status(key)

    def status(self, key, cancel=False):
        with self.condition:
            job = self.jobs.get(key)
            if job is None:
                return dict(ok=False, state="expired", message="历史任务已过期，请重新读取")
            if cancel:
                job["control"].cancelled.set()
                job["state"] = "cancelled"
                job.pop("result", None)
            result = dict(ok=True, job_id=key, state=job["state"],
                          completed=job["control"].completed, total=job["control"].total)
            if "result" in job:
                result["result"] = job["result"]
            if "message" in job:
                result["message"] = job["message"]
            return result

    def _work(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: any(j["state"] == "queued" for j in self.jobs.values()))
                job = next(j for j in self.jobs.values() if j["state"] == "queued")
                job["state"] = "running"
            try:
                job["control"].check()
                result = self.review(_control=job["control"], **job["params"])
                job["control"].check()
                with self.condition:
                    if not job["control"].cancelled.is_set():
                        job.update(state="complete", result=result)
            except Exception as exc:
                with self.condition:
                    job.update(state="cancelled" if job["control"].cancelled.is_set() else "failed", message=str(exc))
