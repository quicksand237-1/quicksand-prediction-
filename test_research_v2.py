"""Synthetic, offline regression tests for explicitly opted-in v2 algorithms."""
import copy
from datetime import datetime, timedelta, timezone
import json
import math
import unittest

from research_features_v2 import Coverage, Event, Post, build_features_v2, historical_profile
from research_probability_v2 import prediction_for_context_v2, predict_probabilities_v2
from strategy_execution_v2 import adapt_archived_book_v2, execute_paper_order_v2


UTC = timezone.utc
START = datetime(2026, 1, 12, 15, tzinfo=UTC)


class FeatureV2Tests(unittest.TestCase):
    def features(self, **kwargs):
        inputs = dict(slug="market-a", start=START, end=START + timedelta(hours=168),
                      as_of=START + timedelta(minutes=5), posts=[], coverage=[], events=[])
        inputs.update(kwargs)
        return build_features_v2(**inputs)

    def test_five_minutes_and_short_market_are_not_rounded_to_hour(self):
        result = self.features(end=START + timedelta(minutes=30))
        self.assertEqual(result["features"]["total_hours"], 0.5)
        self.assertAlmostEqual(result["features"]["elapsed_hours"], 1 / 12)
        self.assertFalse(result["model_ready"])
        self.assertTrue(result["do_not_auto_trade"])

    def test_smoothing_uses_observed_exposure_and_historical_prior(self):
        before = START - timedelta(hours=1)
        now = START + timedelta(minutes=5)
        posts = [Post("old", before, before), Post("new1", START, START),
                 Post("new2", START + timedelta(minutes=1), START + timedelta(minutes=1))]
        result = self.features(posts=posts, coverage=[Coverage(before, START, START), Coverage(START, now, now)])
        values = result["features"]
        self.assertAlmostEqual(values["smoothed_rate"], (2 + 6) / (6 + 1 / 12))
        self.assertEqual(values["recent_1h_rate"], 24)
        self.assertAlmostEqual(values["recent_6h_coverage"], 1 / 72)
        self.assertEqual(values["current_count"], 2)

    def test_missing_history_is_not_zero_rate(self):
        result = self.features()
        self.assertIsNone(result["features"]["seasonal_final_count"])
        self.assertIsNone(result["features"]["smoothed_rate"])
        self.assertIn("history_coverage_missing", result["quality_flags"])

    def test_verified_zero_posts_is_valid_zero_rate(self):
        result = self.features(coverage=[Coverage(START - timedelta(hours=1), START, START)])
        self.assertEqual(result["history_profile"]["global_rate"], 0)
        self.assertEqual(result["features"]["smoothed_rate"], 0)

    def test_deduplication_and_overlapping_coverage(self):
        before = START - timedelta(hours=2)
        post = Post("same", before, before)
        profile = historical_profile([post, post], [Coverage(before, START, START),
            Coverage(before + timedelta(hours=1), START, START)], cutoff=START)
        self.assertEqual(sum(profile["slot_hours"]), 2)
        self.assertEqual(sum(profile["slot_counts"]), 1)
        self.assertEqual(profile["global_rate"], 0.5)

    def test_future_posts_coverage_and_event_approval_cannot_change_features(self):
        baseline = self.features()
        future = START + timedelta(hours=1)
        result = self.features(posts=[Post("late", START, future), Post("future", future, START)],
            coverage=[Coverage(START - timedelta(hours=1), START, future)],
            events=[Event("unapproved", START, START, future, 3)])
        self.assertEqual(result, baseline)

    def test_market_start_freezes_historical_reference(self):
        result = self.features(coverage=[Coverage(START - timedelta(hours=1), START,
                                                  START + timedelta(minutes=1))])
        self.assertIsNone(result["history_profile"]["global_rate"])

    def test_seasonal_shrinkage_and_remaining_partial_hour(self):
        before = START - timedelta(days=7)
        posts = [Post(str(i), before + timedelta(minutes=i), before + timedelta(minutes=i)) for i in range(4)]
        coverage = [Coverage(before, before + timedelta(hours=2), START)]
        result = self.features(posts=posts, coverage=coverage, as_of=START,
                               end=START + timedelta(minutes=30))
        # Global 2/hour; Monday 10am has 4 posts in 1 hour, shrunk by 24 hours.
        self.assertAlmostEqual(result["features"]["seasonal_remaining_count"], 0.5 * (4 + 24 * 2) / 25)

    def test_dst_exposure_counts_repeated_hour_twice_and_missing_hour_zero(self):
        for start, end, slot, expected in [
            (datetime(2026, 11, 1, 4, tzinfo=UTC), datetime(2026, 11, 1, 8, tzinfo=UTC), 6 * 24 + 1, 2),
            (datetime(2026, 3, 8, 5, tzinfo=UTC), datetime(2026, 3, 8, 9, tzinfo=UTC), 6 * 24 + 2, 0),
        ]:
            profile = historical_profile([], [Coverage(start, end, end)], cutoff=end)
            self.assertEqual(sum(profile["slot_hours"]), 4)
            self.assertEqual(profile["slot_hours"][slot], expected)

    def test_event_decay_separates_signs_and_deduplicates(self):
        now = START + timedelta(hours=24)
        positive = Event("positive", START, START, START, 2)
        negative = Event("negative", START, START, START, -2)
        result = self.features(as_of=now, events=[positive, positive, negative])
        values = result["features"]
        self.assertAlmostEqual(values["event_positive_tau_24h"], 2 / math.e)
        self.assertAlmostEqual(values["event_negative_tau_24h"], -2 / math.e)
        self.assertGreater(values["event_positive_tau_72h"], values["event_positive_tau_3h"])

    def test_half_hour_timezone_splits_local_hour_correctly(self):
        start = datetime(2026, 1, 12, 0, tzinfo=UTC)
        end = start + timedelta(hours=1)
        profile = historical_profile([], [Coverage(start, end, end)], cutoff=end,
                                     timezone_name="Asia/Kolkata")
        self.assertEqual(profile["slot_hours"][5], 0.5)
        self.assertEqual(profile["slot_hours"][6], 0.5)

    def test_longer_exposure_moves_smoothed_rate_toward_observed_rate(self):
        before = START - timedelta(hours=1)
        posts = [Post("old", before, before)]
        early = self.features(posts=posts, coverage=[Coverage(before, START, START)])
        now = START + timedelta(hours=6)
        late = self.features(posts=posts, as_of=now,
                             coverage=[Coverage(before, START, START), Coverage(START, now, now)])
        self.assertEqual(early["features"]["smoothed_rate"], 1)
        self.assertEqual(late["features"]["smoothed_rate"], 0.5)

    def test_invalid_parameters_and_naive_time_rejected(self):
        for kwargs in ({"prior_hours": 0}, {"shrink_hours": -1},
                       {"start": START.replace(tzinfo=None)}, {"end": START}):
            with self.assertRaises(ValueError):
                self.features(**kwargs)


