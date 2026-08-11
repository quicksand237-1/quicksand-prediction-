import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from polymarket_pipeline import (
    Example,
    MarketStore,
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
from live_dashboard_server import paper_buy_cost, parse_levels


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
    def test_paper_buy_cost_includes_slippage_and_fee_assumption(self) -> None:
        executable, fee = paper_buy_cost(0.50, 0.02, 50)
        self.assertAlmostEqual(executable, 0.505)
        self.assertAlmostEqual(fee, 0.01)

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


if __name__ == "__main__":
    unittest.main()
