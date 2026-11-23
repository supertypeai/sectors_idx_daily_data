"""Standalone IQPlus repair for open/high/low left at 0 or NULL.

Scrape-free: does NOT import or touch idx_daily_updater. All IDX-fetch logic
stays in idx_daily_updater; all IQPlus + candle logic lives here.

Each symbol is fetched from IQPlus exactly once, regardless of how many dates
are requested (one response carries the full history).

Read-only by default. Writes only with --apply, which needs SUPABASE_KEY set to
the service key (the anon/publishable key is silently dropped by RLS).

Usage:
  iqplus_repair.py YYYY-MM-DD [YYYY-MM-DD ...] [--workers N] [--with-prev] [--fill-missing] [--apply]
"""
import os
import sys
import time
import requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client

load_dotenv('.env')

# ── IQPlus primitives ────────────────────────────────────────────────────────

OHL_COLS = ["open", "high", "low"]
# The fill-missing path inserts a whole candle, so take close/volume from IQPlus too
CANDLE_COLS = OHL_COLS + ["close", "volume"]
IQPLUS_TIMEOUT = 20
# Liquid name used to check how far IQPlus has published; any active ticker works
PROBE_SYMBOL = "BBCA.JK"
IQPLUS_HEADERS = {"User-Agent": "Mozilla/5.0"}


def is_candle_valid(open_p, high_p, low_p, close_p) -> bool:
    """Validate candlestick geometry: Low <= Open <= High, Low <= Close <= High."""
    if open_p <= 0 or high_p <= 0 or low_p <= 0 or close_p <= 0:
        return False
    return (low_p <= open_p <= high_p) and (low_p <= close_p <= high_p) and (low_p <= high_p)


def _ohl_from_row(row) -> dict:
    return {
        c: int(round(float(row[c])))
        for c in CANDLE_COLS
        if row.get(c) is not None and row[c] > 0
    }


def _ohl_for_date(rows: list, target_date_str: str) -> dict:
    """Extract OHL for one date from a history list (newest last)."""
    for row in reversed(rows):
        t = row.get("time")
        if t == target_date_str:
            return _ohl_from_row(row)
        if t < target_date_str:
            break
    return {}


