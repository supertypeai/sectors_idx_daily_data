import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
import urllib.request
import os
import random
import json
import time
from datetime import datetime
from supabase import create_client
from dotenv import load_dotenv
from importlib import reload
import sys

import logging

LOG_FILENAME = 'daily_data_scrapper.log'
IDX_HOME = 'https://www.idx.co.id/en/'
IDX_IMPERSONATE = 'chrome124'
IDX_FINGERPRINT_ATTEMPTS = 3
IDX_BACKOFF_SECONDS = 5
IDX_WARMUP_TIMEOUT = 15

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
        return response
    finally:
        session.close()   # the body is already buffered, so closing here is safe


def _fetch_summary(url, headers, timeout=(10, 30), attempts=IDX_FINGERPRINT_ATTEMPTS):
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

    for attempt in range(1, attempts + 1):
        session = cffi_requests.Session(impersonate=IDX_IMPERSONATE, proxies=proxies, verify=False)
        try:
            session.get(IDX_HOME, timeout=min(timeout[1], IDX_WARMUP_TIMEOUT))   # seeds the Cloudflare cookies
            response = session.get(url, headers=fingerprint_headers, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as e:
            last = e
            print(f"⚠️ IDX fingerprint attempt {attempt}/{attempts} failed: {type(e).__name__}: {e}")
            if attempt < attempts:
                time.sleep(IDX_BACKOFF_SECONDS * attempt)
        finally:
            session.close()

    raise RuntimeError(f"IDX fetch failed after plain requests + {attempts} fingerprint attempts: {last}") from last


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

if __name__ == "__main__":
    initiate_logging(LOG_FILENAME)

    run_date = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        upload_data = get_daily_data(run_date)
    except Exception as e:
        logging.error(f'🔴 Failed scraping IDX for {run_date or datetime.today().date()}: {e}')
        print(f'🔴 Failed scraping IDX for {run_date or datetime.today().date()}: {e}')
        sys.exit(1)

    if upload_data is None:
        logging.info(f'⚪ No trading data for {run_date or datetime.today().date()}, nothing to upsert')
        print(f'⚪ No trading data for {run_date or datetime.today().date()}, nothing to upsert')
        sys.exit(0)

    upload_data['updated_on'] = upload_data['updated_on'].astype(str)
    upload_data['date'] = upload_data['date'].astype(str)

    active_company = supabase.table("idx_active_company_profile").select("symbol").execute()
    active_company = pd.DataFrame(active_company.data)

    upload_data = upload_data[upload_data.symbol.isin(active_company.symbol.unique())]

    records = upload_data.to_dict(orient='records')

    try:
        supabase.table('idx_daily_data').upsert(records).execute()
        logging.info(f'🟢 Finish upserting data for {datetime.today()}, with {upload_data.shape[0]} companies appended')
        print(f'🟢 Finish upserting data for {datetime.today()}, with {upload_data.shape[0]} companies appended')
    except Exception as e:
        logging.error(f'🔴 Failed upserting data for {datetime.today()}: {e}')
        print(f'🔴 Failed upserting data for {datetime.today()}: {e}')
        sys.exit(1)
