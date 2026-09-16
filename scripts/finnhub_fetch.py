#!/usr/bin/env python3
"""
APEX Terminal — Finnhub Multi-Endpoint Fetcher
===============================================
Exposes every free-tier Finnhub endpoint relevant to options analytics:
  quote, metrics, option_chain, earnings_calendar, earnings_surprise,
  company_profile, price_target, recommendation_trends, insider_transactions,
  sentiment, peers, technical_indicators, candles, support_resistance
"""

import sys, json, os, math, time
sys.path.insert(0, os.path.dirname(__file__))
from data_sources import (
    finnhub_quote, finnhub_bidask, finnhub_option_chain, finnhub_metrics,
    finnhub_earnings, finnhub_candles, finnhub_sentiment,
    finnhub_recommendation, finnhub_peers, finnhub_insider,
    finnhub_price_target, finnhub_technical, finnhub_support_resistance,
    _http_get, FINNHUB_KEY, _sf, _si,
)
from zoneinfo import ZoneInfo
from datetime import datetime

def _now_ny() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

def fetch_quote(sym: str) -> dict:
    q = finnhub_quote(sym)
    if not q: return {}
    return {
        "symbol": sym,
        "price":  q.get("c", 0),
        "open":   q.get("o", 0),
        "high":   q.get("h", 0),
        "low":    q.get("l", 0),
        "prev":   q.get("pc", 0),
        "change": q.get("d", 0),
        "pctChange": q.get("dp", 0),
        "timestamp": q.get("t", 0),
        "nyTime":    _now_ny(),
        "source": "finnhub",
    }

def fetch_bidask(sym: str) -> dict:
    quote = finnhub_bidask(sym)
    if not quote:
        return {"symbol": sym, "quoteAvailable": False, "source": "finnhub", "endpoint": "/stock/bidask"}
    return {"symbol": sym, **quote, "nyTime": _now_ny()}


def fetch_metrics(sym: str) -> dict:
    m = finnhub_metrics(sym)
    if not m: return {}
    met = m.get("metric", {})
    return {
        "symbol": sym,
        # Volatility
        "52WeekHigh":     _sf(met.get("52WeekHigh")),
        "52WeekLow":      _sf(met.get("52WeekLow")),
        "52WeekHighDate": met.get("52WeekHighDate", ""),
        "52WeekLowDate":  met.get("52WeekLowDate", ""),
        "beta":           _sf(met.get("beta")),
        "marketCap":      _sf(met.get("marketCapitalization")),
        "shareOutstanding": _sf(met.get("shareOutstanding")),
        # Earnings
        "epsTTM":         _sf(met.get("epsTTM")),
        "epsGrowthTTM":   _sf(met.get("epsGrowthTTMYoy")),
        "revenuePerShare": _sf(met.get("revenuePerShareTTM")),
        # Dividends
        "dividendYield":      _sf(met.get("currentDividendYieldTTM")),
        "dividendPerShare":   _sf(met.get("dividendPerShareAnnual")),
        "dividendGrowthRate": _sf(met.get("dividendGrowthRate5Y")),
        # Valuation
        "peRatio":  _sf(met.get("peTTM")),
        "pbRatio":  _sf(met.get("pbAnnual")),
        "psRatio":  _sf(met.get("psTTM")),
        "evEbitda": _sf(met.get("evToEbitdaTTM")),
        # Risk
        "roaRfy":  _sf(met.get("roaRfy")),
        "roeRfy":  _sf(met.get("roeRfy")),
        "debtEquity": _sf(met.get("totalDebt/totalEquityAnnual")),
        "source": "finnhub",
    }

def fetch_option_chain(sym: str) -> dict:
    chain = finnhub_option_chain(sym)
    if not chain: return {}
    return {"symbol": sym, "chain": chain, "source": "finnhub"}

def fetch_earnings(sym: str) -> dict:
    e = finnhub_earnings(sym)
    if not e: return {"symbol": sym, "earnings": [], "source": "finnhub"}
    return {"symbol": sym, "earnings": e, "source": "finnhub"}

