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
import hashlib
import json
import math
import queue
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ai_provider import AIConfigurationError, AIProvider, AIProviderError
from history_replay import ReplayJobs, isolated_review, read_history_catalog, _market_completeness, _table_names
from history_acquisition import AcquisitionEvidence
from replay_probability import POLICY_VERSION, guarded_forecast
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
    count_before,
    event_window,
    generate_slugs,
    hourly_test_command,
    hourly_checkpoints,
    market_features,
    outcome_label_for_count,
    parse_bound,
    parse_json,
    parse_range,
    parse_timestamp,
    price_distribution_is_valid,
    prior_rate_for_windows,
    estimate_prediction_variance,
    utc_now,
    load_model,
    FEATURE_NAMES,
)
from forecast_contracts import (
    build_decision_output,
    build_display_snapshot,
    build_feature_snapshot,
    build_prediction_output,
)
from market_dynamics_research import build_snapshot_features
from data_file_organizer import consolidate_csv_candidates, delete_safe_duplicates, scan_data_files
from strategy_simulator import (
    DEFAULT_ENTRY_CHECKPOINTS,
    DEFAULT_ENTRY_EDGE,
    DEFAULT_EXIT_EDGE,
    DEFAULT_RETAIN_EDGE,
    DEFAULT_PROBABILITY_COVERAGE,
    DEFAULT_STOP_LOSS_RETURN,
    DEFAULT_TAKE_PROFIT_RETURN,
    paper_buy_cost as _isolated_paper_buy_cost,
    paper_position_rebalance as _isolated_paper_position_rebalance,
    settle_paper_positions,
)
from paper_grid_research import (
    DEFAULT_COVERAGE_GRID,
    DEFAULT_ENTRY_MINUTES,
    DEFAULT_MARKET_BUDGET,
    run_grid_research,
)


DEFAULT_NEWS_FEED = (
    "https://news.google.com/rss/search?"
    "q=Elon+Musk+Tesla+SpaceX+xAI&hl=en-US&gl=US&ceid=US:en"
)
LIVE_SERVICE_VERSION = "2026-09-18-strict-live-window-v2"
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
            "include_orderbooks": False,
            "price_interval": None,
            "price_fidelity_minutes": 5,
            "total": 0,
            "completed": 0,
            "current_slug": None,
            "succeeded": 0,
            "failed": 0,
            "results": [],
            "rebuild_reports": False,
            "format_locked": False,
            "report_result": None,
        }

    def status(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self._state, ensure_ascii=False))

    def _crawl_args(
        self,
        include_prices: bool,
        price_interval: str,
        price_fidelity_minutes: int = 5,
        include_orderbooks: bool = False,
    ) -> argparse.Namespace:
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
            price_fidelity=price_fidelity_minutes,
            # Order-book capture is opt-in. When enabled it records snapshots
            # available from the start of this run; it never reconstructs
            # historical books and must be presented as future capture data.
            orderbooks=bool(include_orderbooks),
            request_delay=0.2,
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
            # Keep fixed historical reports on the unified five-minute replay
            # cadence while supplying the complete point-in-time argument set.
            step_minutes=5,
            min_elapsed_minutes=5,
            backtest_min_edge=0.08,
            backtest_max_spread=0.04,
            backtest_min_depth=3.0,
            ridge=10.0,
            require_verified_final_count=True,
            use_resolved_bin_labels=True,
            report=str(self.report_paths["private"]),
            summary=str(self.report_paths["summary"]),
            public_report=str(self.report_paths["public"]),
        )

    def _slugs_for_range(self, start: str, end: str) -> list[str]:
        """Resolve real Gamma market identities before using legacy date slugs."""
        generated = list(dict.fromkeys(self.slug_generator(start, end, self.service_args.timezone)))
        # Injected generators are used by tests and callers that explicitly want
        # legacy/replay identities; only the default historical UI path discovers.
        if self.slug_generator is not generate_slugs:
            return generated
        try:
            lower = parse_bound(start, self.service_args.timezone)
            upper = parse_bound(end, self.service_args.timezone)
            discovered = GammaClient(
                self.service_args.gamma_url,
                self.service_args.timeout,
                self.service_args.proxy,
            ).discover_count_events(lower, upper, self.service_args.timezone)
            slugs = list(dict.fromkeys(
                str(row.get("slug") or row.get("ticker") or "").strip()
                for row in discovered
                if isinstance(row, dict) and (row.get("slug") or row.get("ticker"))
            ))
            # An empty successful response means there are no real markets in
            # the range; do not manufacture 404-prone identities.
            return slugs
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError) as exc:
            # A failed discovery must not turn into a batch of guaranteed
            # 404 date-generated slugs. Let the UI show the discovery error.
            raise ValueError(f"Gamma市场发现失败：{type(exc).__name__}: {str(exc)[:200]}；未启动抓取，请检查代理或接口") from exc

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
        # `end` is an exclusive market-start boundary. Allow one calendar day
        # beyond the service timezone's date so clients in Asia can submit their
        # current date while New York is still on the previous day.
        latest_end = today + dt.timedelta(days=1)
        if end_date > latest_end:
            raise ValueError(
                f"历史抓取结束日期不能晚于服务时区日期 {today.isoformat()} 的次日"
            )
        raw_include_prices = payload.get("include_prices", True)
        include_prices = raw_include_prices is True or str(raw_include_prices).lower() in {
            "1", "true", "yes", "on",
        }
        raw_include_orderbooks = payload.get("include_orderbooks", False)
        include_orderbooks = raw_include_orderbooks is True or str(raw_include_orderbooks).lower() in {
            "1", "true", "yes", "on",
        }
        price_interval = str(payload.get("price_interval") or "1d")
        if price_interval not in {"1d", "1w", "1m", "max"}:
            raise ValueError("历史赔率间隔仅支持 1d、1w、1m 或 max")
        raw_fidelity = payload.get("price_fidelity_minutes", 60)
        try:
            price_fidelity_minutes = int(raw_fidelity)
        except (TypeError, ValueError) as exc:
            raise ValueError("历史价格研究步长必须为 60 或 5 分钟") from exc
        if price_fidelity_minutes not in (5, 60):
            raise ValueError("历史价格研究步长必须为 60 或 5 分钟")
        raw_rebuild_reports = payload.get("rebuild_reports", True)
        rebuild_reports = raw_rebuild_reports is True or str(raw_rebuild_reports).lower() in {
            "1", "true", "yes", "on",
        }
        slugs = self._slugs_for_range(start, end)
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
                "include_orderbooks": include_orderbooks,
                "price_interval": price_interval if include_prices else None,
                "price_fidelity_minutes": price_fidelity_minutes if include_prices else None,
                "total": len(slugs),
                "completed": 0,
                "current_slug": None,
                "succeeded": 0,
                "failed": 0,
                "results": [],
                "rebuild_reports": rebuild_reports,
                "format_locked": True,
                "report_result": None,
            }
            crawl_args = self._crawl_args(
                include_prices,
                price_interval,
                price_fidelity_minutes,
                include_orderbooks,
            )
            crawl_args.force_prices = payload.get('force_prices') is True
            def update_progress(progress):
                with self.lock:
                    self._state['market_progress'] = progress
            crawl_args.history_progress = update_progress
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
                    self._state['market_progress'] = {}
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
                        "new_orderbook_rows": result.get("new_orderbook_rows", 0),
                        "orderbooks_requested": bool(crawl_args.orderbooks),
                        "price_fidelity_minutes": result.get("price_fidelity_minutes", crawl_args.price_fidelity),
                        "price_interval": crawl_args.price_interval if not crawl_args.skip_prices else None,
                        "source_errors": len(result.get("source_errors", [])),
                        "price_errors": result.get("price_errors", 0),
                        "status": result.get("status", "partial" if result.get("source_errors") else "complete"),
                        "price_coverage": result.get("price_coverage", []),
                        "error_details": result.get("source_errors", []),
                        "price_window_start_utc": result.get("price_window_start_utc"),
                        "price_window_end_utc": result.get("price_window_end_utc"),
                        "quality_warnings": result.get('quality_warnings', []),
                        "price_quality": result.get('price_quality', 'complete'),
                        "request_count": len(result.get('price_requests', [])),
                    }
                    succeeded = True
                except Exception as exc:
                    not_found = isinstance(exc, urllib.error.HTTPError) and exc.code == 404
                    summary = {
                        "slug": slug,
                        "ok": False,
                        "error": type(exc).__name__,
                        "message": str(exc)[:300],
                        "failure_reason": "market_not_found" if not_found else "crawl_error",
                    }
                    succeeded = False
                with self.lock:
                    self._state["results"].append(summary)
                    self._state["completed"] += 1
                    self._state["succeeded"] += int(succeeded)
                    self._state["failed"] += int(not succeeded)
            if not self._state["succeeded"]:
                raise ValueError("所有市场抓取失败；请查看逐盘错误，未覆盖固定报告")
            incomplete = self._state['failed'] or any(r.get('status') in ('partial','sparse','empty') for r in self._state['results'])
            request_failures = self._state['failed'] or any(r.get('status') == 'partial' for r in self._state['results'])
            if rebuild_reports and not incomplete:
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
                self._state["state"] = ("partial" if request_failures else "sparse") if incomplete else "succeeded"
                self._state["format_locked"] = False
                self._state["current_slug"] = None
                self._state["finished_at_utc"] = utc_now().isoformat()
                capture_note = "已追加从任务启动时起可获得的订单簿快照（不是历史盘口恢复）" if crawl_args.orderbooks else "未采集订单簿"
                self._state["message"] = (
                    f"历史抓取和固定复盘文件生成完成；{capture_note}；未自动重训模型"
                    if rebuild_reports else f"历史抓取完成；{capture_note}；未自动重训模型或生成复盘 CSV"
                )
                if incomplete:
                    self._state['message'] = ('部分来源请求失败，请查看请求错误；已保存成功数据，未覆盖固定报告' if request_failures
                                              else '请求已完成，但源价格仍有稀疏/空记录（不是网络失败）；详见覆盖明细，未覆盖固定报告')
        except Exception as exc:
            with self.lock:
                self._state["state"] = "failed"
                self._state["format_locked"] = False
                self._state["current_slug"] = None
                self._state["finished_at_utc"] = utc_now().isoformat()
                self._state["error_type"] = type(exc).__name__
                self._state["error_detail"] = str(exc)[:500]
                self._state["message"] = f"历史抓取任务失败：{type(exc).__name__}"


