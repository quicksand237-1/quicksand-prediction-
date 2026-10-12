import argparse
import datetime as dt
import json
import tempfile
import threading
import urllib.error
import unittest
from unittest import mock
from pathlib import Path

from musk_tracker import Store as PostStore, normalize_post
from polymarket_pipeline import (
    Example,
    GammaClient,
    MarketStore,
    PostObservation,
    Window,
    hourly_checkpoints,
    count_distribution,
    market_distribution,
    negative_binomial_pmf,
    outcome_label_for_count,
    parse_count_market_window,
    price_distribution_is_valid,
    split_examples_purged,
    training_label_for_window,
    write_hourly_test_reports,
)
from live_dashboard_server import (
    HistorySyncManager,
    LiveService,
    MultiLiveService,
    NewsSyncManager,
    build_parser,
    paper_buy_cost,
    paper_position_rebalance,
    parse_levels,
    public_report_is_safe,
)
from forecast_contracts import (
    build_decision_output,
    build_display_snapshot,
    build_feature_snapshot,
    build_prediction_output,
)
from strategy_simulator import (
    DEFAULT_ENTRY_CHECKPOINTS,
    DEFAULT_ENTRY_EDGE,
    DEFAULT_EXIT_EDGE,
    DEFAULT_RETAIN_EDGE,
    paper_position_rebalance as isolated_paper_position_rebalance,
)


class ForecastContractTests(unittest.TestCase):
    def test_contracts_are_versioned_and_preserve_safety_invariant(self) -> None:
        features = build_feature_snapshot(
            slug="market-a",
            as_of_utc="2026-08-30T00:00:00+00:00",
            features={"current_count": 4},
            market_coordinates={"elapsed_hours": 12},
            calendar_coordinates={"weekday": 6},
        )
        prediction = build_prediction_output(
            slug="market-a",
            as_of_utc=features["as_of_utc"],
            prediction={"final_count": 20},
            feature_snapshot=features,
        )
        decision = build_decision_output(
            slug="market-a",
            as_of_utc=features["as_of_utc"],
            decision={"label": "不交易", "do_not_auto_trade": False},
        )
        display = build_display_snapshot({
            "slug": "market-a",
            "generated_at_utc": features["as_of_utc"],
            "prediction": prediction["result"],
            "recommendation": decision["result"],
            "feature_snapshot": features,
            "prediction_output": prediction,
            "decision_output": decision,
        })
        self.assertEqual(features["schema_version"], "feature_snapshot_v1")
        self.assertEqual(prediction["schema_version"], "prediction_output_v1")
        self.assertEqual(decision["schema_version"], "decision_output_v1")
        self.assertEqual(display["schema_version"], "display_snapshot_v1")
        self.assertTrue(display["recommendation"]["do_not_auto_trade"])
        self.assertEqual(display["news"], [])
        self.assertEqual(display["market"]["outcomes"], [])



