import datetime as dt
import unittest
import tempfile
import urllib.error
import csv
import json
import io
import http.client
import socket
import concurrent.futures
import threading
from pathlib import Path
from unittest import mock
from polymarket_pipeline import build_parser, crawl_one
from polymarket_pipeline import ClobClient, PublicDataClient, MarketStore, JsonClient
from hourly_price_research import hourly_rows, main as export_hourly


class HistoryPriceWindowTests(unittest.TestCase):
    def test_dashboard_history_sync_enables_request_delay(self):
        from live_dashboard_server import HistorySyncManager
        manager = HistorySyncManager.__new__(HistorySyncManager)
        manager.service_args = build_parser().parse_args(['crawl'])
        manager.service_args.book_workers = 10
        args = manager._crawl_args(True, '1w', 60)
        self.assertEqual(args.request_delay, 0.2)
        self.assertEqual(args.price_workers, 8)
        self.assertFalse(args.orderbooks)

    def test_actual_http_opens_share_delay_across_workers_and_methods(self):
        client = JsonClient('https://synthetic.invalid', 1)
        client.request_delay = 0.2
        clock = [0.0]
        starts = []
        barrier = threading.Barrier(6)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers.get_content_charset.return_value = 'utf-8'
        response.read.return_value = b'{}'
        def open_request(*args, **kwargs):
            starts.append(clock[0])
            return response
        def sleep(seconds):
            clock[0] += seconds
        def request(index):
            barrier.wait()
            return client.get('/history') if index % 2 else client.post_json('/books', [])
        with mock.patch.object(client.opener, 'open', side_effect=open_request), \
             mock.patch('polymarket_pipeline.time.monotonic', side_effect=lambda: clock[0]), \
             mock.patch('polymarket_pipeline.time.sleep', side_effect=sleep):
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                self.assertEqual(list(pool.map(request, range(6))), [{}] * 6)
        self.assertEqual(len(starts), 6)
        self.assertTrue(all(b - a >= 0.2 - 1e-9 for a, b in zip(starts, starts[1:])))

    def test_retries_use_actual_request_gate(self):
        client = JsonClient('https://synthetic.invalid', 1, retries=2)
        client.request_delay = 0.5
        clock = [0.0]
        starts = []
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers.get_content_charset.return_value = 'utf-8'
        response.read.return_value = b'{}'
        def open_request(*args, **kwargs):
            starts.append(clock[0])
            if len(starts) == 1:
                raise urllib.error.HTTPError('synthetic', 429, 'limited', {}, None)
            return response
        def sleep(seconds):
            clock[0] += seconds
        with mock.patch.object(client.opener, 'open', side_effect=open_request), \
             mock.patch('polymarket_pipeline.time.monotonic', side_effect=lambda: clock[0]), \
             mock.patch('polymarket_pipeline.time.sleep', side_effect=sleep):
            self.assertEqual(client.get('/history'), {})
        self.assertEqual(starts, [0.0, 0.5])

    def test_invalid_request_delay_fails_before_store_or_network(self):
        args = build_parser().parse_args(['crawl'])
        for value in (-1, float('nan'), float('inf'), 'invalid'):
            args.request_delay = value
            with mock.patch('polymarket_pipeline.MarketStore') as store, \
                 mock.patch('polymarket_pipeline.GammaClient') as gamma:
                with self.assertRaisesRegex(ValueError, 'request_delay'):
                    crawl_one(args, 'synthetic')
                store.assert_not_called()
                gamma.assert_not_called()

    def test_review_resolution_never_fills_other_windows_or_safe_features(self):
        checkpoint = dt.datetime(2020, 1, 1, 0, 5, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / 'synthetic.sqlite')
            try:
                for resolution in (None, 300, 10800):
                    store.conn.execute(
                        'insert into market_prices(event_slug,market_id,token_id,price_time_utc,price,captured_at_utc,resolution_seconds,source) values (?,?,?,?,?,?,?,?)',
                        ('test', str(resolution), str(resolution),
                         (checkpoint - dt.timedelta(minutes=2)).isoformat(), .25,
                         (checkpoint + dt.timedelta(days=1)).isoformat(), resolution, 'synthetic_history'))
                store.conn.commit()
                rows = store.historical_prices_in_window('test', checkpoint)
                self.assertEqual(len(rows), 3)
                by_resolution = {r['resolution_seconds']: r for r in rows}
                self.assertTrue(by_resolution[10800]['coarser_than_review_window'])
                self.assertFalse(by_resolution[300]['coarser_than_review_window'])
                self.assertFalse(by_resolution[None]['coarser_than_review_window'])
                self.assertTrue(all(r['review_only'] and not r['point_in_time_safe'] for r in rows))
                self.assertTrue(all(r['price_source'] == 'synthetic_history' for r in rows))
                self.assertEqual(store.prices_in_window('test', checkpoint, 24), [])
                self.assertEqual(store.historical_prices_in_window('test', checkpoint + dt.timedelta(minutes=5)), [])
            finally:
                store.close()

    def test_daily_failure_splits_half_days_including_automatic_resolution(self):
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).replace(second=0, microsecond=0)
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                client = PublicDataClient.__new__(PublicDataClient)
                calls = []
                def get(path, params):
                    calls.append(params)
                    span = params['end'] - params['start']
                    self.assertLessEqual(span, 86400)
                    if fallback and 'bucket_seconds' in params:
                        if params['bucket_seconds'] == 300:
                            return {'data': [], 'pagination': {'has_more': False, 'next_cursor': None}}
                    if span > 43200:
                        raise http.client.IncompleteRead(b'partial')
                    return {'data': [{'timestamp': t, 'price': .3, 'resolution_seconds': 300}
                                     for t in range(params['start'], params['end'], 300)],
                            'pagination': {'has_more': False, 'next_cursor': None}}
                client.get = get
                end = start + dt.timedelta(days=2, hours=1)
                result = client.get_history_window('yes', start, end, 5)
                self.assertEqual(result['planned_windows'], 3)
                self.assertEqual(len(result['history']), 49*12)
                self.assertEqual(result['errors'], [])
                self.assertEqual(len(result['verified_windows']), 5)
                self.assertEqual(sum(r['status'] == 'failed' for r in result['requests']), 2)
                windows = result['verified_windows']
                self.assertEqual(windows[0]['start'], start.isoformat())
                self.assertEqual(windows[-1]['end'], end.isoformat())
                self.assertTrue(all(a['end'] == b['start'] for a, b in zip(windows, windows[1:])))

    def test_rate_limit_permission_and_dns_failures_do_not_split(self):
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).replace(second=0, microsecond=0)
        failures = [urllib.error.HTTPError('synthetic', code, 'failed', {}, None)
                    for code in (401, 403, 429)]
        failures.append(urllib.error.URLError(socket.gaierror('synthetic DNS failure')))
        for error in failures:
            with self.subTest(error=error):
                client = PublicDataClient.__new__(PublicDataClient)
                client.get = mock.Mock(side_effect=error)
                result = client.get_history_window('yes', start, start+dt.timedelta(days=1), 5)
                self.assertEqual(client.get.call_count, 1)
                self.assertEqual(len(result['errors']), 1)
                self.assertEqual(result['verified_windows'], [])

    def test_failed_half_day_is_not_cached_and_successful_sibling_is_retained(self):
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).replace(second=0, microsecond=0)
        client = PublicDataClient.__new__(PublicDataClient)
        def get(path, params):
            if params['start'] == int(start.timestamp()):
                raise urllib.error.URLError(TimeoutError('synthetic timeout'))
            return {'data': [{'timestamp': params['start'], 'price': .4, 'resolution_seconds': 300}],
                    'pagination': {'has_more': False, 'next_cursor': None}}
        client.get = get
        result = client.get_history_window('yes', start, start+dt.timedelta(days=1), 5)
        self.assertEqual(len(result['history']), 1)
        self.assertEqual(len(result['errors']), 5)
        self.assertEqual(result['errors'][0]['end'], (start+dt.timedelta(hours=12)).isoformat())
        self.assertEqual(result['verified_windows'], [dict(start=(start+dt.timedelta(hours=12)).isoformat(),
                                                         end=(start+dt.timedelta(days=1)).isoformat())])

    def test_partial_crawl_preserves_data_and_counts_existing_coverage(self):
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).replace(second=0, microsecond=0)
        end = start + dt.timedelta(days=1)
        event = {'slug': 'synthetic-history', 'startDate': start.isoformat(),
                 'endDate': end.isoformat(), 'markets': [
                     {'id': 'm1', 'groupItemTitle': '0-19', 'outcomes': ['Yes', 'No'],
                      'clobTokenIds': ['yes', 'no']}]}
        fetched = {'history': [{'t': start.isoformat(), 'p': 0, 'resolution_seconds': 300}],
                   'errors': [{'message': 'synthetic timeout'}]}
        with tempfile.TemporaryDirectory() as directory:
            args = build_parser().parse_args(['crawl', '--db', str(Path(directory) / 'test.sqlite'),
                                              '--request-delay', '0'])
            with mock.patch('polymarket_pipeline.GammaClient.get_event', return_value=event), \
                 mock.patch('polymarket_pipeline.XTrackerClient.get_posts', return_value=[]), \
                 mock.patch('polymarket_pipeline.PublicDataClient.get_history_window', return_value=fetched) as history:
                first = crawl_one(args, event['slug'])
                second = crawl_one(args, event['slug'])
            history.assert_called_with('yes', start, end, 5, known_history=mock.ANY, checked=mock.ANY)
            self.assertEqual(first['new_price_rows'], 1)
            self.assertEqual(second['new_price_rows'], 0)
            for result in (first, second):
                self.assertEqual(result['status'], 'partial')
                self.assertEqual(result['price_coverage'][0]['covered_buckets'], 1)
                self.assertEqual(result['price_coverage'][0]['expected_buckets'], 288)
                self.assertTrue(any(e.get('message') == 'synthetic timeout' for e in result['source_errors']))

    def test_exact_old_window_chunks_aliases_and_dedup(self):
        start=dt.datetime(2020,1,1,tzinfo=dt.timezone.utc)
        calls=[]
        client=ClobClient.__new__(ClobClient)
        def get(path, params):
            calls.append(params)
            self.assertNotIn('interval',params)
            self.assertEqual(params['market'],'yes')
            self.assertIn(params['fidelity'],(5,1))
            return {'history':[{'t':params['startTs'],'p':.3},{'timestamp':params['startTs'],'price':.3},
                               {'t':params['endTs'],'p':.4},{'t':params['startTs']-300,'p':.2}]}
        client.get=get
        result=client.get_history_window('yes',start,start+dt.timedelta(days=2))
        self.assertEqual(len(calls),4)
        self.assertEqual(calls[0]['startTs'],int(start.timestamp()))
        self.assertEqual(len(result['history']),2)
        self.assertEqual(result['errors'],[])

    def test_failed_chunk_does_not_discard_other_days(self):
        start=dt.datetime(2020,1,1,tzinfo=dt.timezone.utc)
        client=ClobClient.__new__(ClobClient)
        def get(path,params):
            if params['endTs']-params['startTs'] > 86400:
                raise urllib.error.HTTPError('synthetic', 400, 'range too long', {}, None)
            if params['startTs']==int(start.timestamp()):raise TimeoutError('synthetic timeout')
            return {'history':[{'t':params['startTs'],'p':0}]}
        client.get=get
        result=client.get_history_window('yes',start,start+dt.timedelta(days=2))
        self.assertEqual(len(result['errors']),1)
        self.assertEqual(len(result['history']),2)

    def test_empty_vs_malformed_response(self):
        client=ClobClient.__new__(ClobClient)
        start=dt.datetime(2020,1,1,tzinfo=dt.timezone.utc)
        client.get=lambda *args:{'history':[]}
        result = client.get_history_window('yes',start,start+dt.timedelta(days=1))
        self.assertEqual(result['history'], [])
        self.assertEqual(result['errors'], [])
        client.get=lambda *args:{'error':'invalid query'}
        self.assertEqual(len(client.get_history_window('yes',start,start+dt.timedelta(days=1))['errors']),1)

    def test_v2_pages_and_retention_fallback_preserve_actual_resolution(self):
        start = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        client = PublicDataClient.__new__(PublicDataClient)
        calls = []
        def get(path, params):
            calls.append(params)
            self.assertEqual(path, '/v2/prices-history')
            if 'bucket_seconds' in params:
                return {'data': [], 'pagination': {'has_more': False, 'next_cursor': None}}
            offset = 3600 if params.get('cursor') else 0
            return {'data': [{'timestamp': int(start.timestamp())+offset, 'price': .25,
                              'resolution_seconds': 300}],
                    'pagination': {'has_more': not offset, 'next_cursor': 'next' if not offset else None}}
        client.get = get
        result = client.get_history_window('yes',start,start+dt.timedelta(hours=2),60)
        self.assertEqual(len(calls),3)
        self.assertEqual(len(result['history']),2)
        self.assertEqual(result['history'][0]['resolution_seconds'],300)
        self.assertEqual(result['errors'],[])

    def test_v2_fallback_ladder_records_coarse_resolution_without_calling_it_five_minute(self):
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).replace(second=0, microsecond=0)
        client = PublicDataClient.__new__(PublicDataClient)
        calls = []
        def get(path, params):
            calls.append(dict(params))
            if params.get('bucket_seconds') == 300 or 'bucket_seconds' not in params:
                return {'data': [], 'pagination': {'has_more': False, 'next_cursor': None}}
            return {'data': [{'timestamp': int(start.timestamp()), 'price': .4,
                              'resolution_seconds': params['bucket_seconds']}],
                    'pagination': {'has_more': False, 'next_cursor': None}}
        client.get = get
        result = client.get_history_window('yes', start, start + dt.timedelta(hours=1), 5)
        self.assertEqual([row.get('bucket_seconds') for row in calls], [300, 1800])
        self.assertEqual(result['history'][0]['resolution_seconds'], 1800)
        self.assertFalse(result['attempts'][0]['target_complete'])
        self.assertEqual(result['attempts'][0]['fallback_fidelities_minutes'], [30])

    def test_recent_five_minute_window_is_split_before_500_rows(self):
        start = ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3))
                 .replace(second=0, microsecond=0))
        start -= dt.timedelta(minutes=start.minute % 5)
        end = start + dt.timedelta(days=4)
        client = PublicDataClient.__new__(PublicDataClient)
        calls = []

        def get(path, params):
            calls.append(params)
            self.assertLess(params['end'] - params['start'], 500 * 300)
            rows = [{'timestamp': t, 'price': .4, 'resolution_seconds': 300}
                    for t in range(params['start'], params['end'], 300)]
            return {'data': rows, 'pagination': {'has_more': False, 'next_cursor': None}}

        client.get = get
        result = client.get_history_window('yes', start, end, 5)
        self.assertEqual(result['errors'], [])
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(item.get('returned_rows', 0) < 500 for item in result['requests']))

    def test_retention_routes_old_windows_to_thirty_minutes_without_verifying_five(self):
        now = dt.datetime.now(dt.timezone.utc)
        start = now - dt.timedelta(days=75)
        end = start + dt.timedelta(hours=2)
        client = PublicDataClient.__new__(PublicDataClient)
        calls = []

        def get(path, params):
            calls.append(params)
            self.assertEqual(params.get('bucket_seconds'), 1800)
            return {'data': [{'timestamp': params['start'], 'price': .4,
                              'resolution_seconds': 1800}],
                    'pagination': {'has_more': False, 'next_cursor': None}}

        client.get = get
        result = client.get_history_window('yes', start, end, 5)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['verified_windows'], [dict(start=start.isoformat(), end=end.isoformat())])
        self.assertFalse(result['attempts'][0]['target_complete'])
        self.assertTrue(all(item['effective_fidelity_minutes'] == 30 for item in result['requests']))

    def test_v2_repeating_cursor_keeps_rows_and_reports_error(self):
        start = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        client = PublicDataClient.__new__(PublicDataClient)
        client.get = lambda *args: {'data':[{'timestamp':int(start.timestamp()),'price':0,'resolution_seconds':3600}],
                                   'pagination':{'has_more':True,'next_cursor':'same'}}
        result = client.get_history_window('yes',start,start+dt.timedelta(hours=2),60)
        self.assertEqual(len(result['requests']),2)
        self.assertEqual(result['history'][0]['p'],0)
        self.assertEqual(result['verified_windows'],[])
        self.assertEqual(result['errors'][0]['error'],'PaginationError')

    def test_verified_window_skips_sparse_redownload_and_extends_new_tail(self):
        start = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        end = start+dt.timedelta(days=1)
        client = PublicDataClient.__new__(PublicDataClient)
        client.get = mock.Mock(return_value={'data':[], 'pagination': {'has_more':False,'next_cursor':None}})
        result = client.get_history_window('yes',start,end,60,checked=[(start,end)])
        client.get.assert_not_called()
        result = client.get_history_window('yes',start,end+dt.timedelta(hours=1),60,checked=[(start,end)])
        self.assertTrue(all(r['start']==end.isoformat() for r in result['requests']))

    def test_hourly_export_does_not_use_future_or_coarse_price_as_hourly(self):
        start = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        points = [(start+dt.timedelta(minutes=1),.5,300,'v2'),
                  (start+dt.timedelta(hours=2),.2,10800,'v2')]
        rows = list(hourly_rows('s','m','bin','yes',start,start+dt.timedelta(hours=3),points,1))
        self.assertEqual(rows[0]['status'],'missing')
        self.assertEqual(rows[1]['gross_return_percent'],100)
        self.assertEqual(rows[1]['status'],'previous_observation')
        self.assertEqual(rows[2]['status'],'missing')

    def test_invalid_v2_rows_are_not_silently_marked_complete(self):
        start = dt.datetime(2020,1,1,tzinfo=dt.timezone.utc)
        client = PublicDataClient.__new__(PublicDataClient)
        client.get = lambda *args: {'data':[None, {'timestamp':start.isoformat(),'price':.4},
                                               {'timestamp':start.isoformat(),'price':'NaN'}]}
        result = client.get_history_window('yes',start,start+dt.timedelta(hours=1),60)
        self.assertEqual(len(result['history']),1)
        self.assertEqual(result['requests'][0]['invalid_rows'],2)
        self.assertEqual(len(result['errors']),1)
        self.assertEqual(result['verified_windows'],[])

    def test_hourly_crawl_skips_existing_complete_price_window(self):
        start = dt.datetime(2020,1,1,tzinfo=dt.timezone.utc)
        end = start+dt.timedelta(hours=1)
        event = {'slug':'hourly','startDate':start.isoformat(),'endDate':end.isoformat(),
                 'markets':[{'id':'m','groupItemTitle':'0-19','outcomes':['Yes','No'],'clobTokenIds':['yes','no']}]}
        with tempfile.TemporaryDirectory() as directory:
            args = build_parser().parse_args(['crawl','--db',str(Path(directory)/'test.sqlite'),
                                              '--price-fidelity','60','--request-delay','0'])
            response = {'data':[{'timestamp':int(start.timestamp()),'price':.2,'resolution_seconds':3600}],
                        'pagination': {'has_more':False,'next_cursor':None}}
            with mock.patch('polymarket_pipeline.GammaClient.get_event',return_value=event), \
                 mock.patch('polymarket_pipeline.XTrackerClient.get_posts',return_value=[]), \
                 mock.patch('polymarket_pipeline.PublicDataClient.get',return_value=response) as get:
                first = crawl_one(args,'hourly')
                second = crawl_one(args,'hourly')
            self.assertEqual(get.call_count,1)
            self.assertEqual(first['status'],'complete')
            self.assertEqual(second['status'],'complete')
            self.assertEqual(second['new_price_rows'],0)
            self.assertEqual(second['price_coverage'][0]['covered_buckets'],1)
            self.assertEqual(second['price_coverage'][0]['request_count'],0)
            self.assertTrue(second['price_coverage'][0]['cache_used'])

    def test_hourly_export_writes_csv_without_touching_source_or_network_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.sqlite'
            output = root/'export'
            output.mkdir()
            store = MarketStore(source)
            event = {'slug':'test','startDate':'2020-01-01T00:00:00Z','endDate':'2020-01-01T02:00:00Z',
                     'markets':[{'id':'m','groupItemTitle':'0-19','outcomes':['Yes','No'],
                                 'clobTokenIds':['yes','no'],'outcomePrices':[1,0]}]}
            store.save_event(event,'UTC')
            store.save_prices('test','m','yes',[{'t':'2020-01-01T00:00:00Z','p':.25,'resolution_seconds':3600,'source':'test'}])
            store.close()
            before = source.read_bytes()
            audit = output/'requests.jsonl'
            audit.write_text('retained network audit',encoding='utf-8')
            with mock.patch('sys.argv',['hourly_price_research.py','--db',str(source),'--output-dir',str(output)]), \
                 mock.patch('sys.stdout',new_callable=io.StringIO), \
                 mock.patch('polymarket_pipeline.PublicDataClient.get') as get:
                export_hourly()
                get.assert_not_called()
            self.assertEqual(source.read_bytes(),before)
            self.assertEqual(audit.read_text(encoding='utf-8'),'retained network audit')
            with (output/'hourly_prices.csv').open(encoding='utf-8-sig',newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows),2)
            self.assertEqual(rows[0]['status'],'missing')
            self.assertEqual(rows[0]['gross_profit_per_100'],'')
            self.assertEqual(float(rows[1]['gross_profit_per_100']),300)
            self.assertEqual(rows[1]['status'],'previous_observation')
            summary = json.loads((output/'summary.json').read_text())
            self.assertEqual(summary['available'],1)
            self.assertEqual(summary['missing'],1)
            self.assertEqual(summary['rows'],2)
