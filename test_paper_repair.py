import datetime as dt
import json
import sqlite3
import unittest
from polymarket_pipeline import MarketStore, parse_timestamp
from strategy_simulator import paper_position_rebalance


class PaperRepairTests(unittest.TestCase):
    def test_book_midpoint_display_proxy_does_not_become_price_history(self):
        from polymarket_pipeline import MarketStore
        store = MarketStore.__new__(MarketStore)
        store.conn = sqlite3.connect(':memory:')
        store.conn.executescript('create table market_outcomes(event_slug,market_id,yes_token_id); create table market_prices(event_slug,market_id,token_id,price_time_utc,captured_at_utc,price);')
        store.conn.execute("insert into market_outcomes values ('s','m','yes')")
        self.assertEqual(store.conn.execute("select count(*) from market_prices").fetchone()[0],0)
        store.conn.close()
    def test_numeric_string_time(self):
        expected = dt.datetime(2026, 9, 11, 16, 0, 23, 515000, dt.timezone.utc)
        for value in ('1789142423515', 1789142423515, '1789142423.515'):
            self.assertEqual(parse_timestamp(value), expected)
        self.assertIsNone(parse_timestamp(float('inf')))

    def test_shared_cash_turnover_and_high_edge_hold(self):
        positions, account = {}, {'cash': 100.0}
        rows = [dict(outcome_label=str(i), paper_net_edge_after_cost=.1,
                     point_in_time_safe=True, best_ask=.2, best_bid=.19,
                     ask_depth_5=1000, bid_depth_5=1000) for i in range(4)]
        for checkpoint in range(12):
            actions, _, _ = paper_position_rebalance(rows, positions, account=account,
                checkpoint_index=checkpoint, paper_budget=100)
            self.assertLessEqual(account['turnover'], 25 + 1e-8)
            self.assertGreaterEqual(account['cash'], -1e-8)
            self.assertAlmostEqual(account['cash'] + sum(p['quantity']*p['avg_cost'] for p in positions.values()),100)
        rows[0]['paper_net_edge_after_cost'] = .08
        actions, _, _ = paper_position_rebalance(rows, positions, account=account,
            checkpoint_index=20, paper_budget=100)
        self.assertTrue(all(a['quantity_delta'] >= 0 for a in actions))

    def test_recover_archive_without_mutation_and_skip_newer_invalid(self):
        store = MarketStore.__new__(MarketStore)
        store.conn = sqlite3.connect(':memory:')
        store.conn.executescript('''
          create table market_outcomes(event_slug,market_id,label,yes_token_id);
          create table market_orderbooks(event_slug,market_id,token_id,source_timestamp_utc,
          captured_at_utc,best_bid,best_ask,spread,bid_depth_5,ask_depth_5,depth_imbalance,
          last_trade_price,last_trade_side,raw_json);
          insert into market_outcomes values ('s','m','0-19','yes');
        ''')
        cp = dt.datetime(2026,9,11,16,5,tzinfo=dt.timezone.utc)
        for minutes, token, source in [(1,'yes',cp-dt.timedelta(minutes=4)),(2,'yes',cp+dt.timedelta(minutes=1)),(3,'no',cp)]:
            store.conn.execute('insert into market_orderbooks values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('s','m',token,'1789-01-01T00:00:00+00:00',(cp-dt.timedelta(minutes=5-minutes)).isoformat(),
                 .4,.5,.1,10,10,0,None,None,json.dumps({'timestamp':str(int(source.timestamp()*1000))})))
        store.conn.commit()
        before=store.conn.total_changes
        rows=store.orderbooks_in_window('s',cp,24,5)
        yes=next(row for row in rows if row['token_id']=='yes')
        self.assertTrue(yes['point_in_time_safe'])
        self.assertTrue(yes['source_time_recovered'])
        self.assertEqual(yes['source_timestamp_utc'],(cp-dt.timedelta(minutes=4)).isoformat())
        self.assertFalse(next(row for row in rows if row['token_id']=='no')['point_in_time_safe'])
        self.assertEqual(before,store.conn.total_changes)
        store._replay_book_tokens = {}
        self.assertEqual(store.orderbooks_in_window('s',cp,24,5), rows)
        self.assertEqual(store.orderbooks_in_window('s',cp,24,5), rows)
        store.conn.close()

    def test_entry_window_and_zero_position_valuation(self):
        row=dict(outcome_label='x',point_in_time_safe=True,best_bid=.4,best_ask=.5,
                 ask_depth_5=10,bid_depth_5=10,paper_net_edge_after_cost=.1)
        actions,_,_=paper_position_rebalance([row],{},checkpoint_index=12,paper_budget=10)
        self.assertEqual(actions[0]['reason'],'entry_window_closed')
        self.assertEqual(actions[0]['unrealized_pnl'],0)

if __name__ == '__main__':
    unittest.main()
