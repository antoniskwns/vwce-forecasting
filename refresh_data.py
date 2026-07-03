#!/usr/bin/env python3
"""
Refresh universe_prices.csv from Yahoo Finance (raw v8 JSON, no deps).
Pulls full daily history for all 23 universe tickers, aligned on a common
date index, adjusted-close. Overwrites universe_prices.csv after backing up.
"""
import urllib.request, json, time, datetime, sys
import pandas as pd
from pathlib import Path

BASE = Path(__file__).parent
OUT  = BASE / "universe_prices.csv"

TICKERS = ['VWCE.DE','CSPX.L','IWDA.L','EIMI.L','IFSW.L','IWMO.L','WSML.L','IWVL.L',
           'QQQ','VGT','AAPL','MSFT','NVDA','GOOGL','AMZN','TLT','GLD','VNQ','SSO',
           'QLD','BRK-B','VT','ACWI']

def fetch(sym, max_retries=3):
    """Full daily adjusted-close history as a pd.Series indexed by date."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?interval=1d&range=20y&events=div%2Csplit")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(max_retries):
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=20).read())
            r = d["chart"]["result"][0]
            ts = r["timestamp"]
            # Prefer adjusted close (handles splits/divs); fall back to close.
            adj = r.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose")
            cl  = r["indicators"]["quote"][0]["close"]
            vals = adj if adj else cl
            idx = [datetime.datetime.fromtimestamp(t, datetime.UTC).date() for t in ts]
            s = pd.Series(vals, index=pd.to_datetime(idx), name=sym)
            s = s[~s.index.duplicated(keep="last")].dropna()
            return s
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"  {sym:<9} FAILED: {type(e).__name__}: {str(e)[:70]}")
                return None
            time.sleep(1.5)

def main():
    print(f"Refreshing {len(TICKERS)} tickers from Yahoo...\n")
    series = {}
    for t in TICKERS:
        s = fetch(t)
        if s is not None:
            print(f"  {t:<9} {len(s):>5} bars  {s.index.min().date()} -> {s.index.max().date()}")
            series[t] = s
        time.sleep(0.3)

    if len(series) < len(TICKERS):
        print(f"\nWARNING: only {len(series)}/{len(TICKERS)} tickers fetched.")

    df = pd.DataFrame(series).sort_index()
    df.index.name = "Date"
    # Keep the column order identical to the original file.
    df = df[[t for t in TICKERS if t in df.columns]]

    if OUT.exists():
        bak = OUT.with_suffix(".csv.bak")
        OUT.replace(bak)
        print(f"\nBacked up old file -> {bak.name}")
    df.to_csv(OUT)
    print(f"Wrote {OUT.name}: shape {df.shape}, "
          f"range {df.index.min().date()} -> {df.index.max().date()}")

if __name__ == "__main__":
    main()