class ProbabilityV2Tests(unittest.TestCase):
    def predict(self, **kwargs):
        inputs = dict(slug="market-a", as_of=START, observed_count=10, predicted_final=20,
                      ranges=[("below", 0, 9), ("near", 10, 19), ("tail", 20, None)])
        inputs.update(kwargs)
        return predict_probabilities_v2(**inputs)

    def variance(self, value, **kwargs):
        spec = dict(semantics="remaining_count_variance", value=value, source="synthetic",
                    trained_through=START - timedelta(days=1), available_at=START)
        spec.update(kwargs)
        return spec

    def test_shifted_support_and_mass(self):
        result = self.predict()
        self.assertEqual(result["bin_probabilities"]["below"], 0)
        self.assertAlmostEqual(sum(result["bin_probabilities"].values()), 1)
        self.assertEqual(result["remaining_mean"], 10)
        self.assertEqual(result["variance_source"], "poisson_baseline")

    def test_variance_branches(self):
        for value, kind, effective in [(30, "negative_binomial", 30), (10, "poisson", 10), (3, "poisson", 10)]:
            result = self.predict(variance=self.variance(value))
            self.assertEqual(result["distribution"], kind)
            self.assertEqual(result["effective_variance"], effective)
            self.assertEqual(result["requested_variance"], value)
            self.assertAlmostEqual(sum(result["bin_probabilities"].values()), 1)

    def test_negative_binomial_matches_hand_probability_at_zero(self):
        result = self.predict(observed_count=0, predicted_final=2, variance=self.variance(4),
                              ranges=[("zero", 0, 0), ("rest", 1, None)])
        self.assertAlmostEqual(result["bin_probabilities"]["zero"], 0.25)

    def test_zero_remaining_mean_is_point_mass(self):
        result = self.predict(predicted_final=4)
        self.assertEqual(result["distribution"], "point_mass")
        self.assertEqual(result["bin_probabilities"], {"below": 0.0, "near": 1.0, "tail": 0.0})

    def test_missing_ranges_not_renormalized(self):
        result = self.predict(ranges=[("only", 10, 10)])
        self.assertFalse(result["safe_edge_allowed"])
        self.assertAlmostEqual(result["bin_probabilities"]["only"], math.exp(-10))
        self.assertAlmostEqual(result["uncovered_probability"], 1 - math.exp(-10))

    def test_future_or_legacy_variance_not_used(self):
        for changes in ({"semantics": "final_count_variance"},
                        {"available_at": START + timedelta(seconds=1)},
                        {"trained_through": START + timedelta(seconds=1)}):
            result = self.predict(variance=self.variance(200, **changes))
            self.assertEqual(result["effective_variance"], 10)
            self.assertIsNone(result["requested_variance"])

    def test_invalid_ranges_and_nonfinite_values_rejected(self):
        for kwargs in ({"ranges": [("a", 0, 20), ("b", 20, None)]},
                       {"ranges": [("a", -1, None)]}, {"predicted_final": math.nan},
                       {"variance": self.variance(math.inf)}):
            with self.assertRaises(ValueError):
                self.predict(**kwargs)

    def test_extreme_mean_and_tail_are_finite(self):
        result = self.predict(predicted_final=10000, ranges=[("low", 0, 9999), ("high", 10000, None)])
        self.assertAlmostEqual(sum(result["bin_probabilities"].values()), 1)
        self.assertTrue(all(math.isfinite(p) for p in result["bin_probabilities"].values()))

    def test_live_review_share_exact_adapter(self):
        inputs = dict(slug="market-a", as_of=START, observed_count=4, predicted_final=10,
                      ranges=[("all", 0, None)], variance=self.variance(20))
        self.assertEqual(prediction_for_context_v2(context="live", **inputs),
                         prediction_for_context_v2(context="backtest_review", **inputs))