class HistorySyncTests(unittest.TestCase):
    def test_partial_price_coverage_preserves_reports(self):
        manager=HistorySyncManager(argparse.Namespace(),threading.RLock(),{},crawl_fn=lambda *args:{'status':'partial','source_errors':[{'source':'clob_history_coverage','message':'0/2016'}],'price_coverage':[{'status':'empty','covered_buckets':0,'expected_buckets':2016}]})
        args=argparse.Namespace(orderbooks=False,price_fidelity=5,price_interval='max',skip_prices=False)
        with mock.patch('live_dashboard_server.hourly_test_command') as reports:
            manager._run(args,['market'],True)
            reports.assert_not_called()
        result=manager.status()
        self.assertEqual(result['state'],'partial')
        self.assertEqual(result['results'][0]['price_coverage'][0]['covered_buckets'],0)

    def test_discovery_failure_is_not_generated_slug_batch(self):
        args = argparse.Namespace(timezone='America/New_York', gamma_url='https://example.test',timeout=1,proxy=None)
        manager=HistorySyncManager(args,threading.RLock(),{})
        with mock.patch('live_dashboard_server.GammaClient') as client:
            client.return_value.discover_count_events.side_effect=TimeoutError('discovery timeout')
            with self.assertRaisesRegex(ValueError,'Gamma'):
                manager._slugs_for_range('2026-08-01','2026-08-20')

    def test_all_failed_does_not_rebuild_reports(self):
        def fail(*args):
            raise ValueError('bad market')
        manager=HistorySyncManager(argparse.Namespace(),threading.RLock(),{},crawl_fn=fail)
        with mock.patch('live_dashboard_server.hourly_test_command') as reports:
            manager._run(argparse.Namespace(orderbooks=False),['a','b'],True)
            reports.assert_not_called()
        self.assertEqual(manager.status()['state'],'failed')
        self.assertEqual(manager.status()['failed'],2)

    def test_history_report_args_include_backtest_fields(self) -> None:
        args = argparse.Namespace(
            db="data/test.sqlite", model="data/model.json", user="u", platform="p",
            timezone="UTC", gamma_url="https://gamma.test", clob_url="https://clob.test",
            tracker_url="https://tracker.test", proxy=None, timeout=5, book_workers=1,
        )
        manager = HistorySyncManager(args, threading.RLock(), {
            "public": Path("public.csv"), "private": Path("private.csv"),
            "summary": Path("summary.csv"),
        })
        report_args = manager._report_args()
        for name in ("step_minutes", "min_elapsed_minutes", "backtest_min_edge",
                     "backtest_max_spread", "backtest_min_depth"):
            self.assertTrue(hasattr(report_args, name), name)

    def test_explicit_history_sync_uses_sequential_crawl_arguments(self) -> None:
        calls = []

        def fake_crawl(args, slug):
            calls.append((slug, args.skip_prices, args.price_interval, args.orderbooks))
            return {"new_posts_inserted": 2, "new_price_rows": 0, "source_errors": []}

        args = argparse.Namespace(
            db="ignored.sqlite",
            user="elonmusk",
            platform="X",
            timezone="America/New_York",
            gamma_url="https://gamma.example.test",
            clob_url="https://clob.example.test",
            tracker_url="https://tracker.example.test",
            proxy=None,
            timeout=10,
            book_workers=4,
        )
        manager = HistorySyncManager(
            args,
            threading.RLock(),
            {
                "public": Path("public.csv"),
                "private": Path("private.csv"),
                "summary": Path("summary.csv"),
            },
            crawl_fn=fake_crawl,
            slug_generator=lambda start, end, timezone: ["market-one", "market-two"],
        )
        started = manager.start({
            "start": "2026-01-01",
            "end": "2026-01-10",
            "include_prices": False,
            "price_interval": "1d",
            "rebuild_reports": False,
        })
        self.assertEqual(started["state"], "running")
        assert manager.thread is not None
        manager.thread.join(timeout=2)
        status = manager.status()
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["succeeded"], 2)
        self.assertEqual(calls, [("market-one", True, "1d", False), ("market-two", True, "1d", False)])

    def test_history_sync_uses_fixed_five_minute_price_fidelity(self) -> None:
        calls = []
        def fake_crawl(args, slug):
            calls.append((slug, args.skip_prices, args.price_interval, args.price_fidelity, args.orderbooks))
            return {"new_posts_inserted": 0, "new_price_rows": 1, "source_errors": []}
        args = argparse.Namespace(
            db="ignored.sqlite", user="u", platform="p", timezone="UTC",
            gamma_url="https://gamma.example.test", clob_url="https://clob.example.test",
            tracker_url="https://tracker.example.test", proxy=None, timeout=5, book_workers=1,
            model="model.json",
        )
        manager = HistorySyncManager(
            args, threading.RLock(), {"public": Path("public.csv"), "private": Path("private.csv"), "summary": Path("summary.csv")},
            crawl_fn=fake_crawl, slug_generator=lambda start, end, timezone: ["market-one"],
        )
        manager.start({"start": "2026-01-01", "end": "2026-01-10", "include_prices": True, "price_interval": "1w", "price_fidelity_minutes": 5, "rebuild_reports": False})
        assert manager.thread is not None
        manager.thread.join(timeout=2)
        self.assertEqual(calls, [("market-one", False, "1w", 5, False)])

    def test_history_sync_can_opt_in_to_future_orderbook_snapshots(self) -> None:
        calls = []

        def fake_crawl(args, slug):
            calls.append((slug, args.skip_prices, args.orderbooks))
            self.assertEqual(args.price_fidelity, 60)
            return {"new_posts_inserted": 0, "new_price_rows": 0, "new_orderbook_rows": 2, "source_errors": []}

        args = argparse.Namespace(
            db="ignored.sqlite", user="u", platform="p", timezone="UTC",
            gamma_url="https://gamma.example.test", clob_url="https://clob.example.test",
            tracker_url="https://tracker.example.test", proxy=None, timeout=5, book_workers=1,
        )
        manager = HistorySyncManager(
            args, threading.RLock(), {"public": Path("public.csv"), "private": Path("private.csv"), "summary": Path("summary.csv")},
            crawl_fn=fake_crawl, slug_generator=lambda start, end, timezone: ["market-one"],
        )
        started = manager.start({
            "start": "2026-01-01", "end": "2026-01-10", "include_orderbooks": True,
            "rebuild_reports": False,
        })
        self.assertTrue(started["include_orderbooks"])
        assert manager.thread is not None
        manager.thread.join(timeout=2)
        self.assertEqual(calls, [("market-one", False, True)])
        self.assertEqual(manager.status()["results"][0]["new_orderbook_rows"], 2)

    def test_history_sync_rejects_unsupported_fidelity(self) -> None:
        args = argparse.Namespace(
            db="ignored.sqlite", user="u", platform="p", timezone="UTC",
            gamma_url="https://gamma.example.test", clob_url="https://clob.example.test",
            tracker_url="https://tracker.example.test", proxy=None, timeout=5, book_workers=1,
        )
        manager = HistorySyncManager(args, threading.RLock(), {"public": Path("public.csv"), "private": Path("private.csv"), "summary": Path("summary.csv")}, slug_generator=lambda start, end, timezone: ["market-one"])
        with self.assertRaises(ValueError):
            manager.start({"start": "2026-01-01", "end": "2026-01-10", "include_prices": True, "price_interval": "1w", "price_fidelity_minutes": 30, "rebuild_reports": False})

    def test_history_sync_rejects_invalid_dates_before_starting_worker(self) -> None:
        args = argparse.Namespace(
            db="ignored.sqlite", user="elonmusk", platform="X", timezone="America/New_York",
            gamma_url="https://gamma.example.test", clob_url="https://clob.example.test",
            tracker_url="https://tracker.example.test", proxy=None, timeout=10, book_workers=4,
        )
        manager = HistorySyncManager(args, threading.RLock(), {
            "public": Path("public.csv"),
            "private": Path("private.csv"),
            "summary": Path("summary.csv"),
        })
        with self.assertRaises(ValueError):
            manager.start({"start": "2026-01-10", "end": "2026-01-10"})

    def test_history_sync_allows_client_date_one_day_ahead_of_service_timezone(self) -> None:
        args = argparse.Namespace(
            db="ignored.sqlite", user="elonmusk", platform="X", timezone="America/New_York",
            gamma_url="https://gamma.example.test", clob_url="https://clob.example.test",
            tracker_url="https://tracker.example.test", proxy=None, timeout=10, book_workers=4,
        )
        calls = []

        def fake_crawl(crawl_args, slug):
            calls.append(slug)
            return {"new_posts_inserted": 0, "new_price_rows": 0, "source_errors": []}

        manager = HistorySyncManager(
            args,
            threading.RLock(),
            {"public": Path("public.csv"), "private": Path("private.csv"), "summary": Path("summary.csv")},
            crawl_fn=fake_crawl,
            slug_generator=lambda start, end, timezone: ["market"],
        )
        service_now = dt.datetime(2026, 9, 1, 21, tzinfo=dt.timezone.utc)
        with mock.patch("live_dashboard_server.utc_now", return_value=service_now):
            started = manager.start({
                "start": "2026-08-01", "end": "2026-09-02", "rebuild_reports": False,
            })
            self.assertEqual(started["state"], "running")
            assert manager.thread is not None
            manager.thread.join(timeout=2)
            self.assertEqual(manager.status()["state"], "succeeded")
        self.assertEqual(calls, ["market"])

    def test_history_sync_uses_gamma_slugs_instead_of_date_generated_404_slugs(self) -> None:
        args = argparse.Namespace(
            db="ignored.sqlite", user="elonmusk", platform="X", timezone="America/New_York",
            gamma_url="https://gamma.example.test", clob_url="https://clob.example.test",
            tracker_url="https://tracker.example.test", proxy=None, timeout=10, book_workers=4,
            model="model.json",
        )
        calls = []

        def fake_crawl(crawl_args, slug):
            calls.append(slug)
            return {"new_posts_inserted": 0, "new_price_rows": 0, "source_errors": []}

        class FakeGamma:
            def __init__(self, *args, **kwargs):
                pass

            def discover_count_events(self, *args, **kwargs):
                return [
                    {"slug": "elon-musk-of-tweets-august-21-august-28-2026"},
                    {"slug": "elon-musk-of-tweets-august-28-september-4-2026"},
                ]

        manager = HistorySyncManager(
            args, threading.RLock(),
            {"public": Path("public.csv"), "private": Path("private.csv"), "summary": Path("summary.csv")},
            crawl_fn=fake_crawl,
        )
        with mock.patch("live_dashboard_server.GammaClient", FakeGamma):
            started = manager.start({
                "start": "2026-08-21", "end": "2026-09-03", "rebuild_reports": False,
            })
            self.assertEqual(started["state"], "running")
            assert manager.thread is not None
            manager.thread.join(timeout=2)
        self.assertEqual(calls, [
            "elon-musk-of-tweets-august-21-august-28-2026",
            "elon-musk-of-tweets-august-28-september-4-2026",
        ])

    def test_history_sync_rejects_dates_more_than_one_day_ahead(self) -> None:
        args = argparse.Namespace(
            db="ignored.sqlite", user="elonmusk", platform="X", timezone="America/New_York",
            gamma_url="https://gamma.example.test", clob_url="https://clob.example.test",
            tracker_url="https://tracker.example.test", proxy=None, timeout=10, book_workers=4,
        )
        manager = HistorySyncManager(args, threading.RLock(), {
            "public": Path("public.csv"), "private": Path("private.csv"), "summary": Path("summary.csv"),
        })
        service_now = dt.datetime(2026, 9, 1, 21, tzinfo=dt.timezone.utc)
        with mock.patch("live_dashboard_server.utc_now", return_value=service_now):
            with self.assertRaisesRegex(ValueError, "次日"):
                manager.start({"start": "2026-08-01", "end": "2026-09-03"})

    def test_news_sync_rejects_multiline_contact_without_starting_request(self) -> None:
        args = argparse.Namespace(db="ignored.sqlite", news_sources="missing.json")
        manager = NewsSyncManager(args, threading.RLock())
        with self.assertRaises(ValueError):
            manager.start({"contact": "name@example.com\nInjected: value"})
        self.assertEqual(manager.status()["state"], "idle")

    def test_fixed_reports_are_backed_up_before_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public.csv"
            public.write_text("old-public-report", encoding="utf-8")
            manager = HistorySyncManager(
                argparse.Namespace(db=str(root / "market.sqlite")),
                threading.RLock(),
                {"public": public, "private": root / "private.csv", "summary": root / "summary.csv"},
            )
            backups = manager._backup_reports()
            self.assertEqual(len(backups), 1)
            self.assertTrue(Path(backups[0]).is_file())
            self.assertEqual(Path(backups[0]).read_text(encoding="utf-8"), "old-public-report")
            self.assertEqual(public.read_text(encoding="utf-8"), "old-public-report")


