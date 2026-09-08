#!/usr/bin/env python3
"""
eulerpool_source.py — Eulerpool Financial Data API integration
==============================================================
Standalone script for on-demand fundamental, institutional, and
sentiment data from Eulerpool (https://eulerpool.com/developers).

Budget: 1,000 requests/month. All responses are cached in-process
and on-disk (JSON, 1-hour TTL) to stay well within budget.

Modes (argv[1]):
  profile     <SYM>   — company profile, sector, market cap
  fundamentals <SYM>  — income statement, EPS, P/E, ROE, FCF
  analysts    <SYM>   — consensus ratings and price targets
  institutional <SYM> — top institutional holders and % ownership
  sentiment   <SYM>   — social/news sentiment scores
  screener    [--sector=X] [--min-pe=N] [--max-pe=N] [--limit=N]
  derivatives <SYM>   — options/futures open interest (Eulerpool Derivatives)
  macro       <CODE>  — macroeconomic indicators (GDP, CPI, etc.)

All output is a single JSON object on stdout. Errors go to stderr.

Usage:
  python3 eulerpool_source.py profile AAPL
  python3 eulerpool_source.py fundamentals NVDA
  python3 eulerpool_source.py screener --sector=Technology --max-pe=30 --limit=10
"""

import sys, os, json, time, math, hashlib, pathlib

EP_KEY   = os.environ.get("EULERPOOL_API_KEY", "eu_prod_1782933237805_jp4xbr2ag5c")
_EP_BASE = "https://api.eulerpool.com"
_HDRS    = lambda: {"Authorization": f"Bearer {EP_KEY}", "Accept": "application/json"}

# ── On-disk response cache (survives process restarts; budget protection) ──────
_CACHE_DIR = pathlib.Path("/tmp/eulerpool_cache")
_CACHE_DIR.mkdir(exist_ok=True)
_CACHE_TTL  = 3600   # 1 hour


def _cache_key(path: str, params: dict) -> str:
    raw = f"{path}:{json.dumps(params, sort_keys=True)}"
    return hashlib.sha1(raw.encode()).hexdigest()


def _cache_get(key: str):
    p = _CACHE_DIR / f"{key}.json"
    if not p.exists(): return None
    try:
        payload = json.loads(p.read_text())
        if time.time() - payload["ts"] < _CACHE_TTL:
            return payload["data"]
    except: pass
    return None


def _cache_set(key: str, data) -> None:
    p = _CACHE_DIR / f"{key}.json"
    try: p.write_text(json.dumps({"ts": time.time(), "data": data}))
    except: pass


# ── HTTP helper ────────────────────────────────────────────────────────────────

import urllib.request, urllib.error, urllib.parse


def _ep_get(path: str, params: dict | None = None, timeout: int = 10):
    if not EP_KEY:
        sys.stderr.write("[eulerpool] no API key set\n")
        return None
    merged_params = {**(params or {}), "token": EP_KEY}
    ck = _cache_key(path, merged_params)
    cached = _cache_get(ck)
    if cached is not None:
        sys.stderr.write(f"[eulerpool] cache hit: {path}\n")
        return cached
    q   = "?" + urllib.parse.urlencode(merged_params)
    url = f"{_EP_BASE}/{path}{q}"
    try:
        req = urllib.request.Request(url, headers=_HDRS())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
            _cache_set(ck, data)
            sys.stderr.write(f"[eulerpool] fetched: {path}\n")
            return data
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"[eulerpool] HTTP {e.code} {path}: {e.reason}\n")
        return None
    except Exception as e:
        sys.stderr.write(f"[eulerpool] error {path}: {e}\n")
        return None


# ── Type helpers ───────────────────────────────────────────────────────────────

def _sf(v, d: float = 0.0) -> float:
    try:
        f = float(v)
        return d if (math.isnan(f) or math.isinf(f)) else f
    except: return d


def _si(v, d: int = 0) -> int:
    try: return int(float(v)) if v is not None else d
    except: return d


# ── Endpoint implementations ───────────────────────────────────────────────────

