"""Bounded history repair. Sampling gaps are not transport failures."""
import datetime as dt
import hashlib
import http.client
import json
import math
import time
import urllib.error

DEFAULT_FIDELITY_MINUTES = 5
MAX_ABSOLUTE_WINDOW = dt.timedelta(days=15)
MAX_RETRY_SPLIT_WINDOW = dt.timedelta(hours=12)
MIN_RETRY_SPLIT_WINDOW = dt.timedelta(hours=1)
FIVE_MINUTE_RETENTION = dt.timedelta(days=60)
THIRTY_MINUTE_RETENTION = dt.timedelta(days=90)
MAX_RESPONSE_ROWS = 500
SAFE_RESPONSE_ROWS = 450
FALLBACK_FIDELITIES_MINUTES = (30, 60, 180, 720)


def _retention_route(lower, upper, fidelity, now):
    """Choose the first usable bucket without relabeling a coarser source."""
    if fidelity != DEFAULT_FIDELITY_MINUTES:
        return fidelity, f"requested_{fidelity}m"
    if upper <= now - THIRTY_MINUTE_RETENTION:
        return 180, "long_term_3h"
    if upper <= now - FIVE_MINUTE_RETENTION:
        return 30, "thirty_minute_90d"
    return 5, "five_minute_60d"


def _window_limit(sample):
    """Keep a single request comfortably below the documented 500-row cap."""
    if sample is None or sample <= DEFAULT_FIDELITY_MINUTES:
        return dt.timedelta(days=1)
    rows = max(1, SAFE_RESPONSE_ROWS - 1)
    seconds = rows * sample * 60
    return min(MAX_ABSOLUTE_WINDOW, dt.timedelta(seconds=seconds))


