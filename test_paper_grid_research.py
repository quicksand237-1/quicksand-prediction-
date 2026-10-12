import unittest

from paper_grid_research import _metrics, _shared_pool_metrics, run_grid_research


def market(slug, pnl, invested=50, settled=True, filled=True, end=None):
    return {
        "slug": slug,
        "paper_final_pnl": pnl,
        "paper_total_buy_cost": invested,
        "paper_settled": settled,
        "paper_entry_actual_utc": "2026-01-01T00:00:00Z" if filled else None,
        "window_end_utc": end or slug,
    }


class PaperGridResearchTests(unittest.TestCase):
    def test_metrics_materialize_iterators_and_exclude_unfilled(self):
        rows = iter([market("a", 10), market("b", 50, filled=False), market("c", None, settled=False)])
        result = _metrics(rows, 100)
        self.assertEqual(result["markets_total"], 3)
        self.assertEqual(result["filled_and_settled_markets"], 1)
        self.assertEqual(result["not_filled_markets"], 1)
        self.assertAlmostEqual(result["return_on_invested"], .2)

    def test_shared_pool_blocks_market_when_cash_is_insufficient(self):
        result = _shared_pool_metrics([market("a", 1, invested=100), market("b", 2, invested=110)], 100)
        self.assertEqual(result["filled_and_settled_markets"], 1)
        self.assertEqual(result["capital_blocked_markets"], 1)
        self.assertEqual(result["final_capital"], 101)

    def test_grid_reuses_generator_inputs_and_marks_center(self):
        calls = []

        def replay(entry, coverage):
            calls.append((entry, coverage))
            return {"markets": [market("m1", 1), market("m2", -1), market("m3", 2), market("m4", 0)]}

        result = run_grid_research(replay, entry_minutes=(v for v in [0, 30]), coverage_grid=(v for v in [.01, .5]))
        self.assertEqual(len(calls), 4)
        self.assertEqual(result["entry_minutes"], [0, 30])
        self.assertEqual(result["coverage_grid"], [.01, .5])
        center = next(item for item in result["results"] if item["config"]["probability_policy"] == "center")
        self.assertEqual(center["config"]["label"], "entry_0m_center")
        self.assertTrue(result["do_not_auto_trade"])

    def test_invalid_budget_or_coverage_rejected(self):
        with self.assertRaises(ValueError):
            run_grid_research(lambda *_: {"markets": []}, market_budget=0)
        with self.assertRaises(ValueError):
            run_grid_research(lambda *_: {"markets": []}, entry_minutes=[0], coverage_grid=[0])


if __name__ == "__main__":
    unittest.main()