def cmd_profile(sym: str) -> dict:
    """Company profile: name, sector, market cap, description, CEO, employees."""
    # Try Eulerpool equity profile endpoint
    data = _ep_get(f"v1/equity/{sym.upper()}/profile")
    if not isinstance(data, dict):
        # Try alternate path
        data = _ep_get(f"v1/stock/{sym.upper()}/profile")
    if not isinstance(data, dict):
        return {"error": f"no profile for {sym}", "symbol": sym}
    return {
        "symbol":      sym.upper(),
        "name":        str(data.get("name", data.get("companyName", ""))),
        "sector":      str(data.get("sector", "")),
        "industry":    str(data.get("industry", "")),
        "exchange":    str(data.get("exchange", "")),
        "country":     str(data.get("country", "")),
        "description": str(data.get("description", data.get("summary", ""))),
        "marketCap":   _sf(data.get("marketCap", data.get("market_cap"))),
        "employees":   _si(data.get("employees", data.get("fullTimeEmployees"))),
        "website":     str(data.get("website", "")),
        "ceo":         str(data.get("ceo", data.get("CEO", ""))),
        "founded":     str(data.get("founded", data.get("ipoDate", ""))),
        "source":      "eulerpool",
    }


def cmd_fundamentals(sym: str) -> dict:
    """Latest annual fundamental metrics: revenue, EPS, P/E, ROE, FCF, debt."""
    data = _ep_get(f"v1/equity/{sym.upper()}/financials")
    if not isinstance(data, dict):
        data = _ep_get(f"v1/stock/{sym.upper()}/financials")
    if not isinstance(data, dict):
        return {"error": f"no fundamentals for {sym}", "symbol": sym}
    return {
        "symbol":              sym.upper(),
        "revenueAnnual":       _sf(data.get("revenue", data.get("totalRevenue"))),
        "grossProfitAnnual":   _sf(data.get("grossProfit")),
        "netIncomeAnnual":     _sf(data.get("netIncome")),
        "epsAnnual":           _sf(data.get("eps", data.get("epsBasic"))),
        "epsDiluted":          _sf(data.get("epsDiluted")),
        "peRatio":             _sf(data.get("peRatio", data.get("pe"))),
        "pbRatio":             _sf(data.get("pbRatio", data.get("pb"))),
        "psRatio":             _sf(data.get("psRatio", data.get("ps"))),
        "evEbitda":            _sf(data.get("evEbitda", data.get("enterpriseValueEbitda"))),
        "dividendYield":       _sf(data.get("dividendYield")),
        "payoutRatio":         _sf(data.get("payoutRatio")),
        "roe":                 _sf(data.get("roe", data.get("returnOnEquity"))),
        "roa":                 _sf(data.get("roa", data.get("returnOnAssets"))),
        "grossMargin":         _sf(data.get("grossMargin")),
        "netMargin":           _sf(data.get("netMargin", data.get("profitMargin"))),
        "operatingMargin":     _sf(data.get("operatingMargin")),
        "debtToEquity":        _sf(data.get("debtToEquity", data.get("totalDebtToEquity"))),
        "currentRatio":        _sf(data.get("currentRatio")),
        "quickRatio":          _sf(data.get("quickRatio")),
        "freeCashFlow":        _sf(data.get("freeCashFlow")),
        "capex":               _sf(data.get("capex", data.get("capitalExpenditures"))),
        "bookValuePerShare":   _sf(data.get("bookValuePerShare")),
        "sharesOutstanding":   _sf(data.get("sharesOutstanding")),
        "beta":                _sf(data.get("beta")),
        "52wHigh":             _sf(data.get("52wHigh", data.get("yearHigh"))),
        "52wLow":              _sf(data.get("52wLow",  data.get("yearLow"))),
        "source":              "eulerpool",
    }


