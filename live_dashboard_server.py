#!/usr/bin/env python3
"""Local read-only live aggregator for the Musk count dashboard.

The server keeps collection and prediction on the local machine so the HTML
page does not need direct access to XTracker, CLOB, RSS or market-data APIs.
It never places orders. Raw news is displayed as context only; model features
come from approved source_events already stored in SQLite.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import math
import shutil
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ai_provider import AIConfigurationError, AIProvider, AIProviderError
from news_event_crawler import NewsCorpusStore, USER_AGENT, crawl_sources, load_sources

from musk_tracker import (
    DEFAULT_PLATFORM,
    DEFAULT_TZ,
    DEFAULT_USER,
    XTrackerClient,
    normalize_post,
)
from polymarket_pipeline import (
    ClobClient,
    GammaClient,
    MarketStore,
    Window,
    crawl_one,
    count_distribution,
    generate_slugs,
    hourly_test_command,
    market_features,
    outcome_label_for_count,
    parse_timestamp,
    price_distribution_is_valid,
    prior_rate_for_windows,
    utc_now,
    load_model,
)


DEFAULT_NEWS_FEED = (
    "https://news.google.com/rss/search?"
    "q=Elon+Musk+Tesla+SpaceX+xAI&hl=en-US&gl=US&ceid=US:en"
)
LIVE_SERVICE_VERSION = "2026-08-12-ai-assist-v1"
PRIVATE_REPORT_COLUMNS = {
    "observed_local_final_count",
    "resolved_bin",
    "absolute_error_to_observed_count",
    "resolved_bin_probability",
    "brier_score",
    "log_loss",
}


class HistorySyncManager:
    """Run an explicit, sequential historical backfill without blocking HTTP."""

    def __init__(
        self,
        service_args: argparse.Namespace,
        database_lock: Any,
        report_paths: dict[str, Path],
        crawl_fn: Any = crawl_one,
        slug_generator: Any = generate_slugs,
    ) -> None:
        self.service_args = service_args
        self.database_lock = database_lock
        self.report_paths = report_paths
        self.crawl_fn = crawl_fn
        self.slug_generator = slug_generator
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "state": "idle",
            "message": "尚未启动历史市场抓取",
            "started_at_utc": None,
            "finished_at_utc": None,
            "start": None,
            "end": None,
            "include_prices": False,
            "price_interval": None,
            "total": 0,
            "completed": 0,
            "current_slug": None,
            "succeeded": 0,
            "failed": 0,
            "results": [],
            "rebuild_reports": False,
            "report_result": None,
        }

    def status(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self._state, ensure_ascii=False))

    def _crawl_args(self, include_prices: bool, price_interval: str) -> argparse.Namespace:
        return argparse.Namespace(
            db=self.service_args.db,
            user=self.service_args.user,
            platform=self.service_args.platform,
            timezone=self.service_args.timezone,
            gamma_url=self.service_args.gamma_url,
            clob_url=self.service_args.clob_url,
            tracker_url=self.service_args.tracker_url,
            proxy=self.service_args.proxy,
            timeout=self.service_args.timeout,
            skip_prices=not include_prices,
            price_interval=price_interval,
            price_fidelity=60,
            orderbooks=False,
            request_delay=0.1,
            price_workers=max(1, min(8, self.service_args.book_workers)),
        )

    def _report_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            db=self.service_args.db,
            slug=None,
            start=None,
            end=None,
            model=self.service_args.model,
            user=self.service_args.user,
            platform=self.service_args.platform,
            timezone=self.service_args.timezone,
            min_elapsed_hours=1,
            training_step_hours=12,
            training_min_elapsed_hours=12,
            min_final_count=1,
            max_price_age_hours=24,
            ridge=10.0,
            require_verified_final_count=True,
            use_resolved_bin_labels=True,
            report=str(self.report_paths["private"]),
            summary=str(self.report_paths["summary"]),
            public_report=str(self.report_paths["public"]),
        )

    def _backup_reports(self) -> list[str]:
        existing = [path for path in self.report_paths.values() if path.is_file()]
        if not existing:
            return []
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        backup_dir = Path(self.service_args.db).resolve().parent / "report_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backups = []
        for path in existing:
            backup = backup_dir / f"{path.name}.{stamp}.bak"
            shutil.copy2(path, backup)
            backups.append(str(backup))
        return backups

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        start = str(payload.get("start") or "").strip()
        end = str(payload.get("end") or "").strip()
        try:
            start_date = dt.date.fromisoformat(start)
            end_date = dt.date.fromisoformat(end)
        except ValueError as exc:
            raise ValueError("历史抓取日期必须为 YYYY-MM-DD") from exc
        if end_date <= start_date:
            raise ValueError("结束日期必须晚于开始日期；结束日期为排他边界")
        today = utc_now().astimezone(ZoneInfo(self.service_args.timezone)).date()
        if end_date > today:
            raise ValueError("历史抓取结束日期不能晚于当前本地日期")
        raw_include_prices = payload.get("include_prices", False)
        include_prices = raw_include_prices is True or str(raw_include_prices).lower() in {
            "1", "true", "yes", "on",
        }
        price_interval = str(payload.get("price_interval") or "1d")
        if price_interval not in {"1d", "1w", "1m", "max"}:
            raise ValueError("历史赔率间隔仅支持 1d、1w、1m 或 max")
        raw_rebuild_reports = payload.get("rebuild_reports", True)
        rebuild_reports = raw_rebuild_reports is True or str(raw_rebuild_reports).lower() in {
            "1", "true", "yes", "on",
        }
        slugs = list(dict.fromkeys(self.slug_generator(start, end, self.service_args.timezone)))
        if not slugs:
            raise ValueError("日期范围内没有可抓取的周二或周五市场")
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("已有历史抓取任务正在运行，请等待其完成")
            self._state = {
                "state": "running",
                "message": "历史抓取已开始；实时刷新将等待本任务释放数据库锁",
                "started_at_utc": utc_now().isoformat(),
                "finished_at_utc": None,
                "start": start,
                "end": end,
                "include_prices": include_prices,
                "price_interval": price_interval if include_prices else None,
                "total": len(slugs),
                "completed": 0,
                "current_slug": None,
                "succeeded": 0,
                "failed": 0,
                "results": [],
                "rebuild_reports": rebuild_reports,
                "report_result": None,
            }
            crawl_args = self._crawl_args(include_prices, price_interval)
            self.thread = threading.Thread(
                target=self._run,
                args=(crawl_args, slugs, rebuild_reports),
                name="historical-market-sync",
                daemon=True,
            )
            self.thread.start()
        return self.status()

    def _run(self, crawl_args: argparse.Namespace, slugs: list[str], rebuild_reports: bool) -> None:
        try:
            for slug in slugs:
                with self.lock:
                    self._state["current_slug"] = slug
                try:
                    # crawl_one creates a separate SQLite connection, so all live writes
                    # must wait until this market has finished its transaction sequence.
                    with self.database_lock:
                        result = self.crawl_fn(crawl_args, slug)
                    summary = {
                        "slug": slug,
                        "ok": True,
                        "new_posts_inserted": result.get("new_posts_inserted", 0),
                        "new_price_rows": result.get("new_price_rows", 0),
                        "source_errors": len(result.get("source_errors", [])),
                        "price_errors": result.get("price_errors", 0),
                    }
                    succeeded = True
                except Exception as exc:
                    summary = {
                        "slug": slug,
                        "ok": False,
                        "error": type(exc).__name__,
                        "message": str(exc)[:300],
                    }
                    succeeded = False
                with self.lock:
                    self._state["results"].append(summary)
                    self._state["completed"] += 1
                    self._state["succeeded"] += int(succeeded)
                    self._state["failed"] += int(not succeeded)
            if rebuild_reports:
                with self.lock:
                    self._state["message"] = "市场抓取完成，正在生成固定公开/私有/汇总复盘文件"
                with self.database_lock:
                    backups = self._backup_reports()
                    hourly_test_command(self._report_args())
                with self.lock:
                    self._state["report_result"] = {
                        "ok": True,
                        "backups": backups,
                        "public": str(self.report_paths["public"]),
                        "private": str(self.report_paths["private"]),
                        "summary": str(self.report_paths["summary"]),
                    }
            with self.lock:
                self._state["state"] = "succeeded"
                self._state["current_slug"] = None
                self._state["finished_at_utc"] = utc_now().isoformat()
                self._state["message"] = (
                    "历史抓取和固定复盘文件生成完成；未自动重训模型"
                    if rebuild_reports else "历史抓取完成；未自动重训模型或生成复盘 CSV"
                )
        except Exception as exc:
            with self.lock:
                self._state["state"] = "failed"
                self._state["current_slug"] = None
                self._state["finished_at_utc"] = utc_now().isoformat()
                self._state["message"] = f"历史抓取任务失败：{type(exc).__name__}"


class NewsSyncManager:
    """Fetch the fixed local whitelist before optional AI analysis."""

    def __init__(self, service_args: argparse.Namespace, database_lock: Any) -> None:
        self.service_args = service_args
        self.database_lock = database_lock
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "state": "idle",
            "message": "尚未抓取合规新闻语料",
            "started_at_utc": None,
            "finished_at_utc": None,
            "documents_seen": 0,
            "errors": 0,
            "eligible_documents": 0,
            "run_id": None,
            "error_summaries": [],
        }

    def status(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self._state, ensure_ascii=False))

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        contact = str(payload.get("contact") or "").strip()
        if len(contact) > 160 or any(char in contact for char in "\r\n"):
            raise ValueError("公开联系邮箱格式无效")
        user_agent = USER_AGENT
        if contact:
            user_agent = f"AIPRP-local-news-corpus/1.0 (research; contact {contact})"
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("已有新闻抓取任务正在运行，请等待其完成")
            self._state = {
                "state": "running",
                "message": "正在抓取固定白名单来源；联系邮箱不会保存到本地",
                "started_at_utc": utc_now().isoformat(),
                "finished_at_utc": None,
                "documents_seen": 0,
                "errors": 0,
                "eligible_documents": 0,
                "run_id": None,
                "error_summaries": [],
            }
            self.thread = threading.Thread(
                target=self._run,
                args=(user_agent,),
                name="approved-news-sync",
                daemon=True,
            )
            self.thread.start()
        return self.status()

    def _run(self, user_agent: str) -> None:
        try:
            with self.database_lock:
                store = NewsCorpusStore(Path(self.service_args.db))
                try:
                    result = crawl_sources(
                        store,
                        load_sources(Path(self.service_args.news_sources)),
                        proxy=self.service_args.proxy,
                        timeout=self.service_args.timeout,
                        user_agent=user_agent,
                    )
                    eligible = store.conn.execute(
                        "select count(*) from source_documents where corpus_eligible=1 and status='accepted'"
                    ).fetchone()[0]
                    errors = store.conn.execute(
                        """select source_id, error_type, message from crawl_errors
                           where run_id=? order by occurred_at_utc desc limit 3""",
                        (result["run_id"],),
                    ).fetchall()
                finally:
                    store.close()
            with self.lock:
                self._state.update({
                    "state": "succeeded",
                    "message": "新闻抓取完成；只有合规来源文档可供 AI 手动分析",
                    "finished_at_utc": utc_now().isoformat(),
                    "documents_seen": result["documents_seen"],
                    "errors": result["errors"],
                    "eligible_documents": eligible,
                    "run_id": result["run_id"],
                    "error_summaries": [
                        f"{row[0]}: {row[1]}: {str(row[2])[:180]}" for row in errors
                    ],
                })
        except Exception as exc:
            with self.lock:
                self._state.update({
                    "state": "failed",
                    "message": f"新闻抓取失败：{type(exc).__name__}",
                    "finished_at_utc": utc_now().isoformat(),
                    "error_summaries": [str(exc)[:180]],
                })


def parse_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def iso_or_none(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def parse_levels(book: dict[str, Any]) -> tuple[float | None, float | None, float | None, float, float]:
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for side, target in (("bids", bids), ("asks", asks)):
        for row in book.get(side, []) or []:
            if not isinstance(row, dict):
                continue
            price = parse_float(row.get("price"))
            size = parse_float(row.get("size"))
            if price is not None and size is not None and 0 <= price <= 1 and size >= 0:
                target.append((price, size))
    best_bid = max((price for price, _ in bids), default=None)
    best_ask = min((price for price, _ in asks), default=None)
    midpoint = (
        (best_bid + best_ask) / 2
        if best_bid is not None and best_ask is not None
        else best_bid if best_bid is not None
        else best_ask
    )
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    bid_depth = sum(size for price, size in bids if best_bid is not None and price >= best_bid - 0.05)
    ask_depth = sum(size for price, size in asks if best_ask is not None and price <= best_ask + 0.05)
    return best_bid, best_ask, midpoint, bid_depth, ask_depth


def paper_buy_cost(
    best_ask: float | None,
    fee_rate: float,
    slippage_bps: float,
) -> tuple[float | None, float | None]:
    """Return the per-share price and fee used by the local paper simulator."""
    if best_ask is None or not 0 <= best_ask <= 1:
        return None, None
    slippage = max(0.0, slippage_bps) / 10000.0
    executable = min(1.0, best_ask + slippage)
    fee = best_ask * max(0.0, fee_rate)
    return executable, fee


def public_report_is_safe(payload: bytes) -> bool:
    """Do not accidentally publish answer-bearing CSV through the public route."""
    header = payload.splitlines()[0].decode("utf-8-sig", errors="replace") if payload else ""
    fields = {item.strip() for item in header.split(",")}
    return not bool(fields & PRIVATE_REPORT_COLUMNS)


class WebFetcher:
    def __init__(self, proxy: str | None, timeout: int) -> None:
        handler = urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy} if proxy else {}
        )
        self.opener = urllib.request.build_opener(handler)
        self.timeout = timeout

    def get_bytes(self, url: str, accept: str) -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": accept,
                "User-Agent": "musk-live-dashboard/1.0",
            },
        )
        with self.opener.open(request, timeout=self.timeout) as response:
            return response.read()


class LiveService:
    def __init__(
        self,
        args: argparse.Namespace,
        database_lock: Any = None,
    ) -> None:
        self.args = args
        self.store = MarketStore(Path(args.db))
        self.gamma = GammaClient(args.gamma_url, args.timeout, args.proxy)
        self.clob = ClobClient(args.clob_url, args.timeout, args.proxy)
        self.tracker = XTrackerClient(args.tracker_url, args.timeout, args.proxy)
        self.fetcher = WebFetcher(args.proxy, args.timeout)
        self.model = load_model(Path(args.model))
        self.model_payload = self._read_model_payload(Path(args.model))
        self.news_feeds = args.news_feed or [DEFAULT_NEWS_FEED]
        self.last_refresh: dt.datetime | None = None
        self.last_tracker_success: dt.datetime | None = None
        self.last_snapshot: dict[str, Any] | None = None
        self.last_news: list[dict[str, Any]] = []
        self.last_news_at: dt.datetime | None = None
        self.last_stock: dict[str, Any] = {}
        self.last_stock_at: dt.datetime | None = None
        # One lock is shared with an explicit historical sync task so separate
        # SQLite connections never write during the same refresh transaction.
        self.lock = database_lock or threading.RLock()
        self.ai_provider = AIProvider(Path(__file__).parent, args.proxy, args.timeout + 25)
        self.ai_lock = threading.Lock()
        self.ai_last_call: dt.datetime | None = None
        self.stop_event = threading.Event()
        self.background_thread: threading.Thread | None = None

    @staticmethod
    def _read_model_payload(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def close(self) -> None:
        self.stop_event.set()
        if self.background_thread and self.background_thread.is_alive():
            self.background_thread.join(timeout=5)
        self.store.close()

    def start_background(self) -> None:
        if self.background_thread and self.background_thread.is_alive():
            return
        self.background_thread = threading.Thread(
            target=self._background_loop,
            name="live-dashboard-refresh",
            daemon=True,
        )
        self.background_thread.start()

    def _background_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.refresh(force=True)
            except Exception as exc:
                self.last_snapshot = {
                    "generated_at_utc": utc_now().isoformat(),
                    "slug": self.args.slug,
                    "errors": [f"background: {type(exc).__name__}: {exc}"],
                    "recommendation": {
                        "label": "不交易",
                        "do_not_auto_trade": True,
                        "risk_reasons": ["live_refresh_failed"],
                    },
                }
            self.stop_event.wait(max(10, self.args.refresh_seconds))

    def _fetch_news(self, now: dt.datetime) -> list[dict[str, Any]]:
        if self.last_news_at and (now - self.last_news_at).total_seconds() < 60:
            return self.last_news
        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        for feed_url in self.news_feeds:
            try:
                root = ET.fromstring(self.fetcher.get_bytes(feed_url, "application/rss+xml, application/xml"))
            except (urllib.error.URLError, OSError, ET.ParseError) as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                continue
            for item in root.findall(".//item")[:12]:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                published = (item.findtext("pubDate") or item.findtext("published") or "").strip()
                source = item.find("source")
                source_name = (source.text or "").strip() if source is not None else ""
                if title and link:
                    rows.append({
                        "title": title,
                        "url": link,
                        "published_at": published,
                        "source": source_name or (urllib.parse.urlsplit(link).hostname or ""),
                        "model_input": False,
                    })
        unique: dict[str, dict[str, Any]] = {row["url"]: row for row in rows}
        self.last_news = list(unique.values())[:20]
        self.last_news_at = now
        if errors:
            self.last_news.append({
                "title": "News feed unavailable",
                "url": "",
                "published_at": now.isoformat(),
                "source": "local-server",
                "error": errors[0],
                "model_input": False,
            })
        return self.last_news

    def _fetch_stock(self, now: dt.datetime) -> dict[str, Any]:
        if self.last_stock_at and (now - self.last_stock_at).total_seconds() < 30:
            return self.last_stock
        symbol = urllib.parse.quote(self.args.stock_symbol, safe="")
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            "?range=1d&interval=1m"
        )
        try:
            payload = json.loads(self.fetcher.get_bytes(url, "application/json").decode("utf-8"))
            result = payload.get("chart", {}).get("result", [{}])[0] or {}
            meta = result.get("meta", {})
            price = parse_float(meta.get("regularMarketPrice"))
            previous = parse_float(meta.get("previousClose"))
            change = price - previous if price is not None and previous is not None else None
            change_pct = change / previous if change is not None and previous else None
            self.last_stock = {
                "symbol": self.args.stock_symbol,
                "price": price,
                "previous_close": previous,
                "change": change,
                "change_pct": change_pct,
                "currency": meta.get("currency"),
                "as_of": now.isoformat(),
                "error": None,
            }
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            self.last_stock = {
                "symbol": self.args.stock_symbol,
                "price": None,
                "previous_close": None,
                "change": None,
                "change_pct": None,
                "currency": None,
                "as_of": now.isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        self.last_stock_at = now
        return self.last_stock

    def _approved_events(self, now: dt.datetime) -> list[dict[str, Any]]:
        rows = self.store.conn.execute(
            """
            select event_key, occurred_at_utc, kind, direction, relevance_score,
                   ai_confidence, bounded_impact, source_quality,
                   evidence_count, cross_source_count, summary
            from source_events
            where status = 'approved' and available_at_utc <= ?
            order by occurred_at_utc desc
            limit 20
            """,
            (now.isoformat(),),
        ).fetchall()
        return [
            {
                "event_key": row[0],
                "occurred_at_utc": row[1],
                "kind": row[2],
                "direction": row[3],
                "relevance": row[4],
                "ai_confidence": row[5],
                "bounded_impact": row[6],
                "source_quality": row[7],
                "evidence_count": row[8],
                "cross_source_count": row[9],
                "summary": row[10],
                "model_input": True,
            }
            for row in rows
        ]

    def ai_status(self) -> dict[str, Any]:
        return self.ai_provider.status()

    def _ai_documents(self, now: dt.datetime) -> list[dict[str, Any]]:
        try:
            rows = self.store.conn.execute(
                """
                select document_id, source_id, source_tier, source_reliability,
                       url, title, published_at_utc, available_at_utc, excerpt
                from source_documents
                where corpus_eligible = 1 and status = 'accepted'
                  and published_at_utc <= ? and available_at_utc <= ?
                order by published_at_utc desc
                limit 12
                """,
                (now.isoformat(), now.isoformat()),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]

    def ai_analyze(self) -> dict[str, Any]:
        now = utc_now()
        with self.ai_lock:
            if self.ai_last_call and (now - self.ai_last_call).total_seconds() < 30:
                return {
                    "ok": False,
                    "message": "AI 分析冷却中，请 30 秒后再试",
                    "do_not_auto_trade": True,
                }
            snapshot = self.refresh()
            documents = self._ai_documents(now)
            if not documents:
                return {
                    "ok": False,
                    "message": "当前没有通过来源审核且不晚于当前时点的本地语料；请先运行 crawl-news 并配置合规来源",
                    "documents_used": 0,
                    "probability_changed": False,
                    "recommendation_changed": False,
                    "do_not_auto_trade": True,
                }
            self.ai_last_call = now
            context = {
                "slug": snapshot.get("slug"),
                "current_count": (snapshot.get("count") or {}).get("current"),
                "local_model_prediction": (snapshot.get("prediction") or {}).get("final_count"),
                "forecast_bin": (snapshot.get("prediction") or {}).get("forecast_bin"),
                "market_expected_count": (snapshot.get("market") or {}).get("expected_count"),
                "approved_events": snapshot.get("approved_events", []),
            }
            result = self.ai_provider.analyze(documents, context)
            result["ok"] = True
            return result

    def _prior_rate(self, window: Window) -> float:
        prior_windows = [
            candidate
            for candidate in self.store.windows()
            if candidate.end_utc <= window.start_utc
        ]
        if not prior_windows:
            return 0.0
        return prior_rate_for_windows(
            self.store,
            prior_windows,
            self.args.user,
            self.args.platform,
            True,
            False,
        )

    def _fetch_books(self, slug: str) -> tuple[
        list[tuple[int | None, int | None, float]],
        list[dict[str, Any]],
        list[str],
    ]:
        outcomes = self.store.conn.execute(
            """
            select market_id, label, lower_count, upper_count, yes_token_id
            from market_outcomes
            where event_slug = ? and yes_token_id is not null
            order by lower_count
            """,
            (slug,),
        ).fetchall()
        books: list[dict[str, Any]] = []
        prices: list[tuple[int | None, int | None, float]] = []
        errors: list[str] = []

        def fetch(row: Any) -> tuple[Any, dict[str, Any] | None, str | None]:
            try:
                return row, self.clob.get_orderbook(str(row[4])), None
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError) as exc:
                return row, None, f"{row[1]}: {type(exc).__name__}: {exc}"

        workers = max(1, min(self.args.book_workers, len(outcomes) or 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetch, row) for row in outcomes]
            for future in concurrent.futures.as_completed(futures):
                row, book, error = future.result()
                if error:
                    errors.append(error)
                    continue
                assert book is not None
                self.store.save_orderbook(slug, str(row[0]), str(row[4]), book)
                best_bid, best_ask, midpoint, bid_depth, ask_depth = parse_levels(book)
                if midpoint is not None:
                    prices.append((row[2], row[3], midpoint))
                books.append({
                    "market_id": row[0],
                    "label": row[1],
                    "lower": row[2],
                    "upper": row[3],
                    "token_id": row[4],
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "midpoint": midpoint,
                    "spread": best_ask - best_bid
                    if best_bid is not None and best_ask is not None
                    else None,
                    "bid_depth_5": bid_depth,
                    "ask_depth_5": ask_depth,
                    "captured_at_utc": utc_now().isoformat(),
                })
        books.sort(key=lambda item: item["lower"] if item["lower"] is not None else 10**9)
        prices.sort(key=lambda item: item[0] if item[0] is not None else 10**9)
        return prices, books, errors

    def _recommendation(
        self,
        *,
        window: Window,
        now: dt.datetime,
        current_count: int,
        forecast: float,
        market_expected: float,
        probability_mass: float,
        price_valid: bool,
        books: list[dict[str, Any]],
        tracker_age: int | None,
        training_windows: int,
        paper_candidate: dict[str, Any] | None,
        ranges_complete: bool,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        if now < window.start_utc or now >= window.end_utc:
            reasons.append("outside_market_window")
        if tracker_age is None or tracker_age > self.args.max_tracker_age:
            reasons.append("xtracker_refresh_stale")
        if not books:
            reasons.append("no_live_orderbook")
        if not price_valid:
            reasons.append("incomplete_or_invalid_odds")
        if not ranges_complete:
            reasons.append("missing_outcome_orderbooks")
        if training_windows < self.args.min_training_windows:
            reasons.append("insufficient_training_windows")
        edge = paper_candidate.get("edge_after_cost") if paper_candidate else None
        if paper_candidate is None:
            reasons.append("no_executable_candidate")
        else:
            if paper_candidate.get("spread") is None:
                reasons.append("missing_best_bid_or_ask")
            elif paper_candidate["spread"] > self.args.max_spread:
                reasons.append("spread_above_threshold")
            if paper_candidate.get("book_age_seconds") is None or paper_candidate["book_age_seconds"] > self.args.max_book_age:
                reasons.append("orderbook_refresh_stale")
            if (paper_candidate.get("available_depth") or 0) < self.args.paper_size * self.args.min_depth_multiple:
                reasons.append("insufficient_ask_depth")
            if edge is None or edge < self.args.min_edge:
                reasons.append("edge_after_cost_below_threshold")
        label = "不交易" if reasons else "纸面方向研究"
        return {
            "label": label,
            "paper_direction": (
                f"买入 Yes · {paper_candidate['label']}"
                if paper_candidate and not reasons
                else "观望"
            ),
            "edge_proxy": edge,
            "edge_after_cost": edge,
            "candidate": paper_candidate,
            "risk_reasons": reasons,
            "do_not_auto_trade": True,
            "basis": [
                "模型区间概率与 Yes 卖一可成交价格比较",
                "手续费和滑点采用本地保守假设",
                "XTracker 和 CLOB 数据新鲜度",
                "来源事件仅使用 approved 记录",
                "未建模的原始新闻不会直接改变预测",
            ],
        }

    def _paper_candidate(
        self,
        probabilities: list[float],
        books: list[dict[str, Any]],
        now: dt.datetime,
    ) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for index, book in enumerate(books):
            if index >= len(probabilities):
                continue
            ask = parse_float(book.get("best_ask"))
            model_probability = parse_float(probabilities[index])
            if ask is None or model_probability is None:
                continue
            executable, fee_per_share = paper_buy_cost(
                ask, self.args.paper_fee_rate, self.args.paper_slippage_bps
            )
            if executable is None or fee_per_share is None:
                continue
            captured = parse_timestamp(book.get("captured_at_utc"))
            age = max(0, int((now - captured).total_seconds())) if captured else None
            spread = parse_float(book.get("spread"))
            candidates.append({
                "market_id": book.get("market_id"),
                "label": book.get("label"),
                "model_probability": round(model_probability, 6),
                "best_bid": parse_float(book.get("best_bid")),
                "best_ask": ask,
                "executable_price": round(executable, 6),
                "fee_per_share": round(fee_per_share, 6),
                "spread": spread,
                "available_depth": parse_float(book.get("ask_depth_5")),
                "book_age_seconds": age,
                "edge_after_cost": round(model_probability - executable - fee_per_share, 6),
            })
        return max(candidates, key=lambda item: item["edge_after_cost"], default=None)

    def refresh(self, force: bool = False) -> dict[str, Any]:
        with self.lock:
            now = utc_now()
            if (
                not force
                and self.last_snapshot is not None
                and self.last_refresh is not None
                and (now - self.last_refresh).total_seconds() < self.args.refresh_seconds
            ):
                return self.last_snapshot

            errors: list[str] = []
            event = self.gamma.get_event(self.args.slug)
            window = self.store.save_event(event, self.args.timezone)
            posts_returned = 0
            try:
                local_start = window.start_utc.astimezone(
                    ZoneInfo(self.args.timezone)
                ).date().isoformat()
                local_end = window.end_utc.astimezone(
                    ZoneInfo(self.args.timezone)
                ).date().isoformat()
                raw_posts = self.tracker.get_posts(
                    self.args.user,
                    self.args.platform,
                    local_start,
                    local_end,
                    self.args.timezone,
                )
                posts_returned = len(raw_posts)
                self.store.upsert_posts([
                    normalize_post(row, self.args.user, self.args.platform)
                    for row in raw_posts
                ])
                self.last_tracker_success = utc_now()
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"xtracker: {type(exc).__name__}: {exc}")

            current_count = self.store.count_posts(
                self.args.user,
                self.args.platform,
                window.start_utc,
                min(window.end_utc, now),
            )
            newest_capture = self.store.newest_capture(self.args.user, self.args.platform)
            capture_age = (
                max(0, int((now - newest_capture).total_seconds()))
                if newest_capture
                else None
            )
            tracker_age = (
                max(0, int((now - self.last_tracker_success).total_seconds()))
                if self.last_tracker_success
                else None
            )
            self.store.insert_sample(
                self.args.user,
                self.args.platform,
                self.args.slug,
                window.start_utc,
                window.end_utc,
                current_count,
                tracker_age,
                posts_returned,
            )

            live_prices, books, book_errors = self._fetch_books(self.args.slug)
            errors.extend(f"clob: {message}" for message in book_errors[:5])
            prior_rate = self._prior_rate(window)
            features = market_features(
                self.store,
                window,
                now,
                self.store.post_times(self.args.user, self.args.platform),
                prior_rate,
                self.args.max_price_age_hours,
                live_prices=live_prices if live_prices else None,
            )
            forecast = max(float(features[0]), float(self.model.predict(features)), 0.0)
            ranges = self.store.outcome_ranges(self.args.slug)
            variance_source = parse_float(self.model_payload.get("prediction_variance"))
            if variance_source is None:
                rmse = parse_float(self.model_payload.get("test_rmse")) or 50.0
                variance_source = max(1.0, rmse * rmse)
            probabilities = count_distribution(forecast, variance_source, ranges)
            forecast_bin = outcome_label_for_count(forecast, ranges)
            bin_probability = (
                probabilities[ranges.index(next(
                    item for item in ranges if item[0] == forecast_bin
                ))]
                if forecast_bin and forecast_bin in {item[0] for item in ranges}
                else None
            )
            price_valid = price_distribution_is_valid(features[8])
            books.sort(key=lambda item: item["lower"] if item["lower"] is not None else 10**9)
            paper_candidate = self._paper_candidate(probabilities, books, now)
            ranges_complete = len(books) == len(ranges)
            training_windows = len([
                candidate for candidate in self.store.windows()
                if candidate.end_utc <= window.start_utc
            ])
            recommendation = self._recommendation(
                window=window,
                now=now,
                current_count=current_count,
                forecast=forecast,
                market_expected=float(features[7]),
                probability_mass=float(features[8]),
                price_valid=price_valid,
                books=books,
                tracker_age=tracker_age,
                training_windows=training_windows,
                paper_candidate=paper_candidate,
                ranges_complete=ranges_complete,
            )
            events = self._approved_events(now)
            news = self._fetch_news(now)
            stock = self._fetch_stock(now)
            for row in events:
                row["model_input"] = True
            outcome_payload: list[dict[str, Any]] = []
            for index, book in enumerate(books):
                model_probability = (
                    round(probabilities[index], 6) if index < len(probabilities) else None
                )
                executable, fee = paper_buy_cost(
                    parse_float(book.get("best_ask")),
                    self.args.paper_fee_rate,
                    self.args.paper_slippage_bps,
                )
                edge_after_cost = (
                    model_probability - executable - fee
                    if model_probability is not None and executable is not None and fee is not None
                    else None
                )
                outcome_payload.append({
                    **book,
                    "model_probability": model_probability,
                    "paper_executable_price": round(executable, 6) if executable is not None else None,
                    "paper_fee_per_share": round(fee, 6) if fee is not None else None,
                    "paper_edge_after_cost": round(edge_after_cost, 6)
                    if edge_after_cost is not None else None,
                    "paper_candidate": bool(
                        paper_candidate and paper_candidate.get("label") == book.get("label")
                    ),
                })
            self.last_snapshot = {
                "service_version": LIVE_SERVICE_VERSION,
                "generated_at_utc": now.isoformat(),
                "slug": self.args.slug,
                "window": {
                    "start_utc": window.start_utc.isoformat(),
                    "end_utc": window.end_utc.isoformat(),
                    "outside": now < window.start_utc or now >= window.end_utc,
                },
                "count": {
                    "current": current_count,
                    "tracker_refresh_age_seconds": tracker_age,
                    "latest_post_capture_age_seconds": capture_age,
                    "posts_returned": posts_returned,
                    "stale": tracker_age is None or tracker_age > self.args.max_tracker_age,
                },
                "prediction": {
                    "final_count": round(forecast, 2),
                    "final_count_rounded": int(round(forecast)),
                    "forecast_bin": forecast_bin,
                    "forecast_bin_probability": bin_probability,
                    "variance": variance_source,
                    "training_windows": training_windows,
                    "model": self.model_payload.get("model"),
                },
                "market": {
                    "expected_count": round(float(features[7]), 2),
                    "probability_mass": round(float(features[8]), 4),
                    "distribution_valid": price_valid,
                    "outcomes": outcome_payload,
                    "quote_basis": "CLOB best ask is the paper Yes entry quote; midpoint is context only",
                    "paper_cost_assumptions": {
                        "fee_rate": self.args.paper_fee_rate,
                        "slippage_bps": self.args.paper_slippage_bps,
                        "paper_size": self.args.paper_size,
                        "min_depth_multiple": self.args.min_depth_multiple,
                    },
                },
                "stock": stock,
                "news": news,
                "approved_events": events,
                "recommendation": recommendation,
                "errors": errors,
            }
            paper_forecast_id = self.store.save_paper_forecast(
                captured_at_utc=now,
                window=window,
                current_count=current_count,
                forecast=forecast,
                forecast_bin=forecast_bin,
                forecast_bin_probability=bin_probability,
                market_expected_count=float(features[7]),
                probability_mass=float(features[8]),
                price_distribution_valid=price_valid,
                recommendation=str(recommendation["label"]),
                paper_direction=str(recommendation["paper_direction"]),
                edge_proxy=recommendation["edge_proxy"],
                risk_reasons=list(recommendation["risk_reasons"]),
                snapshot=self.last_snapshot,
            )
            trade_id = None
            if paper_candidate is not None:
                filled = min(
                    1.0,
                    max(0.0, float(paper_candidate.get("available_depth") or 0.0))
                    / max(self.args.paper_size, 1e-9),
                ) if not recommendation["risk_reasons"] else 0.0
                status = "paper_filled" if filled > 0 else "no_trade"
                fee_total = (
                    self.args.paper_size * filled * paper_candidate["fee_per_share"]
                )
                trade_id = self.store.save_paper_trade(
                    created_at_utc=now,
                    event_slug=self.args.slug,
                    outcome_label=str(paper_candidate["label"]),
                    side="yes",
                    model_probability=float(paper_candidate["model_probability"]),
                    quoted_best_bid=paper_candidate.get("best_bid"),
                    quoted_best_ask=paper_candidate.get("best_ask"),
                    executable_price=paper_candidate.get("executable_price"),
                    spread=paper_candidate.get("spread"),
                    available_depth=paper_candidate.get("available_depth"),
                    requested_size=self.args.paper_size,
                    fill_fraction=filled,
                    fee_rate=self.args.paper_fee_rate,
                    slippage_bps=self.args.paper_slippage_bps,
                    estimated_fee=fee_total,
                    status=status,
                    recommendation=str(recommendation["label"]),
                    edge_after_cost=recommendation.get("edge_after_cost"),
                    risk_reasons=list(recommendation["risk_reasons"]),
                    snapshot={"recommendation": recommendation, "market": self.last_snapshot["market"]},
                )
            self.store.settle_paper_trades(self.args.slug)
            self.last_snapshot["paper_trade_id"] = trade_id
            self.last_snapshot["paper_forecast_id"] = paper_forecast_id
            self.last_refresh = now
            return self.last_snapshot

    def paper_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.store.conn.execute(
                """
                select id, captured_at_utc, event_slug, current_count,
                       model_forecast_final_count, forecast_bin,
                       market_expected_count, recommendation, paper_direction,
                       edge_proxy, price_distribution_valid, risk_reasons
                from paper_forecasts
                where event_slug = ?
                order by captured_at_utc desc
                limit ?
                """,
                (self.args.slug, max(1, min(500, int(limit)))),
            ).fetchall()
            return [
                {
                    "id": row[0],
                    "captured_at_utc": row[1],
                    "slug": row[2],
                    "current_count": row[3],
                    "model_forecast_final_count": row[4],
                    "forecast_bin": row[5],
                    "market_expected_count": row[6],
                    "recommendation": row[7],
                    "paper_direction": row[8],
                    "edge_proxy": row[9],
                    "price_distribution_valid": bool(row[10]),
                    "risk_reasons": json.loads(row[11]) if row[11] else [],
                }
                for row in rows
            ]

    def paper_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.store.conn.execute(
                """
                select id, created_at_utc, event_slug, outcome_label, side,
                       model_probability, quoted_best_bid, quoted_best_ask,
                       executable_price, spread, available_depth, requested_size,
                       fill_fraction, fee_rate, slippage_bps, status,
                       recommendation, edge_after_cost, resolved_yes,
                       settlement_price, gross_pnl, net_pnl, risk_reasons
                from paper_trades
                where event_slug = ?
                order by created_at_utc desc
                limit ?
                """,
                (self.args.slug, max(1, min(500, int(limit)))),
            ).fetchall()
            return [
                {
                    "id": row[0], "created_at_utc": row[1], "slug": row[2],
                    "outcome_label": row[3], "side": row[4],
                    "model_probability": row[5], "best_bid": row[6],
                    "best_ask": row[7], "executable_price": row[8],
                    "spread": row[9], "available_depth": row[10],
                    "requested_size": row[11], "fill_fraction": row[12],
                    "fee_rate": row[13], "slippage_bps": row[14],
                    "status": row[15], "recommendation": row[16],
                    "edge_after_cost": row[17], "resolved_yes": row[18],
                    "settlement_price": row[19], "gross_pnl": row[20],
                    "net_pnl": row[21], "risk_reasons": json.loads(row[22]) if row[22] else [],
                }
                for row in rows
            ]

    def live_markets(self, force: bool = False) -> list[dict[str, Any]]:
        snapshot = dict(self.refresh(force=force))
        snapshot["paper_trades"] = self.paper_trades(12)
        return [snapshot]


class MultiLiveService:
    """Keep each selected market isolated while sharing process configuration."""

    def __init__(self, args: argparse.Namespace, slugs: list[str]) -> None:
        self.database_lock = threading.RLock()
        self.services: list[LiveService] = []
        for slug in slugs:
            child_args = argparse.Namespace(**vars(args))
            child_args.slug = slug
            self.services.append(LiveService(child_args, self.database_lock))
        self.refresh_seconds = max(10, args.refresh_seconds)
        self.stop_event = threading.Event()
        self.background_thread: threading.Thread | None = None

    def start_background(self) -> None:
        if self.background_thread and self.background_thread.is_alive():
            return
        self.background_thread = threading.Thread(
            target=self._background_loop,
            name="multi-live-dashboard-refresh",
            daemon=True,
        )
        self.background_thread.start()

    def _background_loop(self) -> None:
        while not self.stop_event.is_set():
            for service in self.services:
                if self.stop_event.is_set():
                    break
                try:
                    # SQLite writes stay sequential across selected markets.
                    service.refresh(force=True)
                except Exception as exc:
                    service.last_snapshot = {
                        "generated_at_utc": utc_now().isoformat(),
                        "slug": service.args.slug,
                        "errors": [f"background: {type(exc).__name__}: {exc}"],
                        "recommendation": {
                            "label": "不交易",
                            "paper_direction": "观望",
                            "do_not_auto_trade": True,
                            "risk_reasons": ["live_refresh_failed"],
                        },
                    }
            self.stop_event.wait(self.refresh_seconds)

    def close(self) -> None:
        self.stop_event.set()
        if self.background_thread and self.background_thread.is_alive():
            self.background_thread.join(timeout=5)
        for service in self.services:
            service.close()

    def refresh(self) -> dict[str, Any]:
        return self.services[0].refresh()

    def paper_history(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.services[0].paper_history(limit)

    def paper_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.services[0].paper_trades(limit)

    def ai_status(self) -> dict[str, Any]:
        return self.services[0].ai_status()

    def ai_analyze(self) -> dict[str, Any]:
        return self.services[0].ai_analyze()

    def live_markets(self, force: bool = False) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        for service in self.services:
            try:
                snapshot = dict(service.refresh(force=True) if force else service.refresh())
                snapshot["paper_trades"] = service.paper_trades(12)
            except Exception as exc:
                snapshot = {
                    "generated_at_utc": utc_now().isoformat(),
                    "slug": service.args.slug,
                    "errors": [f"market_refresh: {type(exc).__name__}: {exc}"],
                    "recommendation": {
                        "label": "不交易",
                        "paper_direction": "观望",
                        "do_not_auto_trade": True,
                        "risk_reasons": ["market_refresh_failed"],
                    },
                    "paper_trades": [],
                }
            snapshots.append(snapshot)
        return snapshots


class DashboardHandler(BaseHTTPRequestHandler):
    service: LiveService | MultiLiveService
    static_file: Path
    report_paths: dict[str, Path]
    history_sync: HistorySyncManager
    news_sync: NewsSyncManager

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _local_origin_allowed(self) -> bool:
        client_host = self.client_address[0]
        if client_host not in {"127.0.0.1", "::1", "localhost"}:
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urllib.parse.urlsplit(origin)
        return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port in {None, self.server.server_port}

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 65536:
            raise ValueError("请求体大小无效")
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return body

    def _json_response(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _serve_report(self, name: str) -> None:
        path = self.report_paths[name]
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            self._json_response(404, {"ok": False, "message": f"本地复盘文件不存在：{path.name}"})
            return
        except OSError:
            self._json_response(500, {"ok": False, "message": "本地复盘文件无法读取"})
            return
        if name == "public" and not public_report_is_safe(payload):
            self._json_response(409, {"ok": False, "message": "公开复盘路径包含结算答案字段，已拒绝加载"})
            return
        self._send(200, payload, "text/csv; charset=utf-8")

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path in {"/", "/forecast_dashboard.html"}:
            try:
                self._send(200, self.static_file.read_bytes(), "text/html; charset=utf-8")
            except OSError as exc:
                self._send(500, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
            return
        if path == "/api/health":
            body = json.dumps({
                "ok": True,
                "service": "live_dashboard_server",
                "version": LIVE_SERVICE_VERSION,
                "do_not_auto_trade": True,
                "ai": self.service.ai_status(),
            }, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/live":
            try:
                payload = self.service.refresh()
                self._send(
                    200,
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
            except Exception as exc:
                self._send(
                    503,
                    json.dumps({
                        "ok": False,
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/ai/status":
            self._json_response(200, {"ok": True, **self.service.ai_status(), "do_not_auto_trade": True})
            return
        if path == "/api/reports/public":
            self._serve_report("public")
            return
        if path == "/api/reports/status":
            self._json_response(200, {
                "ok": True,
                "public": {"name": self.report_paths["public"].name, "exists": self.report_paths["public"].is_file()},
                "private": {"name": self.report_paths["private"].name, "exists": self.report_paths["private"].is_file()},
                "summary": {"name": self.report_paths["summary"].name, "exists": self.report_paths["summary"].is_file()},
            })
            return
        if path == "/api/history-sync/status":
            self._json_response(200, {
                "ok": True,
                "history_sync": self.history_sync.status(),
                "do_not_auto_trade": True,
            })
            return
        if path == "/api/news-sync/status":
            self._json_response(200, {
                "ok": True,
                "news_sync": self.news_sync.status(),
                "do_not_auto_trade": True,
            })
            return
        if path == "/api/live-markets":
            try:
                body = json.dumps(
                    self.service.live_markets(),
                    ensure_ascii=False,
                ).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:
                self._send(
                    503,
                    json.dumps({
                        "ok": False,
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/refresh-now":
            try:
                body = json.dumps({
                    "ok": True,
                    "markets": self.service.live_markets(force=True),
                    "do_not_auto_trade": True,
                }, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:
                self._json_response(503, {
                    "ok": False,
                    "message": f"立即刷新失败：{type(exc).__name__}",
                    "do_not_auto_trade": True,
                })
            return
        if path == "/api/paper-history":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                limit = int(query.get("limit", ["100"])[0])
                body = json.dumps(
                    self.service.paper_history(limit),
                    ensure_ascii=False,
                ).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except (TypeError, ValueError) as exc:
                self._send(400, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
            return
        if path == "/api/paper-trades":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                limit = int(query.get("limit", ["100"])[0])
                body = json.dumps(
                    self.service.paper_trades(limit),
                    ensure_ascii=False,
                ).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except (TypeError, ValueError) as exc:
                self._send(400, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/news-sync":
            if not self._local_origin_allowed():
                self._json_response(403, {"ok": False, "message": "仅允许本地网页启动新闻抓取"})
                return
            try:
                self._json_response(202, {
                    "ok": True,
                    "news_sync": self.news_sync.start(self._json_body()),
                    "do_not_auto_trade": True,
                })
            except (ValueError, json.JSONDecodeError) as exc:
                self._json_response(400, {"ok": False, "message": str(exc), "do_not_auto_trade": True})
            return
        if path == "/api/history-sync":
            if not self._local_origin_allowed():
                self._json_response(403, {"ok": False, "message": "仅允许本地网页启动历史抓取"})
                return
            try:
                payload = self._json_body()
                self._json_response(202, {
                    "ok": True,
                    "history_sync": self.history_sync.start(payload),
                    "do_not_auto_trade": True,
                })
            except (ValueError, json.JSONDecodeError) as exc:
                self._json_response(400, {"ok": False, "message": str(exc), "do_not_auto_trade": True})
            return
        if path in {"/api/reports/private", "/api/reports/summary"}:
            if not self._local_origin_allowed():
                self._json_response(403, {"ok": False, "message": "只允许本机网站读取私有复盘文件"})
                return
            try:
                self._json_body()
                self._serve_report("private" if path.endswith("/private") else "summary")
            except (ValueError, json.JSONDecodeError) as exc:
                self._json_response(400, {"ok": False, "message": str(exc)})
            return
        if path == "/api/refresh-now" and not self._local_origin_allowed():
            self._json_response(403, {"ok": False, "message": "只允许本机网站访问本地刷新接口"})
            return
        if path == "/api/refresh-now":
            try:
                self._json_body()
                self._json_response(200, {
                    "ok": True,
                    "markets": self.service.live_markets(force=True),
                    "do_not_auto_trade": True,
                })
            except Exception as exc:
                self._json_response(503, {
                    "ok": False,
                    "message": f"立即刷新失败：{type(exc).__name__}",
                    "do_not_auto_trade": True,
                })
            return
        if path.startswith("/api/ai/") and not self._local_origin_allowed():
            self._json_response(403, {"ok": False, "message": "只允许本机网站访问 AI 设置接口"})
            return
        try:
            payload = self._json_body()
            if path == "/api/ai/config":
                result = self.service.ai_provider.configure(payload) if hasattr(self.service, "ai_provider") else self.service.services[0].ai_provider.configure(payload)
                self._json_response(200, {"ok": True, **result, "do_not_auto_trade": True})
                return
            if path == "/api/ai/clear":
                result = self.service.ai_provider.clear() if hasattr(self.service, "ai_provider") else self.service.services[0].ai_provider.clear()
                self._json_response(200, {"ok": True, **result, "do_not_auto_trade": True})
                return
            if path == "/api/ai/analyze":
                result = self.service.ai_analyze()
                self._json_response(200 if result.get("ok") else 409, result)
                return
        except (AIConfigurationError, AIProviderError, ValueError, json.JSONDecodeError) as exc:
            self._json_response(400, {"ok": False, "message": str(exc), "do_not_auto_trade": True})
            return
        except Exception:
            self._json_response(500, {"ok": False, "message": "AI 本地服务处理失败", "do_not_auto_trade": True})
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def log_message(self, format: str, *args: Any) -> None:
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the local live Musk forecast dashboard.")
    parser.add_argument("--slug", required=True)
    parser.add_argument(
        "--extra-slug",
        action="append",
        default=[],
        help="Add another market to the isolated live-market selector; repeatable.",
    )
    parser.add_argument("--db", default="data/extended.sqlite")
    parser.add_argument("--model", default="data/extended_model_v4.json")
    parser.add_argument("--public-report", default="data/hourly_extended_v4_fullprob_public.csv")
    parser.add_argument("--private-report", default="data/hourly_extended_v4_fullprob.csv")
    parser.add_argument("--summary-report", default="data/hourly_extended_v4_fullprob_summary.csv")
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--refresh-seconds", type=int, default=30)
    parser.add_argument("--max-tracker-age", type=int, default=120)
    parser.add_argument("--max-price-age-hours", type=float, default=1)
    parser.add_argument("--min-edge", type=float, default=0.08)
    parser.add_argument("--max-spread", type=float, default=0.04)
    parser.add_argument("--max-book-age", type=int, default=30)
    parser.add_argument("--paper-size", type=float, default=1.0)
    parser.add_argument("--min-depth-multiple", type=float, default=3.0)
    parser.add_argument("--paper-fee-rate", type=float, default=0.02)
    parser.add_argument("--paper-slippage-bps", type=float, default=50.0)
    parser.add_argument("--min-training-windows", type=int, default=10)
    parser.add_argument("--book-workers", type=int, default=8)
    parser.add_argument("--stock-symbol", default="TSLA")
    parser.add_argument("--news-feed", action="append")
    parser.add_argument("--news-sources", default="news_sources.example.json")
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--platform", default=DEFAULT_PLATFORM)
    parser.add_argument("--timezone", default=DEFAULT_TZ)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--clob-url", default="https://clob.polymarket.com")
    parser.add_argument("--tracker-url", default="https://xtracker.polymarket.com")
    parser.add_argument(
        "--static-file",
        default=str(Path(__file__).with_name("forecast_dashboard.html")),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    slugs = list(dict.fromkeys([args.slug, *args.extra_slug]))
    service: LiveService | MultiLiveService = (
        MultiLiveService(args, slugs) if len(slugs) > 1 else LiveService(args)
    )
    database_lock = service.database_lock if isinstance(service, MultiLiveService) else service.lock
    report_paths = {
        "public": Path(args.public_report).resolve(),
        "private": Path(args.private_report).resolve(),
        "summary": Path(args.summary_report).resolve(),
    }
    history_sync = HistorySyncManager(args, database_lock, report_paths)
    news_sync = NewsSyncManager(args, database_lock)
    service.start_background()
    handler = type(
        "ConfiguredDashboardHandler",
        (DashboardHandler,),
        {
            "service": service,
            "static_file": Path(args.static_file),
            "report_paths": report_paths,
            "history_sync": history_sync,
            "news_sync": news_sync,
        },
    )
    server = ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"Live dashboard: http://{args.bind}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
