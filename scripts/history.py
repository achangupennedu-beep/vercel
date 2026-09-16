#!/usr/bin/env python3
"""
OHLCV history fetcher — Alpaca (primary) + Polygon (fallback).

Feed strategy (free tier):
  - Historical queries always use feed=sip for 100% market coverage.
    On the free tier historical SIP data is unrestricted (no 15-min delay
    for past data — only the "latest 15 minutes" of today is delayed).
  - IEX historical is also available but only covers ~2% of volume.
  - We therefore use sip for history and iex only for real-time snapshots.

Usage: python3 history.py TSLA 1d 3mo
       python3 history.py TSLA 5m 1d
       python3 history.py TSLA 1h 1mo
"""
import sys, json, os, math, time, urllib.request
import requests
from datetime import datetime, timezone, timedelta, date as _date

APCA_KEY = os.environ.get("APCA_API_KEY_ID", "").strip()
APCA_SEC = os.environ.get("APCA_API_SECRET_KEY", "").strip()
POLY_KEY = os.environ.get("POLYGON_API_KEY", "").strip()
EULERPOOL_KEY = os.environ.get("EULERPOOL_API_KEY", "").strip()

def fetch(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def sf(v, d=0.0):
    try:
        f = float(v)
        return d if (math.isnan(f) or math.isinf(f)) else f
    except: return d

PERIOD_DAYS = {
    "1d": 1, "2d": 2, "5d": 5, "1w": 7, "2w": 14,
    "1mo": 35, "3mo": 95, "6mo": 185, "1y": 370,
    "2y": 740, "5y": 1825, "max": 3650,
}

APCA_TF = {
    "1m":"1Min","2m":"2Min","5m":"5Min","15m":"15Min","30m":"30Min",
    "1h":"1Hour","4h":"4Hour","1d":"1Day","1w":"1Week","1mo":"1Month",
}

POLY_TF = {
    "1m":(1,"minute"),"5m":(5,"minute"),"15m":(15,"minute"),"30m":(30,"minute"),
    "1h":(1,"hour"),"4h":(4,"hour"),"1d":(1,"day"),"1w":(1,"week"),"1mo":(1,"month"),
}

def period_to_dates(period):
    end   = _date.today()
    days  = PERIOD_DAYS.get(period, 95)
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()

# ── Eulerpool primary ─────────────────────────────────────────────────────────
def eulerpool_bars(symbol, start, end):
    response = requests.get(
        f"https://api.eulerpool.com/api/1/equity/quotes/{symbol}",
        params={"token": EULERPOOL_KEY, "startdate": start, "enddate": end},
        headers={"Accept": "application/json"}, timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload if isinstance(payload, list) else payload.get("data", []) if isinstance(payload, dict) else []
    bars = []
    for row in rows:
        if not isinstance(row, dict): continue
        close = sf(row.get("close") or row.get("price") or row.get("value"))
        if not close: continue
        raw_time = row.get("date") or row.get("timestamp") or row.get("time")
        try:
            ts = int(float(raw_time) * (1000 if float(raw_time) < 1e11 else 1))
        except (TypeError, ValueError):
            ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        bars.append({"t": ts, "o": sf(row.get("open"), close), "h": sf(row.get("high"), close),
                     "l": sf(row.get("low"), close), "c": close, "v": int(sf(row.get("volume"))),
                     "vw": sf(row.get("vwap"), close)})
    return bars

# ── Alpaca ────────────────────────────────────────────────────────────────────

def alpaca_bars(symbol, interval, start, end):
    tf  = APCA_TF.get(interval, "1Day")
    hdrs = {"APCA-API-KEY-ID": APCA_KEY, "APCA-API-SECRET-KEY": APCA_SEC}
    # Use sip for historical: 100% volume coverage, no delay restriction on past data.
    url  = (f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
            f"?timeframe={tf}&start={start}&end={end}&limit=1000&sort=asc&feed=sip")
    bars = []
    while url:
        d = fetch(url, hdrs)
        for b in d.get("bars", []):
            ts = int(datetime.fromisoformat(b["t"].replace("Z","+00:00")).timestamp() * 1000)
            bars.append({"t":ts,"o":sf(b.get("o")),"h":sf(b.get("h")),
                         "l":sf(b.get("l")),"c":sf(b.get("c")),
                         "v":int(b.get("v",0)),"vw":sf(b.get("vw"))})
        npt = d.get("next_page_token")
        url = (f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
               f"?timeframe={tf}&start={start}&end={end}&limit=1000&sort=asc&feed=sip&page_token={npt}"
               if npt and len(bars) < 2000 else None)
    return bars

# ── Polygon ───────────────────────────────────────────────────────────────────

def polygon_bars(symbol, interval, start, end):
    mult, span = POLY_TF.get(interval, (1,"day"))
    url = (f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/{mult}/{span}"
           f"/{start}/{end}?adjusted=true&sort=asc&limit=1000&apiKey={POLY_KEY}")
    d = fetch(url)
    return [{"t":b["t"],"o":sf(b.get("o")),"h":sf(b.get("h")),
             "l":sf(b.get("l")),"c":sf(b.get("c")),"v":int(b.get("v",0)),"vw":sf(b.get("vw"))}
            for b in d.get("results",[])]

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    symbol   = sys.argv[1].upper() if len(sys.argv) > 1 else "AAPL"
    interval = sys.argv[2]         if len(sys.argv) > 2 else "1d"
    period   = sys.argv[3]         if len(sys.argv) > 3 else "3mo"

    start, end = period_to_dates(period)
    bars, source = [], "none"

    if EULERPOOL_KEY:
        try:
            bars = eulerpool_bars(symbol, start, end)
            if bars: source = "eulerpool"
        except Exception as e:
            sys.stderr.write(f"eulerpool: {e}\n")

    if not bars:
        try:
            bars = alpaca_bars(symbol, interval, start, end)
            if bars: source = "alpaca"
        except Exception as e:
            sys.stderr.write(f"alpaca: {e}\n")

    if not bars:
        try:
            bars = polygon_bars(symbol, interval, start, end)
            if bars: source = "polygon"
        except Exception as e:
            sys.stderr.write(f"polygon: {e}\n")

    print(json.dumps({"symbol":symbol,"interval":interval,"period":period,
                      "source":source,"count":len(bars),"bars":bars}))

if __name__ == "__main__":
    main()
