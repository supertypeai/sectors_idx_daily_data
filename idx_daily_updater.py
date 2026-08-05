import requests
import pandas as pd
import yfinance as yf
import urllib.request
import os
import random
import json
import time
from datetime import datetime, timedelta
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

YF_FILL_COLS = {"open": "Open", "high": "High", "low": "Low"}


def fill_ohl_from_yfinance(df, date):
    """IDX leaves OpenPrice/High/Low at 0 for some rows — backfill those columns
    from Yahoo. Each column is filled independently and only where it is 0, so a
    row with a real high but a zero open only has its open replaced. Nothing
    outside these three columns is touched, and a symbol Yahoo has no usable
    price for keeps its 0."""

    needs = df[list(YF_FILL_COLS)].eq(0).any(axis=1)
    missing = df.loc[needs, 'symbol'].unique().tolist()
    if not missing:
        return df

    start_date = pd.Timestamp(date).normalize()
    end_date = start_date + timedelta(days=1)   # yfinance end is exclusive

    fetched = {}
    for i in missing:
        try:
            ticker = yf.Ticker(i)
            # auto_adjust=False keeps the raw traded price, matching IDX
            a = ticker.history(start=start_date, end=end_date, auto_adjust=False)
            if a.empty:
                continue
            a = a.reset_index()[["Date"] + list(YF_FILL_COLS.values())]
            a = a[a["Date"].dt.strftime("%Y-%m-%d") == start_date.strftime("%Y-%m-%d")]
            if a.empty:
                continue
            row = a.iloc[0]
            fetched[i] = {
                col: int(round(float(row[yf_col])))
                for col, yf_col in YF_FILL_COLS.items()
                if pd.notna(row[yf_col]) and row[yf_col] > 0
            }
        except Exception as e:
            print(f"⚠️ yfinance lookup failed for {i}: {e}")

    for col in YF_FILL_COLS:
        was_zero = df[col] == 0
        if not was_zero.any():
            continue
        fill = df['symbol'].map({s: v[col] for s, v in fetched.items() if col in v})
        # only rows that are still 0 and got a real number back
        target = was_zero & fill.notna()
        df.loc[target, col] = fill[target].astype(int)

        print(f"🟡 {col}==0 for {int(was_zero.sum())} rows, filled {int(target.sum())} "
              f"from yfinance, {int(was_zero.sum() - target.sum())} left at 0")
        logging.info(f"{col}==0 for {int(was_zero.sum())} rows, filled {int(target.sum())} from yfinance")

    return df


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
    
    response = requests.get(
        url,
        headers=headers,
        proxies=PROXIES,
        verify=False   # disables SSL cert check
    )

    try:
        full_df = pd.DataFrame(response.json()['data'])
        # full_df = pd.concat([full_df, test], ignore_index=True)
        print(f"🟢Finish for date: {end}")
    except:
        print(f"🔴error for date {end}")

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

    full_df = fill_ohl_from_yfinance(full_df, end)

    full_df = full_df[['date','symbol','close','volume','market_cap','foreign_sell_volume','foreign_buy_volume','open','high','low','value','mcap_method','updated_on']]

    full_df['date'] = pd.to_datetime(full_df['date'])

    return full_df

run_date = sys.argv[1] if len(sys.argv) > 1 else None

upload_data = get_daily_data(run_date)
upload_data['updated_on'] = upload_data['updated_on'].astype(str)
upload_data['date'] = upload_data['date'].astype(str)

active_company = supabase.table("idx_active_company_profile").select("symbol").execute()
active_company = pd.DataFrame(active_company.data)

upload_data = upload_data[upload_data.symbol.isin(active_company.symbol.unique())]

records = upload_data.to_dict(orient='records')

initiate_logging(LOG_FILENAME)

try:
    supabase.table('idx_daily_data').upsert(records).execute()
    logging.info(f'🟢 Finish upserting data for {datetime.today()}, with {upload_data.shape[0]} companies appended')
    print(f'🟢 Finish upserting data for {datetime.today()}, with {upload_data.shape[0]} companies appended')
except:
    logging.info('🔴 Failed upserting data for {datetime.today()')
    print('🔴 Failed upserting data for {datetime.today()')
    sys.exit(1)