def _fetch_iqplus_rows(symbol: str, session: requests.Session = None, failures: list = None) -> list:
    """Full OHLCV history for a symbol from IQPlus (newest last); [] on failure.

    Failures are appended to `failures` (list append is thread-safe) so a rate-limited
    run reports one summary instead of one line per symbol.
    """
    code = symbol.replace(".JK", "").upper()
    http = session or requests
    try:
        resp = http.get(
            f"https://www.iqplus.info/api/v1/ohlcv.php?code={code}",
            headers=IQPLUS_HEADERS, timeout=IQPLUS_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()
        reason = f"HTTP {resp.status_code}"
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"

    if failures is None:
        print(f"⚠️ iqplus {symbol}: {reason}", flush=True)
    else:
        failures.append(f"{symbol} {reason}")
    return []


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    argv = sys.argv[1:]
    workers = 10
    if '--workers' in argv:
        _i = argv.index('--workers')
        if _i + 1 >= len(argv) or not argv[_i + 1].isdigit():
            raise SystemExit('--workers needs an integer, e.g. --workers 10')
        workers = int(argv[_i + 1])
        del argv[_i:_i + 2]
    apply_  = '--apply'     in argv
    with_prev = '--with-prev' in argv
    fill_missing = '--fill-missing' in argv
    dates = list(dict.fromkeys(a for a in argv if not a.startswith('--')))
    if not dates:
        raise SystemExit('usage: iqplus_repair.py YYYY-MM-DD [...] [--workers N] [--with-prev] [--apply]')
    for d in dates:
        try:
            datetime.strptime(d, '%Y-%m-%d')
        except ValueError:
            raise SystemExit(f'invalid date {d!r}, expected YYYY-MM-DD')

    # SUPABASE_KEY must be the service key: --apply writes are dropped by RLS otherwise.
    sb = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_KEY'])
    session = requests.Session()
    session.headers.update(IQPLUS_HEADERS)

    if with_prev:
        prev = (sb.table('idx_daily_data').select('date')
                .lt('date', dates[0]).order('date', desc=True).limit(1).execute().data)
        if prev and prev[0]['date'] not in dates:
            dates.append(prev[0]['date'])
            print(f'anchor={dates[0]}  prev trading day={dates[-1]}', flush=True)

    # ── read all target dates ────────────────────────────────────────────────
    universe = []
    if fill_missing:
        universe = sorted({r['symbol'] for r in
                           sb.table('idx_active_company_profile').select('symbol').execute().data})

    need = defaultdict(list)
    missing_dates = set()
    for date in dates:
        db, off = [], 0
        while True:
            b = (sb.table('idx_daily_data').select('symbol,date,open,high,low,close')
                 .eq('date', date).order('symbol').range(off, off + 999).execute().data)
            if not b:
                break
            db += b
            if len(b) < 1000:
                break
            off += 1000

        if not db and fill_missing:
            missing_dates.add(date)
            need[date] = [{'symbol': s, 'open': 0, 'high': 0, 'low': 0, 'close': 0}
                          for s in universe]
        else:
            need[date] = [r for r in db if min(r['open'] or 0, r['high'] or 0, r['low'] or 0) <= 0]

        print(f'[{date}] rows={len(db)} needing_repair={len(need[date])} '
              f'{"FILL MISSING" if date in missing_dates else ""} '
              f'mode={"APPLY" if apply_ else "DRY RUN"}', flush=True)

    # ── skip dates IQPlus has not published yet ──────────────────────────────
    # One probe costs ~15s; the full sweep costs ~10 min to discover the same thing.
    symbols = sorted({r['symbol'] for rows in need.values() for r in rows})
    skipped_dates = set()
    probe_calls = 0
    if symbols:
        probe_symbol = PROBE_SYMBOL if PROBE_SYMBOL in symbols else symbols[0]
        probe_rows = _fetch_iqplus_rows(probe_symbol, session=session)
        probe_calls = 1
        if probe_rows:
            newest = max(r['time'] for r in probe_rows)
            stale = [d for d in dates if d > newest]
            if stale:
                skipped_dates.update(stale)
                print(f'⚠️ IQPlus newest bar is {newest} ({probe_symbol}); '
                      f'skipping {stale} - not published yet', flush=True)
                for d in stale:
                    need.pop(d, None)
                    missing_dates.discard(d)
                symbols = sorted({r['symbol'] for rows in need.values() for r in rows})

    # ── one IQPlus call per unique symbol ────────────────────────────────────
    print(f'symbols={len(symbols)} workers={workers} (1 IQPlus call each)', flush=True)

    fetch_failures = []

    def fetch(sym):
        for attempt in (1, 2):
            # Only the last attempt is reported, so the summary counts symbols not retries
            rows = _fetch_iqplus_rows(sym, session=session,
                                      failures=fetch_failures if attempt == 2 else [])
            if rows or attempt == 2:
                return rows
            time.sleep(2)

    t0 = time.time()
    history = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for sym, rows in zip(symbols, ex.map(fetch, symbols)):
            history[sym] = rows

    if fetch_failures:
        preview = ', '.join(fetch_failures[:5])
        more = f' ... (+{len(fetch_failures) - 5})' if len(fetch_failures) > 5 else ''
        print(f'⚠️ iqplus fetch failed for {len(fetch_failures)}/{len(symbols)} symbols: '
              f'{preview}{more}', flush=True)

    # ── apply per date ───────────────────────────────────────────────────────
    total_fixed = total_stuck = 0
    for date in dates:
        d_fixed = d_stuck = 0
        inserting = date in missing_dates
        batch, skipped = [], []
        for i, r in enumerate(need[date], 1):
            sym = r['symbol']
            iq = _ohl_for_date(history.get(sym, []), date)

            if not iq:
                d_stuck += 1
                skipped.append(sym)
                continue

            c = iq.get('close', 0) if inserting else (r['close'] or 0)
            o = r['open'] if (r['open'] or 0) > 0 else iq.get('open', 0)
            h = r['high'] if (r['high'] or 0) > 0 else iq.get('high', 0)
            l = r['low']  if (r['low']  or 0) > 0 else iq.get('low',  0)

            if not is_candle_valid(o, h, l, c):
                d_stuck += 1
                print(f'[{date}] [{i}/{len(need[date])}] {sym:12} REJECT guard '
                      f'o={o} h={h} l={l} c={c}', flush=True)
                continue

            if inserting:
                batch.append({'date': date, 'symbol': sym, 'close': int(c),
                              'volume': int(iq.get('volume') or 0),
                              'open': int(o), 'high': int(h), 'low': int(l)})
                if not apply_:
                    d_fixed += 1          # dry run: report what would be inserted
                continue

            if apply_:
                res = (sb.table('idx_daily_data')
                       .update({'open': int(o), 'high': int(h), 'low': int(l)})
                       .eq('symbol', sym).eq('date', date).execute())
                if not res.data:
                    d_stuck += 1
                    print(f'[{date}] [{i}/{len(need[date])}] {sym:12} '
                          f'WRITE BLOCKED (RLS/key) -> not persisted', flush=True)
                    continue

            d_fixed += 1
            print(f'[{date}] [{i}/{len(need[date])}] {sym:12} '
                  f'open {r["open"]}->{o}  high {r["high"]}->{h}  low {r["low"]}->{l}  '
                  f'{"APPLIED" if apply_ else "dry"}', flush=True)

        if inserting and batch and apply_:
            for k in range(0, len(batch), 500):
                sb.table('idx_daily_data').upsert(batch[k:k + 500]).execute()
            d_fixed += len(batch)          # only rows that actually landed
            print(f'[{date}] INSERTED {len(batch)} rows', flush=True)

        if skipped:
            preview = ', '.join(skipped[:5])
            more = f' ... (+{len(skipped) - 5})' if len(skipped) > 5 else ''
            print(f'[{date}] no IQPlus data for {len(skipped)} symbols -> '
                  f'{"skipped" if inserting else "left blank"}: {preview}{more}', flush=True)

        total_fixed += d_fixed
        total_stuck += d_stuck
        skipped = ' (skipped: IQPlus has no bar yet)' if date in skipped_dates else ''
        print(f'[{date}] DONE fixed={d_fixed} unresolved={d_stuck}{skipped}', flush=True)

    el = time.time() - t0
    note = f' skipped={sorted(skipped_dates)}' if skipped_dates else ''
    print(f'--- TOTAL fixed={total_fixed} unresolved={total_stuck} dates={len(dates)} '
          f'iqplus_calls={len(symbols) + probe_calls} elapsed={el:.0f}s{note} '
          f'mode={"APPLY" if apply_ else "DRY RUN"}', flush=True)


if __name__ == '__main__':
    main()
