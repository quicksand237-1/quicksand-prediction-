"""Display-only acquisition evidence and conservative price time checks."""
import bisect
import datetime as dt
import json
import math


def price_known_by(timestamp, captured, resolution, checkpoint):
    if timestamp is None or captured is None or captured > checkpoint:
        return False
    # Zero can be either a terminal tick or a settlement; unknowns stay review-only.
    return (isinstance(resolution, (int, float)) and math.isfinite(resolution)
            and resolution > 0
            and timestamp + dt.timedelta(seconds=resolution) <= checkpoint)


class AcquisitionEvidence:
    def __init__(self, conn, slug, parse_timestamp):
        self.parse_timestamp = parse_timestamp
        self.outcomes = conn.execute(
            'select market_id,yes_token_id,label from market_outcomes where event_slug=?', (slug,)
        ).fetchall()
        self.by_token = {}
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
        if 'history_price_checks' in tables:
            for market, token, source, start, end, fidelity, finished in conn.execute(
                    'select market_id,token_id,source_url,start_utc,end_utc,fidelity,checked_at_utc '
                    'from history_price_checks where event_slug=?', (slug,)):
                self._add(market, token, dict(start=start, end=end, target_seconds=fidelity*60,
                          finished_at_utc=finished, status='ok', pagination_complete=True,
                          source_url=source, evidence_kind='legacy_success_receipt'))
        if 'history_price_attempts' in tables:
            for market, token, source, raw in conn.execute(
                    'select market_id,token_id,source_url,evidence_json '
                    'from history_price_attempts where event_slug=? order by id', (slug,)):
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError('采集审计记录无效')
                self._add(market, token, {**record, 'source_url': source, 'evidence_kind': 'attempt'})
        # Older failed pages live in archives; project only the small audit array.
        if 'prediction_archive' in tables:
            for captured, raw in conn.execute(
                    "select captured_at_utc,json_extract(payload_json,'$.price_requests') "
                    "from prediction_archive where event_slug=? "
                    "and json_type(payload_json,'$.price_requests')='array'", (slug,)):
                for page in json.loads(raw):
                    if page.get('status') != 'failed':
                        continue
                    for market, token, _ in self.outcomes:
                        if str(token) == str(page.get('token_id')):
                            self._add(market, token, dict(
                                start=page.get('start'), end=page.get('end'),
                                target_seconds=(page.get('fidelity') or 0)*60,
                                finished_at_utc=page.get('finished_at_utc') or captured,
                                status='failed', pagination_complete=False,
                                errors=[page], evidence_kind='legacy_failed_page'))
        self.starts = {}
        for key, records in self.by_token.items():
            records.sort(key=lambda r: r['_start'])
            self.starts[key] = [r['_start'] for r in records]

    def _add(self, market, token, record):
        start, end = self.parse_timestamp(record.get('start')), self.parse_timestamp(record.get('end'))
        if not start or not end or end <= start:
            raise ValueError('采集审计时间范围无效')
        self.by_token.setdefault((str(market), str(token)), []).append(
            {**record, '_start': start, '_end': end})

    def status(self, label, checkpoint, minutes, price_row):
        start = checkpoint - dt.timedelta(minutes=minutes)
        seconds = minutes*60
        resolution = price_row.get('resolution_seconds') if price_row else None
        source = price_row.get('price_source') if price_row else None
        target_available = bool(price_row and source != 'archived_book_midpoint'
                                and resolution is not None and 0 < resolution <= seconds)
        candidates = []
        for market, token, outcome in self.outcomes:
            if outcome != label:
                continue
            key = (str(market), str(token))
            records = self.by_token.get(key, [])
            upper = bisect.bisect_left(self.starts.get(key, []), checkpoint)
            candidates.extend(r for r in records[:upper] if r['_end'] > start)
        # A success must cover the entire window, possibly through adjacent receipts.
        relevant = [r for r in candidates if 0 < r.get('target_seconds', 0) <= seconds
                    or r.get('status') != 'ok']
        relevant.sort(key=lambda r: (r.get('finished_at_utc', ''), r.get('evidence_kind') == 'attempt'), reverse=True)
        latest = relevant[0] if relevant else None
        success = [r for r in relevant if r.get('status') == 'ok' and r.get('pagination_complete')]
        covered_to = start
        used = []
        for record in sorted(success, key=lambda r: r['_start']):
            if record['_start'] <= covered_to and record['_end'] > covered_to:
                covered_to = record['_end']
                used.append(record)
        complete = covered_to >= checkpoint
        if target_available:
            status = 'available'
        elif latest and latest.get('status') != 'ok':
            status = 'fetch_failed'
        elif complete:
            status = 'attempted_missing'
        else:
            status = 'evidence_insufficient' if price_row or candidates else 'not_attempted'
        evidence = latest or (used[-1] if used else None)
        summary = None if evidence is None else {
            key: evidence.get(key) for key in ('start', 'end', 'target_seconds', 'finished_at_utc',
                'status', 'pagination_complete', 'unique_rows', 'resolutions', 'source_url', 'evidence_kind')}
        if summary is not None:
            summary['window_verified'] = complete
            summary['errors'] = [{key: error.get(key) for key in
                ('error', 'message', 'http_status', 'error_code', 'trace_id', 'stage')}
                for error in evidence.get('errors', [])]
        return dict(price_acquisition_status=status, price_acquisition_evidence=summary,
                    price_target_available=target_available,
                    price_target_seconds=seconds)