def cmd_analysts(sym: str) -> dict:
    """Analyst consensus ratings and price targets."""
    data = _ep_get(f"v1/equity/{sym.upper()}/analyst-ratings")
    if not isinstance(data, dict):
        data = _ep_get(f"v1/stock/{sym.upper()}/recommendations")
    if not isinstance(data, dict):
        return {"error": f"no analyst data for {sym}", "symbol": sym}
    return {
        "symbol":       sym.upper(),
        "consensus":    str(data.get("consensus", data.get("recommendation", ""))),
        "targetHigh":   _sf(data.get("targetHigh",   data.get("priceTargetHigh"))),
        "targetLow":    _sf(data.get("targetLow",    data.get("priceTargetLow"))),
        "targetMean":   _sf(data.get("targetMean",   data.get("priceTargetMean"))),
        "targetMedian": _sf(data.get("targetMedian", data.get("priceTargetMedian"))),
        "analystCount": _si(data.get("analystCount", data.get("numberOfAnalysts"))),
        "strongBuy":    _si(data.get("strongBuy")),
        "buy":          _si(data.get("buy")),
        "hold":         _si(data.get("hold")),
        "sell":         _si(data.get("sell")),
        "strongSell":   _si(data.get("strongSell")),
        "source":       "eulerpool",
    }


def cmd_institutional(sym: str) -> dict:
    """Institutional holdings and top shareholders."""
    data = _ep_get(f"v1/equity/{sym.upper()}/institutional-ownership")
    if not isinstance(data, dict):
        data = _ep_get(f"v1/stock/{sym.upper()}/institutional")
    if not isinstance(data, dict):
        return {"error": f"no institutional data for {sym}", "symbol": sym}
    holders = data.get("holders", data.get("topHolders", data.get("institutions", [])))
    return {
        "symbol":                sym.upper(),
        "institutionalOwnership": _sf(data.get("institutionalOwnership", data.get("pctHeld"))),
        "institutionalCount":    _si(data.get("institutionalCount")),
        "floatHeld":             _sf(data.get("floatHeld")),
        "topHolders": [
            {
                "name":    str(h.get("name", h.get("holderName", ""))),
                "shares":  _si(h.get("shares",     h.get("sharesHeld"))),
                "pct":     _sf(h.get("percentage", h.get("pctHeld"))),
                "value":   _sf(h.get("value")),
                "change":  _sf(h.get("change",     h.get("sharesChange"))),
                "quarter": str(h.get("quarter",    h.get("reportDate", ""))),
            }
            for h in (holders[:15] if isinstance(holders, list) else [])
        ],
        "source": "eulerpool",
    }


def cmd_sentiment(sym: str) -> dict:
    """Social media and news sentiment scores."""
    data = _ep_get(f"v1/equity/{sym.upper()}/sentiment")
    if not isinstance(data, dict):
        data = _ep_get(f"v1/stock/{sym.upper()}/sentiment")
    if not isinstance(data, dict):
        return {"error": f"no sentiment for {sym}", "symbol": sym}
    return {
        "symbol":          sym.upper(),
        "sentimentScore":  _sf(data.get("sentimentScore",  data.get("score"))),
        "bullishPct":      _sf(data.get("bullish",         data.get("bullishPercentage"))),
        "bearishPct":      _sf(data.get("bearish",         data.get("bearishPercentage"))),
        "neutralPct":      _sf(data.get("neutral",         data.get("neutralPercentage"))),
        "newsCount":       _si(data.get("newsCount")),
        "socialVolume":    _si(data.get("socialVolume")),
        "socialSentiment": _sf(data.get("socialSentiment")),
        "twitterSentiment":_sf(data.get("twitterSentiment")),
        "redditSentiment": _sf(data.get("redditSentiment")),
        "source":          "eulerpool",
    }


def cmd_screener(sector: str | None = None, min_pe: float | None = None,
                 max_pe: float | None = None, limit: int = 20) -> dict:
    """Equity screener returning matching tickers with key metrics."""
    params: dict = {"limit": str(limit)}
    if sector:  params["sector"]  = sector
    if min_pe:  params["minPE"]   = str(min_pe)
    if max_pe:  params["maxPE"]   = str(max_pe)
    data = _ep_get("v1/screener/equity", params, timeout=12)
    if not isinstance(data, (list, dict)):
        return {"error": "screener unavailable", "results": []}
    rows = data if isinstance(data, list) else data.get("results", data.get("data", []))
    return {
        "count": len(rows),
        "results": [
            {
                "symbol":    str(r.get("symbol", r.get("ticker", ""))),
                "name":      str(r.get("name", "")),
                "marketCap": _sf(r.get("marketCap")),
                "peRatio":   _sf(r.get("peRatio", r.get("pe"))),
                "sector":    str(r.get("sector", "")),
                "country":   str(r.get("country", "")),
                "price":     _sf(r.get("price")),
                "change1d":  _sf(r.get("change1d", r.get("changePercent"))),
            }
            for r in (rows[:limit] if isinstance(rows, list) else [])
        ],
        "source": "eulerpool",
    }