class MarketWindowTests(unittest.TestCase):
    def test_rule_text_overrides_event_creation_time(self) -> None:
        event = {
            "slug": "elon-musk-of-tweets-february-10-february-17",
            "startDate": "2026-02-07T05:11:27Z",
            "endDate": "2026-02-17T17:00:00Z",
            "description": (
                "This market will resolve according to posts on X from February 10 "
                "12:00 PM ET to February 17, 2026 12:00 PM ET."
            ),
            "markets": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite")
            try:
                window = store.save_event(event, "America/New_York")
            finally:
                store.close()

        self.assertEqual(window.start_utc, dt.datetime(2026, 2, 10, 17, tzinfo=dt.timezone.utc))
        self.assertEqual(window.end_utc, dt.datetime(2026, 2, 17, 17, tzinfo=dt.timezone.utc))

    def test_rule_text_respects_daylight_saving_time(self) -> None:
        window = parse_count_market_window(
            "from July 28 12:00 PM ET to August 4, 2026 12:00 PM ET.",
            "America/New_York",
        )
        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual(window[0], dt.datetime(2026, 7, 28, 16, tzinfo=dt.timezone.utc))
        self.assertEqual(window[1], dt.datetime(2026, 8, 4, 16, tzinfo=dt.timezone.utc))


class DistributionTests(unittest.TestCase):
    def test_distribution_normalizes_incomplete_price_mass(self) -> None:
        expected, mass, entropy, maximum = market_distribution([(0, 19, 0.2), (20, 39, 0.6)])
        self.assertAlmostEqual(expected, 24.5)
        self.assertAlmostEqual(mass, 0.8)
        self.assertGreater(entropy, 0)
        self.assertAlmostEqual(maximum, 0.75)

    def test_open_ended_outcome_uses_one_bucket_tail_midpoint(self) -> None:
        expected, _, _, _ = market_distribution([(500, None, 1.0)])
        self.assertEqual(expected, 510.0)

    def test_probability_mass_rejects_oversized_distribution(self) -> None:
        self.assertFalse(price_distribution_is_valid(6.8795))
        self.assertTrue(price_distribution_is_valid(0.996))

    def test_count_distribution_is_normalized(self) -> None:
        ranges = [("<20", 0, 19), ("20-39", 20, 39), ("40+", 40, None)]
        probabilities = count_distribution(30.0, 180.0, ranges)
        self.assertAlmostEqual(sum(probabilities), 1.0, places=6)
        self.assertGreater(probabilities[1], probabilities[0])
        self.assertGreater(negative_binomial_pmf(30, 30.0, 180.0), 0.0)


class SplitTests(unittest.TestCase):
    def test_overlapping_train_window_is_purged(self) -> None:
        base = dt.datetime(2026, 2, 10, 17, tzinfo=dt.timezone.utc)
        starts = (0, 3, 7, 10)
        examples = []
        for index, offset in enumerate(starts):
            window = Window(
                f"market-{index}",
                base + dt.timedelta(days=offset),
                base + dt.timedelta(days=offset + 7),
            )
            examples.append(Example(window, window.start_utc, [0.0], 100.0, 0))

        train, test, purged = split_examples_purged(examples, 0.75)
        self.assertEqual([example.window.slug for example in train], ["market-0", "market-1"])
        self.assertEqual([example.window.slug for example in test], ["market-3"])
        self.assertEqual(purged, ["market-2"])


class HourlyReplayTests(unittest.TestCase):
    def test_public_report_preserves_full_bin_probabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = {
                "slug": "market",
                "checkpoint_utc": "2026-08-01T00:00:00+00:00",
                "forecast_bin": "20-39",
                "bin_probabilities_json": '{"<20": 0.2, "20-39": 0.8}',
            }
            write_hourly_test_reports(
                root / "private.csv",
                root / "summary.csv",
                [row],
                [],
                root / "public.csv",
            )
            public = (root / "public.csv").read_text(encoding="utf-8")
            self.assertIn("bin_probabilities_json", public.splitlines()[0])
            self.assertIn("20-39", public)

    def test_hourly_checkpoints_start_after_minimum_elapsed_time(self) -> None:
        start = dt.datetime(2026, 7, 28, 16, tzinfo=dt.timezone.utc)
        window = Window("market", start, start + dt.timedelta(hours=4))
        checkpoints = hourly_checkpoints(window, 1)
        self.assertEqual(
            checkpoints,
            [
                start + dt.timedelta(hours=1),
                start + dt.timedelta(hours=2),
                start + dt.timedelta(hours=3),
            ],
        )

    def test_forecast_maps_to_market_bin(self) -> None:
        ranges = [("<20", 0, 19), ("20-39", 20, 39), ("40+", 40, None)]
        self.assertEqual(outcome_label_for_count(19.6, ranges), "20-39")
        self.assertEqual(outcome_label_for_count(75.0, ranges), "40+")

    def test_resolved_bin_midpoint_can_label_unverified_history(self) -> None:
        class FakeStore:
            def final_count(self, window, user, platform):
                return 213

            def final_count_is_verified(self, window, user, platform):
                return False

            def resolved_range(self, slug):
                return (220, 239)

        start = dt.datetime(2026, 7, 28, 16, tzinfo=dt.timezone.utc)
        window = Window("market", start, start + dt.timedelta(days=7))
        label, source = training_label_for_window(
            FakeStore(), window, "elonmusk", "X", True, True
        )
        self.assertEqual(label, 229.5)
        self.assertEqual(source, "resolved_bin_midpoint")


class SourceEventTests(unittest.TestCase):
    def write_jsonl(self, directory: str, name: str, rows: list[dict]) -> Path:
        path = Path(directory) / name
        path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        return path

    def test_source_event_is_bounded_and_available_only_after_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "events.sqlite")
            try:
                events = self.write_jsonl(directory, "events.jsonl", [{
                    "id": "tesla-launch-1",
                    "timestamp": "2026-07-30T13:00:00Z",
                    "kind": "launch",
                    "direction": "up",
                    "relevance": 1.0,
                    "ai_confidence": 1.0,
                    "impact": 99,
                    "summary": "A test event.",
                    "evidence": [{
                        "source_id": "tesla_ir",
                        "url": "https://www.tesla.com/blog/test",
                        "published_at": "2026-07-30T13:00:00Z",
                        "retrieved_at": "2026-07-30T13:05:00Z",
                        "title": "Test",
                        "excerpt": "Evidence",
                    }],
                }])
                result = store.save_source_events(events, "America/New_York")
                self.assertEqual(result["source_events_written"], 1)
                event = store.conn.execute(
                    "select status, bounded_impact, confidence_cap from source_events where event_key = ?",
                    ("tesla-launch-1",),
                ).fetchone()
                self.assertEqual(event["status"], "approved")
                self.assertLessEqual(event["bounded_impact"], 2.0)
                self.assertLessEqual(event["confidence_cap"], 0.9)
                self.assertEqual(
                    len(store.events_before(
                        dt.datetime(2026, 7, 28, tzinfo=dt.timezone.utc),
                        dt.datetime(2026, 7, 31, tzinfo=dt.timezone.utc),
                    )),
                    1,
                )
                self.assertEqual(
                    store.events_before(
                        dt.datetime(2026, 7, 28, tzinfo=dt.timezone.utc),
                        dt.datetime(2026, 7, 30, 1, tzinfo=dt.timezone.utc),
                    ),
                    [],
                )
            finally:
                store.close()

    def test_invalid_domain_and_conflicting_evidence_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "events.sqlite")
            try:
                events = self.write_jsonl(directory, "events.jsonl", [
                    {
                        "id": "bad-domain",
                        "timestamp": "2026-07-30T13:00:00Z",
                        "kind": "news",
                        "direction": "up",
                        "relevance": 0.9,
                        "ai_confidence": 0.9,
                        "evidence": [{
                            "source_id": "reuters",
                            "url": "https://example.com/not-reuters",
                            "published_at": "2026-07-30T13:00:00Z",
                        }],
                    },
                    {
                        "id": "conflict",
                        "timestamp": "2026-07-30T14:00:00Z",
                        "kind": "policy",
                        "direction": "up",
                        "relevance": 0.9,
                        "ai_confidence": 0.9,
                        "evidence": [
                            {
                                "source_id": "reuters",
                                "url": "https://www.reuters.com/world/test",
                                "direction": "up",
                                "published_at": "2026-07-30T14:00:00Z",
                            },
                            {
                                "source_id": "ap",
                                "url": "https://apnews.com/article/test",
                                "direction": "down",
                                "published_at": "2026-07-30T14:00:00Z",
                            },
                        ],
                    },
                ])
                result = store.save_source_events(events, "America/New_York")
                self.assertEqual(result["source_events_rejected"], 2)
                statuses = {
                    row["event_key"]: row["status"]
                    for row in store.conn.execute(
                        "select event_key, status from source_events"
                    ).fetchall()
                }
                self.assertEqual(statuses, {
                    "bad-domain": "rejected",
                    "conflict": "rejected",
                })
                self.assertEqual(
                    store.events_before(
                        dt.datetime(2026, 7, 28, tzinfo=dt.timezone.utc),
                        dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
                    ),
                    [],
                )
            finally:
                store.close()


