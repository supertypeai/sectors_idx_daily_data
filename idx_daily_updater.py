import pandas as pd
import os
import random
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client

from idx_fetch import idx_get
from dotenv import load_dotenv
from importlib import reload
import sys

import logging

LOG_FILENAME = 'daily_data_scrapper.log'
REFRESH_DAYS = 2
# Cloudflare accepts some TLS fingerprints and rejects the rest, and that set rotates over
# days. Verified 20 Nov on 7 IDX endpoints + 8x soak: firefox133 and tor145 pass reliably;
# the 19 Nov winners (safari17_0, safari17_2_ios) are 403 today but kept at the bottom so a
# rotation back costs one extra try instead of a failed run. chrome131_android flips
# 403/200 on the same URL, so it is deliberately not in the ladder.
def initiate_logging(LOG_FILENAME):
    reload(logging)

    formatLOG = '%(asctime)s - %(levelname)s: %(message)s'
    logging.basicConfig(filename=LOG_FILENAME,level=logging.INFO, format=formatLOG)
    logging.info('Daily data scrapper started')

load_dotenv()

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")
supabase = create_client(url, key)

def _proxies():
    proxy = os.environ.get("PROXY_URL")
    return {"http": proxy, "https": proxy} if proxy else None


def get_daily_data(date=None):

    # optional CLI date lets a missed day be re-run; defaults to today
    end = pd.Timestamp(date) if date else datetime.today()

    url = f"https://www.idx.co.id/primary/TradingSummary/GetStockSummary?length=9999&start=0&date={end.strftime('%Y%m%d')}"

    payload = json.loads(idx_get(url, kind="json", proxies=_proxies(), timeout=(10, 30)))
    data = payload.get("data") if isinstance(payload, dict) else None
    if data is None:
        raise ValueError(f"IDX returned no 'data' key for {end}")
    if not data:
        # IDX answers an empty list for a date it has no session for (holiday/weekend),
        # which is not a broken source: nothing to write, and the run should stay green.
        print(f"⚪ IDX returned 0 rows for {end} (no trading day)")
        return None

    full_df = pd.DataFrame(data)
    print(f"🟢Finish for date: {end}")

    full_df = full_df[['Date','StockCode','Close','ListedShares','Volume','ForeignSell','ForeignBuy','OpenPrice','High','Low','Value']].drop_duplicates()
    full_df['StockCode'] = full_df['StockCode']+".JK"

    full_df['Close'] = full_df['Close'].astype(int)
    full_df['Volume'] = full_df['Volume'].astype(int)
    full_df['ForeignSell'] = full_df['ForeignSell'].astype(int)
    full_df['ForeignBuy'] = full_df['ForeignBuy'].astype(int)
    full_df['OpenPrice'] = full_df['OpenPrice'].astype(int)
    full_df['High'] = full_df['High'].astype(int)
    full_df['Low'] = full_df['Low'].astype(int)
    full_df['Value'] = full_df['Value'].astype(int)
    full_df['market_cap'] = full_df['Close'] * full_df['ListedShares']
    full_df['market_cap'] = full_df['market_cap'].astype(int)

    full_df = full_df.rename(columns={"StockCode": "symbol", "Close": "close",  "Volume": "volume",'Date':'date','ForeignSell':"foreign_sell_volume",'ForeignBuy':'foreign_buy_volume', 'OpenPrice':'open','High':'high','Low':'low','Value':'value'})

    full_df['mcap_method'] = "1"

    full_df["updated_on"] = pd.Timestamp.now(tz="GMT").strftime("%Y-%m-%d %H:%M:%S")

    # open/high/low stay as scraped (0 when IDX omits them); iqplus_repair.py fills them.
    full_df = full_df[['date','symbol','close','volume','market_cap','foreign_sell_volume','foreign_buy_volume','open','high','low','value','mcap_method','updated_on']]

    full_df['date'] = pd.to_datetime(full_df['date'])

    return full_df

def _weekdays_before(day, n):
    out, cur = [], day
    while len(out) < n:
        cur -= timedelta(days=1)
        if cur.weekday() < 5:
            out.append(cur)
    return out


