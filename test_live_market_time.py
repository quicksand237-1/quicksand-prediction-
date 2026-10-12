import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from live_market_time import strict_time, live_window_details, classify_live_event
from live_dashboard_server import MultiLiveService, discover_active_slugs

UTC = dt.timezone.utc
BOUNDARY = dt.datetime(2026, 9, 18, 16, tzinfo=UTC)


def event(slug='old', start='2026-09-11T16:00:00Z', end='2026-09-18T16:00:00Z'):
    return {'slug': slug, 'markets': [{'gameStartTime': start}], 'endDate': end}


class WindowTests(unittest.TestCase):
    def test_initial_children_are_parent_managed(self):
        args = argparse.Namespace(dynamic_discovery=True, gamma_url='unused', timeout=20,
                                  proxy=None, refresh_seconds=30)
        with patch('live_dashboard_server.LiveService') as child, patch('live_dashboard_server.GammaClient'):
            MultiLiveService(args, ['a', 'b'])
        for call in child.call_args_list:
            self.assertTrue(call.args[0].parent_managed_slug)
            self.assertFalse(call.args[0].dynamic_discovery)

    def test_boundary_and_beijing_midnight(self):
        old = event()
        new = event('new', '2026-09-18T16:00:00Z', '2026-09-25T16:00:00Z')
        self.assertEqual(classify_live_event(old, BOUNDARY-dt.timedelta(seconds=1))['phase'], 'active')
        self.assertEqual(classify_live_event(old, BOUNDARY)['phase'], 'ended')
        self.assertEqual(classify_live_event(new, BOUNDARY)['phase'], 'active')
        self.assertEqual(classify_live_event(new, BOUNDARY-dt.timedelta(seconds=1))['phase'], 'upcoming')
        self.assertEqual(strict_time('2026-09-19T00:00:00+08:00'), BOUNDARY)

    def test_strict_invalid_and_dst_times(self):
        for value in ['2026-09-18', '2026-09-18 nonsense', '2026-03-08T02:30:00', '2026-11-01T01:30:00']:
            with self.assertRaises(ValueError):
                strict_time(value)
        self.assertEqual(strict_time('2026-09-18T12:00:00'), BOUNDARY)
        self.assertEqual(strict_time('2026-01-18T12:00:00').hour, 17)
        self.assertEqual(strict_time('2026-11-01T01:30:00-04:00').hour, 5)

    def test_rule_timezone_and_cross_year(self):
        data = {'description': 'from December 30 12 PM ET to January 6, 2027 12 PM ET'}
        window = live_window_details(data, 'Asia/Shanghai')
        self.assertTrue(window['valid'])
        self.assertEqual(window['start_utc'], '2026-12-30T17:00:00+00:00')
        self.assertEqual(window['timezone'], 'America/New_York')

    def test_conflicts_and_creation_time_are_excluded(self):
        data = event()
        data['description'] = 'from September 12 12:00 PM ET to September 18, 2026 12:00 PM ET'
        self.assertEqual(live_window_details(data)['reason'], 'structured_rule_conflict')
        data = event()
        data['markets'].append({'gameStartTime': '2026-09-12T16:00:00Z'})
        self.assertFalse(live_window_details(data)['valid'])
        self.assertFalse(live_window_details({'startDate':'2026-09-11T16:00:00Z','endDate':'2026-09-18T16:00:00Z'})['valid'])

    def test_discovery_keeps_overlapping_newest_first(self):
        gamma = Mock()
        gamma.discover_count_events.return_value = [event(), event('overlap','2026-09-15T16:00:00Z','2026-09-22T16:00:00Z'), event('new','2026-09-18T16:00:00Z','2026-09-25T16:00:00Z')]
        self.assertEqual(discover_active_slugs(gamma, BOUNDARY, 'America/New_York'), ['new','overlap'])

    def test_reorders_existing_instances_and_recovers_after_failure(self):
        service = MultiLiveService.__new__(MultiLiveService)
        service.dynamic_discovery = True
        service.args = argparse.Namespace(timezone='America/New_York', db='unused')
        service.database_lock = threading.RLock()
        service.discovery_client = Mock()
        service.discovery_status = {}
        service.retired_services = {}
        old, new = Mock(), Mock()
        old.args.slug, new.args.slug = 'overlap', 'new'
        old.store.conn.execute.return_value.fetchall.return_value = []
        new.store.conn.execute.return_value.fetchall.return_value = []
        service.services = [old, new]
        service.discovery_client.discover_count_events.return_value = [event('overlap','2026-09-15T16:00:00Z','2026-09-22T16:00:00Z'), event('new','2026-09-18T16:00:00Z','2026-09-25T16:00:00Z')]
        service._sync_dynamic_services(BOUNDARY)
        self.assertEqual(service.services, [new, old])
        service.discovery_client.discover_count_events.side_effect = TimeoutError('offline')
        service._sync_dynamic_services(BOUNDARY)
        self.assertEqual(service.services, [])
        with patch.object(service, 'live_markets', return_value=[]):
            self.assertEqual(service.refresh()['slug'], '')
        service.discovery_client.discover_count_events.side_effect = None
        with patch('live_dashboard_server.MarketStore', side_effect=AssertionError('no database')):
            service._sync_dynamic_services(BOUNDARY)
        self.assertEqual(service.services, [new, old])

    def test_frontend_boundary_and_timezone_independence(self):
        html = Path('forecast_dashboard.html').read_text(encoding='utf-8')
        source = html[html.index('      function liveWindowLabel('):html.index('      function aiEndpoint(')]
        harness = '''const assert=require('assert'); const nodes={}; const $=id=>nodes[id] ||= {classList:{add(){},remove(){}}}; const text=()=>{}; const escapeHtml=x=>x; const setInterval=()=>{}; const state={liveSnapshots:[],liveMarket:''}; let shown; const renderLive=x=>shown=x;\n'''
        now = int(BOUNDARY.timestamp()*1000)
        rows = [{'slug': 'old', 'window': {'start_utc':'2026-09-11T16:00:00Z','end_utc':'2026-09-18T16:00:00Z'},'market_discovery':{'active_slugs':['old','new','overlap']}},
                {'slug': 'new','window':{'start_utc':'2026-09-18T16:00:00Z','end_utc':'2026-09-25T16:00:00Z'},'market_discovery':{'active_slugs':['old','new','overlap']}},
                {'slug': 'overlap','window':{'start_utc':'2026-09-15T16:00:00Z','end_utc':'2026-09-22T16:00:00Z'},'market_discovery':{'active_slugs':['old','new','overlap']}}]
        checks = f'''Date.now=()=>{now}; const rows={json.dumps(rows)};
assert.deepEqual(currentLiveRows(rows).map(x=>x.slug),['new','overlap']);
state.liveMarket='old';renderLiveMarkets(rows);assert.equal(state.liveMarket,'new');
state.liveMarket='overlap';renderLiveMarkets(rows);assert.equal(state.liveMarket,'overlap');
const label=liveWindowLabel(rows[0]);assert.ok(label.includes('2026/09/19'));assert.ok(label.includes('纽约时间'));
renderLiveMarkets([]);assert.equal(state.liveMarket,'');assert.equal($('liveMarketSelect').disabled,true);
'''
        result = subprocess.run(['node','-'],input=harness+source+checks,text=True,encoding='utf-8',capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
