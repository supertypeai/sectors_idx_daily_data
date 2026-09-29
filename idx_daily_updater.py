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
from imp import reload
import sys

import logging

LOG_FILENAME = 'daily_data_scrapper.log'

def initiate_logging(LOG_FILENAME):
    reload(logging)

    formatLOG = '%(asctime)s - %(levelname)s: %(message)s'
    logging.basicConfig(filename=LOG_FILENAME,level=logging.INFO, format=formatLOG)
    logging.info('Daily data scrapper started')

load_dotenv()

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")
supabase = create_client(url, key)

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

    PROXY_URL = os.environ.get("PROXY_URL")
    PROXIES = {
        "http": PROXY_URL,
        "https": PROXY_URL,
    }

    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(
        total=3, connect=3, read=3, backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"],
    )))

    response = session.get(
        url,
        headers=headers,
        proxies=PROXIES,
        verify=False,      # disables SSL cert check
        timeout=(10, 30),  # (connect, read)
    )
    response.raise_for_status()

    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not data:
        raise ValueError(f"IDX returned no 'data' for {end} (HTTP {response.status_code})")

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
