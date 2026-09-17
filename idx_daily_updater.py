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

OHL_COLS = ["open", "high", "low"]
YF_FILL_COLS = {"open": "Open", "high": "High", "low": "Low"}


IQPLUS_TIMEOUT = 20
IQPLUS_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _fetch_iqplus_ohl(symbol: str, target_date_str: str, session: requests.Session = None) -> dict:
    code = symbol.replace(".JK", "").upper()
    url = f"https://www.iqplus.info/api/v1/ohlcv.php?code={code}"
    http = session or requests
    try:
        resp = http.get(url, headers=IQPLUS_HEADERS, timeout=IQPLUS_TIMEOUT)
        if resp.status_code != 200:
            return {}
        # IQPlus returns newest at end; search reversed to find target date immediately
        for row in reversed(resp.json()):
            t = row.get("time")
            if t == target_date_str:
                return {
                    c: int(round(float(row[c])))
                    for c in OHL_COLS
                    if row.get(c) is not None and row[c] > 0
                }
            if t < target_date_str:
                break
    except Exception as e:
        print(f"⚠️ iqplus lookup failed for {symbol}: {e}")
    return {}


def is_candle_valid(open_p, high_p, low_p, close_p) -> bool:
    """Validate candlestick geometry: Low <= Open <= High, Low <= Close <= High, Low <= High."""
    if open_p <= 0 or high_p <= 0 or low_p <= 0 or close_p <= 0:
        return False
    return (low_p <= open_p <= high_p) and (low_p <= close_p <= high_p) and (low_p <= high_p)


def fill_ohl_fallbacks(df, date):
    """Fallback 2: IQPlus. Fallback 3: Yahoo Finance.
    Open/High/Low only. Close & Volume stay untouched from IDX.
    Columns filled independently only where still 0, guarded by is_candle_valid."""
    target_date_str = pd.Timestamp(date).strftime("%Y-%m-%d")

    # --- Fallback 2: IQPlus ---
    needs = df[OHL_COLS].eq(0).any(axis=1)
    missing = df.loc[needs, 'symbol'].unique().tolist()
    if missing:
        with requests.Session() as s:
            iq_fetched = {sym: _fetch_iqplus_ohl(sym, target_date_str, session=s) for sym in missing}

        for idx, row in df[needs].iterrows():
            sym = row['symbol']
            iq = iq_fetched.get(sym, {})
            if not iq:
                continue
            cand_o = row['open'] if row['open'] > 0 else iq.get('open', 0)
            cand_h = row['high'] if row['high'] > 0 else iq.get('high', 0)
            cand_l = row['low'] if row['low'] > 0 else iq.get('low', 0)
            c = row['close']

            if is_candle_valid(cand_o, cand_h, cand_l, c):
                for col, val in [('open', cand_o), ('high', cand_h), ('low', cand_l)]:
                    if row[col] == 0 and val > 0:
                        df.at[idx, col] = int(val)
                        print(f"🟡 {sym} {col} filled from IQPlus: {val}")
                        logging.info(f"{sym} {col} filled from IQPlus: {val}")

    # --- Fallback 3: Yahoo Finance ---
    needs_yf = df[OHL_COLS].eq(0).any(axis=1)
    missing_yf = df.loc[needs_yf, 'symbol'].unique().tolist()
    if not missing_yf:
        return df

    start_date = pd.Timestamp(date).normalize()
    end_date = start_date + timedelta(days=1)

    yf_fetched = {}
    for i in missing_yf:
        try:
            ticker = yf.Ticker(i)
            a = ticker.history(start=start_date, end=end_date, auto_adjust=False)
            if a.empty:
                continue
            a = a.reset_index()[["Date"] + list(YF_FILL_COLS.values())]
            a = a[a["Date"].dt.strftime("%Y-%m-%d") == target_date_str]
            if a.empty:
                continue
            row_data = a.iloc[0]
            yf_fetched[i] = {
                col: int(round(float(row_data[yf_col])))
                for col, yf_col in YF_FILL_COLS.items()
                if pd.notna(row_data[yf_col]) and row_data[yf_col] > 0
            }
        except Exception as e:
            print(f"⚠️ yfinance lookup failed for {i}: {e}")

    for idx, row in df[needs_yf].iterrows():
        sym = row['symbol']
        yf_vals = yf_fetched.get(sym, {})
        if not yf_vals:
            continue
        cand_o = row['open'] if row['open'] > 0 else yf_vals.get('open', 0)
        cand_h = row['high'] if row['high'] > 0 else yf_vals.get('high', 0)
        cand_l = row['low'] if row['low'] > 0 else yf_vals.get('low', 0)
        c = row['close']

        # Enforce candle integrity to reject split-adjusted scale mismatches
        if is_candle_valid(cand_o, cand_h, cand_l, c):
            for col, val in [('open', cand_o), ('high', cand_h), ('low', cand_l)]:
                if row[col] == 0 and val > 0:
                    df.at[idx, col] = int(val)
                    print(f"🟡 {sym} {col} filled from yfinance: {val}")
                    logging.info(f"{sym} {col} filled from yfinance: {val}")
        else:
            print(f"⚠️ {sym} rejected yfinance candidate: candle geometry invalid against IDX close ({cand_o}/{cand_h}/{cand_l} vs {c})")
            logging.warning(f"{sym} rejected yfinance candidate: candle geometry invalid against IDX close")

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

    full_df = fill_ohl_fallbacks(full_df, end)

    full_df = full_df[['date','symbol','close','volume','market_cap','foreign_sell_volume','foreign_buy_volume','open','high','low','value','mcap_method','updated_on']]

    full_df['date'] = pd.to_datetime(full_df['date'])

    return full_df

if __name__ == "__main__":
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
        logging.info('🔴 Failed upserting data for {datetime.today()}')
        print('🔴 Failed upserting data for {datetime.today()}')
        sys.exit(1)
