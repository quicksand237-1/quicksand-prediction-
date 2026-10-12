"""Inspect local history coverage without migrations, network, or model replay."""
import argparse
import collections
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def audit(path, quick_check=False):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = dict(database=str(path), bytes=path.stat().st_size,
                      journal_mode=conn.execute('PRAGMA journal_mode').fetchone()[0],
                      locking_mode=conn.execute('PRAGMA locking_mode').fetchone()[0],
                      tables=sorted(tables))
        if quick_check:
            result['quick_check'] = conn.execute('PRAGMA quick_check').fetchall()
        print(json.dumps(result), flush=True)
        for table in sorted(tables & {'market_events', 'market_outcomes', 'market_prices',
                                     'market_orderbooks', 'posts', 'prediction_archive',
                                     'history_price_checks', 'prices', 'checks', 'source_events'}):
            print(json.dumps(dict(table=table, rows=conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])), flush=True)
        if 'market_events' not in tables:
            if 'prices' in tables:
                print(json.dumps(dict(staged_resolution=conn.execute(
                    'SELECT resolution,COUNT(*),COUNT(DISTINCT slug) FROM prices GROUP BY resolution').fetchall())), flush=True)
            return
        price_columns = {r[1] for r in conn.execute('PRAGMA table_info(market_prices)')}
        if 'resolution_seconds' in price_columns:
            print(json.dumps(dict(resolution=conn.execute(
                'SELECT resolution_seconds,COUNT(*),COUNT(DISTINCT event_slug) FROM market_prices GROUP BY resolution_seconds').fetchall())), flush=True)
        totals = collections.Counter()
        for slug, start, end in conn.execute('SELECT slug,game_start_utc,end_date_utc FROM market_events ORDER BY game_start_utc'):
            prices = conn.execute('SELECT COUNT(*),MIN(price_time_utc),MAX(price_time_utc) FROM market_prices WHERE event_slug=? AND price_time_utc>=? AND price_time_utc<?', (slug, start, end)).fetchone()
            books = conn.execute('SELECT COUNT(*),MIN(captured_at_utc),MAX(captured_at_utc) FROM market_orderbooks WHERE event_slug=? AND captured_at_utc>=? AND captured_at_utc<?', (slug, start, end)).fetchone()
            all_books = conn.execute('SELECT COUNT(*) FROM market_orderbooks WHERE event_slug=?', (slug,)).fetchone()[0]
            outcomes = conn.execute('SELECT COUNT(*) FROM market_outcomes WHERE event_slug=?', (slug,)).fetchone()[0]
            totals['markets'] += 1
            totals['without_prices'] += prices[0] == 0
            totals['without_books'] += all_books == 0
            totals['only_outside_books'] += all_books > 0 and books[0] == 0
            print(json.dumps(dict(slug=slug, start=start, end=end, outcomes=outcomes,
                                  prices_in_market=prices, books_captured_in_market=books,
                                  total_books=all_books)), flush=True)
        print(json.dumps(dict(coverage_totals=totals)), flush=True)
        if 'prediction_archive' in tables:
            columns = [r[1] for r in conn.execute('PRAGMA table_info(prediction_archive)')]
            print(json.dumps(dict(archive_columns=columns)), flush=True)
            for row in conn.execute('SELECT * FROM prediction_archive ORDER BY rowid DESC LIMIT 6'):
                record = dict(zip(columns, row))
                compact = {k: v for k, v in record.items() if not k.endswith('_json')}
                for key, value in record.items():
                    if not key.endswith('_json') or not isinstance(value, str):
                        continue
                    try:
                        payload = json.loads(value)
                    except ValueError:
                        continue
                    if isinstance(payload, dict):
                        compact[key] = {k: payload[k] for k in ('status', 'price_quality', 'price_errors', 'source_errors', 'price_fidelity_minutes') if k in payload}
                print(json.dumps(dict(recent_archive=compact)), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path('data/extended.sqlite'))
    parser.add_argument('--quick-check', action='store_true')
    args = parser.parse_args()
    audit(args.db, args.quick_check)


if __name__ == '__main__':
    main()