def cmd_derivatives(sym: str) -> dict:
    """Options and futures open interest / volume from Eulerpool Derivatives."""
    data = _ep_get(f"v1/derivatives/{sym.upper()}/options")
    if not isinstance(data, dict):
        data = _ep_get(f"v1/equity/{sym.upper()}/options")
    if not isinstance(data, dict):
        return {"error": f"no derivatives for {sym}", "symbol": sym}
    return {
        "symbol":            sym.upper(),
        "totalOI":           _si(data.get("totalOpenInterest",   data.get("putCallOI"))),
        "callOI":            _si(data.get("callOpenInterest",    data.get("callOI"))),
        "putOI":             _si(data.get("putOpenInterest",     data.get("putOI"))),
        "putCallRatio":      _sf(data.get("putCallRatio")),
        "impliedMove":       _sf(data.get("impliedMove",         data.get("expectedMove"))),
        "historicalVolatility": _sf(data.get("historicalVolatility", data.get("hv30"))),
        "impliedVolatility": _sf(data.get("impliedVolatility",   data.get("iv30"))),
        "ivPercentile":      _sf(data.get("ivPercentile")),
        "ivRank":            _sf(data.get("ivRank")),
        "source":            "eulerpool",
    }


def cmd_macro(indicator_code: str) -> dict:
    """Macroeconomic indicators (GDP, CPI, unemployment, etc.)."""
    data = _ep_get(f"v1/macro/{indicator_code.upper()}", timeout=10)
    if not isinstance(data, (dict, list)):
        return {"error": f"no macro data for {indicator_code}", "code": indicator_code}
    rows = data if isinstance(data, list) else data.get("data", [])
    return {
        "code":   indicator_code.upper(),
        "count":  len(rows),
        "series": [
            {
                "date":  str(r.get("date", r.get("period", ""))),
                "value": _sf(r.get("value", r.get("actual"))),
                "prev":  _sf(r.get("previous")),
            }
            for r in (rows[-100:] if isinstance(rows, list) else [])
        ],
        "source": "eulerpool",
    }


# ── CLI dispatcher ─────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(json.dumps({"error": "usage: eulerpool_source.py <mode> [args...]"}))
        sys.exit(1)

    mode = args[0].lower()
    rest = args[1:]

    def _flag(name: str) -> str | None:
        for a in rest:
            if a.startswith(f"--{name}="):
                return a.split("=", 1)[1]
        return None

    if mode == "profile":
        sym = rest[0].upper() if rest else "AAPL"
        print(json.dumps(cmd_profile(sym)))

    elif mode == "fundamentals":
        sym = rest[0].upper() if rest else "AAPL"
        print(json.dumps(cmd_fundamentals(sym)))

    elif mode == "analysts":
        sym = rest[0].upper() if rest else "AAPL"
        print(json.dumps(cmd_analysts(sym)))

    elif mode == "institutional":
        sym = rest[0].upper() if rest else "AAPL"
        print(json.dumps(cmd_institutional(sym)))

    elif mode == "sentiment":
        sym = rest[0].upper() if rest else "AAPL"
        print(json.dumps(cmd_sentiment(sym)))

    elif mode == "derivatives":
        sym = rest[0].upper() if rest else "AAPL"
        print(json.dumps(cmd_derivatives(sym)))

    elif mode == "macro":
        code = rest[0].upper() if rest else "GDP"
        print(json.dumps(cmd_macro(code)))

    elif mode == "screener":
        sector  = _flag("sector")
        min_pe  = float(_flag("min-pe")) if _flag("min-pe") else None
        max_pe  = float(_flag("max-pe")) if _flag("max-pe") else None
        limit   = int(_flag("limit") or "20")
        print(json.dumps(cmd_screener(sector, min_pe, max_pe, limit)))

    else:
        print(json.dumps({"error": f"unknown mode: {mode}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