def missing_day_windows(start, end, timestamps, checked=(), fidelity=5):
    """Coalesce missing buckets within each market-relative day (no interpolation)."""
    seconds = fidelity * 60
    covered = {int((t-start).total_seconds() // seconds) for t in timestamps if start <= t < end}
    windows = []
    cursor = start
    while cursor < end:
        upper = min(end, cursor + dt.timedelta(days=1))
        missing = []
        for index in range(int((cursor-start).total_seconds() // seconds),
                           math.ceil((upper-start).total_seconds() / seconds)):
            lower = start + dt.timedelta(seconds=index*seconds)
            right = min(end, lower + dt.timedelta(seconds=seconds))
            if index not in covered and not any(a <= lower and right <= b for a, b in checked):
                missing.append(lower)
        if missing:
            # Include adjacent samples to avoid a boundary rounding hole at the source.
            windows.append((max(cursor, missing[0]-dt.timedelta(seconds=seconds)),
                            min(upper, missing[-1]+dt.timedelta(seconds=2*seconds))))
        cursor = upper
    return windows


def fetch_history(get, token_id, start, end, fidelity, parse_timestamp, parse_float,
                  known_history=(), checked=(), api='clob'):
    if fidelity <= 0:
        raise ValueError('fidelity must be positive')
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError('历史价格窗口必须是有时区且end大于start')
    points = {}
    known_times = [t for row in known_history if (t := parse_timestamp(row[0])) is not None]
    errors, requests, verified, attempts = [], [], [], []
    started = time.monotonic()

    def request_page(lower, upper, sample, cursor=None):
        audit = dict(start=lower.isoformat(), end=upper.isoformat(), fidelity=sample,
                     target_fidelity_minutes=fidelity,
                     effective_fidelity_minutes=sample,
                     endpoint='/v2/prices-history' if api == 'v2' else '/prices-history', cursor=cursor,
                     started_at_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        request_start = time.monotonic()
        requests.append(audit)
        try:
            if upper - lower > MAX_ABSOLUTE_WINDOW:
                raise ValueError('历史价格绝对窗口超过官方15天限制')
            if api == 'v2':
                params = dict(token_id=token_id, start=int(lower.timestamp()), end=math.ceil(upper.timestamp()))
                if sample is not None:
                    params['bucket_seconds'] = sample*60
                if cursor:
                    params['cursor'] = cursor
                payload = get('/v2/prices-history', params)
            else:
                payload = get('/prices-history', dict(market=token_id, startTs=int(lower.timestamp()),
                                                      endTs=math.ceil(upper.timestamp()), fidelity=sample))
            audit['sha256'] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            rows = payload
            while isinstance(rows, dict):
                for key in ('history', 'data', 'items', 'result', 'results'):
                    if key in rows:
                        rows = rows[key]
                        break
                else:
                    raise ValueError('历史价格响应缺少记录数组')
            if not isinstance(rows, list):
                raise ValueError('历史价格记录不是数组')
            audit['returned_rows'] = len(rows)
            if len(rows) >= SAFE_RESPONSE_ROWS:
                audit.update(status='split_required', split_allowed=True,
                             split_reason='response_near_500', safe_row_limit=SAFE_RESPONSE_ROWS,
                             maximum_response_rows=MAX_RESPONSE_ROWS)
                return False, None
            invalid = outside = accepted = 0
            for row in rows:
                if not isinstance(row, dict):
                    invalid += 1
                    continue
                timestamp = parse_timestamp(row.get('t', row.get('timestamp')))
                price = parse_float(row.get('p', row.get('price')))
                if timestamp is None or price is None or not math.isfinite(price) or not 0 <= price <= 1:
                    invalid += 1
                elif lower <= timestamp < upper:
                    resolution = row.get('resolution_seconds')
                    if resolution is not None:
                        resolution = parse_float(resolution)
                        if resolution is None or not math.isfinite(resolution) or resolution < 0 or resolution != int(resolution):
                            invalid += 1
                            continue
                        resolution = int(resolution)
                    # Preserve the finest source observation when fallback returns coarser buckets.
                    key = timestamp.isoformat()
                    previous = points.get(key)
                    old_resolution = previous.get('resolution_seconds') if previous else None
                    if previous is None or (resolution is not None and
                                           (old_resolution is None or resolution < old_resolution)):
                        points[key] = {'t': key, 'p': price, 'resolution_seconds': resolution,
                                       'source': 'data-api-v2' if api == 'v2' else 'clob'}
                    accepted += 1
                else:
                    outside += 1
            audit.update(returned_rows=len(rows), accepted_rows=accepted, invalid_rows=invalid,
                         outside_rows=outside, status='ok' if not invalid else 'invalid')
            if invalid:
                raise ValueError(f'历史价格响应含 {invalid} 条无效记录，已保存其余合法记录')
            if api != 'v2':
                return True, None
            pagination = payload.get('pagination') if isinstance(payload, dict) else None
            if not isinstance(pagination, dict):
                raise ValueError('历史价格分页结构无效')
            next_cursor = pagination.get('next_cursor')
            has_more = pagination.get('has_more')
            if (not isinstance(has_more, bool) or 'next_cursor' not in pagination
                    or (has_more and (not isinstance(next_cursor, str) or not next_cursor))
                    or (not has_more and next_cursor is not None)):
                raise ValueError('历史价格分页状态或next_cursor不一致')
            audit.update(next_cursor=next_cursor, page_complete=not has_more)
            return True, next_cursor
        except (urllib.error.URLError, OSError, ValueError, http.client.IncompleteRead, http.client.RemoteDisconnected) as exc:
            reason = getattr(exc, 'reason', exc)
            audit.update(status='failed', error=type(exc).__name__, message=str(exc)[:250],
                         http_status=getattr(exc, 'code', None),
                         error_code=getattr(exc, 'api_error_code', None),
                         trace_id=getattr(exc, 'trace_id', None),
                         retry_after_seconds=getattr(exc, 'retry_after_seconds', None),
                         split_allowed=(getattr(exc, 'code', None) in (400, 408, 413, 422, 504)
                                        or isinstance(reason, (TimeoutError, ConnectionResetError,
                                                               http.client.IncompleteRead,
                                                               http.client.RemoteDisconnected))))
            return False, None
        finally:
            audit['seconds'] = round(time.monotonic()-request_start, 3)
            audit['finished_at_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()

    def request(lower, upper, sample):
        cursor, seen = None, set()
        for _ in range(100):
            ok, next_cursor = request_page(lower, upper, sample, cursor)
            if not ok or not next_cursor:
                return ok
            if next_cursor in seen:
                break
            seen.add(next_cursor)
            cursor = next_cursor
        requests[-1].update(status='failed', error='PaginationError', message='历史分页重复游标或超过100页上限')
        return False

    windows = missing_day_windows(start, end, known_times, checked, fidelity)

    def target_times(lower, upper):
        return known_times + [parse_timestamp(key) for key, row in points.items()
                              if lower <= parse_timestamp(key) < upper
                              and row.get('resolution_seconds') is not None
                              and 0 < row['resolution_seconds'] <= fidelity*60]

    def target_complete(lower, upper):
        return not missing_day_windows(lower, upper, target_times(lower, upper), fidelity=fidelity)

    def target_has_any(lower, upper):
        return any(lower <= timestamp < upper for timestamp in target_times(lower, upper))

    def collect(lower, upper, split_depth=0, request_fidelity=None, retention_policy='requested'):
        begin = len(requests)
        requested_sample = fidelity if request_fidelity is None else request_fidelity
        ok = request(lower, upper, requested_sample)
        fallback_used = []
        target_missing = not target_complete(lower, upper)
        if ok and api == 'v2' and requested_sample == fidelity and (fidelity == DEFAULT_FIDELITY_MINUTES or target_missing):
            # Empty or sparse 5-minute data can reflect retention or source gaps.
            # Try 30-minute data first, then long-retention tiers; consumers must
            # inspect resolution_seconds before treating a row as five-minute data.
            fallbacks = FALLBACK_FIDELITIES_MINUTES if fidelity == DEFAULT_FIDELITY_MINUTES else (None, *FALLBACK_FIDELITIES_MINUTES)
            for fallback in fallbacks:
                fallback_used.append(fallback)
                fallback_begin = len(requests)
                fallback_ok = request(lower, upper, fallback)
                if not fallback_ok:
                    ok = False
                    break
                ok = ok and fallback_ok
                if any(page.get('accepted_rows', 0) for page in requests[fallback_begin:]):
                    break
        elif ok and requested_sample == fidelity and fidelity > 1:
            times = known_times + [parse_timestamp(key) for key in points]
            for a, b in missing_day_windows(lower, upper, times, fidelity=fidelity):
                ok = request(a, b, 1) and ok
        failed = [r for r in requests[begin:] if r['status'] != 'ok']
        window_points = [row for key, row in points.items() if lower <= parse_timestamp(key) < upper]
        attempts.append(dict(start=lower.isoformat(), end=upper.isoformat(), target_seconds=fidelity*60,
                             started_at_utc=requests[begin]['started_at_utc'],
                             finished_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                             status='ok' if ok else 'failed', pagination_complete=ok,
                             target_complete=target_complete(lower, upper),
                             fallback_fidelities_minutes=fallback_used,
                             requested_fidelity_minutes=requested_sample,
                             retention_policy=retention_policy,
                             unique_rows=len(window_points),
                             resolutions=sorted({r['resolution_seconds'] for r in window_points
                                                 if r.get('resolution_seconds') is not None}),
                             pages=requests[begin:], errors=failed))
        density_split = any(r.get('status') == 'split_required' for r in failed)
        split_allowed = failed and all(r.get('split_allowed') for r in failed)
        can_split = upper - lower > MIN_RETRY_SPLIT_WINDOW
        if not ok and split_allowed and can_split and (density_split or upper-lower > MAX_RETRY_SPLIT_WINDOW):
            middle = lower + (upper - lower) / 2 if density_split else lower + MAX_RETRY_SPLIT_WINDOW
            if middle <= lower or middle >= upper:
                errors.extend(failed)
                return
            collect(lower, middle, split_depth + 1, requested_sample, retention_policy)
            collect(middle, upper, split_depth + 1, requested_sample, retention_policy)
            return
        if (not ok and api == 'v2' and fidelity == DEFAULT_FIDELITY_MINUTES
                and requested_sample == fidelity and split_allowed):
            for fallback in FALLBACK_FIDELITIES_MINUTES:
                fallback_used.append(fallback)
                fallback_begin = len(requests)
                fallback_ok = request(lower, upper, fallback)
                if not fallback_ok:
                    continue
                if any(page.get('accepted_rows', 0) for page in requests[fallback_begin:]):
                    break
        failed = [r for r in requests[begin:] if r['status'] != 'ok']
        attempts[-1]['pages'] = requests[begin:]
        attempts[-1]['errors'] = failed
        attempts[-1]['fallback_fidelities_minutes'] = fallback_used
        errors.extend(failed)
        if ok and not failed and (
                (requested_sample == fidelity and (target_complete(lower, upper) or bool(fallback_used)))
                or retention_policy in ('thirty_minute_90d', 'long_term_3h')):
            # A verified query window is only a short-lived acquisition receipt;
            # target completeness remains separately computed from source resolution.
            verified.append(dict(start=lower.isoformat(), end=upper.isoformat()))

    now = dt.datetime.now(dt.timezone.utc)
    for lower, upper in windows:
        routed = [(lower, upper, fidelity, 'requested')]
        if api == 'v2' and fidelity == DEFAULT_FIDELITY_MINUTES:
            routed = []
            boundaries = sorted((lower, upper, now-FIVE_MINUTE_RETENTION,
                                 now-THIRTY_MINUTE_RETENTION))
            points_in_range = [point for point in boundaries if lower < point < upper]
            edges = [lower, *points_in_range, upper]
            for left, right in zip(edges, edges[1:]):
                sample, policy = _retention_route(left, right, fidelity, now)
                routed.append((left, right, sample, policy))
        for route_start, route_end, sample, policy in routed:
            limit = _window_limit(sample)
            cursor = route_start
            while cursor < route_end:
                chunk_end = min(route_end, cursor + limit)
                request_begin = len(requests)
                collect(cursor, chunk_end, request_fidelity=sample, retention_policy=policy)
                if len(requests) > request_begin:
                    for audit in requests[request_begin:]:
                        audit.setdefault('retention_policy', policy)
                        audit.setdefault('target_fidelity_minutes', fidelity)
                        audit.setdefault('requested_fidelity_minutes', sample)
                        audit.setdefault('effective_fidelity_minutes', sample)
                        audit.setdefault('safe_row_limit', SAFE_RESPONSE_ROWS)
                        audit.setdefault('maximum_response_rows', MAX_RESPONSE_ROWS)
                cursor = chunk_end
    return dict(history=[points[key] for key in sorted(points)], errors=errors,
                requests=requests, verified_windows=verified, attempts=attempts,
                planned_windows=len(windows), elapsed_seconds=round(time.monotonic()-started, 3))
