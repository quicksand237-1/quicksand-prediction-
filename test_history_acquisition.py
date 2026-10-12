import datetime as dt
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from history_acquisition import AcquisitionEvidence, price_known_by
from polymarket_pipeline import MarketStore, PublicDataClient, JsonClient, parse_timestamp, build_parser, crawl_one


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.store = MarketStore(Path(self.root.name)/'test.sqlite')
        self.addCleanup(self.store.close)
        self.start = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        self.end = self.start + dt.timedelta(minutes=5)
        self.store.conn.execute("insert into market_outcomes(event_slug,market_id,label,yes_token_id,raw_json) "
                                "values ('s','m','bin','t','{}')")
        self.store.conn.commit()

    def attempt(self, status='ok', start=None, end=None, finished=None, **extra):
        record = dict(start=(start or self.start).isoformat(), end=(end or self.end).isoformat(),
                      finished_at_utc=(finished or self.end).isoformat(), target_seconds=300,
                      status=status, pagination_complete=status == 'ok', storage_complete=status == 'ok',
                      unique_rows=0, resolutions=[], errors=[])
        record.update(extra)
        self.store.save_history_attempts('s','m','t','synthetic', [record])

    def status(self, price=None):
        return AcquisitionEvidence(self.store.conn, 's', parse_timestamp).status('bin',self.end,5,price)

    def test_no_evidence_empty_success_and_latest_failure(self):
        self.assertEqual(self.status()['price_acquisition_status'], 'not_attempted')
        self.attempt()
        status = self.status()
        self.assertEqual(status['price_acquisition_status'], 'attempted_missing')
        self.assertTrue(status['price_acquisition_evidence']['window_verified'])
        self.attempt('failed', finished=self.end+dt.timedelta(seconds=1),
                     errors=[dict(http_status=429, error_code='rate_limited', trace_id='test')])
        self.assertEqual(self.status()['price_acquisition_status'], 'fetch_failed')
        self.attempt(finished=self.end+dt.timedelta(seconds=2))
        self.assertEqual(self.status()['price_acquisition_status'], 'attempted_missing')

    def test_adjacent_receipts_partial_overlap_and_expired_receipt(self):
        middle = self.start+dt.timedelta(minutes=2)
        self.attempt(end=middle)
        self.assertEqual(self.status()['price_acquisition_status'], 'evidence_insufficient')
        self.attempt(start=middle)
        self.assertEqual(self.status()['price_acquisition_status'], 'attempted_missing')
        self.store.conn.execute('delete from history_price_attempts')
        self.store.conn.execute('insert into history_price_checks values (?,?,?,?,?,?,?,?)',
            ('s','m','t','synthetic',self.start.isoformat(),self.end.isoformat(),5,
             (self.end-dt.timedelta(days=30)).isoformat()))
        self.store.conn.commit()
        self.assertEqual(self.status()['price_acquisition_status'], 'attempted_missing')

    def test_samples_unknown_coarse_and_midpoint_are_distinct(self):
        self.assertEqual(self.status({'resolution_seconds':300})['price_acquisition_status'], 'available')
        for row in ({'resolution_seconds':None}, {'resolution_seconds':10800},
                    {'resolution_seconds':0}, {'resolution_seconds':300,'price_source':'archived_book_midpoint'}):
            self.assertEqual(self.status(row)['price_acquisition_status'], 'evidence_insufficient')
        self.attempt(target_seconds=3600)
        self.assertEqual(self.status()['price_acquisition_status'], 'evidence_insufficient')

    def test_legacy_failure_new_success_and_absent_new_table(self):
        page = dict(token_id='t',start=self.start.isoformat(),end=self.end.isoformat(),
                    fidelity=5,status='failed',http_status=503)
        self.store.save_prediction_archive(event_slug='s',captured_at_utc=self.end,
            refresh_status='partial',payload={'price_requests':[page]})
        self.assertEqual(self.status()['price_acquisition_status'],'fetch_failed')
        self.attempt(finished=self.end+dt.timedelta(seconds=1))
        self.assertEqual(self.status()['price_acquisition_status'],'attempted_missing')
        self.store.conn.execute('drop table history_price_attempts')
        self.assertEqual(self.status()['price_acquisition_status'],'fetch_failed')

    def test_read_failure_is_not_empty_data(self):
        self.store.conn.execute('drop table market_outcomes')
        with self.assertRaises(sqlite3.Error):
            self.status()

    def test_bucket_time_safety_and_safe_older_fallback(self):
        for resolution in (None,0,10800):
            self.assertFalse(price_known_by(self.start,self.start,resolution,self.end))
        self.assertTrue(price_known_by(self.start,self.start,300,self.end))
        self.assertFalse(price_known_by(self.start,self.end+dt.timedelta(seconds=1),300,self.end))
        for offset,resolution in ((0,300),(120,300),(180,None),(240,0)):
            source = self.start+dt.timedelta(seconds=offset)
            self.store.conn.execute('insert into market_prices values (?,?,?,?,?,?,?,?)',
                ('s','m','t',source.isoformat(),.3,source.isoformat(),resolution,'data-api-v2'))
        self.store.conn.commit()
        rows = self.store.prices_at('s',self.end,24)
        self.assertEqual(rows[0]['source_price_time_utc'],self.start.isoformat())
        self.assertEqual(len(self.store.prices_in_window('s',self.end,24)),1)
        self.assertEqual(len(self.store.latest_prices('s',self.end,24)),1)
        self.assertTrue(self.store.historical_prices_in_window('s',self.end)[0]['review_only'])

    def test_fill_fingerprint_preserves_multiple_fills_same_transaction(self):
        rows = [dict(transaction_hash='hash',token_id='t',timestamp=int(self.start.timestamp()),
                     price=.3,size=size,side='BUY') for size in (1,2)]
        self.assertEqual(self.store.save_market_trades('s','m',rows),2)
        self.assertEqual(self.store.save_market_trades('s','m',rows),0)

    def test_successful_empty_crawl_persists_attempts_and_storage_failure(self):
        event = dict(slug='s',startDate=self.start.isoformat(),endDate=self.end.isoformat(),markets=[
            dict(id='m',groupItemTitle='bin',outcomes=['Yes','No'],clobTokenIds=['t','no'])])
        args = build_parser().parse_args(['crawl','--db',str(Path(self.root.name)/'crawl.sqlite'),
                                         '--request-delay','0'])
        response = {'data':[], 'pagination':{'has_more':False,'next_cursor':None}}
        with mock.patch('polymarket_pipeline.GammaClient.get_event',return_value=event), \
             mock.patch('polymarket_pipeline.XTrackerClient.get_posts',return_value=[]), \
             mock.patch.object(PublicDataClient,'get',return_value=response):
            result = crawl_one(args,'s')
        self.assertEqual(result['price_errors'],0)
        reader = sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True)
        self.addCleanup(reader.close)
        self.assertEqual(AcquisitionEvidence(reader,'s',parse_timestamp).status('bin',self.end,5,None)
                         ['price_acquisition_status'],'attempted_missing')
        with mock.patch('polymarket_pipeline.GammaClient.get_event',return_value=event), \
             mock.patch('polymarket_pipeline.XTrackerClient.get_posts',return_value=[]), \
             mock.patch.object(PublicDataClient,'get',return_value=response), \
             mock.patch.object(MarketStore,'save_prices',side_effect=sqlite3.OperationalError('synthetic disk failure')):
            args.force_prices = True
            result = crawl_one(args,'s')
        self.assertGreater(result['price_errors'],0)
        self.assertEqual(AcquisitionEvidence(reader,'s',parse_timestamp).status('bin',self.end,5,None)
                         ['price_acquisition_status'],'fetch_failed')


