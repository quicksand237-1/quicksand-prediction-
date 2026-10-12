"""Bounded, read-only source audit; never opens the project database for writing."""
import argparse
import concurrent.futures
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--token')
    parser.add_argument('--start', type=int)
    parser.add_argument('--end', type=int)
    parser.add_argument('--proxy')
    parser.add_argument('--output', default='history_source_audit.json')
    args = parser.parse_args()
    urls = [('documentation', 'https://docs.polymarket.com/market-data/prices-order-books.md'),
            ('concepts', 'https://docs.polymarket.com/concepts/prices-orderbook.md'),
            ('index', 'https://docs.polymarket.com/llms.txt')]
    if args.token:
        if args.start is None or args.end is None or not 0 < args.end - args.start <= 7 * 86400:
            parser.error('Provide a positive window no longer than seven days')
        for fidelity in (5, 1):
            query = urllib.parse.urlencode(dict(market=args.token, startTs=args.start,
                                               endTs=args.end, fidelity=fidelity))
            urls.append((f'prices_{fidelity}m', 'https://clob.polymarket.com/prices-history?' + query))
        for bucket in (3600, None):
            params = dict(token_id=args.token, start=args.start, end=args.end)
            if bucket is not None:
                params['bucket_seconds'] = bucket
            urls.append((f'prices_v2_{bucket}', 'https://data-api.polymarket.com/v2/prices-history?' + urllib.parse.urlencode(params)))

    def fetch(item):
        name, url = item
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {'https': args.proxy, 'http': args.proxy} if args.proxy else None))
        started = time.monotonic()
        try:
            with opener.open(urllib.request.Request(url, headers={'User-Agent': 'AIPRP-history-audit/1.0'}), timeout=20) as response:
                body = response.read().decode('utf-8')
            return dict(name=name, url=url, seconds=round(time.monotonic()-started, 3), body=body)
        except Exception as exc:
            return dict(name=name, url=url, seconds=round(time.monotonic()-started, 3), error=str(exc))

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(fetch, urls))
    Path(args.output).write_text(json.dumps(results, ensure_ascii=True, indent=2), encoding='utf-8')
    for result in results:
        print(json.dumps({k: v for k, v in result.items() if k != 'body'}))
        if result['name'].startswith('prices') and 'body' in result:
            payload = json.loads(result['body'])
            rows = payload.get('history', payload.get('data', []))
            print(json.dumps(dict(points=len(rows), first=rows[:2], last=rows[-2:], pagination=payload.get('pagination'))))


if __name__ == '__main__':
    main()
