import unittest

from market_dynamics_research import (
    DEFAULT_FEATURE_NAMES,
    build_snapshot_features,
    attach_forward_targets,
    evaluate_fixed_entry_times,
    fit_mapping,
    walk_forward_entry_selection,
)


class MarketDynamicsResearchTests(unittest.TestCase):
    def test_forward_targets_use_later_checkpoints_only(self):
        rows = [
            {"market_id": "m", "checkpoint_utc": "2026-10-04T00:00:00Z", "market_expected_count": 10, "final_value": 15},
            {"market_id": "m", "checkpoint_utc": "2026-10-04T00:05:00Z", "market_expected_count": 11, "final_value": 15},
            {"market_id": "m", "checkpoint_utc": "2026-10-04T00:15:00Z", "market_expected_count": 13, "final_value": 15},
        ]
        labeled = attach_forward_targets(rows, 10)
        self.assertEqual(len(labeled), 2)
        self.assertEqual(labeled[0]["short_change"], 3)
        self.assertEqual(labeled[0]["final_change"], 5)

    def test_snapshot_features_keep_missing_values_and_compute_rates(self):
        features = build_snapshot_features({
            "generated_at_utc": "2026-10-04T00:10:00+00:00",
            "window": {"start_utc": "2026-10-04T00:00:00+00:00"},
            "count": {"current": 12, "posts_5m": 4, "posts_15m": 9, "tracker_refresh_age_seconds": 2},
            "prediction": {"final_count": 20},
            "market": {"expected_count": 18},
        })
        self.assertEqual(features["elapsed_minutes"], 10)
        self.assertEqual(features["post_rate_5m"], .8)
        self.assertAlmostEqual(features["post_acceleration"], .2)
        self.assertEqual(features["model_market_gap"], 2)
        self.assertIsNone(features["best_ask"])

    def test_mapping_returns_coefficients_intervals_and_coverage(self):
        rows = []
        for index in range(24):
            row = {name: float(index * (position + 1) + position) for position, name in enumerate(DEFAULT_FEATURE_NAMES)}
            row["short_change"] = 2 * row["elapsed_minutes"] + 3 * row["current_count"] + 1
            row["final_change"] = -row["elapsed_minutes"] + .5 * row["current_count"]
            rows.append(row)
        result = fit_mapping(rows, alpha=0.1)
        self.assertEqual(result["rows"], 24)
        self.assertIn("short_change", result["coefficients"])
        self.assertIn("elapsed_minutes", result["confidence_intervals_95"]["final_change"])

    def test_fixed_entry_and_walk_forward_are_paper_only(self):
        rows = []
        for market_index in range(12):
            for minute, value in ((5, 1.0), (10, 1.1)):
                rows.append({
                    "market_id": f"m{market_index}", "market_end_utc": f"2026-10-{market_index + 1:02d}",
                    "entry_elapsed_minutes": minute, "point_in_time_safe": True,
                    "paper_settled": True, "invested": 1.0, "final_value": value + market_index * .001,
                })
        result = evaluate_fixed_entry_times(rows, (5, 10))
        self.assertEqual(result["recommended_entry_elapsed_minutes"], 10)
        self.assertTrue(result["paper_only"])
        walk = walk_forward_entry_selection(rows, (5, 10), min_training_markets=3)
        self.assertTrue(walk["walk_forward"])
        self.assertTrue(all(item["entry_elapsed_minutes"] in (5, 10) for item in walk["selections"]))


if __name__ == "__main__":
    unittest.main()
