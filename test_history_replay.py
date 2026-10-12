import argparse
import math
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from history_replay import ReplayControl, ReplayJobs, isolated_review, read_history_catalog
from replay_probability import guarded_forecast
from strategy_simulator import paper_position_rebalance


class NoLiveMarketReplayTests(unittest.TestCase):
    def test_multi_service_without_live_markets_replays_readonly_database(self):
        from live_dashboard_server import MultiLiveService
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'history.sqlite'
            with sqlite3.connect(db) as conn:
                conn.execute('create table marker(value)')
            conn.close()
            service=MultiLiveService.__new__(MultiLiveService)
            service.services=[]
            service.retired_services={}
            service.args=argparse.Namespace(db=str(db),model='unused.json')
            def review(reader, **kwargs):
                with self.assertRaises(sqlite3.OperationalError):
                    reader.store.conn.execute('insert into marker values(1)')
                return {'records':[{'slug':kwargs['slug']}]}
            with mock.patch('polymarket_pipeline.load_model'), \
                 mock.patch('polymarket_pipeline.MarketStore.__init__',side_effect=AssertionError('must not migrate')), \
                 mock.patch('live_dashboard_server.LiveService._backtest_review',review):
                result=service.backtest_review(slug='historical')
            self.assertEqual(result['records'][0]['slug'],'historical')


class ReplayTests(unittest.TestCase):
    def test_history_catalog_needs_no_collector_lock_or_large_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'catalog.sqlite'
            conn=sqlite3.connect(db)
            conn.execute('create table market_events(slug,game_start_utc,end_date_utc)')
            conn.executemany('insert into market_events values (?,?,?)', [('old','2026-01-01','2026-01-08'),('new','2026-02-01','2026-02-08')])
            conn.commit()
            lock=threading.Lock()
            with lock:
                self.assertEqual([r['slug'] for r in read_history_catalog(db)],['new','old'])
                self.assertEqual(len(read_history_catalog(db,1)),1)
            conn.close()

    def test_reader_does_not_wait_for_collector_lock_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "test.sqlite"
            original = sqlite3.connect(db)
            original.execute("create table marker(value)")
            original.commit()
            class Service:
                def _backtest_review(self, _control, **kwargs):
                    with self.lock:
                        with self_test.assertRaises(sqlite3.OperationalError):
                            self.store.conn.execute("insert into marker values(1)")
                        return self.store.conn.execute("select count(*) from marker").fetchone()[0]
            self_test = self
            service = Service()
            service.args = argparse.Namespace(db=db)
            service.store = SimpleNamespace(conn=original)
            service.lock = threading.Lock()
            with service.lock:
                self.assertEqual(isolated_review(service), 0)
                self.assertEqual(isolated_review(service), 0)
            original.close()

    def test_cancel_running_a_then_b_and_repeat_a(self):
        started = threading.Event()
        def review(slug, _control):
            if slug == "slow":
                started.set()
                while True:
                    _control.check()
                    time.sleep(.005)
            return {"slug": slug, "records": []}
        jobs = ReplayJobs(review)
        first = jobs.submit({"slug": "slow"})["job_id"]
        self.assertTrue(started.wait(1))
        jobs.status(first, cancel=True)
        for slug in ("b", "a", "b"):
            key = jobs.submit({"slug": slug})["job_id"]
            deadline = time.monotonic()+2
            while jobs.status(key)["state"] in {"queued", "running"} and time.monotonic()<deadline:
                time.sleep(.005)
            self.assertEqual(jobs.status(key)["result"]["slug"], slug)
        self.assertLessEqual(len(jobs.jobs), 3)

    def test_deadline_and_sql_interrupt(self):
        with self.assertRaises(TimeoutError):
            ReplayControl(seconds=-1).check()

    def test_supersede_without_received_job_id_and_ignore_late_submit(self):
        started = threading.Event()
        def review(_control, **kwargs):
            started.set()
            while True:
                _control.check()
                time.sleep(.005)
        jobs = ReplayJobs(review)
        first = jobs.submit({}, "tab", 1)["job_id"]
        self.assertTrue(started.wait(1))
        second = jobs.submit({}, "tab", 2)["job_id"]
        self.assertTrue(jobs.jobs[first]["control"].cancelled.is_set())
        with self.assertRaises(ValueError):
            jobs.submit({}, "tab", 1)
        self.assertFalse(jobs.jobs[second]["control"].cancelled.is_set())
        jobs.status(second, cancel=True)


class ProbabilityGuardTests(unittest.TestCase):
    class Model:
        def predict(self, features):
            return .55 * features[3] + .45 * features[6]

    def forecast(self, current, elapsed, remaining, prior=1.4, ranges=None):
        f = [current, elapsed, remaining, current/max(elapsed, 1e-9)*(elapsed+remaining), 0, 0, current+prior*remaining]
        return guarded_forecast(self.Model(), f, ranges or [("0-199", None, 199), ("200-499",200,499),("500+",500,None)], prior)

    def test_opening_burst_does_not_become_500_yes(self):
        forecast, probabilities, guard = self.forecast(4, 1/12, 168-1/12)
        self.assertGreater(guard["raw_forecast"], 4000)
        self.assertLess(forecast, 350)
        self.assertLess(probabilities[-1], .001)
        self.assertAlmostEqual(sum(probabilities), 1, places=9)

    def test_remaining_support_and_convergence(self):
        forecast, probabilities, guard = self.forecast(280, 168, 0)
        self.assertEqual(forecast, 280)
        self.assertEqual(probabilities, [0., 1., 0.])
        self.assertEqual(guard["variance"], 0)
        self.assertTrue(all(math.isfinite(p) for p in probabilities))

    def test_incomplete_bins_are_not_renormalized_and_high_rate_allowed(self):
        _, probabilities, _ = self.forecast(4, 1/12, 168-1/12, ranges=[("500+",500,None)])
        self.assertLess(sum(probabilities), .001)
        forecast, probabilities, _ = self.forecast(600, 144, 24, prior=4)
        self.assertGreater(forecast, 600)
        self.assertAlmostEqual(probabilities[-1], 1)

    def test_guard_blocks_new_buys_but_allows_exit(self):
        row = dict(outcome_label="500+", point_in_time_safe=True, entry_allowed=False,
                   entry_block_reason="probability_risk_guard", paper_net_edge_after_cost=.2,
                   best_ask=.01, best_bid=.009, ask_depth_5=1000,bid_depth_5=1000)
        positions = {}
        actions, _, _ = paper_position_rebalance([row], positions, checkpoint_index=0, paper_budget=100)
        self.assertEqual(actions[0]["quantity_delta"],0)
        self.assertEqual(actions[0]["reason"],"probability_risk_guard")
        positions["500+"] = dict(quantity=1, avg_cost=.01, realized_pnl=0)
        row["paper_net_edge_after_cost"] = -.1
        actions, _, _ = paper_position_rebalance([row], positions, checkpoint_index=30, paper_budget=100)
        self.assertEqual(actions[0]["quantity_delta"],-1)


if __name__ == "__main__":
    unittest.main()