def _fetch_existing_ohl(dates):
    wanted = [d.strftime('%Y-%m-%d') for d in dates]
    rows = (supabase.table('idx_daily_data')
            .select('symbol,date,open,high,low').in_('date', wanted).execute().data)
    return pd.DataFrame(rows) if rows else None


def _preserve_ohl(df, date_str, existing):
    if existing is None or existing.empty:
        return df, 0
    today = existing[existing.date == date_str]
    if today.empty:
        return df, 0
    keep = today.set_index('symbol')
    filled = 0
    for col in ('open', 'high', 'low'):
        prev = df.symbol.map(keep[col]).fillna(0).astype('int64')
        use_old = (df[col] == 0) & (prev > 0)
        filled += int(use_old.sum())
        df[col] = df[col].where(~use_old, prev)
    return df, filled


def scrape_date(date_str, existing):
    upload_data = get_daily_data(date_str)
    if upload_data is None:
        return None, 0

    upload_data['updated_on'] = upload_data['updated_on'].astype(str)
    upload_data['date'] = upload_data['date'].astype(str)

    active_company = supabase.table("idx_active_company_profile").select("symbol").execute()
    active_syms = pd.DataFrame(active_company.data).symbol.unique()
    upload_data = upload_data[upload_data.symbol.isin(active_syms)].copy()

    upload_data, preserved = _preserve_ohl(upload_data, date_str, existing)
    return upload_data, preserved


if __name__ == "__main__":
    initiate_logging(LOG_FILENAME)

    argv = [a for a in sys.argv[1:] if not a.startswith('--')]
    dry_run = '--dry-run' in sys.argv
    run_date = argv[0] if argv else None

    if run_date:
        try:
            target = datetime.strptime(run_date, '%Y-%m-%d').date()
        except ValueError:
            raise SystemExit(f'invalid date {run_date!r}, expected YYYY-MM-DD')
    else:
        target = datetime.now(ZoneInfo('Asia/Jakarta')).date()

    if target.weekday() >= 5:
        # IDX does not trade on Saturday/Sunday: fetching the target only burns the
        # fingerprint ladder on a date that can never return rows. Keep refreshing the
        # prior weekdays so late corrections are still picked up.
        print(f'{target} is a weekend - no session, skipping it')
        dates = _weekdays_before(target, REFRESH_DAYS + 1)
    else:
        dates = [target] + _weekdays_before(target, REFRESH_DAYS)
    print(f'Dates: {" ".join(str(d) for d in dates)}')

    existing = _fetch_existing_ohl(dates)

    # On a weekend the target is not fetched at all, so the refresh dates are what the run
    # is really responsible for: a failure there must still mark the run red.
    target_is_fetched = target in dates
    run_failed = False
    for d in dates:
        date_str = d.strftime('%Y-%m-%d')
        is_failure = (d == target) or not target_is_fetched
        try:
            upload_data, preserved = scrape_date(date_str, existing)
        except Exception as e:
            msg = f'🔴 Failed scraping IDX for {date_str}: {e}'
            print(msg)
            logging.error(msg)
            if is_failure:
                run_failed = True
            continue

        if upload_data is None:
            msg = f'⚪ No trading data for {date_str}, nothing to upsert'
            print(msg)
            logging.info(msg)
            continue

        records = upload_data.to_dict(orient='records')
        note = f' (ohl preserved={preserved})' if preserved else ''

        if dry_run:
            print(f'DRY RUN: would upsert {len(records)} rows for {date_str}{note}')
            continue

        try:
            supabase.table('idx_daily_data').upsert(records).execute()
            msg = f'🟢 Finish upserting data for {date_str}, with {len(records)} companies appended'
            print(msg + note)
            logging.info(msg)
        except Exception as e:
            msg = f'🔴 Failed upserting data for {date_str}: {e}'
            print(msg)
            logging.error(msg)
            if is_failure:
                run_failed = True

    if run_failed:
        sys.exit(1)