class ExecutionV2Tests(unittest.TestCase):
    def row(self, **kwargs):
        row = dict(event_slug="market-a", token_id="yes-a", source_timestamp_utc=START.isoformat(),
                   captured_at_utc=START.isoformat(), raw_json=json.dumps({
                       "asks": [{"price": "0.55", "size": "8"}, {"price": "0.50", "size": "2"}],
                       "bids": [{"price": "0.49", "size": "10"}]}))
        row.update(kwargs)
        return row

    def book(self, **kwargs):
        return adapt_archived_book_v2(self.row(**kwargs), slug="market-a", token_id="yes-a", as_of=START)

    def execute(self, **kwargs):
        inputs = dict(book=self.book(), slug="market-a", token_id="yes-a", as_of=START,
                      side="buy", quantity=10, position_quantity=0, remaining_cash=100,
                      remaining_turnover=100)
        inputs.update(kwargs)
        return execute_paper_order_v2(**inputs)

    def test_walks_sorted_levels_and_calculates_weighted_price(self):
        result = self.execute()
        self.assertAlmostEqual(result["average_price"], 0.54)
        self.assertAlmostEqual(result["fees"], 0.108)
        self.assertAlmostEqual(result["remaining_cash"], 100 - 5.508)
        self.assertEqual(result["filled_quantity"], 10)
        self.assertTrue(result["do_not_auto_trade"])

    def test_partial_fill_cash_includes_fees(self):
        result = self.execute(remaining_cash=0.51)
        self.assertAlmostEqual(result["filled_quantity"], 1)
        self.assertAlmostEqual(result["remaining_cash"], 0)
        self.assertEqual(result["reason"], "partial_fill")

    def test_shared_turnover_and_cash_can_be_carried_between_outcomes(self):
        first = self.execute(quantity=1, remaining_cash=1.02, remaining_turnover=0.75)
        row = self.row(token_id="yes-b")
        book = adapt_archived_book_v2(row, slug="market-a", token_id="yes-b", as_of=START)
        second = self.execute(book=book, token_id="yes-b", remaining_cash=first["remaining_cash"],
                              remaining_turnover=first["remaining_turnover"], consumed=first["consumed"])
        self.assertAlmostEqual(second["filled_quantity"], 0.5)
        self.assertAlmostEqual(second["remaining_turnover"], 0)
        self.assertGreaterEqual(second["remaining_cash"], 0)

    def test_same_snapshot_does_not_refill_at_next_checkpoint(self):
        first = self.execute()
        now = START + timedelta(minutes=5)
        row = self.row(captured_at_utc=now.isoformat())
        book = adapt_archived_book_v2(row, slug="market-a", token_id="yes-a", as_of=now)
        second = self.execute(book=book, as_of=now, consumed=first["consumed"])
        self.assertEqual(second["action"], "no_fill")
        self.assertEqual(second["filled_quantity"], 0)

    def test_new_source_snapshot_restores_visible_depth(self):
        first = self.execute()
        now = START + timedelta(minutes=1)
        row = self.row(source_timestamp_utc=now.isoformat(), captured_at_utc=now.isoformat())
        book = adapt_archived_book_v2(row, slug="market-a", token_id="yes-a", as_of=now)
        result = self.execute(book=book, as_of=now, consumed=first["consumed"])
        self.assertEqual(result["filled_quantity"], 10)

    def test_slug_isolation_and_wrong_book_rejection(self):
        first = self.execute()
        row = self.row(event_slug="market-b")
        book = adapt_archived_book_v2(row, slug="market-b", token_id="yes-a", as_of=START)
        result = self.execute(book=book, slug="market-b", consumed=first["consumed"])
        self.assertEqual(result["filled_quantity"], 10)
        self.assertEqual(self.execute(book=book)["filled_quantity"], 0)
        self.assertEqual(self.execute(book=book)["reason"], "market_identity_mismatch")

    def test_missing_unsafe_stale_and_malformed_depth_rejected(self):
        bad_rows = [self.row(source_timestamp_utc=None),
                    self.row(captured_at_utc=(START + timedelta(seconds=1)).isoformat()),
                    self.row(source_timestamp_utc=(START - timedelta(minutes=6)).isoformat()),
                    self.row(raw_json='{}'), self.row(raw_json='invalid'),
                    self.row(raw_json=json.dumps({"asks": [{"price": "nan", "size": 4}], "bids": []}))]
        for row in bad_rows:
            book = adapt_archived_book_v2(row, slug="market-a", token_id="yes-a", as_of=START)
            self.assertFalse(book["point_in_time_safe"])
            self.assertEqual(self.execute(book=book)["action"], "no_fill")

    def test_previously_safe_book_expires(self):
        result = self.execute(as_of=START + timedelta(minutes=6))
        self.assertEqual(result["reason"], "unsafe_execution_time")

    def test_zero_depth_and_outside_price_band(self):
        self.assertEqual(self.execute(book=self.book(raw_json='{"asks": [], "bids": []}'))["action"], "no_fill")
        book = self.book(raw_json='{"asks": [{"price": 0.5, "size": 2}, {"price": 0.56, "size": 9}], "bids": []}')
        self.assertEqual(self.execute(book=book)["filled_quantity"], 2)

    def test_sell_cannot_exceed_holdings_and_credit_is_net_of_fees(self):
        result = self.execute(side="sell", position_quantity=2)
        self.assertEqual(result["filled_quantity"], 2)
        self.assertEqual(result["position_quantity"], 0)
        self.assertAlmostEqual(result["cash_delta"], 2 * 0.49 * 0.98)

    def test_extra_slippage_is_separate_and_adverse(self):
        result = self.execute(quantity=1, extra_slippage_bps=50)
        self.assertAlmostEqual(result["average_price"], 0.505)
        self.assertAlmostEqual(result["fees"], 0.505 * 0.02)

    def test_inputs_not_mutated_and_output_json_serializable(self):
        book, consumed = self.book(), {}
        original = copy.deepcopy(book)
        result = self.execute(book=book, consumed=consumed)
        self.assertEqual(book, original)
        self.assertEqual(consumed, {})
        json.dumps(result, allow_nan=False)

    def test_legacy_exports_and_defaults_remain_compatible(self):
        from live_dashboard_server import paper_position_rebalance as server_rebalance
        from strategy_simulator import DEFAULT_SLIPPAGE_BPS, paper_position_rebalance
        self.assertIs(server_rebalance, paper_position_rebalance)
        self.assertEqual(DEFAULT_SLIPPAGE_BPS, 50)


if __name__ == "__main__":
    unittest.main()