def fetch_candles(sym: str) -> dict:
    c = finnhub_candles(sym)
    if not c: return {"symbol": sym, "candles": [], "source": "finnhub"}
    return {"symbol": sym, "candles": c, "source": "finnhub"}

def fetch_sentiment(sym: str) -> dict:
    s = finnhub_sentiment(sym)
    if not s: return {"symbol": sym, "sentiment": {}, "source": "finnhub"}
    return {"symbol": sym, "sentiment": s, "source": "finnhub"}

def fetch_recommendation(sym: str) -> dict:
    r = finnhub_recommendation(sym)
    if not r: return {"symbol": sym, "recommendations": [], "source": "finnhub"}
    return {"symbol": sym, "recommendations": r[:6], "source": "finnhub"}

def fetch_peers(sym: str) -> dict:
    p = finnhub_peers(sym)
    if not p: return {"symbol": sym, "peers": [], "source": "finnhub"}
    return {"symbol": sym, "peers": p, "source": "finnhub"}

def fetch_insider(sym: str) -> dict:
    i = finnhub_insider(sym)
    if not i: return {"symbol": sym, "insider": [], "source": "finnhub"}
    return {"symbol": sym, "insider": i[:20], "source": "finnhub"}

def fetch_price_target(sym: str) -> dict:
    pt = finnhub_price_target(sym)
    if not pt: return {"symbol": sym, "priceTarget": {}, "source": "finnhub"}
    return {"symbol": sym, "priceTarget": pt, "source": "finnhub"}

def fetch_technical(sym: str) -> dict:
    t = finnhub_technical(sym)
    if not t: return {"symbol": sym, "technical": {}, "source": "finnhub"}
    return {"symbol": sym, "technical": t, "source": "finnhub"}

def fetch_support_resistance(sym: str) -> dict:
    sr = finnhub_support_resistance(sym)
    if not sr: return {"symbol": sym, "supportResistance": {}, "source": "finnhub"}
    return {"symbol": sym, "supportResistance": sr, "source": "finnhub"}

def fetch_all(sym: str) -> dict:
    """Fetch all endpoints in one call — for dashboard enrichment."""
    result = {"symbol": sym, "nyTime": _now_ny(), "source": "finnhub"}
    try: result["quote"]         = fetch_quote(sym)
    except: pass
    try: result["metrics"]       = fetch_metrics(sym)
    except: pass
    try: result["earnings"]      = fetch_earnings(sym)
    except: pass
    try: result["sentiment"]     = fetch_sentiment(sym)
    except: pass
    try: result["recommendation"]= fetch_recommendation(sym)
    except: pass
    try: result["priceTarget"]   = fetch_price_target(sym)
    except: pass
    try: result["peers"]         = fetch_peers(sym)
    except: pass
    try: result["technical"]     = fetch_technical(sym)
    except: pass
    try: result["insider"]       = fetch_insider(sym)
    except: pass
    try: result["supportResistance"] = fetch_support_resistance(sym)
    except: pass
    return result

DISPATCH = {
    "quote":              fetch_quote,
    "bidask":             fetch_bidask,
    "bid_ask":            fetch_bidask,
    "metrics":            fetch_metrics,
    "option_chain":       fetch_option_chain,
    "earnings":           fetch_earnings,
    "candles":            fetch_candles,
    "sentiment":          fetch_sentiment,
    "recommendation":     fetch_recommendation,
    "peers":              fetch_peers,
    "insider":            fetch_insider,
    "price_target":       fetch_price_target,
    "technical":          fetch_technical,
    "support_resistance": fetch_support_resistance,
    "all":                fetch_all,
}

def main():
    sym      = sys.argv[1].upper() if len(sys.argv) > 1 else "AAPL"
    endpoint = sys.argv[2].lower() if len(sys.argv) > 2 else "all"
    fn = DISPATCH.get(endpoint, fetch_all)
    try:
        result = fn(sym)
        print(json.dumps(result))
    except Exception as e:
        sys.stderr.write(f"finnhub_fetch error: {e}\n")
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

if __name__ == "__main__":
    main()
