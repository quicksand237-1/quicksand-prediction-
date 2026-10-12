"""Export hourly price-only research from a read-only source DB and isolated backfill."""
import argparse
import bisect
import concurrent.futures
import csv
import datetime as dt
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from polymarket_pipeline import PublicDataClient, event_window, parse_timestamp, utc_now


def hourly_rows(slug, market_id, label, token, start, end, prices, resolved_yes):
    """Use completed buckets at/before entry, at most one hour old. Never interpolate."""
    points = sorted((row for row in prices if row[2] is not None and 0 < row[2] <= 3600),
                    key=lambda row: row[0]+dt.timedelta(seconds=row[2]))
    timestamps = [p[0]+dt.timedelta(seconds=p[2]) for p in points]
    hours = int((end-start).total_seconds() // 3600)
    for hour in range(hours+1):
        target = start+dt.timedelta(hours=hour)
        if target >= end:
            break
        index = bisect.bisect_right(timestamps, target)-1
        point = points[index] if index >= 0 else None
        age = (target-point[0]).total_seconds() if point else None
        available = point is not None and age <= 3600
        price = point[1] if available else None
        status = ('observed' if age == 0 else 'previous_observation') if available else 'missing'
        roi = (resolved_yes/price-1)*100 if price is not None and price > 0 and resolved_yes in (0, 1) else None
        yield dict(slug=slug, market_id=market_id, label=label, token_id=token,
                   elapsed_hours=hour, entry_time_utc=target.isoformat(), price=price,
                   price_time_utc=point[0].isoformat() if point else None, price_age_seconds=age,
                   resolution_seconds=point[2] if point else None,
                   source=point[3] if point else None, status=status,
                   resolved_yes=resolved_yes, gross_return_percent=roi,
                   gross_profit_per_100=roi, execution='price_only_no_fees_no_depth')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='data/extended.sqlite')
    parser.add_argument('--output-dir', default='data/hourly_research')
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--retry-errors', action='store_true', help='Retry only failed tokens from requests.jsonl; preserve main CSV and audit')
    args = parser.parse_args()
    root = Path(args.output_dir)
    root.mkdir(parents=True, exist_ok=True)
    source_uri = Path(args.db).resolve().as_uri()+'?mode=ro'
    conn = sqlite3.connect(source_uri, uri=True, timeout=30)
    events = conn.execute('select slug,raw_json from market_events order by game_start_utc').fetchall()
    tasks = []
    now = utc_now()
    for slug, raw in events:
        start, end = event_window(json.loads(raw), 'America/New_York')
        if start is None or end is None or start >= now:
            continue
        for market_id, label, token, resolved in conn.execute(
            'select market_id,label,yes_token_id,resolved_yes from market_outcomes where event_slug=? and yes_token_id is not null', (slug,)):
            tasks.append((slug, market_id, label, token, start, min(end, now), resolved if end <= now else None))
    columns = {r[1] for r in conn.execute('pragma table_info(market_prices)')}
    conn.close()
    if args.limit:
        tasks = tasks[:args.limit]
    if args.retry_errors:
        if not args.fetch:
            parser.error('--retry-errors requires --fetch')
        failed = set()
        for line in (root/'requests.jsonl').read_text(encoding='utf-8').splitlines():
            record = json.loads(line)
            if record.get('errors'):
                failed.add((record['slug'],record['token']))
        tasks = [task for task in tasks if (task[0],task[3]) in failed]
    cache = sqlite3.connect(root/'source_prices.sqlite')
    cache.execute('create table if not exists prices (slug text, market_id text, token text, time text, price real, resolution integer, source text, primary key(slug,market_id,token,time))')
    cache.execute('create table if not exists checks (slug text, market_id text, token text, start text, end text, checked_at text, primary key(slug,market_id,token,start,end))')
    cache.commit()
    cache_uri = (root/'source_prices.sqlite').resolve().as_uri()+'?mode=ro'
    client = PublicDataClient('https://data-api.polymarket.com', timeout=15, retries=2)
    client.request_delay = 0.2

    def collect(task):
        slug, market_id, label, token, start, end, resolved = task
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as read:
            extra = ',resolution_seconds,source' if 'resolution_seconds' in columns else ',NULL,NULL'
            rows = read.execute('select price_time_utc,price'+extra+' from market_prices where event_slug=? and market_id=? and token_id=? and price_time_utc>=? and price_time_utc<?',
                                (slug, market_id, token, start.isoformat(), end.isoformat())).fetchall()
        with closing(sqlite3.connect(cache_uri, uri=True, timeout=30)) as read:
            staged = read.execute('select time,price,resolution,source from prices where slug=? and market_id=? and token=?', (slug, market_id, token)).fetchall()
            checked = [(parse_timestamp(a),parse_timestamp(b)) for a,b in read.execute(
                'select start,end from checks where slug=? and market_id=? and token=? and checked_at>=?',
                (slug,market_id,token,(now-dt.timedelta(hours=6)).isoformat()))]
        by_time = {r[0]: r for r in staged}
        by_time.update({r[0]: r for r in rows})
        usable = [(r[0],) for r in by_time.values() if r[2] is not None and 0 < r[2] <= 3600]
        result = client.get_history_window(token, start, end, 60, known_history=usable, checked=checked) if args.fetch else dict(history=[],errors=[],requests=[])
        result['price_conflicts'] = []
        for row in result['history']:
            previous = by_time.get(row['t'])
            if previous and abs(previous[1]-row['p']) > 1e-9:
                result['price_conflicts'].append(dict(time=row['t'],local_price=previous[1],source_price=row['p']))
            if previous is None:
                by_time[row['t']] = (row['t'],row['p'],row.get('resolution_seconds'),row.get('source'))
        points = [(parse_timestamp(r[0]),r[1],r[2],r[3] or 'local_archive_resolution_unknown') for r in by_time.values()]
        return task, result, list(hourly_rows(slug,market_id,label,token,start,end,points,resolved))

    started = time.monotonic()
    summary = dict(as_of_utc=now.isoformat(), markets=len({t[0] for t in tasks}), tokens=len(tasks), rows=0, available=0, missing=0,
                   request_errors=0, requests=0, fetched_rows=0, price_only=True, source_database_readonly=True)
    hourly = {}
    writer = None
    # Audit raw request hashes and errors separately from the compact CSV.
    with (root/('retry_prices.csv' if args.retry_errors else 'hourly_prices.csv')).open('w',encoding='utf-8-sig',newline='') as out, \
         (root/('retry_requests.jsonl' if args.retry_errors else 'requests.jsonl' if args.fetch else 'export_audit.jsonl')).open('w',encoding='utf-8') as audit:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,min(8,args.workers))) as pool:
            futures = {pool.submit(collect,task):task for task in tasks}
            for count,future in enumerate(concurrent.futures.as_completed(futures),1):
                task = futures[future]
                try:
                    task,result,rows = future.result()
                except Exception as exc:
                    result = dict(history=[],errors=[dict(error=type(exc).__name__,message=str(exc))],requests=[])
                    rows = list(hourly_rows(*task[:6], [], task[6]))
                with cache:
                    cache.executemany('insert or replace into prices values (?,?,?,?,?,?,?)',
                        [(task[0],task[1],task[3],r['t'],r['p'],r.get('resolution_seconds'),r.get('source')) for r in result['history']])
                    cache.executemany('insert or replace into checks values (?,?,?,?,?,?)',
                        [(task[0],task[1],task[3],r['start'],r['end'],utc_now().isoformat()) for r in result.get('verified_windows',[])])
                audit.write(json.dumps(dict(slug=task[0],token=task[3],errors=result['errors'],requests=result['requests'],price_conflicts=result.get('price_conflicts',[])),ensure_ascii=True)+'\n')
                summary['requests'] += len(result['requests'])
                summary['request_errors'] += len(result['errors'])
                summary['fetched_rows'] += len(result['history'])
                for row in rows:
                    if writer is None:
                        writer = csv.DictWriter(out,fieldnames=list(row))
                        writer.writeheader()
                    writer.writerow(row)
                    summary['rows'] += 1
                    key = 'missing' if row['status']=='missing' else 'available'
                    summary[key] += 1
                    group = hourly.setdefault(row['elapsed_hours'],dict(elapsed_hours=row['elapsed_hours'],available=0,missing=0))
                    group[key] += 1
                if count % 25 == 0 or count == len(tasks):
                    print(json.dumps(dict(done=count,total=len(tasks),seconds=round(time.monotonic()-started,1),**summary)),flush=True)
    cache.close()
    summary['seconds'] = round(time.monotonic()-started,1)
    summary['hourly_coverage'] = [hourly[h] for h in sorted(hourly)]
    with (root/('retry_coverage.csv' if args.retry_errors else 'hourly_coverage.csv')).open('w',encoding='utf-8-sig',newline='') as output:
        coverage_writer = csv.DictWriter(output,fieldnames=['elapsed_hours','available','missing'])
        coverage_writer.writeheader()
        coverage_writer.writerows(summary['hourly_coverage'])
    summary['methodology'] = 'Each YES outcome separately; bucket end <= entry, source timestamp age <= 1h, known resolution in (0, 3600] seconds. Unknown legacy resolution is excluded. No interpolation, fees, depth, or model-based selection; ROI is not strategy performance.'
    (root/('retry_summary.json' if args.retry_errors else 'summary.json')).write_text(json.dumps(summary,ensure_ascii=True,indent=2),encoding='utf-8')
    if args.fetch and not args.retry_errors:
        (root/'backfill_summary.json').write_text(json.dumps(summary,ensure_ascii=True,indent=2),encoding='utf-8')
    print(str((root/'hourly_prices.csv').resolve()))


if __name__ == '__main__':
    main()