class RequestTests(unittest.TestCase):
    def test_trade_cursor_filters_empty_page_and_invalid_condition(self):
        client = PublicDataClient.__new__(PublicDataClient)
        client.get = mock.Mock(side_effect=[
            dict(data=[],pagination=dict(has_more=True,next_cursor='next')),
            dict(data=[{'size':1}],pagination=dict(has_more=False,next_cursor=None))])
        self.assertEqual(client.get_trades('0x'+'a'*64),[{'size':1}])
        first,second = [call.args[1] for call in client.get.call_args_list]
        self.assertEqual(first,{key:value for key,value in second.items() if key!='cursor'})
        self.assertNotIn('start',first)
        with self.assertRaises(ValueError):
            client.get_trades('123')

    def test_trade_partial_page_never_reported_complete(self):
        client = PublicDataClient.__new__(PublicDataClient)
        for pagination in ({},dict(has_more=True,next_cursor='same'),dict(has_more=False,next_cursor='extra')):
            client.get = mock.Mock(return_value=dict(data=[],pagination=pagination))
            with self.assertRaises(ValueError):
                client.get_trades('0x'+'a'*64)

    def test_retry_after_is_shared_and_error_evidence_retained(self):
        for code in (429,503):
            client = JsonClient('https://synthetic.invalid',1,retries=2)
            clock = [0.0]
            starts = []
            error = urllib.error.HTTPError('synthetic',code,'busy',{'Retry-After':'10','x-trace-id':'header'},
                io.BytesIO(json.dumps(dict(code='rate_limited' if code==429 else 'request_timeout',
                                          retryable=True,trace_id='body')).encode()))
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.headers.get_content_charset.return_value = 'utf-8'
            response.read.return_value = b'{}'
            def open_request(*args,**kwargs):
                starts.append(clock[0])
                if len(starts)==1:
                    raise error
                return response
            def sleep(seconds):
                clock[0] += seconds
            with mock.patch.object(client.opener,'open',side_effect=open_request), \
                 mock.patch('polymarket_pipeline.time.monotonic',side_effect=lambda:clock[0]), \
                 mock.patch('polymarket_pipeline.time.sleep',side_effect=sleep):
                client.get('/v2/prices-history')
            self.assertGreaterEqual(starts[1]-starts[0],10)
            self.assertEqual(error.trace_id,'body')
            self.assertEqual(error.retry_after_seconds,10)

    def test_price_missing_pagination_is_failed_and_keeps_valid_rows(self):
        start = dt.datetime(2026,8,1,tzinfo=dt.timezone.utc)
        client = PublicDataClient.__new__(PublicDataClient)
        client.get = mock.Mock(return_value=dict(data=[dict(timestamp=int(start.timestamp()),price=.3,resolution_seconds=300)]))
        result = client.get_history_window('t',start,start+dt.timedelta(minutes=5),5)
        self.assertEqual(len(result['history']),1)
        self.assertEqual(result['verified_windows'],[])
        self.assertEqual(result['attempts'][0]['status'],'failed')


if __name__ == '__main__':
    unittest.main()