class LiveDashboardTests(unittest.TestCase):
    def test_market_windows_are_capture_time_based_and_half_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "boundary.sqlite")
            try:
                slug = "boundary-market"
                start = dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc)
                store.conn.execute(
                    "insert into market_outcomes(event_slug, market_id, label, lower_count, upper_count, raw_json) values (?, ?, ?, ?, ?, ?)",
                    (slug, "m1", "0-19", 0, 19, "{}"),
                )
                for captured in (start, start + dt.timedelta(minutes=5)):
                    value = captured.isoformat()
                    store.conn.execute(
                        "insert into market_prices(event_slug, market_id, token_id, price_time_utc, price, captured_at_utc,resolution_seconds) values (?, ?, ?, ?, ?, ?, ?)",
                        (slug, "m1", "t1", value, 0.4, value,300),
                    )
                store.conn.commit()
                rows = store.prices_in_window(slug, start + dt.timedelta(minutes=10), 24, 5)
                self.assertEqual(rows[0]["window_sample_count"], 1)
                self.assertEqual(rows[0]["captured_at_utc"], (start + dt.timedelta(minutes=5)).isoformat())
            finally:
                store.close()

    def test_backfilled_five_minute_price_is_review_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "backfill-price.sqlite")
            try:
                slug = "backfilled-price-market"
                checkpoint = dt.datetime(2026, 8, 20, 0, 10, tzinfo=dt.timezone.utc)
                store.conn.execute(
                    "insert into market_outcomes(event_slug, market_id, label, lower_count, upper_count, raw_json) values (?, ?, ?, ?, ?, ?)",
                    (slug, "m1", "0-19", 0, 19, "{}"),
                )
                store.conn.execute(
                    "insert into market_prices(event_slug, market_id, token_id, price_time_utc, price, captured_at_utc) values (?, ?, ?, ?, ?, ?)",
                    (slug, "m1", "t1", (checkpoint - dt.timedelta(minutes=2)).isoformat(), 0.4, (checkpoint + dt.timedelta(days=1)).isoformat()),
                )
                store.conn.commit()
                self.assertEqual(store.prices_in_window(slug, checkpoint, 24, 5), [])
                rows = store.historical_prices_in_window(slug, checkpoint, 5)
                self.assertEqual(rows[0]["price"], 0.4)
                self.assertTrue(rows[0]["backfilled_after_checkpoint"])
                self.assertTrue(rows[0]["review_only"])
                self.assertFalse(rows[0]["point_in_time_safe"])
            finally:
                store.close()

    def test_backtest_run_identity_and_cost_gate_are_stable(self) -> None:
        start = dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc)
        window = Window("backtest-market", start, start + dt.timedelta(hours=2))

        class FakeStore:
            price_mass = 1.0

            def windows(self, start_bound=None, end_bound=None):
                return [window]

            def post_observations(self, user, platform):
                return []

            def outcome_ranges(self, slug):
                return [("0-19", 0, 19), ("20+", 20, None)]

            def resolved_range(self, slug):
                return (0, 19)

            def final_count(self, current_window, user, platform):
                return 10

            def prices_at(self, slug, checkpoint, max_age_hours):
                return [
                    {
                        "lower_count": 0, "upper_count": 19, "price": 0.5,
                        "outcome_label": "0-19", "source_price_time_utc": checkpoint.isoformat(),
                    },
                    {
                        "lower_count": 20, "upper_count": None, "price": 0.5 * self.price_mass,
                        "outcome_label": "20+", "source_price_time_utc": checkpoint.isoformat(),
                    },
                ]

            def orderbooks_at(self, slug, checkpoint, max_age_hours):
                return [{
                    "outcome_label": "0-19", "best_bid": 0.48, "best_ask": 0.5,
                    "spread": 0.02, "ask_depth_5": 0.5, "captured_at_utc": checkpoint.isoformat(),
                    "point_in_time_safe": True,
                }]

            def events_before(self, window_start, checkpoint):
                return []

            def detailed_events_before(self, window_start, checkpoint):
                return []

        class FakeModel:
            def predict(self, features):
                return 10.0

        service = LiveService.__new__(LiveService)
        service.args = argparse.Namespace(
            timezone="America/New_York", user="elonmusk", platform="X",
            paper_fee_rate=0.02, paper_slippage_bps=50.0, paper_size=1.0,
        )
        service.lock = threading.RLock()
        service.store = FakeStore()
        service.model = FakeModel()
        service.model_payload = {"model": "test-fixed-model", "version": 1}
        first = service.backtest_review(step_minutes=60, min_elapsed_minutes=60)
        second = service.backtest_review(step_minutes=60, min_elapsed_minutes=60)
        record = first["records"][0]
        self.assertTrue(first["do_not_auto_trade"])
        self.assertEqual(first["backtest_run_id"], second["backtest_run_id"])
        self.assertLess(record["paper_net_edge_after_cost"], record["model_market_edge"])
        self.assertEqual(record["estimated_fill_fraction"], 0.5)
        self.assertEqual(record["decision_status"], "no_trade_quality_or_edge")
        self.assertEqual([row["outcome_label"] for row in record["outcome_market_rows"]], ["0-19", "20+"])
        self.assertIsNone(record["outcome_market_rows"][1]["best_ask"])
        self.assertEqual(record["interval_minutes"], 60)
        self.assertEqual(record["post_count_in_interval"], 0)
        self.assertFalse(first["coverage"]["truncated"])
        self.assertEqual(first["calibration"]["resolved_checkpoint_records"], 1)
        self.assertEqual(
            first["summary"]["market_weighted_mean_brier_score"],
            first["markets"][0]["mean_brier_score"],
        )
        self.assertIsNotNone(record["local_pace_brier_score"])
        self.assertIsNotNone(record["market_brier_score"])
        self.assertIsNotNone(record["model_vs_local_pace_brier_delta"])
        self.assertIsNotNone(record["model_vs_market_brier_delta"])
        self.assertEqual(first["data_quality"]["paired_model_local_pace_score_records"], 1)
        self.assertEqual(first["data_quality"]["paired_model_market_score_records"], 1)
        def review_prices(slug, checkpoint, step):
            return [{**row, "price": 0.1, "resolution_seconds": 10800,
                     "price_source": "synthetic_history", "coarser_than_review_window": True,
                     "review_only": True, "point_in_time_safe": False,
                     "backfilled_after_checkpoint": True}
                    for row in service.store.prices_at(slug, checkpoint, 24)]
        with mock.patch.object(service.store, "historical_prices_in_window", side_effect=review_prices, create=True):
            review = service.backtest_review(step_minutes=60, min_elapsed_minutes=60)
        displayed = review["records"][0]["outcome_market_rows"][0]
        self.assertEqual(displayed["price_resolution_seconds"], 10800)
        self.assertEqual(displayed["price_source"], "synthetic_history")
        self.assertTrue(displayed["price_coarser_than_review_window"])
        self.assertTrue(displayed["price_review_only"])
        self.assertEqual(displayed["latest_price"], 0.1)
        self.assertEqual(displayed["paper_net_edge_after_cost"], record["outcome_market_rows"][0]["paper_net_edge_after_cost"])
        self.assertEqual(review["records"][0]["market_brier_score"], record["market_brier_score"])
        self.assertEqual(review["records"][0]["position_actions"], record["position_actions"])
        complete = service.backtest_review(step_minutes=5, min_elapsed_minutes=5)
        outside = service.backtest_review(start='2026-09-01',end='2026-09-02')
        self.assertEqual(outside['records'],[])
        self.assertEqual(outside['coverage']['empty_reason'],'date_range_outside_market')
        self.assertEqual(outside['coverage']['selected_market_windows'][0]['slug'],'backtest-market')
        bounded = service.backtest_review(
            start=(start + dt.timedelta(minutes=30)).isoformat(),
            end=(start + dt.timedelta(minutes=50)).isoformat(),
            step_minutes=5, min_elapsed_minutes=5,
        )
        expected = [item for item in complete["records"]
                    if start + dt.timedelta(minutes=30) <= dt.datetime.fromisoformat(item["checkpoint_utc"])
                    < start + dt.timedelta(minutes=50)]
        self.assertEqual(len(bounded["records"]), len(expected))
        self.assertEqual(bounded["coverage"]["total_checkpoint_count"], len(expected))
        for actual, reference in zip(bounded["records"], expected):
            self.assertEqual(actual["position_actions"], reference["position_actions"])
        # With no prior windows and <24h evidence the new probability guard
        # prohibits opening, rather than manufacturing early trades.
        self.assertEqual(bounded["records"][0]["paper_prior_actions"], [])
        self.assertFalse(complete["records"][0]["probability_guard"]["entry_supported"])
        # Separately retain the warm-up test with an explicitly supported
        # synthetic prior, so the test actually carries nonzero positions.
        from replay_probability import guarded_forecast
        with mock.patch("live_dashboard_server.guarded_forecast", side_effect=lambda model, features, ranges, prior: guarded_forecast(model, features, ranges, 5.0)):
            supported = service.backtest_review(step_minutes=5, min_elapsed_minutes=5)
            cropped = service.backtest_review(start=(start + dt.timedelta(minutes=30)).isoformat(),
                                              end=(start + dt.timedelta(minutes=50)).isoformat())
        self.assertTrue(cropped["records"][0]["paper_prior_actions"])
        self.assertEqual(cropped["records"][0]["position_actions"], supported["records"][5]["position_actions"])
        limited = service.backtest_review(step_minutes=5, min_elapsed_minutes=5, limit=2)
        self.assertTrue(limited["coverage"]["truncated"])
        self.assertEqual(len(limited["records"]), 2)
        requested = start + dt.timedelta(minutes=30)
        original_books = service.store.orderbooks_at
        service.store.orderbooks_at = lambda slug, cp, age: [] if cp == requested else original_books(slug, cp, age)
        with mock.patch("live_dashboard_server.guarded_forecast", side_effect=lambda model, features, ranges, prior: guarded_forecast(model, features, ranges, 5.0)):
            forced = service.backtest_review(paper_mode="mandatory_entry", paper_entry_checkpoint=requested.isoformat())
            cropped_forced = service.backtest_review(paper_mode="mandatory_entry", paper_entry_checkpoint=requested.isoformat(), start=(start+dt.timedelta(minutes=45)).isoformat())
        self.assertFalse(any(a["quantity_delta"] for r in forced["records"][:6] for a in r["position_actions"]))
        self.assertEqual(forced["markets"][0]["paper_entry_actual_utc"], (requested+dt.timedelta(minutes=5)).isoformat())
        self.assertEqual(forced["markets"][0]["paper_fill_action_count"],cropped_forced["markets"][0]["paper_fill_action_count"])
        self.assertEqual(forced["markets"][0]["paper_final_pnl"],cropped_forced["markets"][0]["paper_final_pnl"])
        self.assertTrue(forced["records"][-1]["paper_settlement_actions"])
        relative = service.backtest_review(paper_mode="mandatory_entry",paper_entry_elapsed_minutes=32,paper_exit_policy="hold_to_settlement")
        self.assertEqual(relative['markets'][0]['paper_entry_actual_utc'],(start+dt.timedelta(minutes=35)).isoformat())
        self.assertFalse(any(a['quantity_delta'] for r in relative['records'][:6] for a in r['position_actions']))
        self.assertFalse(any(a['quantity_delta'] < 0 for r in relative['records'] for a in r['position_actions']))
        self.assertTrue(relative['records'][-1]['paper_settlement_actions'])
        fast=service.backtest_review(paper_mode='mandatory_entry',paper_entry_elapsed_minutes=32,paper_exit_policy='hold_to_settlement',paper_hold_summary=True)
        self.assertEqual(fast['markets'][0]['paper_final_pnl'],relative['markets'][0]['paper_final_pnl'])
        self.assertEqual(fast['markets'][0]['paper_total_buy_cost'],relative['markets'][0]['paper_total_buy_cost'])
        with self.assertRaises(ValueError):
            service.backtest_review(paper_entry_elapsed_minutes=120)
        with self.assertRaises(ValueError):
            service.backtest_review(paper_mode="mandatory_entry",paper_entry_checkpoint=(requested+dt.timedelta(seconds=1)).isoformat())
        service.store.orderbooks_at = lambda *args: []
        unavailable=service.backtest_review(paper_mode="mandatory_entry")
        self.assertEqual(unavailable["markets"][0]["paper_entry_status"],"unavailable_before_market_end")
        self.assertEqual(unavailable["markets"][0]["paper_fill_action_count"],0)
        truncated=service.backtest_review(paper_mode="mandatory_entry",limit=2)
        self.assertEqual(truncated["markets"][0]["paper_entry_status"],"pending_in_remaining_range")
        self.assertFalse(truncated["markets"][0]["paper_settled"])
        service.store.orderbooks_at = original_books
        service.store.price_mass = 0.1
        incomplete_market = service.backtest_review(step_minutes=60, min_elapsed_minutes=60)
        self.assertIsNone(incomplete_market["records"][0]["market_brier_score"])
        self.assertEqual(incomplete_market["data_quality"]["paired_model_market_score_records"], 0)

    def test_gamma_discovery_targets_elon_series_and_orders_by_end_date(self) -> None:
        class FakeGamma(GammaClient):
            def __init__(self):
                self.calls = []

            def get(self, path, params=None):
                self.calls.append((path, params))
                return {"events": []}

        client = FakeGamma()
        result = client.discover_count_events(
            dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc),
            "America/New_York",
            max_pages=1,
        )
        self.assertEqual(result, [])
        self.assertEqual(client.calls[0][1]["series_id"], "10000")
        self.assertEqual(client.calls[0][1]["order"], "endDate")
        self.assertFalse(client.calls[0][1]["ascending"])

    def test_checkpoints_support_five_minute_sliding_progress(self) -> None:
        start = dt.datetime(2026, 7, 28, 16, tzinfo=dt.timezone.utc)
        window = Window("market", start, start + dt.timedelta(minutes=25))
        checkpoints = hourly_checkpoints(window, step_minutes=5, min_elapsed_minutes=5)
        self.assertEqual(len(checkpoints), 4)
        self.assertEqual(checkpoints[0], start + dt.timedelta(minutes=5))
        self.assertEqual(checkpoints[-1], start + dt.timedelta(minutes=20))

    def test_post_availability_is_gated_at_checkpoint(self) -> None:
        start = dt.datetime(2026, 7, 28, 16, tzinfo=dt.timezone.utc)
        observations = [
            PostObservation(start + dt.timedelta(minutes=1), start + dt.timedelta(minutes=10)),
            PostObservation(start + dt.timedelta(minutes=2), start + dt.timedelta(minutes=2)),
        ]
        from polymarket_pipeline import count_before
        self.assertEqual(count_before(observations, start, start + dt.timedelta(hours=1), start + dt.timedelta(minutes=5)), 1)
        self.assertEqual(count_before(observations, start, start + dt.timedelta(hours=1), start + dt.timedelta(minutes=10)), 2)

    def test_404_slug_resolves_to_current_gamma_market(self) -> None:
        class FakeGamma:
            def get_event(self, slug):
                raise urllib.error.HTTPError("https://gamma.test", 404, "missing", {}, None)

            def discover_count_events(self, *args, **kwargs):
                return [{
                    "slug": "real-current-market",
                    "markets": [{"gameStartTime": "2026-08-28T16:00:00Z"}],
                    "endDate": "2026-09-04T16:00:00Z",
                }]

        service = LiveService.__new__(LiveService)
        service.args = type("Args", (), {
            "slug": "stale-date-slug",
            "timezone": "America/New_York",
        })()
        service.gamma = FakeGamma()
        resolved = service._resolve_event(dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(resolved["slug"], "real-current-market")
        self.assertEqual(service.args.slug, "real-current-market")

    def test_404_slug_without_live_market_is_explicit_no_market(self) -> None:
        class EmptyGamma:
            def get_event(self, slug):
                raise urllib.error.HTTPError("https://gamma.test", 404, "missing", {}, None)

            def discover_count_events(self, *args, **kwargs):
                return []

        service = LiveService.__new__(LiveService)
        service.args = type("Args", (), {
            "slug": "stale-date-slug",
            "timezone": "America/New_York",
        })()
        service.gamma = EmptyGamma()
        with self.assertRaisesRegex(ValueError, "无法读取当前盘口"):
            service._resolve_event(dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc))

    def test_dynamic_resolves_200_closed_slug_to_live_market(self) -> None:
        class FakeGamma:
            def get_event(self, slug):
                return {
                    "slug": slug,
                    "markets": [{"gameStartTime": "2026-08-20T16:00:00Z"}],
                    "endDate": "2026-08-27T16:00:00Z",
                }

            def discover_count_events(self, *args, **kwargs):
                return [{
                    "slug": "new-live-market",
                    "markets": [{"gameStartTime": "2026-08-28T16:00:00Z"}],
                    "endDate": "2026-09-04T16:00:00Z",
                }]

        service = LiveService.__new__(LiveService)
        service.args = type("Args", (), {
            "slug": "closed-but-200",
            "timezone": "America/New_York",
            "dynamic_discovery": True,
        })()
        service.gamma = FakeGamma()
        resolved = service._resolve_event(dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(resolved["slug"], "new-live-market")
        self.assertEqual(service.args.slug, "new-live-market")
        self.assertEqual(service.last_discovery["cause"], "outside_window")

    def test_explicit_closed_slug_is_not_switched(self) -> None:
        class FakeGamma:
            def get_event(self, slug):
                return {
                    "slug": slug,
                    "markets": [{"gameStartTime": "2026-08-20T16:00:00Z"}],
                    "endDate": "2026-08-27T16:00:00Z",
                }

        service = LiveService.__new__(LiveService)
        service.args = type("Args", (), {
            "slug": "historical-market",
            "timezone": "America/New_York",
            "dynamic_discovery": False,
        })()
        service.gamma = FakeGamma()
        resolved = service._resolve_event(dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(resolved["slug"], "historical-market")

    def test_public_report_route_rejects_answer_columns(self) -> None:
        self.assertTrue(public_report_is_safe(b"slug,checkpoint_utc,current_count\n"))
        self.assertFalse(public_report_is_safe(
            b"slug,observed_local_final_count,resolved_bin\n"
        ))

    def test_live_parser_accepts_multiple_isolated_slugs(self) -> None:
        args = build_parser().parse_args([
            "--slug", "market-a",
            "--extra-slug", "market-b",
        ])
        self.assertEqual(args.slug, "market-a")
        self.assertEqual(args.extra_slug, ["market-b"])

    def test_live_parser_has_safe_fixed_report_defaults(self) -> None:
        args = build_parser().parse_args(["--slug", "market-a"])
        self.assertTrue(args.public_report.endswith("hourly_extended_v4_fullprob_public.csv"))
        self.assertTrue(args.private_report.endswith("hourly_extended_v4_fullprob.csv"))
        self.assertTrue(args.summary_report.endswith("hourly_extended_v4_fullprob_summary.csv"))

    def test_live_parser_allows_slug_omission_for_dynamic_discovery(self) -> None:
        args = build_parser().parse_args(["--dynamic-discovery"])
        self.assertEqual(args.slug, "")
        self.assertTrue(args.dynamic_discovery)

    def test_live_parser_supports_local_read_only_mode(self) -> None:
        args = build_parser().parse_args(["--no-background"])
        self.assertTrue(args.no_background)

    def test_local_markets_are_ordered_by_nearest_captured_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "local-markets.sqlite")
            try:
                now = dt.datetime.now(dt.timezone.utc)
                with store.conn:
                    store.conn.executemany(
                        "insert into market_events(slug, game_start_utc, end_date_utc, fetched_at_utc, raw_json) values (?, ?, ?, ?, ?)",
                        [
                            ("older", (now - dt.timedelta(days=8)).isoformat(), (now - dt.timedelta(days=1)).isoformat(), now.isoformat(), "{}"),
                            ("nearest", (now - dt.timedelta(days=2)).isoformat(), (now + dt.timedelta(days=5)).isoformat(), now.isoformat(), "{}"),
                        ],
                    )
                store.save_prediction_archive(
                    event_slug="older", captured_at_utc=now - dt.timedelta(hours=2),
                    data_time_utc=now - dt.timedelta(hours=2), refresh_status="complete", payload={},
                )
                store.save_prediction_archive(
                    event_slug="nearest", captured_at_utc=now - dt.timedelta(minutes=5),
                    data_time_utc=now - dt.timedelta(minutes=5), refresh_status="complete", payload={},
                )
                service = LiveService.__new__(LiveService)
                service.store = store
                service.lock = threading.RLock()
                markets = service.local_markets(limit=2)
                self.assertEqual([item["slug"] for item in markets], ["nearest", "older"])
                self.assertEqual(markets[0]["latest_local_data_utc"], (now - dt.timedelta(minutes=5)).isoformat())
            finally:
                store.close()

    def test_multi_live_markets_keeps_paper_history_with_its_market(self) -> None:
        class FakeService:
            def __init__(self, slug: str) -> None:
                self.args = type("Args", (), {"slug": slug})()

            def refresh(self):
                return {"slug": self.args.slug, "recommendation": {"do_not_auto_trade": True}}

            def paper_trades(self, limit: int):
                return [{"slug": self.args.slug, "limit": limit}]

        service = MultiLiveService.__new__(MultiLiveService)
        service.services = [FakeService("market-a"), FakeService("market-b")]
        snapshots = service.live_markets()
        self.assertEqual([row["slug"] for row in snapshots], ["market-a", "market-b"])
        self.assertEqual(snapshots[0]["paper_trades"][0]["slug"], "market-a")
        self.assertEqual(snapshots[1]["paper_trades"][0]["slug"], "market-b")

    def test_dynamic_multi_sync_adds_new_and_retires_finished_slug(self) -> None:
        class FakeGamma:
            def discover_count_events(self, *args, **kwargs):
                return [{
                    "slug": "market-new",
                    "markets": [{"gameStartTime": "2026-08-28T16:00:00Z"}],
                    "endDate": "2026-09-04T16:00:00Z",
                }]

        class FakeService:
            def __init__(self, slug):
                self.args = type("Args", (), {"slug": slug})()
                self.gamma = FakeGamma()

        service = MultiLiveService.__new__(MultiLiveService)
        service.dynamic_discovery = True
        service.args = type("Args", (), {"timezone": "America/New_York"})()
        service.database_lock = threading.RLock()
        service.retired_services = {}
        service.services = [FakeService("market-old")]
        with mock.patch("live_dashboard_server.LiveService", side_effect=lambda args, lock: FakeService(args.slug)):
            service._sync_dynamic_services(dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc))
        self.assertEqual([item.args.slug for item in service.services], ["market-new"])
        self.assertIn("market-old", service.retired_services)

    def test_dynamic_discovery_queues_missing_historical_market(self) -> None:
        class FakeGamma:
            def discover_count_events(self, *args, **kwargs):
                return [{
                    "slug": "market-ended-missing",
                    "markets": [{"gameStartTime": "2026-08-20T16:00:00Z"}],
                    "endDate": "2026-08-27T16:00:00Z",
                }]

        class FakeStore:
            class Conn:
                def execute(self, query):
                    return type("Rows", (), {"fetchall": lambda self: []})()
            conn = Conn()

        class FakeService:
            def __init__(self):
                self.args = type("Args", (), {"slug": "market-old"})()
                self.gamma = FakeGamma()
                self.store = FakeStore()

        service = MultiLiveService.__new__(MultiLiveService)
        service.dynamic_discovery = True
        service.args = type("Args", (), {"timezone": "America/New_York"})()
        service.services = [FakeService()]
        service.retired_services = {}
        service.discovery_client = FakeGamma()
        service.discovery_status = {"state": "idle", "queued_backfills": []}
        service.backfill_attempts = {}
        service.backfill_queue = __import__("queue").Queue()
        with mock.patch.object(MultiLiveService, "_backfill_loop", return_value=None):
            service._sync_dynamic_services(dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(service.discovery_status["queued_backfills"], ["market-ended-missing"])
        self.assertEqual(service.backfill_queue.get_nowait(), "market-ended-missing")

    def test_dynamic_sync_never_keeps_old_market_when_discovery_fails(self) -> None:
        class FailingGamma:
            def discover_count_events(self, *args, **kwargs):
                raise TimeoutError("gamma timeout")

        class FakeService:
            def __init__(self, slug):
                self.args = type("Args", (), {"slug": slug})()

        old = FakeService("settled-market")
        service = MultiLiveService.__new__(MultiLiveService)
        service.dynamic_discovery = True
        service.args = type("Args", (), {"timezone": "America/New_York", "db": "missing.sqlite"})()
        service.database_lock = threading.RLock()
        service.services = [old]
        service.retired_services = {}
        service.discovery_client = FailingGamma()
        service.discovery_status = {"state": "ok", "active_slugs": ["settled-market"], "live_candidate_count": 1}
        service._sync_dynamic_services(dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc))
        self.assertEqual(service.services, [])
        self.assertIn("settled-market", service.retired_services)
        self.assertEqual(service.discovery_status["state"], "error")
        self.assertEqual(service.discovery_status["active_slugs"], [])

    def test_dynamic_sync_empty_success_clears_current_markets(self) -> None:
        class EmptyGamma:
            def discover_count_events(self, *args, **kwargs):
                return []

        class FakeService:
            def __init__(self, slug):
                self.args = type("Args", (), {"slug": slug})()

        old = FakeService("finished-market")
        service = MultiLiveService.__new__(MultiLiveService)
        service.dynamic_discovery = True
        service.args = type("Args", (), {"timezone": "America/New_York", "db": "missing.sqlite"})()
        service.database_lock = threading.RLock()
        service.services = [old]
        service.retired_services = {}
        service.discovery_client = EmptyGamma()
        service.discovery_status = {"state": "ok"}
        service._sync_dynamic_services(dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc))
        self.assertEqual(service.services, [])
        self.assertIn("finished-market", service.retired_services)
        self.assertEqual(service.discovery_status["live_candidate_count"], 0)

    def test_manual_multi_market_refresh_forces_each_market(self) -> None:
        class FakeService:
            def __init__(self, slug: str) -> None:
                self.args = type("Args", (), {"slug": slug})()
                self.forced = []

            def refresh(self, force=False):
                self.forced.append(force)
                return {"slug": self.args.slug, "recommendation": {"do_not_auto_trade": True}}

            def paper_trades(self, limit: int):
                return []

        service = MultiLiveService.__new__(MultiLiveService)
        service.services = [FakeService("market-a"), FakeService("market-b")]
        service.live_markets(force=True)
        self.assertEqual([item.forced for item in service.services], [[True], [True]])

    def test_paper_buy_cost_includes_slippage_and_fee_assumption(self) -> None:
        executable, fee = paper_buy_cost(0.50, 0.02, 50)
        self.assertAlmostEqual(executable, 0.505)
        self.assertAlmostEqual(fee, 0.01)

    def test_strategy_defaults_are_centralized_in_isolated_module(self) -> None:
        self.assertEqual(DEFAULT_ENTRY_CHECKPOINTS, 12)
        self.assertEqual(DEFAULT_ENTRY_EDGE, 0.08)
        self.assertEqual(DEFAULT_RETAIN_EDGE, 0.04)
        self.assertEqual(DEFAULT_EXIT_EDGE, 0.0)

    def test_paper_position_rebalance_uses_hysteresis_and_safe_quotes(self) -> None:
        self.assertIs(paper_position_rebalance, isolated_paper_position_rebalance)
        positions = {}
        outcomes = [
            {"outcome_label": "0-19", "paper_net_edge_after_cost": 0.12,
             "best_bid": 0.40, "best_ask": 0.45, "ask_depth_5": 10, "bid_depth_5": 10,
             "point_in_time_safe": True},
            {"outcome_label": "20+", "paper_net_edge_after_cost": 0.10,
             "best_bid": 0.20, "best_ask": 0.25, "ask_depth_5": 10, "bid_depth_5": 10,
             "point_in_time_safe": True},
        ]
        actions, realized, _ = paper_position_rebalance(
            outcomes, positions, checkpoint_index=0, paper_budget=10.0,
        )
        self.assertTrue(all(item["action"] == "enter" for item in actions))
        self.assertEqual(realized, 0.0)
        self.assertTrue(all(item["position_after"] > 0 for item in actions))
        no_safe = [dict(outcomes[0], point_in_time_safe=False, paper_net_edge_after_cost=0.20)]
        actions, _, _ = paper_position_rebalance(no_safe, positions, checkpoint_index=1, paper_budget=10.0)
        self.assertEqual(actions[0]["action"], "hold")

    def test_paper_position_rebalance_exits_on_nonpositive_edge_at_bid(self) -> None:
        positions = {"0-19": {"quantity": 2.0, "avg_cost": 0.50, "realized_pnl": 0.0}}
        outcomes = [{"outcome_label": "0-19", "paper_net_edge_after_cost": 0.0,
                     "best_bid": 0.60, "best_ask": 0.65, "ask_depth_5": 0,
                     "bid_depth_5": 2, "point_in_time_safe": True}]
        actions, realized, _ = paper_position_rebalance(
            outcomes, positions, checkpoint_index=20, paper_budget=10.0,
        )
        self.assertEqual(actions[0]["action"], "exit_edge_below_zero")
        self.assertEqual(actions[0]["position_after"], 0.0)
        self.assertGreater(realized, 0.0)

    def test_paper_position_rebalance_marks_zero_depth_as_no_fill(self) -> None:
        outcomes = [{"outcome_label": "0-19", "paper_net_edge_after_cost": 0.12,
                     "best_bid": 0.40, "best_ask": 0.45, "ask_depth_5": 0,
                     "bid_depth_5": 0, "point_in_time_safe": True}]
        actions, _, _ = paper_position_rebalance(
            outcomes, {}, checkpoint_index=0, paper_budget=10.0,
        )
        self.assertEqual(actions[0]["action"], "no_fill")
        self.assertEqual(actions[0]["quantity_delta"], 0.0)

    def test_paper_trade_migration_and_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "paper.sqlite")
            try:
                store.conn.execute(
                    """
                    insert into market_outcomes (
                        event_slug, market_id, label, lower_count, upper_count,
                        resolved_yes, raw_json
                    ) values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("market", "outcome", "20-39", 20, 39, 1, "{}"),
                )
                store.conn.commit()
                trade_id = store.save_paper_trade(
                    created_at_utc=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
                    event_slug="market",
                    outcome_label="20-39",
                    side="yes",
                    model_probability=0.75,
                    quoted_best_bid=0.60,
                    quoted_best_ask=0.65,
                    executable_price=0.66,
                    spread=0.05,
                    available_depth=3.0,
                    requested_size=1.0,
                    fill_fraction=0.5,
                    fee_rate=0.02,
                    slippage_bps=50,
                    estimated_fee=0.0065,
                    status="paper_filled",
                    recommendation="纸面方向研究",
                    edge_after_cost=0.0765,
                    risk_reasons=[],
                    snapshot={"do_not_auto_trade": True},
                )
                self.assertEqual(store.settle_paper_trades("market"), 1)
                row = store.conn.execute(
                    "select status, resolved_yes, gross_pnl, net_pnl from paper_trades where id = ?",
                    (trade_id,),
                ).fetchone()
                self.assertEqual(row[0], "settled")
                self.assertEqual(row[1], 1)
                self.assertAlmostEqual(row[2], 0.17)
                self.assertAlmostEqual(row[3], 0.1635)
                self.assertTrue(
                    store.conn.execute(
                        "select do_not_auto_trade from paper_forecasts"
                    ).fetchone() is None
                )
            finally:
                store.close()

    def test_orderbook_levels_produce_midpoint_spread_and_depth(self) -> None:
        best_bid, best_ask, midpoint, bid_depth, ask_depth = parse_levels({
            "bids": [
                {"price": "0.40", "size": "3"},
                {"price": "0.36", "size": "4"},
            ],
            "asks": [
                {"price": "0.50", "size": "2"},
                {"price": "0.54", "size": "5"},
            ],
        })
        self.assertEqual(best_bid, 0.4)
        self.assertEqual(best_ask, 0.5)
        self.assertEqual(midpoint, 0.45)
        self.assertEqual(bid_depth, 7.0)
        self.assertEqual(ask_depth, 7.0)

    def test_market_activity_accepts_keyword_only_capture_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "activity.sqlite")
            try:
                captured = dt.datetime(2026, 8, 31, tzinfo=dt.timezone.utc)
                row_id = store.save_market_activity(
                    "market",
                    {"volume": "12.5", "trade_count": 3},
                    captured_at_utc=captured,
                )
                row = store.conn.execute(
                    "select event_slug, captured_at_utc, trade_count, volume_24h from market_activity_snapshots where id = ?",
                    (row_id,),
                ).fetchone()
                self.assertEqual(row[0], "market")
                self.assertEqual(row[1], captured.isoformat())
                self.assertEqual(row[2], 3)
                self.assertAlmostEqual(row[3], 12.5)
            finally:
                store.close()

    def test_orderbook_archive_writes_quality_fields_without_replacing_raw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "books.sqlite")
            try:
                inserted = store.save_orderbook(
                    "market", "outcome", "token", {
                        "timestamp": "2026-08-29T00:00:00Z",
                        "bids": [{"price": "0.40", "size": "3"}],
                        "asks": [{"price": "0.50", "size": "1"}],
                        "last_trade_price": "0.45",
                        "last_trade_side": "BUY",
                    },
                )
                self.assertEqual(inserted, 1)
                row = store.conn.execute(
                    "select depth_imbalance, last_trade_price, last_trade_side, raw_json from market_orderbooks"
                ).fetchone()
                self.assertAlmostEqual(row[0], 0.5)
                self.assertEqual(row[1], 0.45)
                self.assertEqual(row[2], "BUY")
                self.assertIn("last_trade_price", row[3])
            finally:
                store.close()

    def test_live_snapshot_archive_keeps_partial_context_with_stable_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "live.sqlite")
            try:
                snapshot_id = store.save_live_snapshot(
                    captured_at_utc=dt.datetime(2026, 8, 19, tzinfo=dt.timezone.utc),
                    event_slug="market",
                    refresh_status="partial",
                    snapshot={
                        "window": {"start_utc": "2026-08-12T16:00:00+00:00"},
                        "count": {"current": 12, "posts_returned": 4},
                        "market_event": {"id": "event-1", "title": "Test event"},
                        "market": {"outcomes": [{"label": "20-39", "best_ask": 0.5}]},
                        "news": [{"title": "Context", "url": "https://example.test"}],
                        "sources": {"clob": {"status": "partial", "latency_ms": 15.2}},
                        "errors": ["clob: missing outcome"],
                    },
                )
                rows = store.live_snapshot_archive("market")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["snapshot_id"], snapshot_id)
                self.assertEqual(rows[0]["refresh_status"], "partial")
                self.assertEqual(rows[0]["count"]["current"], 12)
                self.assertEqual(rows[0]["market_event"]["id"], "event-1")
                self.assertEqual(rows[0]["market"]["outcomes"][0]["best_ask"], 0.5)
                self.assertEqual(rows[0]["external_context"]["flight"], {
                    "status": "not_configured", "items": [],
                })
                self.assertTrue(rows[0]["recommendation"]["do_not_auto_trade"])
            finally:
                store.close()

    def test_live_snapshot_archive_normalizes_legacy_paper_forecast(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "legacy.sqlite")
            try:
                window = Window(
                    "market",
                    dt.datetime(2026, 8, 12, tzinfo=dt.timezone.utc),
                    dt.datetime(2026, 8, 19, tzinfo=dt.timezone.utc),
                )
                store.save_paper_forecast(
                    captured_at_utc=window.start_utc,
                    window=window,
                    current_count=8,
                    forecast=20.0,
                    forecast_bin="20-39",
                    forecast_bin_probability=0.4,
                    market_expected_count=22.0,
                    probability_mass=1.0,
                    price_distribution_valid=True,
                    recommendation="no_trade",
                    paper_direction=None,
                    edge_proxy=None,
                    risk_reasons=["legacy"],
                    snapshot={"count": {"current": 8}, "news": None},
                )
                rows = store.live_snapshot_archive("market")
                self.assertEqual(rows[0]["capture_origin"], "legacy_paper_forecast")
                self.assertEqual(rows[0]["refresh_status"], "legacy")
                self.assertEqual(rows[0]["news"], [])
                self.assertEqual(rows[0]["market"]["outcomes"], [])
                self.assertTrue(rows[0]["recommendation"]["do_not_auto_trade"])
            finally:
                store.close()

    def test_prediction_archive_is_append_only_and_frontend_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "archive.sqlite")
            try:
                captured = dt.datetime(2026, 8, 29, 1, tzinfo=dt.timezone.utc)
                first = store.save_prediction_archive(
                    event_slug="market-a",
                    captured_at_utc=captured,
                    data_time_utc=captured - dt.timedelta(minutes=1),
                    refresh_status="complete",
                    payload={"count": {"current": 7}, "sources": {"gamma": {"status": "ok"}}},
                )
                second = store.save_prediction_archive(
                    event_slug="market-a",
                    captured_at_utc=captured + dt.timedelta(hours=1),
                    refresh_status="partial",
                    payload={"count": {"current": 8}, "errors": ["clob"]},
                )
                rows = store.prediction_archive("market-a", 10)
                self.assertEqual([row["archive_id"] for row in rows], [second, first])
                self.assertEqual(rows[0]["schema_version"], "prediction_archive_v1")
                self.assertTrue(rows[0]["do_not_auto_trade"])
                self.assertEqual(rows[0]["refresh_status"], "partial")
                self.assertEqual(store.conn.execute("select count(*) from prediction_archive").fetchone()[0], 2)
            finally:
                store.close()

    def test_history_review_is_slug_isolated_and_marks_capture_only_orderbooks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "review.sqlite")
            try:
                target_start = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
                with store.conn:
                    store.conn.executemany(
                        "insert into market_events(slug, game_start_utc, end_date_utc, fetched_at_utc, raw_json) values (?, ?, ?, ?, ?)",
                        [
                            ("target", target_start.isoformat(), "2026-09-08T00:00:00+00:00", target_start.isoformat(), "{}"),
                            ("other-overlap", "2026-08-29T00:00:00+00:00", "2026-09-05T00:00:00+00:00", target_start.isoformat(), "{}"),
                        ],
                    )
                    store.conn.executemany(
                        "insert into market_outcomes(event_slug, market_id, label, lower_count, upper_count, raw_json) values (?, ?, ?, ?, ?, ?)",
                        [("target", "outcome", "20-39", 20, 39, "{}"), ("other-overlap", "outcome", "40-59", 40, 59, "{}")],
                    )
                store.save_prices("target", "outcome", "token", [{"t": target_start.timestamp(), "p": "0.42"}])
                store.save_orderbook("target", "outcome", "token", {
                    "bids": [{"price": "0.40", "size": "2"}],
                    "asks": [{"price": "0.45", "size": "3"}],
                })
                store.save_market_activity("target", {"volume": 12, "liquidity": 4}, captured_at_utc=target_start)
                store.save_prediction_archive(
                    event_slug="target", captured_at_utc=target_start, data_time_utc=target_start,
                    refresh_status="complete", payload={"count": {"current": 8}, "prediction": {"final_count": 25}},
                )
                store.save_prediction_archive(
                    event_slug="other-overlap", captured_at_utc=target_start, data_time_utc=target_start,
                    refresh_status="complete", payload={"count": {"current": 99}},
                )
                review = store.history_review("target", limit=50, half_life_days=30, max_age_days=180)
                self.assertTrue(review["do_not_auto_trade"])
                self.assertEqual(review["coverage"]["price_records"], 1)
                self.assertEqual(review["coverage"]["orderbook_records"], 1)
                self.assertEqual(review["predictions"][0]["current_count"], 8)
                self.assertEqual(review["prices"][0]["source_price_time_utc"], target_start.isoformat())
                self.assertFalse(review["orderbooks"][0]["point_in_time_safe"])
                self.assertEqual(review["external_context"]["flight"], {"status": "not_configured", "items": []})
            finally:
                store.close()

    def test_history_review_time_decay_excludes_overlapping_reference_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "decay.sqlite")
            try:
                target = Window("target", dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc), dt.datetime(2026, 9, 8, tzinfo=dt.timezone.utc))
                prior = Window("prior", dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc), dt.datetime(2026, 8, 25, tzinfo=dt.timezone.utc))
                overlap = Window("overlap", dt.datetime(2026, 8, 29, tzinfo=dt.timezone.utc), dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc))
                with store.conn:
                    for window in (target, prior, overlap):
                        store.conn.execute(
                            "insert into market_events(slug, game_start_utc, end_date_utc, fetched_at_utc, raw_json) values (?, ?, ?, ?, ?)",
                            (window.slug, window.start_utc.isoformat(), window.end_utc.isoformat(), window.start_utc.isoformat(), "{}"),
                        )
                for window, forecast in ((prior, 20.0), (overlap, 90.0)):
                    store.save_paper_forecast(
                        captured_at_utc=window.start_utc, window=window, current_count=5,
                        forecast=forecast, forecast_bin=None, forecast_bin_probability=None,
                        market_expected_count=0, probability_mass=0, price_distribution_valid=False,
                        recommendation="no_trade", paper_direction=None, edge_proxy=None,
                        risk_reasons=[], snapshot={},
                    )
                decay = store.history_review("target", half_life_days=30, max_age_days=180)["time_decay"]
                self.assertEqual(decay["eligible_prior_markets"], 1)
                self.assertEqual(decay["references"][0]["slug"], "prior")
                self.assertEqual(decay["excluded"]["future_or_overlapping"], 1)
                self.assertAlmostEqual(decay["weighted_archived_forecast_reference"], 20.0)
            finally:
                store.close()

    def test_post_temporal_audit_fields_preserve_source_and_capture_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PostStore(Path(directory) / "posts.sqlite")
            try:
                post = normalize_post({
                    "id": "post-1", "created_at": "2026-09-01T12:34:56Z",
                    "captured_at": "2026-09-01T12:35:10Z", "text": "metadata test",
                }, "elonmusk", "X")
                self.assertEqual(store.upsert_posts([post]), 1)
                row = store.conn.execute(
                    "select created_at_utc, captured_at_utc, available_at_utc, time_precision, time_quality from posts where post_id='post-1'"
                ).fetchone()
                self.assertEqual(row[0], "2026-09-01T12:34:56+00:00")
                self.assertEqual(row[1], "2026-09-01T12:35:10+00:00")
                self.assertEqual(row[2], "2026-09-01T12:34:56+00:00")
                self.assertEqual(row[3:], ("second", "source_timestamp"))
            finally:
                store.close()

class FrontendContractTests(unittest.TestCase):
    def test_dashboard_uses_one_unified_history_workspace(self) -> None:
        html = Path("forecast_dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn('id="guessMode"', html)
        self.assertNotIn('id="reviewMode"', html)
        self.assertEqual(html.count('id="marketSelect"'), 1)
        self.assertIn('id="historyIncludeOrderbooks"', html)
        self.assertIn("统一历史回测与复盘", html)
        self.assertIn("揭晓后显示", html)

    def test_dashboard_ids_are_unique(self) -> None:
        import re
        html = Path("forecast_dashboard.html").read_text(encoding="utf-8")
        ids = re.findall(r'id=[\"\']([^\"\']+)[\"\']', html)
        duplicates = {value for value in ids if ids.count(value) > 1}
        self.assertEqual(duplicates, set())


if __name__ == "__main__":
    unittest.main()
