import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
import urllib.request
import os
import random
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client
from dotenv import load_dotenv
from importlib import reload
import sys

import logging

LOG_FILENAME = 'daily_data_scrapper.log'
IDX_HOME = 'https://www.idx.co.id/en/'
REFRESH_DAYS = 2
# Cloudflare accepts some TLS fingerprints and rejects the rest, and that set rotates
# over days, so try a short ladder per round instead of pinning one.
IDX_FINGERPRINTS = ['safari17_0', 'chrome124', 'chrome131']
IDX_ROUNDS = 2
IDX_BACKOFF_SECONDS = 5
IDX_WARMUP_TIMEOUT = 5

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


def _is_json_body(response) -> bool:
    """Cloudflare sometimes answers 200 with its HTML challenge page instead of JSON."""
    return response.text.lstrip().startswith("{")


def _fetch_plain(url, headers, proxies, timeout):
    session = requests.Session()
    try:
        session.mount("https://", HTTPAdapter(max_retries=Retry(
            total=3, connect=3, read=3, backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"],
        )))
        response = session.get(
            url, headers=headers, proxies=proxies,
            verify=False,      # disables SSL cert check
            timeout=timeout,   # (connect, read)
        )
        response.raise_for_status()
        if not _is_json_body(response):
            raise ValueError(f"non-JSON body (HTTP {response.status_code}) - likely a challenge page")
        return response
    finally:
        session.close()   # the body is already buffered, so closing here is safe


def _fetch_summary(url, headers, timeout=(10, 30), attempts=IDX_ROUNDS):
    """GET the IDX summary, plain requests first, browser fingerprint after.

    Cloudflare fingerprints plain urllib3, so that path only works when the proxy exit
    IP is trusted. When it is exhausted, retry with curl_cffi's browser TLS fingerprint
    and the cookies seeded from the public page, which the same endpoint answers in
    well under a second.
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        cffi_requests = None

    proxies = _proxies()

    try:
        return _fetch_plain(url, headers, proxies, timeout)
    except Exception as e:
        last = e
        print(f"⚠️ plain fetch failed: {type(e).__name__}: {e}")

    if cffi_requests is None:
        raise RuntimeError(f"IDX fetch failed: {last}") from last

    # The impersonated client sets its own User-Agent; keep our headers from overriding it
    fingerprint_headers = {k: v for k, v in headers.items() if k.lower() != "user-agent"}

    warmup_notes = []
    for round_no in range(1, attempts + 1):
        for imp in IDX_FINGERPRINTS:
            session = cffi_requests.Session(impersonate=imp, proxies=proxies, verify=False)
            try:
                # The warm-up only seeds Cloudflare cookies: a slow home page must not stop
                # us from calling the API itself, which is the part that has to succeed.
                try:
                    session.get(IDX_HOME, timeout=IDX_WARMUP_TIMEOUT)
                except Exception as warmup_error:
                    warmup_notes.append(f"{imp}: {type(warmup_error).__name__}")
                response = session.get(url, headers=fingerprint_headers, timeout=timeout)
                response.raise_for_status()
                if not _is_json_body(response):
                    raise ValueError(f"non-JSON body (HTTP {response.status_code}) - likely a challenge page")
                return response
            except Exception as e:
                last = e
                print(f"⚠️ IDX {imp} round {round_no}/{attempts} failed: {type(e).__name__}: {e}")
            finally:
                session.close()
        if round_no < attempts:
            time.sleep(IDX_BACKOFF_SECONDS * round_no)

    warmup_note = f" (warm-up: {warmup_notes[0]})" if warmup_notes else ""
    raise RuntimeError(
        f"IDX fetch failed after plain requests + {attempts} rounds x {len(IDX_FINGERPRINTS)} "
        f"fingerprints: {last}{warmup_note}"
    ) from last


def get_daily_data(date=None):

    # optional CLI date lets a missed day be re-run; defaults to today
    end = pd.Timestamp(date) if date else datetime.today()

    url = f"https://www.idx.co.id/primary/TradingSummary/GetStockSummary?length=9999&start=0&date={end.strftime('%Y%m%d')}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/119.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.idx.co.id/",
    }

    response = _fetch_summary(url, headers)

    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if data is None:
        raise ValueError(f"IDX returned no 'data' key for {end} (HTTP {response.status_code})")
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

    dates = [target] + _weekdays_before(target, REFRESH_DAYS)
    print(f'Dates: {" ".join(str(d) for d in dates)} (target + {REFRESH_DAYS} prior weekdays)')

    existing = _fetch_existing_ohl(dates)

    target_failed = False
    for d in dates:
        date_str = d.strftime('%Y-%m-%d')
        is_target = d == target
        try:
            upload_data, preserved = scrape_date(date_str, existing)
        except Exception as e:
            msg = f'🔴 Failed scraping IDX for {date_str}: {e}'
            print(msg)
            logging.error(msg)
            if is_target:
                target_failed = True
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
            if is_target:
                target_failed = True

    if target_failed:
        sys.exit(1)