class DataOrganizer:
    """Owns explicit, local-only inventory/consolidation requests."""

    def __init__(self, project_root: Path, data_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.data_root = data_root.resolve()
        self.lock = threading.Lock()
        self._state: dict[str, Any] = {"state": "idle", "message": "尚未扫描本地数据文件", "result": None}

    def status(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self._state, ensure_ascii=False))

    def run(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action not in {"scan", "consolidate", "delete"}:
            raise ValueError("未知的数据整理操作")
        with self.lock:
            self._state = {"state": "running", "message": "正在扫描并校验本地数据文件", "result": None}
        inventory = scan_data_files(self.data_root)
        result: dict[str, Any] = inventory
        if action == "consolidate":
            output = self.data_root / "consolidated"
            result = {"inventory": inventory, "consolidation": consolidate_csv_candidates(inventory, self.data_root, output)}
        elif action == "delete":
            selected = payload.get("sha256")
            if selected is not None and not isinstance(selected, list):
                raise ValueError("sha256 必须是数组")
            result = {"inventory": inventory, "deletion": delete_safe_duplicates(inventory, self.data_root, selected)}
        with self.lock:
            message = "本地数据合并完成；源文件未删除" if action == "consolidate" else ("本地重复文件清理完成；仅删除经哈希复核的低风险副本" if action == "delete" else "本地数据扫描完成；尚未删除文件")
            self._state = {"state": "succeeded", "message": message, "result": result}
        return result


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


def paper_position_rebalance(
    outcomes: list[dict[str, Any]],
    positions: dict[str, dict[str, float]],
    *,
    checkpoint_index: int,
    paper_budget: float,
    entry_checkpoints: int = 12,
    entry_edge: float = 0.08,
    retain_edge: float = 0.04,
    exit_edge: float = 0.0,
    max_outcome_weight: float = 0.40,
    max_turnover_fraction: float = 0.25,
    fee_rate: float = 0.02,
    slippage_bps: float = 50.0,
) -> tuple[list[dict[str, Any]], float, float]:
    """Simulate multi-outcome YES rebalancing without placing orders.

    ``positions`` is mutated in-memory and keyed only by outcome label; callers
    must create a fresh mapping for each complete market slug.  Quotes are used
    only when the outcome row is point-in-time safe.  The return values are
    action rows, realized PnL delta and mark-to-market PnL.
    """
    budget = max(0.0, float(paper_budget))
    if budget <= 0 or not outcomes:
        return [], 0.0, 0.0
    eligible: list[tuple[dict[str, Any], float]] = []
    for row in outcomes:
        label = str(row.get("outcome_label") or "")
        edge = parse_float(row.get("paper_net_edge_after_cost"))
        ask = parse_float(row.get("best_ask"))
        if not label or edge is None or ask is None or ask <= 0 or not row.get("point_in_time_safe"):
            continue
        if edge >= entry_edge:
            eligible.append((row, edge))
    total_edge = sum(max(0.0, edge) for _, edge in eligible)
    target_qty: dict[str, float] = {}
    if total_edge > 0:
        for row, edge in eligible:
            weight = min(max(0.0, float(max_outcome_weight)), edge / total_edge)
            ask = float(row["best_ask"])
            target_qty[str(row["outcome_label"])] = budget * weight / ask

    turnover_cap = budget * max(0.0, float(max_turnover_fraction))
    actions: list[dict[str, Any]] = []
    realized_delta = 0.0
    for row in outcomes:
        label = str(row.get("outcome_label") or "")
        if not label:
            continue
        state = positions.setdefault(label, {"quantity": 0.0, "avg_cost": 0.0, "realized_pnl": 0.0})
        current = max(0.0, float(state.get("quantity", 0.0)))
        edge = parse_float(row.get("paper_net_edge_after_cost"))
        ask = parse_float(row.get("best_ask")); bid = parse_float(row.get("best_bid"))
        safe = bool(row.get("point_in_time_safe"))
        desired = current
        reason = "hold"
        if current <= 0 and label in target_qty and checkpoint_index < max(0, int(entry_checkpoints)):
            desired, reason = target_qty[label], "enter"
        elif current > 0:
            if not safe or edge is None:
                desired, reason = current, "hold_data_insufficient"
            elif edge <= exit_edge:
                desired, reason = 0.0, "exit_edge_below_zero"
            elif edge < retain_edge:
                desired, reason = current * max(0.0, edge / max(retain_edge, 1e-9)), "reduce_below_retain"
            elif edge >= entry_edge and label in target_qty:
                desired, reason = target_qty[label], "increase_high_edge"
            else:
                desired, reason = current, "hold_hysteresis"
        delta = desired - current
        executed_delta = 0.0
        if abs(delta) > 0 and safe:
            if delta > 0 and ask is not None:
                depth = max(0.0, parse_float(row.get("ask_depth_5")) or 0.0)
                max_qty = min(delta, depth, turnover_cap / max(ask, 1e-9))
                executable, fee = paper_buy_cost(ask, fee_rate, slippage_bps)
                if max_qty > 0 and executable is not None and fee is not None:
                    old_cost = current * float(state.get("avg_cost", 0.0))
                    new_cost = old_cost + max_qty * (executable + fee)
                    state["quantity"] = current + max_qty
                    state["avg_cost"] = new_cost / state["quantity"]
                    delta = max_qty
                    executed_delta = max_qty
            elif delta < 0 and bid is not None:
                depth = max(0.0, parse_float(row.get("bid_depth_5")) or 0.0)
                max_qty = min(-delta, depth, turnover_cap / max(bid, 1e-9))
                sell_price = max(0.0, bid - max(0.0, slippage_bps) / 10000.0)
                fee = bid * max(0.0, fee_rate)
                if max_qty > 0:
                    avg_cost = float(state.get("avg_cost", 0.0))
                    pnl = max_qty * (sell_price - fee - avg_cost)
                    state["quantity"] = max(0.0, current - max_qty)
                    state["realized_pnl"] = float(state.get("realized_pnl", 0.0)) + pnl
                    realized_delta += pnl
                    delta = -max_qty
                    executed_delta = -max_qty
        action = reason if abs(executed_delta) > 1e-12 else "hold" if abs(desired - current) <= 1e-12 else "no_fill"
        mark = bid if bid is not None and safe else None
        unrealized = (
            float(state["quantity"]) * (mark - float(state.get("avg_cost", 0.0)))
            if mark is not None else None
        )
        actions.append({
            "outcome_label": label, "action": action,
            "position_before": round(current, 8),
            "position_after": round(float(state["quantity"]), 8),
            "quantity_delta": round(float(executed_delta), 8),
            "edge_after_cost": edge, "best_bid": bid, "best_ask": ask,
            "realized_pnl": round(float(state.get("realized_pnl", 0.0)), 8),
            "unrealized_pnl": round(unrealized, 8) if unrealized is not None else None,
            "point_in_time_safe": safe,
            "reason": reason,
            "do_not_auto_trade": True,
        })
    mark_to_market = sum(item["unrealized_pnl"] or 0.0 for item in actions)
    return actions, realized_delta, mark_to_market


def public_report_is_safe(payload: bytes) -> bool:
    """Do not accidentally publish answer-bearing CSV through the public route."""
    header = payload.splitlines()[0].decode("utf-8-sig", errors="replace") if payload else ""
    fields = {item.strip() for item in header.split(",")}
    return not bool(fields & PRIVATE_REPORT_COLUMNS)


# Keep the historical import surface while making the strategy module canonical.
paper_buy_cost = _isolated_paper_buy_cost
paper_position_rebalance = _isolated_paper_position_rebalance


from live_market_time import live_event_window, classify_live_event


def discover_active_slugs(gamma: GammaClient, now: dt.datetime, timezone: str) -> list[str]:
    candidates = gamma.discover_count_events(
        now - dt.timedelta(days=8), now + dt.timedelta(days=8), timezone, max_pages=20,
    )
    live: list[tuple[dt.datetime, str]] = []
    for event in candidates:
        start, end = live_event_window(event, timezone)
        slug = str(event.get("slug") or event.get("ticker") or "").strip()
        if slug and start is not None and end is not None and start <= now < end:
            live.append((start, slug))
    live.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return list(dict.fromkeys(slug for _, slug in live))


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
        self.last_research_refresh: dt.datetime | None = None
        # Research capture reuses a confirmed event window until it ends. This
        # keeps the 30-second sampler focused on odds/posts/news instead of
        # repeatedly rediscovering the same Gamma metadata.
        self._research_event_cache: dict[str, Any] | None = None
        self._research_event_window: Any | None = None
        self.last_tracker_success: dt.datetime | None = None
        self.last_snapshot: dict[str, Any] | None = None
        self.last_research_snapshot: dict[str, Any] | None = None
        self.last_news: list[dict[str, Any]] = []
        self.last_news_at: dt.datetime | None = None
        self.last_stock: dict[str, Any] = {}
        self.last_stock_at: dt.datetime | None = None
        # Each market window is audited once per service lifetime.  The audit
        # only records evidence for completed checkpoints that have no local
        # capture; it never reconstructs historical quotes from the current book.
        self._gap_audit_windows: set[tuple[str, str]] = set()
        self.last_discovery: dict[str, Any] = {
            "mode": "explicit",
            "queried_at_utc": None,
            "candidate_count": 0,
            "selected_slug": args.slug,
        }
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
        formal_interval = max(300, int(getattr(self.args, "refresh_seconds", 300)))
        research_interval = max(30, int(getattr(self.args, "research_refresh_seconds", 30)))
        next_formal = 0.0
        next_research = time.monotonic() + research_interval
        while not self.stop_event.is_set():
            now_mono = time.monotonic()
            if now_mono >= next_formal:
                try:
                    self.refresh(force=True)
                except Exception as exc:
                    self._record_refresh_failure(utc_now(), "background", exc)
                next_formal = now_mono + formal_interval
            if now_mono >= next_research:
                try:
                    self.research_refresh(force=True)
                except Exception as exc:
                    self._record_research_failure(utc_now(), exc)
                next_research = now_mono + research_interval
            delay = max(0.25, min(next_formal, next_research) - time.monotonic())
            self.stop_event.wait(delay)

    def _record_refresh_failure(
        self,
        now: dt.datetime,
        source: str,
        error: Exception,
        source_status: dict[str, Any] | None = None,
    ) -> None:
        sources = dict(source_status or {})
        sources[source] = {
            "status": "error",
            "latency_ms": sources.get(source, {}).get("latency_ms"),
            "error": f"{type(error).__name__}: {error}",
        }
        snapshot = {
            "service_version": LIVE_SERVICE_VERSION,
            "generated_at_utc": now.isoformat(), "slug": self.args.slug,
            "capture": {
                **self._five_minute_capture(now),
                "configured_interval_seconds": int(self.args.refresh_seconds),
                "interval_status": "on_target" if int(self.args.refresh_seconds) == 300 else "coarser_than_target",
            },
            "window": {}, "count": {}, "prediction": {},
            "market": {"outcomes": []}, "stock": {}, "news": [],
            "official_settlement": {
                "status": "pending", "checked_at_utc": now.isoformat(),
                "official_range": None, "outcomes": [],
                "source": "local_market_outcomes",
            },
            "approved_events": [],
            "external_context": {"flight": {"status": "not_configured", "items": []}},
            "sources": sources,
            "market_discovery": dict(self.last_discovery),
            "errors": [f"{source}: {type(error).__name__}: {error}"],
            "recommendation": {
                "label": "no_trade", "paper_direction": "observe",
                "do_not_auto_trade": True, "risk_reasons": ["live_refresh_failed"],
            },
        }
        self.store.save_live_snapshot(
            captured_at_utc=now, event_slug=self.args.slug,
            refresh_status="failed", snapshot=snapshot,
        )
        self.last_snapshot = snapshot

    def _resolve_event(self, now: dt.datetime) -> dict[str, Any]:
        """Resolve a real Gamma event, recovering from stale date-derived slugs."""
        dynamic = bool(getattr(self.args, "dynamic_discovery", False))
        try:
            event = self.gamma.get_event(self.args.slug)
            start, end = live_event_window(event, self.args.timezone) if dynamic else event_window(event, self.args.timezone)
            if not dynamic or (start is not None and end is not None and start <= now < end):
                self.last_discovery = {
                    "mode": "explicit" if not dynamic else "runtime_check",
                    "http_status": 200,
                    "queried_at_utc": now.isoformat(),
                    "candidate_count": 1,
                    "selected_slug": self.args.slug,
                    "event_window_start_utc": start.isoformat() if start else None,
                    "event_window_end_utc": end.isoformat() if end else None,
                }
                return event
            return self._discover_live_event(now, cause="outside_window")
        except urllib.error.HTTPError as exc:
            if getattr(self.args, 'parent_managed_slug', False):
                raise
            if exc.code != 404:
                raise
            return self._discover_live_event(now, cause="http_404")

    def _discover_live_event(self, now: dt.datetime, *, cause: str) -> dict[str, Any]:
        candidates = self.gamma.discover_count_events(
            now - dt.timedelta(days=8),
            now + dt.timedelta(days=8),
            self.args.timezone,
            max_pages=20,
        )
        live: list[tuple[dt.datetime, dict[str, Any]]] = []
        for candidate in candidates:
            start, end = live_event_window(candidate, self.args.timezone)
            if start is None or end is None or not (start <= now < end):
                continue
            live.append((start, candidate))
        if not live:
            self.last_discovery = {
                "mode": "runtime_discovery",
                "cause": cause,
                "http_status": 404 if cause == "http_404" else 200,
                "queried_at_utc": now.isoformat(),
                "candidate_count": len(candidates),
                "live_candidate_count": 0,
                "selected_slug": None,
            }
            raise ValueError(
                "Gamma 未发现当前时间范围内可用的 Elon Musk 发帖市场；无法读取当前盘口"
            )
        live.sort(key=lambda item: (item[0], str(item[1].get("slug") or "")), reverse=True)
        replacement = live[0][1]
        replacement_slug = str(replacement.get("slug") or replacement.get("ticker") or "").strip()
        if not replacement_slug:
            raise ValueError("Gamma 发现的市场缺少 slug，无法读取盘口")
        self.args.slug = replacement_slug
        start, end = live_event_window(replacement, self.args.timezone)
        self.last_discovery = {
            "mode": "runtime_discovery",
            "cause": cause,
            "http_status": 404 if cause == "http_404" else 200,
            "queried_at_utc": now.isoformat(),
            "candidate_count": len(candidates),
            "live_candidate_count": len(live),
            "selected_slug": replacement_slug,
            "event_window_start_utc": start.isoformat() if start else None,
            "event_window_end_utc": end.isoformat() if end else None,
        }
        return replacement

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

    def _research_outcome_rows(self, event: dict[str, Any]) -> list[tuple[Any, ...]]:
        """Build ephemeral outcome rows for research capture.

        The short-interval sampler must not materialize Gamma metadata in the
        formal market tables.  This mirrors the metadata shape used by
        ``MarketStore.save_event`` while keeping it entirely in memory.
        """
        rows: list[tuple[Any, ...]] = []
        for outcome_index, market in enumerate(event.get("markets", [])):
            if not isinstance(market, dict):
                continue
            labels = parse_range(str(market.get("groupItemTitle") or ""))
            tokens = parse_json(market.get("clobTokenIds"), [])
            names = parse_json(market.get("outcomes"), [])
            prices = parse_json(market.get("outcomePrices"), [])
            if not isinstance(tokens, list):
                tokens = []
            names = [str(name).strip().lower() for name in names] if isinstance(names, list) else []
            yes_index = names.index("yes") if "yes" in names else (0 if not names else None)
            no_index = names.index("no") if "no" in names else (1 if len(names) > 1 else None)
            resolved_yes: int | None = None
            if yes_index is not None and no_index is not None and isinstance(prices, list) and len(prices) > max(yes_index, no_index):
                try:
                    yes_value = float(prices[yes_index])
                    no_value = float(prices[no_index])
                    if yes_value in (0.0, 1.0) and no_value in (0.0, 1.0) and yes_value != no_value:
                        resolved_yes = int(yes_value > no_value)
                except (TypeError, ValueError):
                    pass
            rows.append((
                str(market.get("id")), str(market.get("groupItemTitle") or ""),
                labels[0], labels[1],
                str(tokens[yes_index]) if yes_index is not None and len(tokens) > yes_index else None,
                json.dumps(market, ensure_ascii=False), resolved_yes,
            ))
        return [row for row in rows if row[4]]

    def _fetch_books(self, slug: str, *, outcome_rows: list[tuple[Any, ...]] | None = None,
                     persist: bool = True) -> tuple[
        list[tuple[int | None, int | None, float]],
        list[dict[str, Any]],
        list[str],
    ]:
        outcomes = outcome_rows if outcome_rows is not None else self.store.conn.execute(
            """
            select market_id, label, lower_count, upper_count, yes_token_id, raw_json, resolved_yes
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
                expected_closed = False
                try:
                    raw = json.loads(row[5] or "{}")
                    expected_closed = bool(raw.get("closed")) or row[6] is not None
                except (TypeError, ValueError, json.JSONDecodeError):
                    expected_closed = row[6] is not None
                suffix = " [closed_outcome_expected]" if expected_closed and isinstance(exc, urllib.error.HTTPError) and exc.code == 404 else ""
                return row, None, f"{row[1]}: {type(exc).__name__}: {exc}{suffix}"

        workers = max(1, min(self.args.book_workers, len(outcomes) or 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetch, row) for row in outcomes]
            for future in concurrent.futures.as_completed(futures):
                row, book, error = future.result()
                if error:
                    errors.append(error)
                    continue
                assert book is not None
                if persist:
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

    @staticmethod
    def _research_bucket(
        now: dt.datetime,
        market_start: dt.datetime | None,
        interval_seconds: int,
    ) -> str:
        """Align a research row to the market opening without calling it a 5m checkpoint."""
        anchor = market_start or dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
        anchor = anchor.astimezone(dt.timezone.utc)
        now = now.astimezone(dt.timezone.utc)
        interval = max(30, int(interval_seconds))
        elapsed = max(0, int((now - anchor).total_seconds()))
        epoch = int(anchor.timestamp()) + (elapsed // interval) * interval
        return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat()

    def _record_research_failure(self, now: dt.datetime, error: Exception) -> None:
        interval = max(30, int(getattr(self.args, "research_refresh_seconds", 30)))
        snapshot = {
            "schema_version": "research_capture_v1",
            "research_only": True,
            "do_not_auto_trade": True,
            "service_version": LIVE_SERVICE_VERSION,
            "generated_at_utc": now.isoformat(),
            "slug": self.args.slug,
            "capture": {
                "capture_kind": "research_only",
                "data_time_utc": now.isoformat(),
                "bucket_start_utc": self._research_bucket(now, None, interval),
                "target_interval_seconds": interval,
                "configured_interval_seconds": interval,
                "alignment": "market_start_utc_when_known",
                "recording_status": "failed",
            },
            "window": {},
            "count": {"current": None, "posts_returned": 0, "tracker_refresh_age_seconds": None},
            "market": {"outcomes": []},
            "news": [],
            "sources": {"research_capture": {"status": "error", "error": f"{type(error).__name__}: {error}"}},
            "errors": [f"research_capture: {type(error).__name__}: {error}"],
        }
        try:
            self._save_research_snapshot(
                captured_at_utc=now, event_slug=self.args.slug,
                target_interval_seconds=interval, capture_status="failed",
                snapshot=snapshot,
            )
        except Exception:
            pass
        self.last_research_snapshot = snapshot
        self.last_research_refresh = now

    def _save_research_snapshot(self, **kwargs: Any) -> int:
        """Persist only the isolated research row, tolerating brief SQLite locks."""
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                return self.store.save_research_snapshot(**kwargs)
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "locked" not in str(exc).lower() or attempt == 3:
                    raise
                time.sleep(0.35 * (attempt + 1))
        raise last_error or RuntimeError("research snapshot write failed")

    def research_refresh(self, force: bool = False) -> dict[str, Any]:
        """Capture current odds/posts/news for short-horizon research only.

        This path intentionally skips model inference, edge calculation, paper
        trades, and five-minute checkpoint writes.  Its rows are isolated in
        ``research_market_snapshots`` and cannot be used as historical inputs.
        """
        with self.lock:
            now = utc_now()
            interval = max(30, int(getattr(self.args, "research_refresh_seconds", 30)))
            if (
                not force
                and self.last_research_snapshot is not None
                and self.last_research_refresh is not None
                and (now - self.last_research_refresh).total_seconds() < interval
            ):
                return self.last_research_snapshot
            errors: list[str] = []
            sources: dict[str, Any] = {}
            try:
                event_started = time.monotonic()
                cached_event = self._research_event_cache
                cached_window = self._research_event_window
                if (
                    cached_event is not None
                    and cached_window is not None
                    and cached_window.start_utc <= now < cached_window.end_utc
                ):
                    event = cached_event
                    window = cached_window
                else:
                    event = self._resolve_event(now)
                    start_utc, end_utc = event_window(event, self.args.timezone)
                    if start_utc is None or end_utc is None or end_utc <= start_utc:
                        raise ValueError("research event has no valid window")
                    window = Window(str(event.get("slug") or self.args.slug), start_utc, end_utc)
                    self._research_event_cache = event
                    self._research_event_window = window
                sources["gamma"] = {"status": "ok", "latency_ms": round((time.monotonic() - event_started) * 1000, 1)}
            except Exception as exc:
                sources["gamma"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                self._record_research_failure(now, exc)
                return self.last_research_snapshot or {}

            posts_returned = 0
            tracker_started = time.monotonic()
            try:
                local_start = window.start_utc.astimezone(ZoneInfo(self.args.timezone)).date().isoformat()
                local_end = window.end_utc.astimezone(ZoneInfo(self.args.timezone)).date().isoformat()
                raw_posts = self.tracker.get_posts(
                    self.args.user, self.args.platform, local_start, local_end, self.args.timezone,
                )
                posts_returned = len(raw_posts)
                normalized_posts = [normalize_post(row, self.args.user, self.args.platform) for row in raw_posts]
                self.last_tracker_success = now
                sources["xtracker"] = {"status": "ok", "latency_ms": round((time.monotonic() - tracker_started) * 1000, 1)}
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"xtracker: {type(exc).__name__}: {exc}")
                sources["xtracker"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

            current_count = sum(
                1 for post in normalized_posts
                if post.created_at_utc is not None
                and window.start_utc <= post.created_at_utc <= min(window.end_utc, now)
            ) if 'normalized_posts' in locals() else 0
            tracker_age = max(0, int((now - self.last_tracker_success).total_seconds())) if self.last_tracker_success else None
            books_started = time.monotonic()
            _, books, book_errors = self._fetch_books(
                self.args.slug,
                outcome_rows=self._research_outcome_rows(event),
                persist=False,
            )
            sources["clob"] = {
                "status": "ok" if books and not book_errors else ("partial" if books else "error"),
                "latency_ms": round((time.monotonic() - books_started) * 1000, 1),
                "outcomes_captured": len(books),
                "errors": book_errors[:5],
            }
            errors.extend(f"clob: {message}" for message in book_errors[:5])
            news_started = time.monotonic()
            news = self._fetch_news(now)
            sources["rss_news"] = {
                "status": "error" if any(row.get("error") for row in news) else "ok",
                "latency_ms": round((time.monotonic() - news_started) * 1000, 1),
                "items_captured": len(news),
            }
            snapshot = {
                "schema_version": "research_capture_v1",
                "research_only": True,
                "do_not_auto_trade": True,
                "service_version": LIVE_SERVICE_VERSION,
                "generated_at_utc": now.isoformat(),
                "slug": self.args.slug,
                "capture": {
                    "capture_kind": "research_only",
                    "data_time_utc": now.isoformat(),
                    "bucket_start_utc": self._research_bucket(now, window.start_utc, interval),
                    "target_interval_seconds": interval,
                    "configured_interval_seconds": interval,
                    "alignment": "market_start_utc",
                    "recording_status": "recorded_with_errors" if errors else "recorded",
                    "source_time_policy": "CLOB source timestamps retained; local capture time never backfills history",
                },
                "window": {
                    "start_utc": window.start_utc.isoformat(),
                    "end_utc": window.end_utc.isoformat(),
                    "outside": now < window.start_utc or now >= window.end_utc,
                },
                "count": {
                    "current": current_count,
                    "posts_returned": posts_returned,
                    "tracker_refresh_age_seconds": tracker_age,
                },
                "market": {
                    "outcomes": books,
                    "quote_basis": "current CLOB best bid/ask; descriptive research capture only",
                },
                "news": news,
                "sources": sources,
                "errors": errors,
            }
            self._save_research_snapshot(
                captured_at_utc=now,
                event_slug=self.args.slug,
                target_interval_seconds=interval,
                capture_status="partial" if errors else "complete",
                snapshot=snapshot,
            )
            self.last_research_snapshot = snapshot
            self.last_research_refresh = now
            return snapshot

    def research_archive(self, slug: str | None = None, limit: int = 120) -> list[dict[str, Any]]:
        with self.lock:
            return self.store.research_snapshot_archive(slug or self.args.slug, limit)

    def research_status(self, slug: str | None = None) -> dict[str, Any]:
        with self.lock:
            target = slug or self.args.slug
            latest = self.store.latest_research_snapshot(target)
            return {
                "slug": target,
                "enabled": True,
                "target_interval_seconds": max(30, int(getattr(self.args, "research_refresh_seconds", 30))),
                "latest": latest,
                "research_only": True,
                "do_not_auto_trade": True,
            }

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

    def _official_settlement_snapshot(self, checked_at: dt.datetime) -> dict[str, Any]:
        """Read local Gamma settlement flags; quotes and model output are excluded."""
        rows = self.store.conn.execute(
            """
            select label, lower_count, upper_count, resolved_yes, outcome_index
            from market_outcomes
            where event_slug = ?
            order by outcome_index, lower_count
            """,
            (self.args.slug,),
        ).fetchall()
        outcomes = [
            {
                "label": str(row[0]),
                "lower": row[1],
                "upper": row[2],
                "resolved_yes": None if row[3] is None else bool(row[3]),
                "outcome_index": row[4],
            }
            for row in rows
        ]
        resolved = [item for item in outcomes if item["resolved_yes"] is True]
        has_unresolved = any(item["resolved_yes"] is None for item in outcomes)
        official_range = None
        if len(resolved) == 1 and not has_unresolved:
            official_range = {
                "lower": resolved[0]["lower"],
                "upper": resolved[0]["upper"],
            }
        return {
            "status": "verified" if official_range is not None else "pending",
            "checked_at_utc": checked_at.isoformat(),
            "official_range": official_range,
            "outcomes": outcomes,
            "source": "local_market_outcomes",
            "note": "官方结算来自 Gamma 市场元数据落库；未结算时不以盘口或模型填充。",
        }

    @staticmethod
    def _five_minute_capture(
        now: dt.datetime,
        market_start: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Return a five-minute checkpoint anchored to the market opening."""
        anchor = market_start or dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=dt.timezone.utc)
        anchor = anchor.astimezone(dt.timezone.utc)
        now = now.astimezone(dt.timezone.utc)
        elapsed = max(0, int((now - anchor).total_seconds()))
        checkpoint_epoch = int(anchor.timestamp()) + (elapsed // 300) * 300
        checkpoint = dt.datetime.fromtimestamp(checkpoint_epoch, tz=dt.timezone.utc)
        return {
            "data_time_utc": now.isoformat(),
            "target_interval_seconds": 300,
            "checkpoint_utc": checkpoint.isoformat(),
            "mode": "five_minute_polling",
            "alignment": "market_start_utc" if market_start else "unix_epoch_fallback",
        }

    def _audit_missing_checkpoints(
        self,
        window: Window,
        now: dt.datetime,
    ) -> list[str]:
        """Append explicit evidence for completed local capture gaps.

        The current checkpoint is excluded because the refresh about to be
        written may legitimately be its first capture.  Evidence rows contain
        no prices, order books, forecasts, or edges.
        """
        key = (window.start_utc.isoformat(), window.end_utc.isoformat())
        if key in self._gap_audit_windows:
            return []
        current = self._five_minute_capture(now, window.start_utc)["checkpoint_utc"]
        current_checkpoint = parse_timestamp(current)
        if current_checkpoint is None:
            return []
        end = min(now, window.end_utc)
        if end <= window.start_utc:
            return []
        # Do not consume the one-time audit before the first five-minute
        # checkpoint has completed; a later refresh must still audit it.
        if current_checkpoint <= window.start_utc:
            return []
        self._gap_audit_windows.add(key)
        rows = self.store.conn.execute(
            "select snapshot_json from live_market_snapshots where event_slug = ?",
            (self.args.slug,),
        ).fetchall()
        captured = set()
        for row in rows:
            payload = parse_json(row[0], {})
            capture = payload.get("capture") if isinstance(payload, dict) else {}
            checkpoint = capture.get("checkpoint_utc") if isinstance(capture, dict) else None
            if checkpoint:
                captured.add(str(checkpoint))
        missing: list[str] = []
        checkpoint = window.start_utc
        while checkpoint < end and checkpoint < current_checkpoint:
            checkpoint_iso = checkpoint.isoformat()
            if checkpoint_iso not in captured:
                evidence = {
                    "service_version": LIVE_SERVICE_VERSION,
                    "generated_at_utc": now.isoformat(),
                    "slug": self.args.slug,
                    "capture": {
                        "data_time_utc": now.isoformat(),
                        "target_interval_seconds": 300,
                        "checkpoint_utc": checkpoint_iso,
                        "mode": "five_minute_polling",
                        "alignment": "market_start_utc",
                        "recording_status": "missing_checkpoint",
                        "missing_reason": "missing_checkpoint: startup audit found no local capture; historical quote reconstruction was not attempted",
                    },
                    "window": {
                        "start_utc": window.start_utc.isoformat(),
                        "end_utc": window.end_utc.isoformat(),
                        "outside": False,
                    },
                    "count": {"current": 0, "posts_returned": 0, "stale": True},
                    "prediction": {},
                    "market": {"outcomes": [], "distribution_valid": False},
                    "official_settlement": self._official_settlement_snapshot(now),
                    "sources": {"startup_gap_audit": {"status": "missing_checkpoint"}},
                    "external_context": {"flight": {"status": "not_configured", "items": []}},
                    "errors": ["missing_checkpoint: local capture was not recorded at this checkpoint"],
                    "recommendation": {
                        "label": "no_trade", "paper_direction": "observe",
                        "do_not_auto_trade": True,
                        "risk_reasons": ["missing_checkpoint"],
                    },
                }
                self.store.save_live_snapshot(
                    captured_at_utc=now,
                    event_slug=self.args.slug,
                    refresh_status="missing_checkpoint",
                    snapshot=evidence,
                )
                missing.append(checkpoint_iso)
            checkpoint += dt.timedelta(seconds=300)
        return missing

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
            sources: dict[str, Any] = {}
            gamma_started = time.monotonic()
            try:
                event = self._resolve_event(now)
                sources["gamma"] = {
                    "status": "ok",
                    "latency_ms": round((time.monotonic() - gamma_started) * 1000, 1),
                }
            except Exception as exc:
                sources["gamma"] = {
                    "status": "error",
                    "latency_ms": round((time.monotonic() - gamma_started) * 1000, 1),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                self._record_refresh_failure(now, "gamma", exc, sources)
                raise
            window = self.store.save_event(event, self.args.timezone)
            missing_checkpoints = self._audit_missing_checkpoints(window, now)
            posts_returned = 0
            tracker_started = time.monotonic()
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
                sources["xtracker"] = {
                    "status": "ok",
                    "latency_ms": round((time.monotonic() - tracker_started) * 1000, 1),
                }
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"xtracker: {type(exc).__name__}: {exc}")
                sources["xtracker"] = {
                    "status": "error",
                    "latency_ms": round((time.monotonic() - tracker_started) * 1000, 1),
                    "error": f"{type(exc).__name__}: {exc}",
                }

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
            resolved_range = self.store.resolved_range(self.args.slug)
            settlement_reconciliation = {
                "status": "not_resolved" if resolved_range is None else "verified",
                "observed_local_count": current_count,
                "official_range": {
                    "lower": resolved_range[0], "upper": resolved_range[1],
                } if resolved_range is not None else None,
                "count_in_official_range": None if resolved_range is None else (
                    (resolved_range[0] is None or current_count >= resolved_range[0])
                    and (resolved_range[1] is None or current_count <= resolved_range[1])
                ),
                "note": (
                    "本地 XTracker 观测与官方区间不一致；不补写或伪造缺失帖子。"
                    if resolved_range is not None and not (
                        (resolved_range[0] is None or current_count >= resolved_range[0])
                        and (resolved_range[1] is None or current_count <= resolved_range[1])
                    ) else None
                ),
            }

            books_started = time.monotonic()
            live_prices, books, book_errors = self._fetch_books(self.args.slug)
            sources["clob"] = {
                "status": "ok" if books and not book_errors else ("partial" if books else "error"),
                "latency_ms": round((time.monotonic() - books_started) * 1000, 1),
                "outcomes_requested": len(self.store.outcome_ranges(self.args.slug)),
                "outcomes_captured": len(books),
                "errors": book_errors[:5],
            }
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
            news_started = time.monotonic()
            news = self._fetch_news(now)
            sources["rss_news"] = {
                "status": "error" if any(row.get("error") for row in news) else "ok",
                "latency_ms": round((time.monotonic() - news_started) * 1000, 1),
                "items_captured": len(news),
            }
            stock_started = time.monotonic()
            stock = self._fetch_stock(now)
            sources["stock"] = {
                "status": "error" if stock.get("error") else "ok",
                "latency_ms": round((time.monotonic() - stock_started) * 1000, 1),
                "symbol": stock.get("symbol") or "",
                "error": stock.get("error"),
            }
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
            feature_values = dict(zip(FEATURE_NAMES, features))
            elapsed_hours = max(0.0, (now - window.start_utc).total_seconds() / 3600.0)
            remaining_hours = max(0.0, (window.end_utc - now).total_seconds() / 3600.0)
            feature_snapshot = build_feature_snapshot(
                slug=self.args.slug,
                as_of_utc=now,
                features=feature_values,
                feature_names=FEATURE_NAMES,
                sources=sources,
                quality_flags=list(errors),
                market_coordinates={
                    "elapsed_hours": round(elapsed_hours, 6),
                    "remaining_hours": round(remaining_hours, 6),
                    "elapsed_fraction": round(
                        elapsed_hours / max((window.end_utc - window.start_utc).total_seconds() / 3600.0, 1e-9),
                        6,
                    ),
                    "market_day": int(elapsed_hours // 24) + 1,
                },
                calendar_coordinates={
                    "utc_date": now.date().isoformat(),
                    "utc_hour": now.hour,
                    "weekday": now.weekday(),
                },
            )
            prediction_payload = {
                "final_count": round(forecast, 2),
                "final_count_rounded": int(round(forecast)),
                "forecast_bin": forecast_bin,
                "forecast_bin_probability": bin_probability,
                "variance": variance_source,
                "training_windows": training_windows,
                "model": self.model_payload.get("model"),
            }
            prediction_output = build_prediction_output(
                slug=self.args.slug,
                as_of_utc=now,
                prediction=prediction_payload,
                model_version=self.model_payload.get("model"),
                feature_snapshot=feature_snapshot,
            )
            decision_output = build_decision_output(
                slug=self.args.slug,
                as_of_utc=now,
                decision=recommendation,
            )
            self.last_snapshot = {
                "service_version": LIVE_SERVICE_VERSION,
                "generated_at_utc": now.isoformat(),
                "slug": self.args.slug,
                "capture": {
                    **self._five_minute_capture(now, window.start_utc),
                    "configured_interval_seconds": int(self.args.refresh_seconds),
                    "interval_status": "on_target" if int(self.args.refresh_seconds) == 300 else "coarser_than_target",
                    "recording_status": "recorded_with_gaps" if missing_checkpoints else "recorded",
                    "missing_checkpoint_count": len(missing_checkpoints),
                    "missing_checkpoints_utc": missing_checkpoints,
                },
                "market_event": event,
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
                "settlement_reconciliation": settlement_reconciliation,
                "official_settlement": self._official_settlement_snapshot(now),
                "prediction": prediction_payload,
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
                "external_context": {"flight": {"status": "not_configured", "items": []}},
                "sources": sources,
                "recommendation": recommendation,
                "errors": errors,
                "feature_snapshot": feature_snapshot,
                "prediction_output": prediction_output,
                "decision_output": decision_output,
                "market_discovery": dict(self.last_discovery),
            }
            self.last_snapshot = build_display_snapshot(self.last_snapshot)
            # Keep the research feature vector beside the archived snapshot.
            # It is descriptive only and never feeds the live decision path.
            self.last_snapshot["dynamics_features"] = build_snapshot_features(self.last_snapshot)
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
            archive_id = self.store.save_live_snapshot(
                captured_at_utc=now,
                event_slug=self.args.slug,
                refresh_status="partial" if errors else "complete",
                snapshot=self.last_snapshot,
            )
            self.last_snapshot["archive_snapshot_id"] = archive_id
            self.last_refresh = now
            return self.last_snapshot

    def live_archive(self, slug: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            return self.store.live_snapshot_archive(slug or self.args.slug, limit)

    def prediction_archive(self, slug: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            return self.store.prediction_archive(slug, limit)

    def local_markets(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return markets ordered by the nearest locally captured data time."""
        with self.lock:
            now = utc_now()
            rows = self.store.conn.execute(
                "select slug, game_start_utc, end_date_utc from market_events"
            ).fetchall()
            tables = _table_names(self.store.conn)
            results: list[dict[str, Any]] = []
            for row in rows:
                slug = str(row[0])
                timestamps: list[dt.datetime] = []
                for table in ("prediction_archive", "live_market_snapshots", "market_prices", "market_orderbooks", "paper_forecasts"):
                    captured = self.store.conn.execute(
                        f"select max(captured_at_utc) from {table} where event_slug = ?",
                        (slug,),
                    ).fetchone()[0]
                    parsed = parse_timestamp(captured)
                    if parsed is not None:
                        timestamps.append(parsed)
                latest = max(timestamps, default=parse_timestamp(row[2]) or parse_timestamp(row[1]) or now)
                results.append({
                    "slug": slug,
                    "window_start_utc": row[1],
                    "window_end_utc": row[2],
                    "latest_local_data_utc": latest.isoformat(),
                    "distance_minutes": round(abs((latest - now).total_seconds()) / 60, 3),
                    **_market_completeness(self.store.conn, slug, row[1], row[2], tables, now=now),
                })
            results.sort(key=lambda item: (item["distance_minutes"], item["slug"]))
            return results[: max(1, min(500, int(limit)))]

    def history_review(
        self,
        slug: str | None = None,
        limit: int = 100,
        half_life_days: float = 0,
        max_age_days: float = 365,
    ) -> dict[str, Any]:
        with self.lock:
            return self.store.history_review(
                slug or self.args.slug,
                limit=limit,
                half_life_days=half_life_days,
                max_age_days=max_age_days,
            )

    def backtest_review(self, _control=None, **kwargs: Any) -> dict[str, Any]:
        return isolated_review(self, _control, **kwargs)

    def paper_grid_research(
        self,
        *,
        entry_minutes: tuple[int, ...] = DEFAULT_ENTRY_MINUTES,
        coverage_grid: tuple[float, ...] = DEFAULT_COVERAGE_GRID,
        market_budget: float = DEFAULT_MARKET_BUDGET,
        _control=None,
    ) -> dict[str, Any]:
        """Compare paper entry/coverage combinations using local read-only replay."""
        def replay(entry: int, coverage: float) -> dict[str, Any]:
            if _control is not None:
                _control.check()
            return self.backtest_review(
                _control=_control,
                step_minutes=5,
                min_elapsed_minutes=5,
                paper_mode="mandatory_entry",
                paper_entry_elapsed_minutes=float(entry),
                paper_exit_policy="hold_to_settlement",
                paper_hold_summary=True,
                paper_probability_coverage=coverage,
                paper_market_budget=market_budget,
                paper_capital_units=market_budget,
                limit=10000,
            )
        return run_grid_research(
            replay,
            entry_minutes=entry_minutes,
            coverage_grid=coverage_grid,
            market_budget=market_budget,
            control=_control,
        )

    def _backtest_review(
        self,
        slug: str | None = None,
        start: str | None = None,
        end: str | None = None,
        step_minutes: int = 5,
        min_elapsed_minutes: int = 5,
        max_price_age_hours: float = 24,
        backtest_min_edge: float = 0.08,
        backtest_max_spread: float = 0.04,
        backtest_min_depth: float = 3.0,
        paper_fee_rate: float | None = None,
        paper_slippage_bps: float | None = None,
        paper_size: float | None = None,
        paper_market_budget: float | None = None,
        paper_mode: str = "legacy",
        paper_entry_checkpoint: str | None = None,
        paper_entry_elapsed_minutes: float | None = None,
        paper_exit_policy: str = "managed",
        paper_hold_summary: bool = False,
        paper_probability_coverage: float = DEFAULT_PROBABILITY_COVERAGE,
        paper_capital_units: float | None = None,
        paper_take_profit_return: float = DEFAULT_TAKE_PROFIT_RETURN,
        paper_stop_loss_return: float = DEFAULT_STOP_LOSS_RETURN,
        limit: int = 10000,
        _control=None,
    ) -> dict[str, Any]:
        """Replay a fixed local model at historical checkpoints, read-only.

        This is intentionally a fixed-model replay rather than walk-forward
        training. Every feature and market snapshot is restricted to data that
        was locally available at the checkpoint.
        """
        with self.lock:
            bounded_limit = max(1, min(10000, int(limit)))
            step = max(1, min(1440, int(step_minutes)))
            min_elapsed = max(0, min(7 * 24 * 60, int(min_elapsed_minutes)))
            max_age = float(max_price_age_hours)
            if not math.isfinite(max_age) or max_age <= 0:
                raise ValueError("max_price_age_hours must be positive")
            min_edge = float(backtest_min_edge)
            max_spread = float(backtest_max_spread)
            min_depth = float(backtest_min_depth)
            fee_rate = float(
                getattr(self.args, "paper_fee_rate", 0.02) if paper_fee_rate is None else paper_fee_rate
            )
            slippage_bps = float(
                getattr(self.args, "paper_slippage_bps", 50.0) if paper_slippage_bps is None else paper_slippage_bps
            )
            size = float(getattr(self.args, "paper_size", 1.0) if paper_size is None else paper_size)
            mode = str(paper_mode or "legacy").strip().lower()
            if paper_hold_summary and (mode != "mandatory_entry" or paper_exit_policy != "hold_to_settlement"):
                raise ValueError("全历史统计仅支持强制建仓并持有至结算")
            if paper_hold_summary and (start or end):
                raise ValueError("持有统计必须使用完整市场时间范围")
            if mode not in {"legacy", "mandatory_entry"}:
                raise ValueError("paper_mode must be legacy or mandatory_entry")
            market_budget = float(size if paper_market_budget is None else paper_market_budget)
            if mode == "mandatory_entry" and paper_market_budget is None:
                market_budget = 100.0
            if paper_capital_units is not None:
                market_budget = float(paper_capital_units)
            probability_coverage = float(paper_probability_coverage)
            take_profit_return = float(paper_take_profit_return)
            stop_loss_return = float(paper_stop_loss_return)
            if not all(math.isfinite(value) for value in (min_edge, max_spread, min_depth, fee_rate, slippage_bps, size, market_budget, probability_coverage, take_profit_return, stop_loss_return)):
                raise ValueError("backtest thresholds must be finite")
            if fee_rate < 0 or slippage_bps < 0 or size <= 0 or market_budget <= 0:
                raise ValueError("paper fee, slippage, size and market budget must be non-negative; size and budget must be positive")
            if not 0 < probability_coverage <= 1:
                raise ValueError("paper_probability_coverage must be in (0, 1]")
            if stop_loss_return >= 0 or take_profit_return <= 0:
                raise ValueError("paper stop loss must be negative and take profit must be positive")
            entry_dt = parse_timestamp(paper_entry_checkpoint) if paper_entry_checkpoint else None
            if paper_exit_policy not in {"managed", "hold_to_settlement"}:
                raise ValueError("invalid paper_exit_policy")
            elapsed_entry = float(paper_entry_elapsed_minutes) if paper_entry_elapsed_minutes is not None else None
            if elapsed_entry is not None and (not math.isfinite(elapsed_entry) or elapsed_entry < 0):
                raise ValueError("建仓等待分钟数必须为非负有限数")
            if elapsed_entry is not None and paper_entry_checkpoint:
                raise ValueError("相对建仓时间与绝对时间不能同时指定")
            if paper_entry_checkpoint and entry_dt is None:
                raise ValueError("paper_entry_checkpoint must be an ISO timestamp")
            start_dt = parse_bound(start, self.args.timezone) if start else None
            end_dt = parse_bound(end, self.args.timezone) if end else None
            if start_dt and end_dt and end_dt <= start_dt:
                raise ValueError("end must be after start")
            all_windows = self.store.windows()
            selected_windows = [window for window in all_windows if not slug or window.slug == str(slug).strip()]
            windows = [window for window in selected_windows
                       if (start_dt is None or window.end_utc >= start_dt)
                       and (end_dt is None or window.start_utc < end_dt)]
            if slug:
                wanted = str(slug).strip()
                windows = [window for window in windows if window.slug == wanted]
            now = utc_now()
            windows = [window for window in windows if window.end_utc <= now]
            completeness_by_slug: dict[str, dict[str, Any]] = {}
            excluded_markets: list[dict[str, Any]] = []
            try:
                # Read-only annotation; do not construct MarketStore here
                # because its constructor may perform migrations.
                tables = _table_names(self.store.conn)
                eligible_windows = []
                for window in windows:
                    archive_payload = None
                    if "prediction_archive" in tables:
                        archive = self.store.conn.execute(
                            "select payload_json from prediction_archive where event_slug = ? "
                            "order by captured_at_utc desc, id desc limit 1", (window.slug,)
                        ).fetchone()
                        if archive:
                            try:
                                parsed = json.loads(archive[0])
                                archive_payload = parsed if isinstance(parsed, dict) else None
                            except (TypeError, ValueError):
                                archive_payload = None
                    completeness = _market_completeness(
                        self.store.conn, window.slug, window.start_utc.isoformat(),
                        window.end_utc.isoformat(), tables, now=now,
                        archive_payload=archive_payload,
                    )
                    completeness_by_slug[window.slug] = completeness
                    if completeness.get("eligible_for_profitability_analysis"):
                        eligible_windows.append(window)
                    else:
                        excluded_markets.append({"slug": window.slug, **completeness})
                windows = eligible_windows
            except (AttributeError, KeyError, sqlite3.Error, TypeError, ValueError):
                # Preserve the lightweight replay contract for legacy/test
                # stores that do not expose completeness tables.
                completeness_by_slug = {}
                excluded_markets = []
            total_checkpoint_count = sum(
                sum((start_dt is None or checkpoint >= start_dt)
                    and (end_dt is None or checkpoint < end_dt)
                    for checkpoint in hourly_checkpoints(window, step_minutes=step, min_elapsed_minutes=min_elapsed))
                for window in windows
            )
            empty_reason = None
            empty_message = None
            if not total_checkpoint_count:
                if not selected_windows:
                    empty_reason, empty_message = 'market_not_found', '本地数据库没有该市场，请刷新市场列表'
                elif not any(w.end_utc <= now for w in selected_windows):
                    empty_reason, empty_message = 'market_not_ended', '该市场尚未结束，历史纸面回放仅支持已结束市场'
                elif not windows:
                    empty_reason, empty_message = 'date_range_outside_market', '所选日期范围与市场历史不重叠，请读取完整市场范围'
                else:
                    empty_reason, empty_message = 'no_checkpoints_in_range', '日期范围内没有符合步长的检查点，请读取完整市场范围或调整日期'
            if excluded_markets and not total_checkpoint_count:
                empty_reason = 'incomplete_data'
                empty_message = '鎵€閫夊競鍦虹己灏戝畬鏁扮殑5鍒嗛挓浠锋牸銆佺洏鍙ｆ垨缁撶畻璇佹嵁'
            if _control:
                _control.check()
                _control.total = sum(len(list(hourly_checkpoints(window, step_minutes=step, min_elapsed_minutes=min_elapsed))) for window in windows)
            observations = self.store.post_observations(self.args.user, self.args.platform)
            records: list[dict[str, Any]] = []
            market_summaries: list[dict[str, Any]] = []
            fixed_variance = estimate_prediction_variance(self.model, [], self.model.predict)
            model_hash = hashlib.sha256(
                json.dumps(self.model_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            run_config = {
                "probability_policy": POLICY_VERSION,
                "slug": slug,
                "start": start,
                "end_exclusive": end,
                "step_minutes": step,
                "min_elapsed_minutes": min_elapsed,
                "max_price_age_hours": max_age,
                "backtest_min_edge": min_edge,
                "backtest_max_spread": max_spread,
                "backtest_min_depth": min_depth,
                "paper_fee_rate": fee_rate,
                "paper_slippage_bps": slippage_bps,
                "paper_size": size,
                "paper_market_budget": market_budget,
                "paper_mode": mode,
                "paper_entry_checkpoint": entry_dt.isoformat() if entry_dt else None,
                "paper_entry_elapsed_minutes": elapsed_entry,
                "paper_exit_policy": paper_exit_policy,
                "paper_hold_summary": paper_hold_summary,
                "paper_probability_coverage": probability_coverage,
                "paper_allocation_policy": "center_symmetric_fractional_v1" if mode == "mandatory_entry" else "positive_edge_proportional",
                "paper_take_profit_return": take_profit_return,
                "paper_stop_loss_return": stop_loss_return,
            }
            run_id = hashlib.sha256(
                json.dumps(run_config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:24]

            def average(values: list[float]) -> float | None:
                return sum(values) / len(values) if values else None

            for window in windows:
                if len(records) >= bounded_limit:
                    break
                prior_windows = [item for item in all_windows if item.end_utc <= window.start_utc]
                prior_rate = prior_rate_for_windows(
                    self.store,
                    prior_windows,
                    self.args.user,
                    self.args.platform,
                    True,
                    False,
                ) if prior_windows else 0.0
                ranges = self.store.outcome_ranges(window.slug)
                acquisition = (AcquisitionEvidence(self.store.conn, window.slug, parse_timestamp)
                               if isinstance(getattr(self.store, 'conn', None), sqlite3.Connection)
                               else None)
                resolved = self.store.resolved_range(window.slug)
                resolved_label = next(
                    (label for label, lower, upper in ranges if resolved == (lower, upper)),
                    None,
                )
                observed_final = self.store.final_count(window, self.args.user, self.args.platform)
                checkpoints = hourly_checkpoints(
                    window,
                    min_elapsed_hours=1,
                    step_minutes=step,
                    min_elapsed_minutes=min_elapsed,
                )
                market_records: list[dict[str, Any]] = []
                position_state: dict[str, dict[str, float]] = {}
                paper_account = {"cash": market_budget, "initial_cash": market_budget}
                entry_coverage = 0
                prior_actions: list[dict[str, Any]] = []
                requested_entry = entry_dt or (checkpoints[0] if checkpoints else window.start_utc)
                if elapsed_entry is not None:
                    requested_entry = window.start_utc + dt.timedelta(minutes=elapsed_entry)
                    if requested_entry >= window.end_utc:
                        raise ValueError("建仓等待时间必须小于市场持续时间")
                if entry_dt and (entry_dt < window.start_utc or entry_dt >= window.end_utc or entry_dt not in checkpoints):
                    raise ValueError("首次建仓时间必须是所选市场内的有效检查点")
                actual_entry: dt.datetime | None = None
                safe_entry_checkpoints = 0
                last_processed_index = -1
                for checkpoint_index, checkpoint in enumerate(checkpoints):
                    if paper_hold_summary and checkpoint < requested_entry:
                        continue
                    if _control:
                        _control.check()
                        _control.completed += 1
                    if end_dt is not None and checkpoint >= end_dt:
                        break
                    if len(records) >= bounded_limit:
                        break
                    # Aggregate only the backward-looking five-minute bucket so
                    # interval statistics never see data captured after the
                    # checkpoint.  The latest row remains the executable proxy.
                    if hasattr(self.store, "orderbooks_in_window"):
                        book_rows = self.store.orderbooks_in_window(window.slug, checkpoint, max_age, step)
                    else:
                        book_rows = self.store.orderbooks_at(window.slug, checkpoint, max_age)
                    if paper_hold_summary and not any(b.get("point_in_time_safe") for b in book_rows):
                        last_processed_index = checkpoint_index
                        continue
                    if checkpoint >= requested_entry and any(b.get("point_in_time_safe") for b in book_rows):
                        safe_entry_checkpoints += 1
                    if hasattr(self.store, "prices_in_window"):
                        price_rows = self.store.prices_in_window(window.slug, checkpoint, max_age, step)
                    else:
                        price_rows = self.store.prices_at(window.slug, checkpoint, max_age)
                    review_price_rows = (
                        self.store.historical_prices_in_window(window.slug, checkpoint, step)
                        if hasattr(self.store, "historical_prices_in_window") else price_rows
                    )
                    live_prices = [
                        (row.get("lower_count"), row.get("upper_count"), float(row["price"]))
                        for row in price_rows
                        if row.get("price") is not None and row.get('point_in_time_safe', True)
                    ]
                    features = market_features(
                        self.store,
                        window,
                        checkpoint,
                        observations,
                        prior_rate,
                        max_age,
                        live_prices=live_prices,
                    )
                    forecast, probabilities, probability_guard = guarded_forecast(self.model, features, ranges, prior_rate)
                    local_pace_forecast = max(float(features[0]), float(features[3]))
                    local_pace_probabilities = count_distribution(
                        local_pace_forecast, fixed_variance, ranges
                    )
                    forecast_index = next(
                        (index for index, item in enumerate(ranges) if item[0] == outcome_label_for_count(forecast, ranges)),
                        None,
                    )
                    local_pace_index = next(
                        (
                            index for index, item in enumerate(ranges)
                            if item[0] == outcome_label_for_count(local_pace_forecast, ranges)
                        ),
                        None,
                    )
                    resolved_index = next(
                        (index for index, item in enumerate(ranges) if resolved == (item[1], item[2])),
                        None,
                    )
                    forecast_bin = ranges[forecast_index][0] if forecast_index is not None else outcome_label_for_count(forecast, ranges)
                    safe_price_rows = [
                        row for row in price_rows
                        if row.get("point_in_time_safe", True)
                        and (parse_timestamp(row.get("source_price_time_utc")) is None
                             or parse_timestamp(row.get("source_price_time_utc")) <= checkpoint)
                    ]
                    price_mass = sum(max(0.0, float(row["price"])) for row in safe_price_rows if row.get("price") is not None)
                    market_probs = {
                        str(row.get("outcome_label")): round(max(0.0, float(row["price"])) / price_mass, 6)
                        for row in safe_price_rows
                        if row.get("outcome_label") and row.get("price") is not None and price_mass > 0
                    }
                    review_price_by_label = {
                        str(row.get("outcome_label")): row for row in review_price_rows
                        if row.get("outcome_label")
                    }
                    review_price_mass = sum(
                        max(0.0, float(row["price"])) for row in review_price_rows
                        if row.get("price") is not None
                    )
                    review_market_probs = {
                        str(row.get("outcome_label")): round(max(0.0, float(row["price"])) / review_price_mass, 6)
                        for row in review_price_rows
                        if row.get("outcome_label") and row.get("price") is not None and review_price_mass > 0
                    }
                    book_by_label = {}
                    for book in book_rows:
                        label = str(book.get("outcome_label") or "")
                        if label and (label not in book_by_label or book.get("point_in_time_safe")):
                            book_by_label[label] = book
                    outcome_market_rows: list[dict[str, Any]] = []
                    # Display-only archived midpoint; not a historical trade or model input.
                    for label, book in book_by_label.items():
                        if label in review_price_by_label or not book.get("point_in_time_safe"):
                            continue
                        bid, ask = parse_float(book.get("best_bid")), parse_float(book.get("best_ask"))
                        if bid is None or ask is None or not 0 <= bid <= ask <= 1:
                            continue
                        review_price_by_label[label] = {
                            "price": (bid + ask) / 2, "price_source": "archived_book_midpoint",
                            "source_price_time_utc": book.get("source_timestamp_utc"),
                            "captured_at_utc": book.get("captured_at_utc"),
                            "age_minutes": book.get("source_age_minutes"),
                            "point_in_time_safe": True, "window_sample_count": 1,
                        }
                    review_price_mass = sum(max(0.0, float(r["price"])) for r in review_price_by_label.values() if r.get("price") is not None)
                    review_market_probs = {label: max(0.0, float(r["price"])) / review_price_mass
                                           for label, r in review_price_by_label.items() if r.get("price") is not None and review_price_mass > 0}
                    for index, (label, lower_count, upper_count) in enumerate(ranges):
                        price_row = review_price_by_label.get(label)
                        book_row = book_by_label.get(label)
                        ask = parse_float(book_row.get("best_ask")) if book_row else None
                        bid = parse_float(book_row.get("best_bid")) if book_row else None
                        row_model_probability = probabilities[index] if index < len(probabilities) else None
                        safe_book = bool(book_row and book_row.get("point_in_time_safe"))
                        row_executable = min(1.0, ask + max(0.0, slippage_bps) / 10000.0) if ask is not None and safe_book else None
                        row_fee = ask * fee_rate if ask is not None and safe_book else None
                        ask_depth = parse_float(book_row.get("ask_depth_5")) if book_row else None
                        row_fill = min(1.0, max(0.0, ask_depth) / size) if ask_depth is not None and safe_book else None
                        row_edge = row_model_probability - ask if row_model_probability is not None and ask is not None and safe_book else None
                        row_net_edge = row_model_probability - row_executable - row_fee if row_model_probability is not None and row_executable is not None and row_fee is not None else None
                        row_flags: list[str] = []
                        probability_entry_allowed = (
                            probability_guard["entry_supported"]
                            and row_model_probability is not None and row_model_probability >= 0.10
                            and (lower_count is None or lower_count <= probability_guard["support_upper"])
                            and (upper_count is None or upper_count >= probability_guard["support_lower"])
                        )
                        if not probability_entry_allowed:
                            row_flags.append("probability_risk_guard")
                        if price_row is None:
                            row_flags.append("missing_price_snapshot")
                        elif price_row.get("review_only"):
                            row_flags.append("price_backfilled_after_checkpoint")
                        if book_row is None:
                            row_flags.append("missing_orderbook_snapshot")
                        elif not safe_book:
                            row_flags.append(book_row.get("book_rejection_reason", "orderbook_source_time_missing"))
                        outcome_market_rows.append({
                            **(acquisition.status(label, checkpoint, step, price_row) if acquisition else
                               dict(price_acquisition_status='evidence_insufficient',
                                    price_acquisition_evidence=None, price_target_seconds=step*60)),
                            "outcome_label": label, "lower_count": lower_count, "upper_count": upper_count,
                            "model_probability": round(row_model_probability, 6) if row_model_probability is not None else None,
                            "entry_allowed": probability_entry_allowed,
                            "entry_block_reason": None if probability_entry_allowed else "probability_risk_guard",
                            "latest_price": price_row.get("price") if price_row else None,
                            "price_source": price_row.get("price_source", "price_history") if price_row else None,
                            "price_resolution_seconds": price_row.get("resolution_seconds") if price_row else None,
                            "price_coarser_than_review_window": bool(price_row and price_row.get("coarser_than_review_window")),
                            "market_probability": review_market_probs.get(label),
                            "market_probability_point_in_time_safe": market_probs.get(label),
                            "price_min": price_row.get("window_price_min") if price_row else None,
                            "price_max": price_row.get("window_price_max") if price_row else None,
                            "price_mean": price_row.get("window_price_mean") if price_row else None,
                            "price_sample_count": price_row.get("window_sample_count", 0) if price_row else 0,
                            "latest_price_time_utc": price_row.get("source_price_time_utc") if price_row else None,
                            "latest_price_captured_at_utc": price_row.get("captured_at_utc") if price_row else None,
                            "price_age_minutes": price_row.get("age_minutes") if price_row else None,
                            "price_point_in_time_safe": bool(price_row and price_row.get("point_in_time_safe")),
                            "price_backfilled_after_checkpoint": bool(price_row and price_row.get("backfilled_after_checkpoint")),
                            "price_review_only": bool(price_row and price_row.get("review_only")),
                            "price_observation_end_utc": price_row.get('observation_end_utc') if price_row else None,
                            "best_bid": bid, "best_ask": ask,
                            "spread": parse_float(book_row.get("spread")) if book_row else None,
                            "bid_depth_5": parse_float(book_row.get("bid_depth_5")) if book_row else None,
                            "ask_depth_5": ask_depth,
                            "depth_imbalance": book_row.get("depth_imbalance") if book_row else None,
                            "orderbook_snapshot_count": book_row.get("window_snapshot_count", 0) if book_row else 0,
                            "latest_orderbook_source_time_utc": book_row.get("source_timestamp_utc") if book_row else None,
                            "latest_orderbook_captured_at_utc": book_row.get("captured_at_utc") if book_row else None,
                            "orderbook_age_minutes": book_row.get("capture_age_minutes") if book_row else None,
                            "point_in_time_safe": safe_book,
                            "book_rejection_reason": book_row.get("book_rejection_reason", "safe" if safe_book else "invalid_source_time") if book_row else "missing_orderbook_snapshot",
                            "source_time_recovered": bool(book_row and book_row.get("source_time_recovered")),
                            "model_market_edge": round(row_edge, 6) if row_edge is not None else None,
                            "paper_executable_price": round(row_executable, 6) if row_executable is not None else None,
                            "paper_fee_per_share": round(row_fee, 6) if row_fee is not None else None,
                            "estimated_fill_fraction": round(row_fill, 6) if row_fill is not None else None,
                            "paper_net_edge_after_cost": round(row_net_edge, 6) if row_net_edge is not None else None,
                            "quality_status": "ok" if not row_flags else (
                                "audit_only" if any(flag in row_flags for flag in (
                                    "orderbook_source_time_missing", "price_backfilled_after_checkpoint"
                                )) else "missing"
                            ),
                            "quality_flags": row_flags,
                        })
                    position_actions, realized_pnl_delta, unrealized_pnl = paper_position_rebalance(
                        outcome_market_rows,
                        position_state,
                        checkpoint_index=checkpoint_index,
                        paper_budget=market_budget,
                        account=paper_account,
                        fee_rate=fee_rate,
                        slippage_bps=slippage_bps,
                        entry_edge=0.08,
                        retain_edge=0.04,
                        exit_edge=0.0,
                        entry_checkpoints=0 if mode == "mandatory_entry" else DEFAULT_ENTRY_CHECKPOINTS,
                        risk_exits=mode == "mandatory_entry",
                        hold_to_settlement=mode == "mandatory_entry" and paper_exit_policy == "hold_to_settlement",
                        forecast_count=forecast,
                        mandatory_entry=(
                            mode == "mandatory_entry"
                            and not paper_account.get("mandatory_entry_complete")
                            and checkpoint >= requested_entry
                        ),
                        probability_coverage=probability_coverage,
                        support_lower=probability_guard["support_lower"],
                        support_upper=probability_guard["support_upper"],
                        take_profit_return=take_profit_return,
                        stop_loss_return=stop_loss_return,
                    )
                    if actual_entry is None and any(item["quantity_delta"] > 0 for item in position_actions):
                        actual_entry = checkpoint
                    last_processed_index = checkpoint_index
                    position_total = sum(float(item.get("position_after") or 0.0) for item in position_actions)
                    if checkpoint_index < 12 and any(item["point_in_time_safe"] for item in outcome_market_rows):
                        entry_coverage += 1
                    selected_book = next((row for row in book_rows if row.get("outcome_label") == forecast_bin and row.get("point_in_time_safe")), None)
                    best_ask = parse_float(selected_book.get("best_ask")) if selected_book else None
                    best_bid = parse_float(selected_book.get("best_bid")) if selected_book else None
                    spread = parse_float(selected_book.get("spread")) if selected_book else None
                    depth = parse_float(selected_book.get("ask_depth_5")) if selected_book else None
                    model_probability = probabilities[forecast_index] if forecast_index is not None else None
                    executable_price = (
                        min(1.0, best_ask + max(0.0, slippage_bps) / 10000.0)
                        if best_ask is not None else None
                    )
                    fee_per_share = (
                        best_ask * fee_rate
                        if best_ask is not None else None
                    )
                    fill_fraction = (
                        min(1.0, max(0.0, depth) / size)
                        if depth is not None else None
                    )
                    edge = model_probability - best_ask if model_probability is not None and best_ask is not None else None
                    net_edge = (
                        model_probability - executable_price - fee_per_share
                        if model_probability is not None and executable_price is not None and fee_per_share is not None
                        else None
                    )
                    quality_flags: list[str] = []
                    if not price_rows:
                        quality_flags.append("missing_price_snapshot")
                    if not book_rows:
                        quality_flags.append("missing_orderbook_snapshot")
                    if book_rows and not all(row.get("point_in_time_safe") for row in book_rows):
                        quality_flags.append("orderbook_source_time_missing")
                    if price_rows and not price_distribution_is_valid(price_mass):
                        quality_flags.append("invalid_price_probability_mass")
                    if not prior_windows:
                        quality_flags.append("no_prior_market_windows")
                    decision_status = "no_trade_data_insufficient"
                    if net_edge is not None and spread is not None and depth is not None:
                        decision_status = (
                            "paper_candidate_only"
                            if net_edge >= min_edge and spread <= max_spread and depth >= min_depth and (fill_fraction or 0.0) >= 1.0
                            else "no_trade_quality_or_edge"
                        )
                    resolved_probability = probabilities[resolved_index] if resolved_index is not None else None
                    if decision_status == "paper_candidate_only" and not any(
                        item["outcome_label"] == forecast_bin and item["entry_allowed"] for item in outcome_market_rows
                    ):
                        decision_status = "no_trade_probability_guard"
                    local_pace_resolved_probability = (
                        local_pace_probabilities[resolved_index] if resolved_index is not None else None
                    )
                    market_resolved_probability = (
                        market_probs.get(resolved_label) if resolved_label is not None else None
                    )
                    brier = (
                        sum((probability - (1.0 if index == resolved_index else 0.0)) ** 2 for index, probability in enumerate(probabilities))
                        if resolved_index is not None else None
                    )
                    log_loss = -math.log(max(resolved_probability or 0.0, 1e-12)) if resolved_index is not None else None
                    local_pace_brier = (
                        sum(
                            (probability - (1.0 if index == resolved_index else 0.0)) ** 2
                            for index, probability in enumerate(local_pace_probabilities)
                        )
                        if resolved_index is not None else None
                    )
                    local_pace_log_loss = (
                        -math.log(max(local_pace_resolved_probability or 0.0, 1e-12))
                        if resolved_index is not None else None
                    )
                    market_score_available = (
                        resolved_label is not None
                        and price_distribution_is_valid(price_mass)
                        and all(label in market_probs for label, _, _ in ranges)
                    )
                    market_brier = (
                        sum(
                            (market_probs.get(label, 0.0) - (1.0 if label == resolved_label else 0.0)) ** 2
                            for label, _, _ in ranges
                        )
                        if market_score_available else None
                    )
                    market_log_loss = (
                        -math.log(max(market_resolved_probability or 0.0, 1e-12))
                        if market_score_available else None
                    )
                    price_time = max(
                        (parse_timestamp(row.get("source_price_time_utc")) for row in price_rows),
                        default=None,
                    )
                    book_time = max(
                        (parse_timestamp(row.get("captured_at_utc")) for row in book_rows),
                        default=None,
                    )
                    record = {
                        "backtest_run_id": run_id,
                        "record_id": hashlib.sha256(
                            f"{run_id}|{window.slug}|{checkpoint.isoformat()}".encode("utf-8")
                        ).hexdigest()[:24],
                        "slug": window.slug,
                        "checkpoint_utc": checkpoint.isoformat(),
                        "post_data_available_as_of": checkpoint.isoformat(),
                        "elapsed_hours": round((checkpoint - window.start_utc).total_seconds() / 3600, 4),
                        "remaining_hours": round((window.end_utc - checkpoint).total_seconds() / 3600, 4),
                        "checkpoint_step_minutes": step,
                        "interval_start_utc": (checkpoint - dt.timedelta(minutes=step)).isoformat(),
                        "interval_end_utc": checkpoint.isoformat(),
                        "interval_minutes": step,
                        "post_count_in_interval": count_before(
                            observations,
                            max(window.start_utc, checkpoint - dt.timedelta(minutes=step)),
                            checkpoint,
                            checkpoint,
                        ),
                        "post_count_cumulative": int(features[0]),
                        "current_count": int(features[0]),
                        "model_forecast_final_count": round(forecast, 4),
                        "forecast_bin": forecast_bin,
                        "bin_probabilities": {label: round(probability, 6) for (label, _, _), probability in zip(ranges, probabilities)},
                        "forecast_bin_probability": round(model_probability, 6) if model_probability is not None else None,
                        "local_pace_forecast_final_count": round(local_pace_forecast, 4),
                        "local_pace_bin": ranges[local_pace_index][0] if local_pace_index is not None else outcome_label_for_count(local_pace_forecast, ranges),
                        "local_pace_bin_probability": round(local_pace_probabilities[local_pace_index], 6) if local_pace_index is not None else None,
                        "market_implicit_probability": market_probs.get(forecast_bin),
                        "market_probability_mass": round(price_mass, 6),
                        "price_snapshot_time_utc": price_time.isoformat() if price_time else None,
                        "price_snapshot_age_minutes": round((checkpoint - price_time).total_seconds() / 60, 3) if price_time else None,
                        "price_snapshot": price_rows,
                        "outcome_market_rows": outcome_market_rows,
                        "position_actions": position_actions,
                        "paper_position_total": round(position_total, 8),
                        "paper_cash": round(paper_account["cash"], 8),
                        "paper_total_buy_cost": round(paper_account.get("total_buy_cost", 0.0), 8),
                        "paper_total_sell_proceeds": round(paper_account.get("total_sell_proceeds", 0.0), 8),
                        "paper_fees_paid": round(paper_account.get("fees_paid", 0.0), 8),
                        "paper_entry_requested_utc": requested_entry.isoformat(),
                        "paper_entry_actual_utc": actual_entry.isoformat() if actual_entry else None,
                        "paper_entry_status": "filled" if actual_entry else (
                            "waiting_for_valid_book" if mode == "mandatory_entry" and checkpoint >= requested_entry else "not_reached"
                        ),
                        "paper_checkpoint_turnover": round(paper_account.get("turnover", 0.0), 8),
                        "paper_entry_coverage_so_far": entry_coverage,
                        "paper_realized_pnl_delta": round(realized_pnl_delta, 8),
                        "paper_unrealized_pnl": (
                            None if any(item["position_after"] > 0 and item["unrealized_pnl"] is None
                                        for item in position_actions) else round(unrealized_pnl, 8)
                        ),
                        "paper_position_methodology": {
                            "entry_checkpoints": DEFAULT_ENTRY_CHECKPOINTS,
                            "entry_edge": DEFAULT_ENTRY_EDGE,
                            "retain_edge": DEFAULT_RETAIN_EDGE,
                            "exit_edge": DEFAULT_EXIT_EDGE,
                            "market_budget": market_budget,
                            "allocation": "center_symmetric_fractional_v1" if mode == "mandatory_entry" else "positive_edge_proportional_capped_40_percent",
                            "execution": "ask_buy_bid_sell_depth_limited",
                            "simulation_version": "legacy_aggregate_depth",
                            "paper_mode": mode,
                            "probability_coverage": probability_coverage,
                            "take_profit_return": take_profit_return,
                            "stop_loss_return": stop_loss_return,
                            "do_not_auto_trade": True,
                        },
                        "price_window_sample_count": sum(int(row.get("window_sample_count") or 0) for row in price_rows),
                        "price_aggregation_window_start_utc": price_rows[0].get("aggregation_window_start_utc") if price_rows else None,
                        "price_aggregation_window_end_utc": price_rows[0].get("aggregation_window_end_utc") if price_rows else None,
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "spread": spread,
                        "ask_depth_5": depth,
                        "orderbook_snapshot_time_utc": book_time.isoformat() if book_time else None,
                        "orderbook_snapshot_age_minutes": round((checkpoint - book_time).total_seconds() / 60, 3) if book_time else None,
                        "orderbook_snapshot": book_rows,
                        "orderbook_window_snapshot_count": sum(int(row.get("window_snapshot_count") or 0) for row in book_rows),
                        "orderbook_aggregation_window_start_utc": book_rows[0].get("aggregation_window_start_utc") if book_rows else None,
                        "orderbook_aggregation_window_end_utc": book_rows[0].get("aggregation_window_end_utc") if book_rows else None,
                        "model_market_edge": round(edge, 6) if edge is not None else None,
                        "paper_executable_price": round(executable_price, 6) if executable_price is not None else None,
                        "paper_fee_rate": fee_rate,
                        "paper_fee_per_share": round(fee_per_share, 6) if fee_per_share is not None else None,
                        "paper_slippage_bps": slippage_bps,
                        "paper_size": size,
                        "estimated_fill_fraction": round(fill_fraction, 6) if fill_fraction is not None else None,
                        "paper_net_edge_after_cost": round(net_edge, 6) if net_edge is not None else None,
                        "decision_status": decision_status,
                        "paper_execution_status": (
                            "filled" if any(item["quantity_delta"] for item in position_actions)
                            else "no_fill" if any(item["action"] == "no_fill" for item in position_actions)
                            else "hold"
                        ),
                        "data_quality_flags": quality_flags,
                        "model_source": "provided_model",
                        "prediction_variance": round(probability_guard["variance"], 4),
                        "probability_guard": probability_guard,
                        "training_windows": len(prior_windows),
                        "observed_local_final_count": observed_final,
                        "resolved_bin": resolved_label,
                        "resolved_lower": resolved[0] if resolved else None,
                        "resolved_upper": resolved[1] if resolved else None,
                        "brier_score": round(brier, 6) if brier is not None else None,
                        "log_loss": round(log_loss, 6) if log_loss is not None else None,
                        "local_pace_brier_score": round(local_pace_brier, 6) if local_pace_brier is not None else None,
                        "local_pace_log_loss": round(local_pace_log_loss, 6) if local_pace_log_loss is not None else None,
                        "market_brier_score": round(market_brier, 6) if market_brier is not None else None,
                        "market_log_loss": round(market_log_loss, 6) if market_log_loss is not None else None,
                        "model_vs_local_pace_brier_delta": round(brier - local_pace_brier, 6) if brier is not None and local_pace_brier is not None else None,
                        "model_vs_local_pace_log_loss_delta": round(log_loss - local_pace_log_loss, 6) if log_loss is not None and local_pace_log_loss is not None else None,
                        "model_vs_market_brier_delta": round(brier - market_brier, 6) if brier is not None and market_brier is not None else None,
                        "model_vs_market_log_loss_delta": round(log_loss - market_log_loss, 6) if log_loss is not None and market_log_loss is not None else None,
                    }
                    if start_dt is not None and checkpoint < start_dt:
                        prior_actions.extend({**item, "checkpoint_utc": checkpoint.isoformat()}
                                             for item in position_actions
                                             if item["quantity_delta"] or item["action"] == "no_fill")
                        continue
                    if not market_records:
                        record["paper_prior_actions"] = prior_actions
                    records.append(record)
                    market_records.append(record)
                    if paper_hold_summary and actual_entry is not None:
                        # Holding mode freezes quantities: subsequent quotations
                        # cannot change final cash or official settlement payout.
                        last_processed_index = len(checkpoints) - 1
                        break
                settlement_actions: list[dict[str, Any]] = []
                complete = last_processed_index == len(checkpoints) - 1
                settled = mode == "mandatory_entry" and complete and resolved_label is not None
                if market_records and mode == "mandatory_entry" and actual_entry is None and complete:
                    market_records[-1]["paper_entry_status"] = "unavailable_before_market_end"
                if settled:
                    settlement_actions = settle_paper_positions(position_state, paper_account, resolved_label)
                    if market_records:
                        final = market_records[-1]
                        final["paper_settlement_actions"] = settlement_actions
                        final["paper_settlement_time_utc"] = window.end_utc.isoformat()
                        final["paper_cash_after_settlement"] = round(paper_account["cash"], 8)
                        final["paper_position_total_after_settlement"] = 0.0
                        final_pnl = paper_account["cash"] - market_budget
                        deployed = float(paper_account.get("total_buy_cost", 0.0))
                        final["paper_final_pnl"] = round(final_pnl, 8)
                        final["paper_final_return"] = round(final_pnl / deployed, 8) if deployed > 0 else None
                market_summaries.append({
                    "slug": window.slug,
                    **(completeness_by_slug.get(window.slug) or {}),
                    "paper_entry_covered_checkpoints": entry_coverage,
                    "paper_entry_expected_checkpoints": 12,
                    "paper_mode": mode,
                    "paper_entry_requested_utc": requested_entry.isoformat(),
                    "paper_entry_actual_utc": actual_entry.isoformat() if actual_entry else None,
                    "paper_entry_status": "filled" if actual_entry else ("unavailable_before_market_end" if complete else "pending_in_remaining_range"),
                    "paper_entry_diagnostic": (None if actual_entry else '建仓节点及后续没有安全历史盘口' if not safe_entry_checkpoints else '存在历史盘口，但所选概率范围内未产生可执行成交'),
                    "paper_entry_delay_minutes": round((actual_entry-requested_entry).total_seconds()/60,3) if actual_entry else None,
                    "paper_settled": settled,
                    "paper_fill_action_count": sum(
                        bool(item.get("quantity_delta")) for record in market_records
                        for item in record.get("position_actions", [])
                    ) + sum(bool(item.get("quantity_delta")) for item in prior_actions),
                    "paper_attempt_count": sum(
                        item.get("action") == "no_fill" for record in market_records
                        for item in record.get("position_actions", [])
                    ) + sum(item.get("action") == "no_fill" for item in prior_actions),
                    "paper_settlement_action_count": len(settlement_actions),
                    "paper_total_buy_cost": round(paper_account.get("total_buy_cost", 0.0), 8),
                    "paper_total_sell_proceeds": round(paper_account.get("total_sell_proceeds", 0.0), 8),
                    "paper_fees_paid": round(paper_account.get("fees_paid", 0.0), 8),
                    "paper_final_cash": round(paper_account.get("cash", 0.0), 8),
                    "paper_final_pnl": round(paper_account.get("cash", 0.0) - market_budget, 8) if settled else None,
                    "paper_final_return": (
                        round((paper_account.get("cash", 0.0) - market_budget) / paper_account.get("total_buy_cost", 1.0), 8)
                        if settled and paper_account.get("total_buy_cost", 0.0) > 0 else None
                    ),
                    "window_start_utc": window.start_utc.isoformat(),
                    "window_end_utc": window.end_utc.isoformat(),
                    "checkpoints": len(market_records),
                    "paper_candidate_checkpoints": sum(item["decision_status"] == "paper_candidate_only" for item in market_records),
                    "quality_limited_checkpoints": sum(item["decision_status"] != "paper_candidate_only" for item in market_records),
                    "observed_local_final_count": observed_final,
                    "resolved_bin": resolved_label,
                    "mean_brier_score": (
                        round(value, 6) if (value := average([
                            item["brier_score"] for item in market_records if item["brier_score"] is not None
                        ])) is not None else None
                    ),
                    "mean_log_loss": (
                        round(value, 6) if (value := average([
                            item["log_loss"] for item in market_records if item["log_loss"] is not None
                        ])) is not None else None
                    ),
                    "mean_local_pace_brier_score": (
                        round(value, 6) if (value := average([
                            item["local_pace_brier_score"] for item in market_records
                            if item["local_pace_brier_score"] is not None
                        ])) is not None else None
                    ),
                    "mean_local_pace_log_loss": (
                        round(value, 6) if (value := average([
                            item["local_pace_log_loss"] for item in market_records
                            if item["local_pace_log_loss"] is not None
                        ])) is not None else None
                    ),
                    "mean_market_brier_score": (
                        round(value, 6) if (value := average([
                            item["market_brier_score"] for item in market_records if item["market_brier_score"] is not None
                        ])) is not None else None
                    ),
                    "mean_market_log_loss": (
                        round(value, 6) if (value := average([
                            item["market_log_loss"] for item in market_records if item["market_log_loss"] is not None
                        ])) is not None else None
                    ),
                    "final_record_id": market_records[-1].get("record_id") if market_records else None,
                    "mean_paper_net_edge_after_cost": (
                        round(value, 6) if (value := average([
                            item["paper_net_edge_after_cost"] for item in market_records
                            if item["paper_net_edge_after_cost"] is not None
                        ])) is not None else None
                    ),
                    "paired_model_local_pace_score_records": sum(
                        item["model_vs_local_pace_brier_delta"] is not None for item in market_records
                    ),
                    "paired_model_market_score_records": sum(
                        item["model_vs_market_brier_delta"] is not None for item in market_records
                    ),
                })
            resolved_records = [
                item for item in records
                if item.get("resolved_bin") is not None and item.get("forecast_bin_probability") is not None
            ]
            calibration_bins: list[dict[str, Any]] = []
            for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
                upper = min(1.0, lower + 0.2)
                bucket = [
                    item for item in resolved_records
                    if lower <= float(item["forecast_bin_probability"]) < upper
                    or (upper == 1.0 and lower <= float(item["forecast_bin_probability"]) <= upper)
                ]
                predicted = average([float(item["forecast_bin_probability"]) for item in bucket])
                observed = average([1.0 if item.get("forecast_bin") == item.get("resolved_bin") else 0.0 for item in bucket])
                calibration_bins.append({
                    "lower": lower,
                    "upper": upper,
                    "records": len(bucket),
                    "mean_predicted_probability": round(predicted, 6) if predicted is not None else None,
                    "observed_hit_rate": round(observed, 6) if observed is not None else None,
                    "absolute_calibration_gap": round(abs(predicted - observed), 6)
                    if predicted is not None and observed is not None else None,
                })
            market_brier_values = [item.get("mean_brier_score") for item in market_summaries if item.get("mean_brier_score") is not None]
            market_log_loss_values = [item.get("mean_log_loss") for item in market_summaries if item.get("mean_log_loss") is not None]
            return {
                "schema_version": "backtest_review_v1",
                "backtest_run_id": run_id,
                "slug": slug or None,
                "do_not_auto_trade": True,
                "model_source": "provided_model",
                "model_hash_sha256": model_hash,
                "run_config": run_config,
                "markets": market_summaries,
                "records": records,
                "summary": {
                    "markets": len(market_summaries),
                    "requested_markets": len(selected_windows),
                    "eligible_markets": len(windows),
                    "excluded_markets": len(excluded_markets),
                    "records": len(records),
                    "paper_candidate_checkpoints": sum(item["decision_status"] == "paper_candidate_only" for item in records),
                    "quality_limited_checkpoints": sum(item["decision_status"] != "paper_candidate_only" for item in records),
                    "mean_model_market_edge": round(sum(item["model_market_edge"] for item in records if item["model_market_edge"] is not None) / max(1, sum(item["model_market_edge"] is not None for item in records)), 6) if any(item["model_market_edge"] is not None for item in records) else None,
                    "mean_paper_net_edge_after_cost": round(sum(item["paper_net_edge_after_cost"] for item in records if item["paper_net_edge_after_cost"] is not None) / max(1, sum(item["paper_net_edge_after_cost"] is not None for item in records)), 6) if any(item["paper_net_edge_after_cost"] is not None for item in records) else None,
                    "mean_brier_score": round(sum(item["brier_score"] for item in records if item["brier_score"] is not None) / max(1, sum(item["brier_score"] is not None for item in records)), 6) if any(item["brier_score"] is not None for item in records) else None,
                    "mean_log_loss": round(sum(item["log_loss"] for item in records if item["log_loss"] is not None) / max(1, sum(item["log_loss"] is not None for item in records)), 6) if any(item["log_loss"] is not None for item in records) else None,
                    "mean_local_pace_brier_score": round(sum(item["local_pace_brier_score"] for item in records if item["local_pace_brier_score"] is not None) / max(1, sum(item["local_pace_brier_score"] is not None for item in records)), 6) if any(item["local_pace_brier_score"] is not None for item in records) else None,
                    "mean_local_pace_log_loss": round(sum(item["local_pace_log_loss"] for item in records if item["local_pace_log_loss"] is not None) / max(1, sum(item["local_pace_log_loss"] is not None for item in records)), 6) if any(item["local_pace_log_loss"] is not None for item in records) else None,
                    "mean_market_brier_score": round(sum(item["market_brier_score"] for item in records if item["market_brier_score"] is not None) / max(1, sum(item["market_brier_score"] is not None for item in records)), 6) if any(item["market_brier_score"] is not None for item in records) else None,
                    "mean_market_log_loss": round(sum(item["market_log_loss"] for item in records if item["market_log_loss"] is not None) / max(1, sum(item["market_log_loss"] is not None for item in records)), 6) if any(item["market_log_loss"] is not None for item in records) else None,
                    "mean_model_vs_local_pace_brier_delta": round(sum(item["model_vs_local_pace_brier_delta"] for item in records if item["model_vs_local_pace_brier_delta"] is not None) / max(1, sum(item["model_vs_local_pace_brier_delta"] is not None for item in records)), 6) if any(item["model_vs_local_pace_brier_delta"] is not None for item in records) else None,
                    "mean_model_vs_market_brier_delta": round(sum(item["model_vs_market_brier_delta"] for item in records if item["model_vs_market_brier_delta"] is not None) / max(1, sum(item["model_vs_market_brier_delta"] is not None for item in records)), 6) if any(item["model_vs_market_brier_delta"] is not None for item in records) else None,
                    "markets_with_settlement": sum(item.get("resolved_bin") is not None for item in market_summaries),
                    "resolved_checkpoint_records": len(resolved_records),
                    "market_weighted_mean_brier_score": round(average(market_brier_values), 6) if average(market_brier_values) is not None else None,
                    "market_weighted_mean_log_loss": round(average(market_log_loss_values), 6) if average(market_log_loss_values) is not None else None,
                },
                "calibration": {
                    "unit": "forecast_bin_probability",
                    "definition": "Observed hit rate of the model-selected bin among locally resolved checkpoints; empty bins are retained.",
                    "resolved_checkpoint_records": len(resolved_records),
                    "bins": calibration_bins,
                },
                "coverage": {
                    "empty_reason": empty_reason,
                    "empty_message": empty_message,
                    "selected_market_windows": [dict(slug=w.slug,start_utc=w.start_utc.isoformat(),end_utc=w.end_utc.isoformat()) for w in selected_windows],
                    "start": start,
                    "end_exclusive": end,
                    "ended_markets_only": True,
                    "requested_market_count": len(selected_windows),
                    "eligible_market_count": len(windows),
                    "excluded_market_count": len(excluded_markets),
                    "excluded_markets": excluded_markets,
                    "profitability_filter": "complete_for_profitability",
                    "checkpoint_step_minutes": step,
                    "min_elapsed_minutes": min_elapsed,
                    "max_price_age_hours": max_age,
                    "record_identity": "sha256(run_config, slug, checkpoint_utc)",
                    "total_checkpoint_count": total_checkpoint_count,
                    "loaded_checkpoint_count": len(records),
                    "truncated": len(records) < total_checkpoint_count,
                },
                "data_quality": {
                    "records_with_price": sum(bool(item["price_snapshot"]) for item in records),
                    "records_with_orderbook": sum(bool(item["orderbook_snapshot"]) for item in records),
                    "records_point_in_time_safe_orderbook": sum(bool(item["orderbook_snapshot"]) and all(row.get("point_in_time_safe") for row in item["orderbook_snapshot"]) for item in records),
                    "records_with_settlement_score": sum(item["brier_score"] is not None for item in records),
                    "paired_model_local_pace_score_records": sum(item["model_vs_local_pace_brier_delta"] is not None for item in records),
                    "paired_model_market_score_records": sum(item["model_vs_market_brier_delta"] is not None for item in records),
                },
                "methodology": {
                    "description": "Fixed provided model replay over local SQLite archives; not walk-forward training.",
                    "probability_policy": POLICY_VERSION,
                    "probability_guard": "24h prior-rate smoothing; remaining-count distribution; 3-sigma support and 10% probability gate for new exposure. Risk overlay, not fitted calibration; raw model forecast retained per checkpoint.",
                    "feature_availability": "Posts and approved source events are gated by checkpoint availability; price and orderbook rows require local capture at or before checkpoint.",
                    "time_decay": "Not applied to model parameters; use history-review sensitivity separately.",
                    "thresholds": {"min_edge": min_edge, "max_spread": max_spread, "min_depth": min_depth},
                    "cost_model": {"fee_rate": fee_rate, "slippage_bps": slippage_bps, "paper_size": size, "execution": "best_ask_plus_slippage"},
                    "settlement_metrics": "Brier and Log Loss are shown only when a local resolved outcome exists; lower is better and they are not profitability evidence.",
                    "baselines": {
                        "local_pace": "Checkpoint-local post count extrapolated at current pace; it does not use price, orderbook, source-event or prior-window features.",
                        "market": "Normalized locally archived outcome prices only when the complete price mass is valid. It is not independent of the model because this model also receives local price features.",
                        "paired_delta": "model score minus baseline score; negative is better for the model. Paired records only are summarized.",
                    },
                },
            }

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

    def discovery(self) -> dict[str, Any]:
        return {"state": "explicit", **dict(self.last_discovery)}


class MultiLiveService:
    """Keep each selected market isolated while sharing process configuration."""

    def __init__(self, args: argparse.Namespace, slugs: list[str]) -> None:
        self.database_lock = threading.RLock()
        self.args = args
        self.dynamic_discovery = bool(getattr(args, "dynamic_discovery", False))
        self.services: list[LiveService] = []
        self.retired_services: dict[str, LiveService] = {}
        self.discovery_client = GammaClient(args.gamma_url, args.timeout, args.proxy)
        self.discovery_status: dict[str, Any] = {
            "state": "idle",
            "queried_at_utc": None,
            "candidate_count": 0,
            "live_candidate_count": 0,
            "active_slugs": list(slugs),
            "queued_backfills": [],
            "completed_backfills": [],
            "failed_backfills": [],
            "error": None,
        }
        self.backfill_queue: queue.Queue[str | None] = queue.Queue()
        self.backfill_attempts: dict[str, dt.datetime] = {}
        self.backfill_thread: threading.Thread | None = None
        for slug in slugs:
            child_args = argparse.Namespace(**vars(args))
            child_args.slug = slug
            if self.dynamic_discovery:
                child_args.dynamic_discovery = False
                child_args.parent_managed_slug = True
            self.services.append(LiveService(child_args, self.database_lock))
        self.refresh_seconds = max(10, args.refresh_seconds)
        self.research_refresh_seconds = max(30, int(getattr(args, "research_refresh_seconds", 30)))
        self.stop_event = threading.Event()
        self.background_thread: threading.Thread | None = None

    def _sync_dynamic_services(self, now: dt.datetime) -> None:
        if not getattr(self, "dynamic_discovery", False):
            return
        if not hasattr(self, "discovery_status"):
            self.discovery_status = {"state": "idle", "active_slugs": []}
        if not hasattr(self, "retired_services"):
            self.retired_services = {}
        try:
            discovery_client = getattr(self, "discovery_client", None)
            if discovery_client is None:
                discovery_client = self.services[0].gamma if self.services else None
            if discovery_client is None:
                return
            candidates = discovery_client.discover_count_events(
                # Keep the live set narrow, but inspect a wider recent history
                # so ended markets are automatically archived after settlement
                # instead of falling out of discovery after eight days.
                now - dt.timedelta(days=35), now + dt.timedelta(days=8),
                self.args.timezone, max_pages=20,
            )
            live: list[tuple[dt.datetime, str]] = []
            assessments = [classify_live_event(event, now, self.args.timezone) for event in candidates]
            self.live_windows = {row['slug']: row for row in assessments}
            for event in candidates:
                start, end = live_event_window(event, self.args.timezone)
                slug = str(event.get("slug") or event.get("ticker") or "").strip()
                if slug and start is not None and end is not None and start <= now < end:
                    live.append((start, slug))
            wanted = list(dict.fromkeys(slug for _, slug in sorted(live, reverse=True)))
            local_slugs = set()
            if self.services:
                local_slugs.update(service.args.slug for service in self.services)
            local_slugs.update(self.retired_services)
            missing_historical = []
            anchor = self.services[0] if self.services else next(iter(self.retired_services.values()), None)
            store = getattr(anchor, "store", None) if anchor is not None else None
            temporary_store = None
            if store is None:
                try:
                    temporary_store = MarketStore(Path(self.args.db))
                    store = temporary_store
                except Exception:
                    store = None
            if store is not None:
                known = {
                    row[0] for row in store.conn.execute("select slug from market_events").fetchall()
                }
                for event in candidates:
                    slug = str(event.get("slug") or event.get("ticker") or "").strip()
                    start, end = event_window(event, self.args.timezone)
                    if slug and start is not None and end is not None and end <= now:
                        # A known event can still be incomplete (for example a
                        # prior Gamma fetch succeeded but archival snapshots did
                        # not).  Queue it until at least one successful archive
                        # prediction exists; all writes remain append/upsert-only.
                        needs_archive = slug not in known
                        if not needs_archive:
                            try:
                                row = store.conn.execute(
                                    "select 1 from prediction_archive where event_slug = ? and coalesce(error, 0) = 0 limit 1",
                                    (slug,),
                                ).fetchone()
                                needs_archive = row is None
                            except Exception:
                                needs_archive = False
                        if needs_archive:
                            missing_historical.append(slug)
            if temporary_store is not None:
                temporary_store.close()
            for slug in missing_historical:
                self._queue_backfill(slug)
            current = {service.args.slug: service for service in self.services}
            for slug, service in list(current.items()):
                if slug not in wanted:
                    self.retired_services[slug] = service
            for slug in wanted:
                if slug not in current:
                    service = self.retired_services.pop(slug, None)
                    if service is None:
                        child_args = argparse.Namespace(**vars(self.args))
                        child_args.slug = slug
                        # Parent owns rollover; children must never change their slug.
                        child_args.dynamic_discovery = False
                        child_args.parent_managed_slug = True
                        service = LiveService(child_args, self.database_lock)
                    self.services.append(service)
            by_slug = {service.args.slug: service for service in self.services}
            self.services = [by_slug[slug] for slug in wanted]
            self.discovery_status.update({
                "state": "ok",
                "queried_at_utc": now.isoformat(),
                "candidate_count": len(candidates),
                "live_candidate_count": len(wanted),
                "active_slugs": [service.args.slug for service in self.services],
                "error": None,
                "window_assessments": assessments,
                "next_boundary_utc": min((value for row in assessments if row['valid']
                    for value in (row['start_utc'], row['end_utc']) if dt.datetime.fromisoformat(value) > now), default=None),
            })
        except Exception as exc:
            # Never present a previously selected/settled market as current when
            # discovery is stale or unavailable.  Retain it only as historical
            # state so local archives remain accessible.
            for service in list(getattr(self, "services", [])):
                self.retired_services[service.args.slug] = service
            self.services = []
            self.discovery_status.update({
                "state": "error",
                "queried_at_utc": now.isoformat(),
                "active_slugs": [],
                "live_candidate_count": 0,
                "error": f"{type(exc).__name__}: {exc}",
            })
            return

    def _backfill_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            db=self.args.db, user=self.args.user, platform=self.args.platform,
            timezone=self.args.timezone, gamma_url=self.args.gamma_url,
            clob_url=self.args.clob_url, data_url="https://data-api.polymarket.com",
            tracker_url=self.args.tracker_url, proxy=self.args.proxy,
            timeout=self.args.timeout, skip_prices=False, price_interval="1w",
            price_fidelity=5, orderbooks=True, batch_clob=True,
            price_workers=max(1, min(8, self.args.book_workers)), request_delay=0.1,
            public_trades=False, trade_limit=500,
        )

    def _queue_backfill(self, slug: str) -> None:
        now = utc_now()
        previous = self.backfill_attempts.get(slug)
        if previous and (now - previous).total_seconds() < 21600:
            return
        self.backfill_attempts[slug] = now
        self.backfill_queue.put(slug)
        queued = list(self.discovery_status.get("queued_backfills", []))
        if slug not in queued:
            queued.append(slug)
        self.discovery_status["queued_backfills"] = queued[-50:]
        if not self.backfill_thread or not self.backfill_thread.is_alive():
            self.backfill_thread = threading.Thread(
                target=self._backfill_loop, name="discovered-market-backfill", daemon=True,
            )
            self.backfill_thread.start()

    def _backfill_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                slug = self.backfill_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if slug is None:
                return
            try:
                with self.database_lock:
                    crawl_one(self._backfill_args(), slug)
                completed = list(self.discovery_status.get("completed_backfills", []))
                completed.append(slug)
                self.discovery_status["completed_backfills"] = completed[-50:]
            except Exception as exc:
                failed = list(self.discovery_status.get("failed_backfills", []))
                failed.append({"slug": slug, "error": f"{type(exc).__name__}: {exc}"})
                self.discovery_status["failed_backfills"] = failed[-50:]
            finally:
                self.backfill_queue.task_done()

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
        formal_interval = max(300, int(self.refresh_seconds))
        research_interval = max(30, int(self.research_refresh_seconds))
        next_formal = 0.0
        next_research = time.monotonic() + research_interval
        while not self.stop_event.is_set():
            now_mono = time.monotonic()
            if now_mono >= next_formal:
                self._sync_dynamic_services(utc_now())
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
                next_formal = now_mono + formal_interval
            if now_mono >= next_research:
                for service in self.services:
                    if self.stop_event.is_set():
                        break
                    try:
                        service.research_refresh(force=True)
                    except Exception as exc:
                        service._record_research_failure(utc_now(), exc)
                next_research = now_mono + research_interval
            delay = max(0.25, min(next_formal, next_research) - time.monotonic())
            boundary = self.discovery_status.get('next_boundary_utc')
            if boundary:
                delay = min(delay, max(0.1, (dt.datetime.fromisoformat(boundary) - utc_now()).total_seconds()))
            self.stop_event.wait(delay)

    def close(self) -> None:
        self.stop_event.set()
        if self.background_thread and self.background_thread.is_alive():
            self.background_thread.join(timeout=5)
        self.backfill_queue.put(None)
        if self.backfill_thread and self.backfill_thread.is_alive():
            self.backfill_thread.join(timeout=5)
        for service in self.services:
            service.close()
        for service in self.retired_services.values():
            service.close()

    def refresh(self) -> dict[str, Any]:
        if getattr(self, 'dynamic_discovery', False):
            snapshots = self.live_markets()
            if snapshots:
                return snapshots[0]
            service = None
        else:
            service = self.services[0] if self.services else next(iter(self.retired_services.values()), None)
        if service is None:
            return {
                "slug": "",
                "errors": ["no_live_market"],
                "recommendation": {"label": "不交易", "paper_direction": "观望", "do_not_auto_trade": True, "risk_reasons": ["no_live_market"]},
            }
        return service.refresh()

    def paper_history(self, limit: int = 100) -> list[dict[str, Any]]:
        service = self.services[0] if self.services else next(iter(self.retired_services.values()), None)
        return service.paper_history(limit) if service else []

    def live_archive(self, slug: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        service = self.services[0] if self.services else next(iter(self.retired_services.values()), None)
        return service.live_archive(slug, limit) if service else []

    def research_archive(self, slug: str | None = None, limit: int = 120) -> list[dict[str, Any]]:
        service = self.services[0] if self.services else next(iter(self.retired_services.values()), None)
        return service.research_archive(slug, limit) if service else []

    def research_status(self, slug: str | None = None) -> dict[str, Any]:
        service = self.services[0] if self.services else next(iter(self.retired_services.values()), None)
        if service:
            return service.research_status(slug)
        return {
            "slug": slug or "",
            "enabled": True,
            "target_interval_seconds": max(30, int(getattr(self.args, "research_refresh_seconds", 30))),
            "latest": None,
            "research_only": True,
            "do_not_auto_trade": True,
        }

    def research_refresh(self, force: bool = False) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        for service in list(self.services):
            try:
                snapshots.append(service.research_refresh(force=force))
            except Exception as exc:
                service._record_research_failure(utc_now(), exc)
                if service.last_research_snapshot is not None:
                    snapshots.append(service.last_research_snapshot)
        return snapshots

    def prediction_archive(self, slug: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        service = self.services[0] if self.services else next(iter(self.retired_services.values()), None)
        return service.prediction_archive(slug, limit) if service else []

    def local_markets(self, limit: int = 100) -> list[dict[str, Any]]:
        service = self.services[0] if self.services else next(iter(self.retired_services.values()), None)
        return service.local_markets(limit) if service else []

    def history_review(
        self,
        slug: str | None = None,
        limit: int = 100,
        half_life_days: float = 0,
        max_age_days: float = 365,
    ) -> dict[str, Any]:
        service = self.services[0] if self.services else next(iter(self.retired_services.values()), None)
        if service:
            return service.history_review(slug, limit, half_life_days, max_age_days)
        return {
            "schema_version": "history_review_v1", "slug": slug or "",
            "do_not_auto_trade": True,
            "coverage": {"market_found": False, "source_notes": ["no_live_market"]},
            "predictions": [], "prices": [], "orderbooks": [], "activity": [],
            "time_decay": {"status": "sensitivity_only_not_model_feature", "eligible_prior_markets": 0},
            "external_context": {"flight": {"status": "not_configured", "items": []}},
        }

    def backtest_review(self, **kwargs: Any) -> dict[str, Any]:
        service = self.services[0] if self.services else next(iter(self.retired_services.values()), None)
        if service:
            return service.backtest_review(**kwargs)
        from history_replay import local_review
        return local_review(self.args, **kwargs)

    def paper_grid_research(self, **kwargs: Any) -> dict[str, Any]:
        service = self.services[0] if self.services else next(iter(self.retired_services.values()), None)
        if service:
            return service.paper_grid_research(**kwargs)
        raise RuntimeError("no local market service available")

    def paper_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        service = self.services[0] if self.services else next(iter(self.retired_services.values()), None)
        return service.paper_trades(limit) if service else []

    def ai_status(self) -> dict[str, Any]:
        service = self.services[0] if self.services else next(iter(self.retired_services.values()), None)
        return service.ai_status() if service else {"configured": False}

    def ai_analyze(self) -> dict[str, Any]:
        service = self.services[0] if self.services else next(iter(self.retired_services.values()), None)
        return service.ai_analyze() if service else {"ok": False, "message": "无当前市场"}

    def discovery(self) -> dict[str, Any]:
        return json.loads(json.dumps(getattr(self, "discovery_status", {"state": "idle"}), ensure_ascii=False))

    def live_markets(self, force: bool = False) -> list[dict[str, Any]]:
        self._sync_dynamic_services(utc_now())
        snapshots: list[dict[str, Any]] = []
        for service in self.services:
            try:
                snapshot = dict(service.refresh(force=True) if force else service.refresh())
                if getattr(self, 'dynamic_discovery', False):
                    window = getattr(self, 'live_windows', {}).get(service.args.slug, {})
                    current = utc_now()
                    if not window.get('valid') or not (dt.datetime.fromisoformat(window['start_utc']) <= current < dt.datetime.fromisoformat(window['end_utc'])):
                        continue
                    snapshot['window'] = {**dict(snapshot.get('window') or {}), **window}
                snapshot["paper_trades"] = service.paper_trades(12)
                snapshot["market_discovery"] = {
                    **dict(snapshot.get("market_discovery") or {}),
                    **self.discovery(),
                }
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
    replay_jobs_lock = threading.Lock()

    def _replay_jobs(self):
        with self.replay_jobs_lock:
            if not hasattr(self.server, "replay_jobs"):
                self.server.replay_jobs = ReplayJobs(self.service.backtest_review)
            return self.server.replay_jobs

    def _paper_grid_jobs(self):
        with self.replay_jobs_lock:
            if not hasattr(self.server, "paper_grid_jobs"):
                self.server.paper_grid_jobs = ReplayJobs(self.service.paper_grid_research)
            return self.server.paper_grid_jobs

    def do_DELETE(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/backtest-jobs/") and self._local_origin_allowed():
            self._json_response(200, self._replay_jobs().status(path.rsplit("/", 1)[-1], cancel=True))
        elif path.startswith("/api/paper-grid-jobs/") and self._local_origin_allowed():
            self._json_response(200, self._paper_grid_jobs().status(path.rsplit("/", 1)[-1], cancel=True))
        else:
            self._json_response(403, {"ok": False, "message": "仅允许本机取消历史任务"})

    service: LiveService | MultiLiveService
    static_file: Path
    report_paths: dict[str, Path]
    history_sync: HistorySyncManager
    news_sync: NewsSyncManager
    data_organizer: Any

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
        if path.startswith("/api/backtest-jobs/"):
            self._json_response(200, {**self._replay_jobs().status(path.rsplit("/", 1)[-1]), "do_not_auto_trade": True})
            return
        if path.startswith("/api/paper-grid-jobs/"):
            self._json_response(200, {**self._paper_grid_jobs().status(path.rsplit("/", 1)[-1]), "do_not_auto_trade": True})
            return
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
                "market_discovery": self.service.discovery(),
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
        if path == "/api/data-organizer/status":
            self._json_response(200, {
                "ok": True,
                **self.data_organizer.status(),
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
        if path == "/api/market-archive":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                limit = int(query.get("limit", ["100"])[0])
                slug = query.get("slug", [None])[0]
                self._json_response(200, {
                    "ok": True,
                    "slug": slug,
                    "snapshots": self.service.live_archive(slug, limit),
                    "do_not_auto_trade": True,
                })
            except (TypeError, ValueError) as exc:
                self._json_response(400, {
                    "ok": False, "message": str(exc), "do_not_auto_trade": True,
                })
            return
        if path == "/api/research-capture/status":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                slug = query.get("slug", [None])[0]
                self._json_response(200, {
                    "ok": True,
                    **self.service.research_status(slug),
                    "do_not_auto_trade": True,
                })
            except (TypeError, ValueError) as exc:
                self._json_response(400, {"ok": False, "message": str(exc), "do_not_auto_trade": True})
            return
        if path == "/api/research-capture":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                limit = int(query.get("limit", ["120"])[0])
                slug = query.get("slug", [None])[0]
                self._json_response(200, {
                    "ok": True,
                    "slug": slug,
                    "schema_version": "research_capture_v1",
                    "research_only": True,
                    "snapshots": self.service.research_archive(slug, limit),
                    "do_not_auto_trade": True,
                })
            except (TypeError, ValueError) as exc:
                self._json_response(400, {"ok": False, "message": str(exc), "do_not_auto_trade": True})
            return
        if path == "/api/prediction-archive":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                limit = int(query.get("limit", ["100"])[0])
                slug = query.get("slug", [None])[0]
                self._json_response(200, {
                    "ok": True,
                    "slug": slug,
                    "schema_version": "prediction_archive_v1",
                    "records": self.service.prediction_archive(slug, limit),
                    "do_not_auto_trade": True,
                })
            except (TypeError, ValueError) as exc:
                self._json_response(400, {
                    "ok": False, "message": str(exc), "do_not_auto_trade": True,
                })
            return
        if path == "/api/local-markets":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                limit = int(query.get("limit", ["100"])[0])
                self._json_response(200, {
                    "ok": True,
                    "markets": (read_history_catalog(self.service.args.db, limit)
                                if query.get("view", [None])[0] == "history"
                                else self.service.local_markets(limit)),
                    "selection": "nearest_local_data_time",
                    "do_not_auto_trade": True,
                })
            except (TypeError, ValueError) as exc:
                self._json_response(400, {"ok": False, "message": str(exc), "do_not_auto_trade": True})
            return
        if path == "/api/history-review":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                slug = query.get("slug", [None])[0]
                limit = int(query.get("limit", ["200"])[0])
                half_life_days = float(query.get("half_life_days", ["0"])[0])
                max_age_days = float(query.get("max_age_days", ["365"])[0])
                self._json_response(200, {
                    "ok": True,
                    "review": self.service.history_review(slug, limit, half_life_days, max_age_days),
                    "do_not_auto_trade": True,
                })
            except (TypeError, ValueError) as exc:
                self._json_response(400, {
                    "ok": False, "message": str(exc), "do_not_auto_trade": True,
                })
            return
        if path == "/api/backtest-review":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                first = lambda key, default=None: query.get(key, [default])[0]
                def numeric_value(key: str, default: Any, cast: Any) -> Any:
                    value = first(key, default)
                    if value is None or str(value).strip().lower() in {"", "undefined", "null", "none"}:
                        return default
                    return cast(value)
                params = dict(
                    slug=first("slug"),
                    start=first("start"),
                    end=first("end"),
                    step_minutes=numeric_value("step_minutes", 5, int),
                    min_elapsed_minutes=numeric_value("min_elapsed_minutes", 5, int),
                    max_price_age_hours=numeric_value("max_price_age_hours", 24, float),
                    backtest_min_edge=numeric_value("backtest_min_edge", 0.08, float),
                    backtest_max_spread=numeric_value("backtest_max_spread", 0.04, float),
                    backtest_min_depth=numeric_value("backtest_min_depth", 3.0, float),
                    paper_fee_rate=(float(first("paper_fee_rate")) if first("paper_fee_rate") is not None else None),
                    paper_slippage_bps=(float(first("paper_slippage_bps")) if first("paper_slippage_bps") is not None else None),
                    paper_size=(float(first("paper_size")) if first("paper_size") is not None else None),
                    paper_market_budget=(float(first("paper_market_budget")) if first("paper_market_budget") is not None else None),
                    paper_mode=str(first("paper_mode", "legacy")),
                    paper_entry_checkpoint=first("paper_entry_checkpoint"),
                    paper_entry_elapsed_minutes=(float(first("paper_entry_elapsed_minutes")) if first("paper_entry_elapsed_minutes") is not None else None),
                    paper_exit_policy=str(first("paper_exit_policy", "managed")),
                    paper_hold_summary=str(first("paper_hold_summary", "0")) == "1",
                    paper_probability_coverage=numeric_value("paper_probability_coverage", DEFAULT_PROBABILITY_COVERAGE, float),
                    paper_capital_units=(float(first("paper_capital_units")) if first("paper_capital_units") is not None else None),
                    paper_take_profit_return=numeric_value("paper_take_profit_return", DEFAULT_TAKE_PROFIT_RETURN, float),
                    paper_stop_loss_return=numeric_value("paper_stop_loss_return", DEFAULT_STOP_LOSS_RETURN, float),
                    limit=numeric_value("limit", 10000, int),
                )
                if first("async") == "1":
                    self._json_response(202, self._replay_jobs().submit(
                        params, str(first("client_id") or "")[:80], numeric_value("generation", 0, int)))
                    return
                payload = self.service.backtest_review(**params)
                self._json_response(200, {"ok": True, **payload, "do_not_auto_trade": True})
            except (TypeError, ValueError) as exc:
                self._json_response(400, {"ok": False, "message": str(exc), "do_not_auto_trade": True})
            except Exception as exc:
                self._json_response(500, {"ok": False, "message": f"{type(exc).__name__}: {exc}", "do_not_auto_trade": True})
            return
        if path == "/api/paper-grid-research":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                first = lambda key, default=None: query.get(key, [default])[0]
                def csv_values(key: str, cast: Any, default: tuple[Any, ...]) -> tuple[Any, ...]:
                    raw = first(key)
                    if raw is None or not str(raw).strip():
                        return default
                    values = tuple(cast(item.strip()) for item in str(raw).split(",") if item.strip())
                    if not values:
                        raise ValueError(f"{key} must not be empty")
                    return values
                params = {
                    "entry_minutes": csv_values("entry_minutes", int, DEFAULT_ENTRY_MINUTES),
                    "coverage_grid": csv_values("coverage_grid", float, DEFAULT_COVERAGE_GRID),
                    "market_budget": float(first("market_budget", DEFAULT_MARKET_BUDGET)),
                }
                if len(params["entry_minutes"]) * len(params["coverage_grid"]) > 64:
                    raise ValueError("research grid is limited to 64 combinations per request")
                if first("async") == "1":
                    status = self._paper_grid_jobs().submit(
                        params, str(first("client_id") or "")[:80], int(first("generation", "0")))
                    self._json_response(202, {**status, "do_not_auto_trade": True})
                else:
                    self._json_response(200, {"ok": True, **self.service.paper_grid_research(**params), "do_not_auto_trade": True})
            except (TypeError, ValueError) as exc:
                self._json_response(400, {"ok": False, "message": str(exc), "do_not_auto_trade": True})
            except Exception as exc:
                self._json_response(500, {"ok": False, "message": f"{type(exc).__name__}: {exc}", "do_not_auto_trade": True})
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
        if path in {"/api/data-organizer/scan", "/api/data-organizer/consolidate", "/api/data-organizer/delete"}:
            if not self._local_origin_allowed():
                self._json_response(403, {"ok": False, "message": "仅允许本地网页整理本地数据文件", "do_not_auto_trade": True})
                return
            try:
                payload = self._json_body() if self.headers.get("Content-Length") else {}
                action = "delete" if path.endswith("/delete") else ("consolidate" if path.endswith("/consolidate") else "scan")
                result = self.data_organizer.run(action, payload)
                self._json_response(200, {"ok": True, **result, "do_not_auto_trade": True})
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                self._json_response(400, {"ok": False, "message": str(exc), "do_not_auto_trade": True})
            return
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
        if path == "/api/research-capture/now" and not self._local_origin_allowed():
            self._json_response(403, {"ok": False, "message": "只允许本机网页访问本地研究采样接口", "do_not_auto_trade": True})
            return
        if path == "/api/research-capture/now":
            try:
                self._json_body()
                self._json_response(200, {
                    "ok": True,
                    "research_only": True,
                    "snapshots": self.service.research_refresh(force=True),
                    "do_not_auto_trade": True,
                })
            except Exception as exc:
                self._json_response(503, {
                    "ok": False,
                    "message": f"研究采样失败：{type(exc).__name__}: {exc}",
                    "research_only": True,
                    "do_not_auto_trade": True,
                })
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
    parser.add_argument("--slug", default="", help="Explicit historical/live slug; omit with --dynamic-discovery.")
    parser.add_argument(
        "--extra-slug",
        action="append",
        default=[],
        help="Add another market to the isolated live-market selector; repeatable.",
    )
    parser.add_argument(
        "--dynamic-discovery",
        action="store_true",
        help="Re-query Gamma during runtime and synchronize active market slugs.",
    )
    parser.add_argument(
        "--no-background",
        action="store_true",
        help="Serve local read-only APIs without starting the periodic refresh thread.",
    )
    parser.add_argument("--db", default="data/extended.sqlite")
    parser.add_argument("--model", default="data/extended_model_v4.json")
    parser.add_argument("--public-report", default="data/hourly_extended_v4_fullprob_public.csv")
    parser.add_argument("--private-report", default="data/hourly_extended_v4_fullprob.csv")
    parser.add_argument("--summary-report", default="data/hourly_extended_v4_fullprob_summary.csv")
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--refresh-seconds", type=int, default=300,
        help="Live capture interval in seconds; default 300 (five minutes).",
    )
    parser.add_argument(
        "--research-refresh-seconds", type=int, default=30,
        help="Descriptive research capture interval; minimum 30 seconds, separate from five-minute archive/decisions.",
    )
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
    # Five minutes is the minimum live polling interval.  This protects the
    # public endpoints even when an operator passes a shorter CLI value.
    args.refresh_seconds = max(300, int(args.refresh_seconds))
    args.research_refresh_seconds = max(30, int(args.research_refresh_seconds))
    slugs = list(dict.fromkeys([slug for slug in [args.slug, *args.extra_slug] if slug]))
    if args.dynamic_discovery and not slugs:
        # Start immediately with no selected market.  The background discovery
        # loop performs the first Gamma query and retries on proxy/API delays;
        # this prevents a slow external request from blocking the local UI and
        # never falls back to a stale or date-generated slug.
        slugs = []
    service: LiveService | MultiLiveService = (
        MultiLiveService(args, slugs)
        if len(slugs) > 1 or bool(getattr(args, "dynamic_discovery", False))
        else LiveService(args)
    )
    database_lock = service.database_lock if isinstance(service, MultiLiveService) else service.lock
    report_paths = {
        "public": Path(args.public_report).resolve(),
        "private": Path(args.private_report).resolve(),
        "summary": Path(args.summary_report).resolve(),
    }
    history_sync = HistorySyncManager(args, database_lock, report_paths)
    news_sync = NewsSyncManager(args, database_lock)
    project_root = Path(__file__).resolve().parent
    data_organizer = DataOrganizer(project_root, project_root / "data")
    if not getattr(args, "no_background", False):
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
            "data_organizer": data_organizer,
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
