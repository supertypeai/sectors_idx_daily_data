"""Read-only health check for idx_daily_data.

Catches the failure modes that stayed invisible until hand-investigated:
stale latest date, partial/empty day, OHLC left at 0, and a close outside its
own [low, high] range (split/basis mismatch, e.g. RDTX.JK on 2026-11-23).

Exit 0 = healthy (warnings allowed), 1 = a hard check failed.
Usage: python health_check.py [YYYY-MM-DD]
"""
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv('.env')

TABLE = 'idx_daily_data'
MIN_ROWS = 800          # the tradable universe is ~944; anything below is a partial day
PAGE = 1000
# A gap larger than this is a broken pipeline, not a holiday. ponytail: no IDX holiday
# calendar here, so a holiday longer than MAX_LAG_DAYS reads as stale - pass the date
# explicitly from the caller, or wire in the calendar, when that becomes a real case.
MAX_LAG_DAYS = 4


def _auth():
    url = os.environ['SUPABASE_URL'].rstrip('/')
    key = os.environ['SUPABASE_KEY']
    return url, {'apikey': key, 'Authorization': f'Bearer {key}'}


def _get(base, auth, params):
    response = requests.get(f'{base}/rest/v1/{TABLE}', headers=auth, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def _rows_on(base, auth, day):
    out, offset = [], 0
    while True:
        batch = _get(base, auth, {'select': 'symbol,open,high,low,close,volume',
                                 'date': f'eq.{day}', 'order': 'symbol',
                                 'offset': offset, 'limit': PAGE})
        out += batch
        if len(batch) < PAGE:
            return out
        offset += PAGE


def _last_trading_day(today):
    day = today
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def main():
    base, auth = _auth()
    today = datetime.now(ZoneInfo('Asia/Jakarta')).date()   # trading day is Jakarta's, not UTC's
    target_date = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else _last_trading_day(today)
    target = target_date.isoformat()

    failures, warnings = [], []

    latest = _get(base, auth, {'select': 'date', 'order': 'date.desc', 'limit': 1})
    latest_date = latest[0]['date'] if latest else None
    print(f'latest date in {TABLE}: {latest_date}  (checking {target})')

    if latest_date is None:
        failures.append('table is empty')
    elif latest_date < (target_date - timedelta(days=MAX_LAG_DAYS)).isoformat():
        failures.append(f'stale: latest {latest_date}, expected within {MAX_LAG_DAYS}d of {target}')

    rows = _rows_on(base, auth, target) if latest_date and latest_date >= target else []
    if latest_date and latest_date >= target:
        print(f'{target}: rows={len(rows)}')
        if len(rows) < MIN_ROWS:
            failures.append(f'{target}: partial day, rows={len(rows)} < {MIN_ROWS}')

    zero_ohl = [r['symbol'] for r in rows
                if min(r['open'] or 0, r['high'] or 0, r['low'] or 0) <= 0]
    null_core = [r['symbol'] for r in rows if r['close'] is None or r['volume'] is None]
    bad_range = [r['symbol'] for r in rows
                 if None not in (r['close'], r['low'], r['high'])
                 and min(r['low'], r['high'], r['close']) > 0   # zero OHL = no trade, not bad data
                 and not r['low'] <= r['close'] <= r['high']]

    if zero_ohl:
        warnings.append(f'{len(zero_ohl)} symbols with open/high/low <= 0 (IQPlus repair pending): '
                        f'{", ".join(zero_ohl[:5])}')
    if null_core:
        failures.append(f'{len(null_core)} symbols with null close/volume: {", ".join(null_core[:5])}')
    if bad_range:
        failures.append(f'{len(bad_range)} symbols with close outside [low, high]: '
                        f'{", ".join(bad_range[:5])}')

    for line in warnings:
        print(f'WARN  {line}')
    for line in failures:
        print(f'FAIL  {line}')
    print('OK' if not failures else f'FAILED ({len(failures)} hard, {len(warnings)} warnings)')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
