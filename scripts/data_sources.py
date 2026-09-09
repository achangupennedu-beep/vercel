#!/usr/bin/env python3
"""
APEX Terminal — Unified Data-Source Library
============================================
Consolidates every external API into clean, rate-limited, retry-capable functions.

APIs covered (all free-tier):
  1.  Alpha Vantage     — 10-key rotation (5 req/min × 10 = 50/min combined)
  2.  Finnhub           — quote, options, earnings, news, metrics, candles
  3.  Massive.com       — EOD options data, 2-year history, all contracts
  4.  InsightSentry     — 10 req/min / 1000/day, advanced market intelligence
  5.  TwelveData        — OHLCV, options chain, greeks, indicators (400/day free)
  6.  Tiingo            — IEX quotes, fundamentals, options IV
  7.  OpenFIGI          — instrument identifier resolution (FIGI, ISIN, CUSIP)
  8.  RapidAPI          — Options Profitability, MyAllies Financials
  9.  CBOE Delayed      — Exchange-level delayed chain via web scrape helper
  10. yfinance          — Universal fallback (no key required)

Rate-Limiting Strategy:
  • Each source has a token-bucket throttle enforced at the module level.
  • All calls are wrapped in a retry harness (up to 2 retries with back-off).
  • Errors are logged to stderr and return empty/None — never crash the caller.

Micro-latency Strategy:
  • Uses urllib (stdlib, no extra import cost) for all HTTP.
  • JSON parsing via stdlib json.
  • numpy vectorised paths where available for batch analytics.
  • Thread-local timestamp via time.perf_counter_ns() for sub-µs timing.
"""

import sys, json, os, math, time, threading, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta, date as _date
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════════════════════
# ── API Keys ──────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

FINNHUB_KEY     = os.environ.get("FINNHUB_API_KEY",    "d8tbcp9r01qhcnk1ft60d8tbcp9r01qhcnk1ft6g")
MASSIVE_KEY     = os.environ.get("MASSIVE_API_KEY",    "Ns0BKHdMyS7tNaAQ_RREHtCpJ1x49FNi")
INSIGHTSENTRY_KEY = os.environ.get("INSIGHTSENTRY_KEY","")   # injected via env
TWELVEDATA_KEY  = os.environ.get("TWELVEDATA_API_KEY", "b437fd2948ec4ffa826dd691c7e6c2df")
TIINGO_KEY      = os.environ.get("TIINGO_API_KEY",     "641295bf53a9841702e86b0bae7a15cd5bd6adf9")
OPENFIGI_KEY    = os.environ.get("OPENFIGI_KEY",       "2052d5d0-cd5d-4863-83fc-083e56e68663")
RAPIDAPI_TOKEN  = os.environ.get("RAPIDAPI_ACCESS_TOKEN", "")
APCA_KEY        = os.environ.get("APCA_API_KEY_ID", "")
APCA_SEC        = os.environ.get("APCA_API_SECRET_KEY", "")
POLYGON_KEY     = os.environ.get("POLYGON_API_KEY",    "110xoAkVSMv7WBdDmfqPM6_f3SUT4tyU")
OPTIONDATA_KEY  = os.environ.get("OPTIONDATA_KEY",     "apikey_Y3VzX1VsQ2tRMWlicFRIdkk5fDE3ODIzMTU0MzgzODN8YjM5MWE0NWY1NWQ4OGE4MQ")

# Alpha Vantage 10-key rotation
_AV_KEYS = [k for k in [
    os.environ.get("AV_KEY_1",  "FUKEKMUEN8GIC82A"),
    os.environ.get("AV_KEY_2",  "CYBWW8VF831209WH"),
    os.environ.get("AV_KEY_3",  "H58YGLP8WN0V8OXS"),
    os.environ.get("AV_KEY_4",  "U3XMEDPQGL1POIAH"),
    os.environ.get("AV_KEY_5",  "ELEXFQA94KKGL0OI"),
    os.environ.get("AV_KEY_6",  "9FRSHRAZCWHI7IHV"),
    os.environ.get("AV_KEY_7",  "UFOY6OS1TKTPN1K5"),
    os.environ.get("AV_KEY_8",  "L5Z0LJA84D07FB60"),
    os.environ.get("AV_KEY_9",  "NYD9SXABZ0D87JR3"),
    os.environ.get("AV_KEY_10", "2L7M89R071KQVT9N"),
] if k]
_av_lock = threading.Lock()
_av_idx  = 0

def _next_av_key() -> str:
    global _av_idx
    with _av_lock:
        if not _AV_KEYS: return ""
        k = _AV_KEYS[_av_idx % len(_AV_KEYS)]; _av_idx += 1; return k

# ═══════════════════════════════════════════════════════════════════════════════
# ── Token-Bucket Rate Limiter ─────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class TokenBucket:
    """Thread-safe token bucket for per-source rate limiting."""
    def __init__(self, rate_per_min: float):
        self._rate    = rate_per_min / 60.0   # tokens/second
        self._tokens  = rate_per_min          # start full
        self._last_ts = time.monotonic()
        self._lock    = threading.Lock()

    def consume(self, block=True, timeout=4.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_ts
                self._tokens = min(self._tokens + elapsed * self._rate,
                                   self._rate * 60)
                self._last_ts = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            if not block or time.monotonic() > deadline:
                return False
            time.sleep(0.05)

# Per-source buckets (conservative — well within free limits)
_buckets: Dict[str, TokenBucket] = {
    "alphavantage":   TokenBucket(45),    # 50/min with 10 keys, leave headroom
    "finnhub":        TokenBucket(55),    # 60/min free
    "massive":        TokenBucket(4),     # 5/min
    "insightsentry":  TokenBucket(9),     # 10/min
    "twelvedata":     TokenBucket(7),     # 8 credits/min free
    "tiingo":         TokenBucket(50),    # generous
    "openfigi":       TokenBucket(20),    # 25/min
    "rapidapi":       TokenBucket(2),     # 2 req/s
    "polygon":        TokenBucket(5),
    "alpaca":         TokenBucket(200),
}

# ═══════════════════════════════════════════════════════════════════════════════
# ── Low-Level HTTP ────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def _http_get(url: str, headers: Optional[Dict] = None,
              timeout: int = 6, retries: int = 1) -> Optional[Any]:
    """
    Robust HTTP GET with retry + exponential back-off.
    Returns parsed JSON or None on failure.
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 503):
                time.sleep(2 ** attempt * 0.5)
            elif e.code in (401, 403, 404):
                break  # no point retrying auth/not-found errors
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.3 * (attempt + 1))
    sys.stderr.write(f"[http] GET {url[:80]}... failed: {last_err}\n")
    return None

def _http_post(url: str, payload: Any, headers: Optional[Dict] = None,
               timeout: int = 8) -> Optional[Any]:
    try:
        data = json.dumps(payload).encode()
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        req  = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        sys.stderr.write(f"[http] POST {url[:80]}... failed: {e}\n")
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# ── Type-safe helpers ─────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def _sf(v, d: float = 0.0) -> float:
    """Safe float conversion."""
    try:
        f = float(v)
        return d if (math.isnan(f) or math.isinf(f)) else f
    except: return d

def _si(v, d: int = 0) -> int:
    """Safe int conversion."""
    try: return int(float(v)) if v is not None else d
    except: return d

def _sb(v, d: bool = False) -> bool:
    if isinstance(v, bool): return v
    if isinstance(v, str): return v.lower() in ("true","1","yes")
    try: return bool(int(v))
    except: return d

def _ts_to_ms(ts_str: str) -> int:
    """ISO-8601 / Unix epoch → milliseconds."""
    if not ts_str: return 0
    try:
        return int(float(ts_str) * 1000) if ts_str.replace(".","").isdigit() else \
               int(datetime.fromisoformat(ts_str.replace("Z","+00:00")).timestamp() * 1000)
    except: return 0

# ═══════════════════════════════════════════════════════════════════════════════
# ══ 1. ALPHA VANTAGE ══════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

_AV_BASE = "https://www.alphavantage.co/query"

def av_realtime_options(symbol: str) -> Dict:
    """
    Alpha Vantage REALTIME_OPTIONS — full chain with IV, greeks, bid/ask, volume, OI.
    Uses round-robin key rotation. Returns {(strike,exp,side): {iv,delta,gamma,theta,vega,bid,ask,oi,vol}}
    """
    if not _buckets["alphavantage"].consume(timeout=3):
        return {}
    key = _next_av_key()
    if not key: return {}
    url  = f"{_AV_BASE}?function=REALTIME_OPTIONS&symbol={symbol}&apikey={key}"
    data = _http_get(url, timeout=10)
    if not data: return {}
    result: Dict = {}
    for row in data.get("data", []):
        K       = _sf(row.get("strike"))
        expDate = str(row.get("expiration", ""))[:10]
        cp      = "call" if str(row.get("type","")).lower().startswith("c") else "put"
        result[(round(K,2), expDate, cp)] = {
            "iv":    _sf(row.get("implied_volatility")),
            "delta": _sf(row.get("delta")),
            "gamma": _sf(row.get("gamma")),
            "theta": _sf(row.get("theta")),
            "vega":  _sf(row.get("vega")),
            "bid":   _sf(row.get("bid")),
            "ask":   _sf(row.get("ask")),
            "oi":    _si(row.get("open_interest")),
            "vol":   _si(row.get("volume")),
            "last":  _sf(row.get("last")),
        }
    return result

def av_historical_options(symbol: str, date_str: Optional[str] = None) -> Dict:
    """
    Alpha Vantage HISTORICAL_OPTIONS — supports date=YYYY-MM-DD.
    Returns same structure as av_realtime_options.
    """
    if not _buckets["alphavantage"].consume(timeout=3):
        return {}
    key = _next_av_key()
    if not key: return {}
    url = f"{_AV_BASE}?function=HISTORICAL_OPTIONS&symbol={symbol}&apikey={key}"
    if date_str: url += f"&date={date_str}"
    data = _http_get(url, timeout=12)
    if not data: return {}
    result: Dict = {}
    for row in data.get("data", []):
        K       = _sf(row.get("strike"))
        expDate = str(row.get("expiration",""))[:10]
        cp      = "call" if str(row.get("type","")).lower().startswith("c") else "put"
        result[(round(K,2), expDate, cp)] = {
            "iv":    _sf(row.get("implied_volatility")),
            "delta": _sf(row.get("delta")),
            "gamma": _sf(row.get("gamma")),
            "theta": _sf(row.get("theta")),
            "vega":  _sf(row.get("vega")),
            "bid":   _sf(row.get("bid")),
            "ask":   _sf(row.get("ask")),
            "oi":    _si(row.get("open_interest")),
            "vol":   _si(row.get("volume")),
            "last":  _sf(row.get("last")),
        }
    return result

def av_put_call_ratio(symbol: str, horizon: str = "3month") -> Dict:
    """Alpha Vantage Realtime Put-Call Ratio."""
    if not _buckets["alphavantage"].consume(timeout=3): return {}
    key = _next_av_key()
    if not key: return {}
    url  = f"{_AV_BASE}?function=REALTIME_PUT_CALL_RATIO&symbol={symbol}&horizon={horizon}&apikey={key}"
    data = _http_get(url, timeout=8)
    if not data: return {}
    return {
        "putCallRatio":   _sf(data.get("put_call_ratio")),
        "callVolume":     _si(data.get("call_volume")),
        "putVolume":      _si(data.get("put_volume")),
        "totalVolume":    _si(data.get("total_volume")),
        "callOI":         _si(data.get("call_open_interest")),
        "putOI":          _si(data.get("put_open_interest")),
        "totalOI":        _si(data.get("total_open_interest")),
        "horizon":        horizon,
    }

def av_volume_oi_ratio(symbol: str, horizon: str = "3month") -> Dict:
    """Alpha Vantage Realtime Volume-to-OI Ratio."""
    if not _buckets["alphavantage"].consume(timeout=3): return {}
    key = _next_av_key()
    if not key: return {}
    url  = f"{_AV_BASE}?function=REALTIME_VOLUME_TO_OI_RATIO&symbol={symbol}&horizon={horizon}&apikey={key}"
    data = _http_get(url, timeout=8)
    if not data: return {}
    return {
        "volumeToOIRatio": _sf(data.get("volume_to_open_interest_ratio")),
        "callVolOIRatio":  _sf(data.get("call_volume_to_open_interest_ratio")),
        "putVolOIRatio":   _sf(data.get("put_volume_to_open_interest_ratio")),
        "horizon":         horizon,
    }

def av_news_sentiment(symbol: str, limit: int = 20) -> List[Dict]:
    """Alpha Vantage NEWS_SENTIMENT — ticker-specific news with sentiment scores."""
    if not _buckets["alphavantage"].consume(timeout=3): return []
    key = _next_av_key()
    if not key: return []
    url  = (f"{_AV_BASE}?function=NEWS_SENTIMENT&tickers={symbol}"
            f"&limit={limit}&sort=LATEST&apikey={key}")
    data = _http_get(url, timeout=10)
    if not data: return []
    articles = []
    for item in data.get("feed", [])[:limit]:
        tickers = item.get("ticker_sentiment", [])
        sentiment = next((
            {"score": _sf(t.get("ticker_sentiment_score")),
             "label": t.get("ticker_sentiment_label",""),
             "relevance": _sf(t.get("relevance_score"))}
            for t in tickers if t.get("ticker","").upper() == symbol.upper()
        ), {"score": 0, "label": "neutral", "relevance": 0})
        articles.append({
            "title":       item.get("title",""),
            "url":         item.get("url",""),
            "source":      item.get("source",""),
            "published":   item.get("time_published",""),
            "summary":     item.get("summary","")[:300],
            "sentiment":   sentiment,
            "overallSentiment": {
                "label": item.get("overall_sentiment_label",""),
                "score": _sf(item.get("overall_sentiment_score")),
            },
        })
    return articles

def av_insider_transactions(symbol: str) -> List[Dict]:
    """Alpha Vantage INSIDER_TRANSACTIONS."""
    if not _buckets["alphavantage"].consume(timeout=3): return []
    key = _next_av_key()
    if not key: return []
    url  = f"{_AV_BASE}?function=INSIDER_TRANSACTIONS&symbol={symbol}&apikey={key}"
    data = _http_get(url, timeout=10)
    if not data: return []
    return [
        {
            "name":       row.get("name",""),
            "relation":   row.get("relationship",""),
            "date":       row.get("transaction_date",""),
            "type":       row.get("transaction_type",""),
            "shares":     _si(row.get("shares")),
            "value":      _sf(row.get("value")),
            "sharePrice": _sf(row.get("share_price")),
        }
        for row in data.get("data", [])[:50]
    ]

def av_earnings(symbol: str) -> List[Dict]:
    """Alpha Vantage EARNINGS — quarterly and annual history."""
    if not _buckets["alphavantage"].consume(timeout=3): return []
    key = _next_av_key()
    if not key: return []
    url  = f"{_AV_BASE}?function=EARNINGS&symbol={symbol}&apikey={key}"
    data = _http_get(url, timeout=10)
    if not data: return []
    return [
        {
            "reportedDate":  row.get("reportedDate",""),
            "fiscalEnd":     row.get("fiscalDateEnding",""),
            "epsEstimate":   _sf(row.get("estimatedEPS")),
            "epsActual":     _sf(row.get("reportedEPS")),
            "epsSurprise":   _sf(row.get("surprise")),
            "surprisePct":   _sf(row.get("surprisePercentage")),
        }
        for row in data.get("quarterlyEarnings", [])[:12]
    ]

def av_company_overview(symbol: str) -> Dict:
    """Alpha Vantage OVERVIEW — fundamentals, ratios, sector."""
    if not _buckets["alphavantage"].consume(timeout=3): return {}
    key = _next_av_key()
    if not key: return {}
    url  = f"{_AV_BASE}?function=OVERVIEW&symbol={symbol}&apikey={key}"
    data = _http_get(url, timeout=10)
    if not data or not data.get("Symbol"): return {}
    return {
        "name":          data.get("Name",""),
        "sector":        data.get("Sector",""),
        "industry":      data.get("Industry",""),
        "marketCap":     _si(data.get("MarketCapitalization")),
        "pe":            _sf(data.get("TrailingPE")),
        "forwardPE":     _sf(data.get("ForwardPE")),
        "peg":           _sf(data.get("PEGRatio")),
        "ps":            _sf(data.get("PriceToSalesRatioTTM")),
        "pb":            _sf(data.get("PriceToBookRatio")),
        "eps":           _sf(data.get("EPS")),
        "beta":          _sf(data.get("Beta")),
        "week52High":    _sf(data.get("52WeekHigh")),
        "week52Low":     _sf(data.get("52WeekLow")),
        "divYield":      _sf(data.get("DividendYield")),
        "divPerShare":   _sf(data.get("DividendPerShare")),
        "exDivDate":     data.get("ExDividendDate",""),
        "earningsDate":  data.get("NextEarningsDate",""),
        "sharesOutstanding": _si(data.get("SharesOutstanding")),
        "float":         _si(data.get("SharesFloat")),
        "shortRatio":    _sf(data.get("ShortRatio")),
        "analystTarget": _sf(data.get("AnalystTargetPrice")),
        "description":   data.get("Description","")[:400],
    }

def av_global_quote(symbol: str) -> Optional[Dict]:
    """Alpha Vantage GLOBAL_QUOTE — real-time price."""
    if not _buckets["alphavantage"].consume(timeout=3): return None
    key = _next_av_key()
    if not key: return None
    url  = f"{_AV_BASE}?function=GLOBAL_QUOTE&symbol={symbol}&apikey={key}"
    data = _http_get(url, timeout=8)
    if not data: return None
    q = data.get("Global Quote", {})
    price = _sf(q.get("05. price"))
    if price == 0: return None
    return {
        "symbol":    symbol,
        "price":     price,
        "open":      _sf(q.get("02. open")),
        "high":      _sf(q.get("03. high")),
        "low":       _sf(q.get("04. low")),
        "volume":    _si(q.get("06. volume")),
        "prevClose": _sf(q.get("08. previous close")),
        "change":    _sf(q.get("09. change")),
        "changePct": _sf(str(q.get("10. change percent","0")).replace("%","")),
        "source":    "alpha_vantage",
    }

def av_time_series_intraday(symbol: str, interval: str = "5min",
                             month: Optional[str] = None) -> List[Dict]:
    """Alpha Vantage TIME_SERIES_INTRADAY — up to 30 days of 1/5/15/30/60min bars."""
    if not _buckets["alphavantage"].consume(timeout=3): return []
    key = _next_av_key()
    if not key: return []
    url = (f"{_AV_BASE}?function=TIME_SERIES_INTRADAY&symbol={symbol}"
           f"&interval={interval}&outputsize=full&apikey={key}")
    if month: url += f"&month={month}"
    data = _http_get(url, timeout=15)
    if not data: return []
    key_ts = f"Time Series ({interval})"
    ts = data.get(key_ts, {})
    bars = []
    for dt_str, bar in ts.items():
        try:
            ts_ms = int(datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                        .replace(tzinfo=timezone.utc).timestamp() * 1000)
            bars.append({"t": ts_ms, "o": _sf(bar.get("1. open")),
                         "h": _sf(bar.get("2. high")), "l": _sf(bar.get("3. low")),
                         "c": _sf(bar.get("4. close")), "v": _si(bar.get("5. volume"))})
        except: pass
    return sorted(bars, key=lambda x: x["t"])

def av_sma(symbol: str, interval: str = "daily", period: int = 20) -> List[Dict]:
    """Alpha Vantage SMA technical indicator."""
    if not _buckets["alphavantage"].consume(timeout=3): return []
    key = _next_av_key()
    if not key: return []
    url = (f"{_AV_BASE}?function=SMA&symbol={symbol}&interval={interval}"
           f"&time_period={period}&series_type=close&apikey={key}")
    data = _http_get(url, timeout=10)
    if not data: return []
    raw = data.get("Technical Analysis: SMA", {})
    out = []
    for dt, vals in sorted(raw.items())[-60:]:
        out.append({"date": dt, "sma": _sf(vals.get("SMA"))})
    return out

def av_rsi(symbol: str, interval: str = "daily", period: int = 14) -> List[Dict]:
    """Alpha Vantage RSI technical indicator."""
    if not _buckets["alphavantage"].consume(timeout=3): return []
    key = _next_av_key()
    if not key: return []
    url = (f"{_AV_BASE}?function=RSI&symbol={symbol}&interval={interval}"
           f"&time_period={period}&series_type=close&apikey={key}")
    data = _http_get(url, timeout=10)
    if not data: return []
    raw = data.get("Technical Analysis: RSI", {})
    out = []
    for dt, vals in sorted(raw.items())[-60:]:
        out.append({"date": dt, "rsi": _sf(vals.get("RSI"))})
    return out

def av_macd(symbol: str, interval: str = "daily") -> List[Dict]:
    """Alpha Vantage MACD."""
    if not _buckets["alphavantage"].consume(timeout=3): return []
    key = _next_av_key()
    if not key: return []
    url = (f"{_AV_BASE}?function=MACD&symbol={symbol}&interval={interval}"
           f"&series_type=close&apikey={key}")
    data = _http_get(url, timeout=10)
    if not data: return []
    raw = data.get("Technical Analysis: MACD", {})
    out = []
    for dt, vals in sorted(raw.items())[-60:]:
        out.append({"date": dt,
                    "macd":   _sf(vals.get("MACD")),
                    "signal": _sf(vals.get("MACD_Signal")),
                    "hist":   _sf(vals.get("MACD_Hist"))})
    return out

def av_bbands(symbol: str, interval: str = "daily", period: int = 20) -> List[Dict]:
    """Alpha Vantage BBANDS — Bollinger Bands."""
    if not _buckets["alphavantage"].consume(timeout=3): return []
    key = _next_av_key()
    if not key: return []
    url = (f"{_AV_BASE}?function=BBANDS&symbol={symbol}&interval={interval}"
           f"&time_period={period}&series_type=close&apikey={key}")
    data = _http_get(url, timeout=10)
    if not data: return []
    raw = data.get("Technical Analysis: BBANDS", {})
    out = []
    for dt, vals in sorted(raw.items())[-60:]:
        out.append({"date": dt,
                    "upper": _sf(vals.get("Real Upper Band")),
                    "middle": _sf(vals.get("Real Middle Band")),
                    "lower": _sf(vals.get("Real Lower Band"))})
    return out

def av_analytics_sliding(symbol: str, lookback: int = 60,
                          calculations: Optional[List[str]] = None) -> Dict:
    """Alpha Vantage ANALYTICS_SLIDING_WINDOW — variance, correlation, beta."""
    if not _buckets["alphavantage"].consume(timeout=3): return {}
    key = _next_av_key()
    if not key: return {}
    calcs = ",".join(calculations or ["MEAN","STDDEV","CORRELATION","COVARIANCE","AUTOCORRELATION"])
    url = (f"{_AV_BASE}?function=ANALYTICS_SLIDING_WINDOW&SYMBOLS={symbol},SPY"
           f"&RANGE=6month&INTERVAL=DAILY&WINDOW_SIZE={lookback}&CALCULATIONS={calcs}&apikey={key}")
    data = _http_get(url, timeout=12)
    return data or {}

def av_top_gainers_losers() -> Dict:
    """Alpha Vantage TOP_GAINERS_LOSERS — today's movers."""
    if not _buckets["alphavantage"].consume(timeout=3): return {}
    key = _next_av_key()
    if not key: return {}
    url  = f"{_AV_BASE}?function=TOP_GAINERS_LOSERS&apikey={key}"
    data = _http_get(url, timeout=10)
    return data or {}


# ═══════════════════════════════════════════════════════════════════════════════
# ══ 2. FINNHUB ════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

_FH_BASE = "https://finnhub.io/api/v1"
_FH_HDR  = {"X-Finnhub-Token": FINNHUB_KEY}

def _fh(path: str, params: str = "", timeout: int = 8) -> Optional[Any]:
    if not _buckets["finnhub"].consume(timeout=3): return None
    url = f"{_FH_BASE}/{path}?token={FINNHUB_KEY}{('&'+params) if params else ''}"
    return _http_get(url, timeout=timeout)

def finnhub_quote(symbol: str) -> Optional[Dict]:
    """Real-time Finnhub quote (c=price, o=open, h=high, l=low, pc=prevClose)."""
    d = _fh(f"quote", f"symbol={symbol}")
    if not d: return None
    c = _sf(d.get("c"))
    if c == 0: return None
    pc = _sf(d.get("pc", c))
    ch = round(c - pc, 4) if pc else 0
    chp = round(ch / pc * 100, 4) if pc else 0
    return {
        "symbol": symbol, "price": c, "open": _sf(d.get("o")),
        "high": _sf(d.get("h")), "low": _sf(d.get("l")),
        "prevClose": pc, "change": ch, "changePct": chp,
        "source": "finnhub",
    }

def finnhub_candles(symbol: str, resolution: str = "D",
                    from_ts: Optional[int] = None, to_ts: Optional[int] = None) -> List[Dict]:
    """Finnhub OHLCV candles — resolution: 1,5,15,30,60,D,W,M."""
    if not to_ts: to_ts = int(time.time())
    if not from_ts: from_ts = to_ts - 90 * 86400
    d = _fh("stock/candle", f"symbol={symbol}&resolution={resolution}&from={from_ts}&to={to_ts}")
    if not d or d.get("s") != "ok": return []
    ts_arr = d.get("t", [])
    return [
        {"t": ts_arr[i]*1000, "o": _sf(d["o"][i]), "h": _sf(d["h"][i]),
         "l": _sf(d["l"][i]), "c": _sf(d["c"][i]), "v": _si(d["v"][i])}
        for i in range(len(ts_arr))
    ]

def finnhub_option_chain(symbol: str, expiration: Optional[str] = None) -> Dict:
    """
    Finnhub option chain — standardised US options with IV, greeks, OI, volume.
    Returns {(strike,exp,'call'/'put'): {iv,delta,gamma,theta,vega,bid,ask,oi,vol,last}}
    """
    params = f"symbol={symbol}"
    if expiration: params += f"&expiration={expiration}"
    d = _fh("stock/option-chain", params, timeout=15)
    if not d: return {}
    result: Dict = {}
    for opt_date in d.get("data", []):
        exp = str(opt_date.get("expirationDate",""))[:10]
        for side in ("call","put"):
            for contract in opt_date.get(f"{side}s",[]) or []:
                K = _sf(contract.get("strike"))
                result[(round(K,2), exp, side)] = {
                    "iv":    _sf(contract.get("impliedVolatility")),
                    "delta": _sf(contract.get("delta")),
                    "gamma": _sf(contract.get("gamma")),
                    "theta": _sf(contract.get("theta")),
                    "vega":  _sf(contract.get("vega")),
                    "bid":   _sf(contract.get("bid")),
                    "ask":   _sf(contract.get("ask")),
                    "last":  _sf(contract.get("lastPrice")),
                    "oi":    _si(contract.get("openInterest")),
                    "vol":   _si(contract.get("volume")),
                    "inTheMoney": _sb(contract.get("inTheMoney")),
                }
    return result

def finnhub_company_news(symbol: str, from_date: str = "", to_date: str = "",
                          limit: int = 15) -> List[Dict]:
    """Finnhub company-specific news."""
    if not from_date:
        from_date = (_date.today() - timedelta(days=7)).isoformat()
    if not to_date:
        to_date = _date.today().isoformat()
    d = _fh("company-news", f"symbol={symbol}&from={from_date}&to={to_date}")
    if not d or not isinstance(d, list): return []
    return [
        {"headline":  item.get("headline",""), "source": item.get("source",""),
         "url":       item.get("url",""), "datetime": item.get("datetime",0),
         "summary":   item.get("summary","")[:300],
         "sentiment": item.get("sentiment",""), "category": item.get("category","")}
        for item in d[:limit]
    ]

def finnhub_company_metrics(symbol: str) -> Dict:
    """Finnhub company metrics — 52-week stats, PE, beta, EPS, etc."""
    d = _fh("stock/metric", f"symbol={symbol}&metric=all")
    if not d: return {}
    m = d.get("metric", {})
    return {
        "beta":        _sf(m.get("beta")),
        "week52High":  _sf(m.get("52WeekHigh")),
        "week52Low":   _sf(m.get("52WeekLow")),
        "pe":          _sf(m.get("peTTM")),
        "pb":          _sf(m.get("pbQuarterly")),
        "eps":         _sf(m.get("epsTTM")),
        "divYield":    _sf(m.get("dividendYieldIndicatedAnnual")),
        "roe":         _sf(m.get("roeTTM")),
        "roa":         _sf(m.get("roaTTM")),
        "currentRatio": _sf(m.get("currentRatioQuarterly")),
        "debtEquity":   _sf(m.get("longTermDebt/equityQuarterly")),
        "revenueGrowth": _sf(m.get("revenueGrowthTTMYoy")),
        "grossMargin":  _sf(m.get("grossMarginTTM")),
        "marketCap":    _sf(m.get("marketCapitalization")),
    }

def finnhub_earnings_calendar(symbol: str) -> List[Dict]:
    """Finnhub earnings calendar — next/recent earnings dates + estimates."""
    from_date = (_date.today() - timedelta(days=30)).isoformat()
    to_date   = (_date.today() + timedelta(days=90)).isoformat()
    d = _fh("calendar/earnings", f"symbol={symbol}&from={from_date}&to={to_date}")
    if not d: return []
    return [
        {"date":        item.get("date",""),
         "epsEstimate": _sf(item.get("epsEstimate")),
         "epsActual":   _sf(item.get("epsActual")),
         "revenueEstimate": _sf(item.get("revenueEstimate")),
         "revenueActual":   _sf(item.get("revenueActual")),
         "hour":        item.get("hour",""),
         "symbol":      item.get("symbol","")}
        for item in (d.get("earningsCalendar") or d if isinstance(d, list) else [])
    ]

def finnhub_social_sentiment(symbol: str) -> Dict:
    """Finnhub social media sentiment (Reddit, Twitter)."""
    from_date = (_date.today() - timedelta(days=7)).isoformat()
    to_date   = _date.today().isoformat()
    d = _fh("stock/social-sentiment", f"symbol={symbol}&from={from_date}&to={to_date}")
    if not d: return {}
    reddit  = d.get("reddit", [])
    twitter = d.get("twitter", [])
    def avg(lst, field):
        vals = [_sf(x.get(field)) for x in lst if x.get(field)]
        return round(sum(vals)/len(vals), 4) if vals else 0
    return {
        "reddit":  {"mention": sum(_si(x.get("mention")) for x in reddit),
                    "score": avg(reddit, "score"), "positiveMention": sum(_si(x.get("positiveMention")) for x in reddit)},
        "twitter": {"mention": sum(_si(x.get("mention")) for x in twitter),
                    "score": avg(twitter, "score"), "positiveMention": sum(_si(x.get("positiveMention")) for x in twitter)},
    }

def finnhub_recommendation_trends(symbol: str) -> List[Dict]:
    """Analyst buy/sell/hold recommendations."""
    d = _fh("stock/recommendation", f"symbol={symbol}")
    if not d or not isinstance(d, list): return []
    return [
        {"period": item.get("period",""), "strongBuy": _si(item.get("strongBuy")),
         "buy": _si(item.get("buy")), "hold": _si(item.get("hold")),
         "sell": _si(item.get("sell")), "strongSell": _si(item.get("strongSell"))}
        for item in d[:6]
    ]

def finnhub_institutional_ownership(symbol: str) -> List[Dict]:
    """Institutional ownership from 13F filings."""
    d = _fh("institutional/ownership", f"symbol={symbol}&limit=20")
    if not d: return []
    data = d.get("ownership", []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
    return [
        {"institution": item.get("name",""), "shares": _si(item.get("share")),
         "pctChange": _sf(item.get("change")), "date": item.get("reportDate","")}
        for item in data[:20]
    ]

def finnhub_pattern_recognition(symbol: str, resolution: str = "D") -> List[Dict]:
    """Finnhub candlestick pattern recognition."""
    d = _fh("scan/pattern", f"symbol={symbol}&resolution={resolution}")
    if not d: return []
    return d.get("points", [])[:20]

def finnhub_technical_indicators(symbol: str, resolution: str = "D") -> Dict:
    """Finnhub aggregate technical analysis signal."""
    d = _fh("scan/technical-indicator", f"symbol={symbol}&resolution={resolution}")
    if not d: return {}
    return {
        "technicalAnalysis": d.get("technicalAnalysis",{}),
        "trend": d.get("trend",{}),
        "oscillators": {k: v for k, v in (d.get("technicalAnalysis") or {}).items()
                        if isinstance(v, dict)},
    }

def finnhub_ipo_calendar() -> List[Dict]:
    """Upcoming IPO calendar."""
    from_date = _date.today().isoformat()
    to_date   = (_date.today() + timedelta(days=60)).isoformat()
    d = _fh("calendar/ipo", f"from={from_date}&to={to_date}")
    if not d: return []
    return (d.get("ipoCalendar") or [])[:15]


# ═══════════════════════════════════════════════════════════════════════════════
# ══ 3. MASSIVE.COM ════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

_MASSIVE_BASE = "https://api.massive.com/v1"
_MASSIVE_HDR  = {"x-api-key": MASSIVE_KEY, "Accept": "application/json"}

def _massive(path: str, timeout: int = 6) -> Optional[Any]:
    if not _buckets["massive"].consume(timeout=2): return None
    url = f"{_MASSIVE_BASE}/{path}"
    return _http_get(url, headers=_MASSIVE_HDR, timeout=timeout, retries=0)

def massive_option_chain(symbol: str, expiration: Optional[str] = None) -> Dict:
    """
    Massive.com end-of-day option chain — highly reliable, 2-year history.
    Returns {(strike,exp,'call'/'put'): {iv,delta,gamma,theta,vega,bid,ask,oi,vol,last}}
    """
    path = f"options/chain?symbol={symbol}"
    if expiration: path += f"&expiration={expiration}"
    d = _massive(path)
    if not d: return {}
    result: Dict = {}
    contracts = d if isinstance(d, list) else d.get("contracts", d.get("options", []))
    for c in contracts:
        K       = _sf(c.get("strike") or c.get("strikePrice"))
        exp_raw = (c.get("expiration") or c.get("expirationDate",""))[:10]
        cp_raw  = str(c.get("type") or c.get("optionType","")).lower()
        cp      = "call" if cp_raw.startswith("c") else "put"
        result[(round(K,2), exp_raw, cp)] = {
            "iv":    _sf(c.get("impliedVolatility") or c.get("iv")),
            "delta": _sf(c.get("delta")),
            "gamma": _sf(c.get("gamma")),
            "theta": _sf(c.get("theta")),
            "vega":  _sf(c.get("vega")),
            "bid":   _sf(c.get("bid")),
            "ask":   _sf(c.get("ask")),
            "last":  _sf(c.get("lastPrice") or c.get("last")),
            "oi":    _si(c.get("openInterest") or c.get("oi")),
            "vol":   _si(c.get("volume") or c.get("vol")),
        }
    return result

def massive_all_contracts(symbol: str) -> List[Dict]:
    """Massive.com /options/contracts/all-contracts — complete listing."""
    d = _massive(f"options/contracts/all-contracts?symbol={symbol}", timeout=6)
    if not d: return []
    return (d if isinstance(d, list) else d.get("contracts", []))[:500]

def massive_historical_options(symbol: str, date_str: str) -> Dict:
    """Massive.com historical options for a specific date (YYYY-MM-DD)."""
    path = f"options/chain?symbol={symbol}&date={date_str}"
    d = _massive(path, timeout=15)
    if not d: return {}
    result: Dict = {}
    contracts = d if isinstance(d, list) else d.get("contracts", d.get("options", []))
    for c in contracts:
        K       = _sf(c.get("strike") or c.get("strikePrice"))
        exp_raw = (c.get("expiration") or c.get("expirationDate",""))[:10]
        cp      = "call" if str(c.get("type","")).lower().startswith("c") else "put"
        result[(round(K,2), exp_raw, cp)] = {
            "iv":    _sf(c.get("impliedVolatility") or c.get("iv")),
            "bid":   _sf(c.get("bid")),
            "ask":   _sf(c.get("ask")),
            "oi":    _si(c.get("openInterest") or c.get("oi")),
            "vol":   _si(c.get("volume") or c.get("vol")),
        }
    return result

def massive_stock_quote(symbol: str) -> Optional[Dict]:
    """Massive.com delayed stock quote."""
    d = _massive(f"stocks/quote?symbol={symbol}")
    if not d: return None
    price = _sf(d.get("price") or d.get("close") or d.get("last"))
    if not price: return None
    return {
        "symbol": symbol, "price": price,
        "open": _sf(d.get("open")), "high": _sf(d.get("high")),
        "low":  _sf(d.get("low")),  "close": price,
        "volume": _si(d.get("volume")),
        "source": "massive",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ══ 4. INSIGHTSENTRY ══════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

_IS_BASE  = "https://api.insightsentry.com/v1"
_IS_HDRS  = {"Authorization": f"Bearer {INSIGHTSENTRY_KEY}", "Accept": "application/json"}

def _is_get(path: str, params: str = "", timeout: int = 10) -> Optional[Any]:
    if not INSIGHTSENTRY_KEY: return None
    if not _buckets["insightsentry"].consume(timeout=4): return None
    url = f"{_IS_BASE}/{path}{'?'+params if params else ''}"
    return _http_get(url, headers=_IS_HDRS, timeout=timeout)

def insightsentry_quote(symbol: str) -> Optional[Dict]:
    """InsightSentry real-time stock quote."""
    d = _is_get("quote", f"symbol={symbol}")
    if not d: return None
    price = _sf(d.get("price") or d.get("close") or d.get("last"))
    if not price: return None
    pc = _sf(d.get("previousClose", price))
    return {
        "symbol": symbol, "price": price,
        "open": _sf(d.get("open")), "high": _sf(d.get("high")),
        "low":  _sf(d.get("low")),  "volume": _si(d.get("volume")),
        "prevClose": pc,
        "change": round(price - pc, 4) if pc else 0,
        "changePct": round((price-pc)/pc*100, 4) if pc else 0,
        "bid": _sf(d.get("bid",0)), "ask": _sf(d.get("ask",0)),
        "source": "insightsentry",
    }

def insightsentry_option_chain(symbol: str, expiration: Optional[str] = None) -> Dict:
    """InsightSentry option chain with greeks and flow data."""
    params = f"symbol={symbol}"
    if expiration: params += f"&expiration={expiration}"
    d = _is_get("options/chain", params, timeout=15)
    if not d: return {}
    result: Dict = {}
    for row in (d.get("options") or d.get("data") or (d if isinstance(d,list) else [])):
        K   = _sf(row.get("strike"))
        exp = str(row.get("expiration",""))[:10]
        cp  = "call" if str(row.get("type","")).lower().startswith("c") else "put"
        result[(round(K,2), exp, cp)] = {
            "iv":    _sf(row.get("impliedVolatility") or row.get("iv")),
            "delta": _sf(row.get("delta")),
            "gamma": _sf(row.get("gamma")),
            "theta": _sf(row.get("theta")),
            "vega":  _sf(row.get("vega")),
            "bid":   _sf(row.get("bid")),
            "ask":   _sf(row.get("ask")),
            "oi":    _si(row.get("openInterest") or row.get("oi")),
            "vol":   _si(row.get("volume") or row.get("vol")),
            "last":  _sf(row.get("lastPrice") or row.get("last")),
        }
    return result

def insightsentry_flow(symbol: str, limit: int = 50) -> List[Dict]:
    """InsightSentry unusual options flow / dark pool prints."""
    d = _is_get("options/flow", f"symbol={symbol}&limit={limit}", timeout=12)
    if not d: return []
    return (d.get("flow") or d.get("data") or (d if isinstance(d,list) else []))[:limit]

def insightsentry_dark_pool(symbol: str, limit: int = 30) -> List[Dict]:
    """InsightSentry dark pool prints."""
    d = _is_get("darkpool/prints", f"symbol={symbol}&limit={limit}", timeout=12)
    if not d: return []
    return (d.get("prints") or d.get("data") or (d if isinstance(d,list) else []))[:limit]

def insightsentry_news(symbol: str, limit: int = 20) -> List[Dict]:
    """InsightSentry news feed with AI sentiment."""
    d = _is_get("news", f"symbol={symbol}&limit={limit}", timeout=10)
    if not d: return []
    return (d.get("news") or d.get("data") or (d if isinstance(d,list) else []))[:limit]


# ═══════════════════════════════════════════════════════════════════════════════
# ══ 5. TWELVEDATA ═════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

_TD_BASE = "https://api.twelvedata.com"

def _td(path: str, params: str = "", timeout: int = 10) -> Optional[Any]:
    if not TWELVEDATA_KEY: return None
    if not _buckets["twelvedata"].consume(timeout=3): return None
    url = f"{_TD_BASE}/{path}?apikey={TWELVEDATA_KEY}{('&'+params) if params else ''}"
    return _http_get(url, timeout=timeout)

def twelvedata_quote(symbol: str) -> Optional[Dict]:
    """TwelveData real-time quote."""
    d = _td("quote", f"symbol={symbol}")
    if not d: return None
    price = _sf(d.get("close"))
    if not price: return None
    pc = _sf(d.get("previous_close", price))
    ch = round(price-pc, 4) if pc else 0
    chp = round(ch/pc*100, 4) if pc else 0
    return {
        "symbol": symbol, "price": price,
        "open": _sf(d.get("open")), "high": _sf(d.get("high")),
        "low":  _sf(d.get("low")),  "volume": _si(d.get("volume")),
        "prevClose": pc, "change": ch, "changePct": chp,
        "bid": 0, "ask": 0, "source": "twelvedata",
    }

def twelvedata_option_chain(symbol: str) -> Dict:
    """TwelveData option chain — IV and greeks."""
    d = _td("options/chain", f"symbol={symbol}", timeout=15)
    if not d: return {}
    result: Dict = {}
    for side, ct in [(d.get("calls",[]),"call"), (d.get("puts",[]),"put")]:
        for row in (side or []):
            K   = _sf(row.get("strike_price"))
            exp = str(row.get("expiration_date",""))[:10]
            result[(round(K,2), exp, ct)] = {
                "iv":    _sf(row.get("implied_volatility")),
                "delta": _sf(row.get("delta")),
                "gamma": _sf(row.get("gamma")),
                "theta": _sf(row.get("theta")),
                "vega":  _sf(row.get("vega")),
                "bid":   _sf(row.get("bid")),
                "ask":   _sf(row.get("ask")),
                "oi":    _si(row.get("open_interest")),
                "vol":   _si(row.get("volume")),
                "last":  _sf(row.get("last_price")),
            }
    return result

def twelvedata_time_series(symbol: str, interval: str = "1day",
                            outputsize: int = 252) -> List[Dict]:
    """TwelveData OHLCV time series."""
    d = _td("time_series", f"symbol={symbol}&interval={interval}&outputsize={outputsize}", timeout=12)
    if not d or "values" not in d: return []
    bars = []
    for bar in reversed(d["values"]):
        try:
            dt_str = bar.get("datetime","")
            if len(dt_str) == 10:  # date only
                ts_ms = int(datetime.strptime(dt_str,"%Y-%m-%d").replace(
                    tzinfo=timezone.utc).timestamp() * 1000)
            else:
                ts_ms = int(datetime.fromisoformat(dt_str.replace("Z","+00:00")).timestamp()*1000)
            bars.append({"t": ts_ms, "o": _sf(bar.get("open")), "h": _sf(bar.get("high")),
                         "l": _sf(bar.get("low")), "c": _sf(bar.get("close")),
                         "v": _si(bar.get("volume"))})
        except: pass
    return bars

def twelvedata_earnings(symbol: str) -> List[Dict]:
    """TwelveData earnings calendar."""
    d = _td("earnings", f"symbol={symbol}&outputsize=8")
    if not d or "earnings" not in d: return []
    return [
        {"date": e.get("date",""), "epsEstimate": _sf(e.get("eps_estimate")),
         "epsActual": _sf(e.get("eps_actual")), "epsSurprise": _sf(e.get("surprise")),
         "surprisePct": _sf(e.get("surprise_percentage"))}
        for e in d["earnings"]
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# ══ 6. TIINGO ═════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

_TIINGO_HDR = {"Authorization": f"Token {TIINGO_KEY}", "Content-Type": "application/json"}

def _tiingo(path: str, timeout: int = 10) -> Optional[Any]:
    if not _buckets["tiingo"].consume(timeout=2): return None
    url = f"https://api.tiingo.com/{path}"
    return _http_get(url, headers=_TIINGO_HDR, timeout=timeout)

def tiingo_quote(symbol: str) -> Optional[Dict]:
    """Tiingo IEX real-time quote."""
    d = _tiingo(f"iex?tickers={symbol}", timeout=8)
    if not d or not isinstance(d, list) or not d: return None
    q = d[0]
    price = _sf(q.get("last") or q.get("tngoLast"))
    if not price: return None
    pc = _sf(q.get("prevClose", price))
    ch = round(price - pc, 4) if pc else 0
    chp = round(ch / pc * 100, 4) if pc else 0
    return {
        "symbol": symbol, "price": price,
        "open": _sf(q.get("open")), "high": _sf(q.get("high")), "low": _sf(q.get("low")),
        "volume": _si(q.get("volume")), "prevClose": pc, "change": ch, "changePct": chp,
        "bid": _sf(q.get("bidPrice",0)), "ask": _sf(q.get("askPrice",0)),
        "source": "tiingo",
    }

def tiingo_option_chain(symbol: str) -> Dict:
    """Tiingo options chain — IV cross-fill source."""
    d = _tiingo(f"tiingo/options/{symbol}/chains")
    if not d or not isinstance(d, list): return {}
    result: Dict = {}
    for chain in d:
        exp = str(chain.get("date",""))[:10]
        for row in chain.get("options",[]):
            K  = _sf(row.get("strike"))
            cp = row.get("type","").lower()
            result[(round(K,2), exp, cp)] = {
                "iv": _sf(row.get("impliedVol")),
                "delta": _sf(row.get("delta")), "gamma": _sf(row.get("gamma")),
                "theta": _sf(row.get("theta")), "vega": _sf(row.get("vega")),
            }
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# ══ 7. OPENFIGI ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

_FIGI_HDR = {"X-OPENFIGI-APIKEY": OPENFIGI_KEY, "Content-Type": "application/json"}

def openfigi_lookup(ticker: str, exchange_code: str = "US") -> Optional[Dict]:
    """
    OpenFIGI identifier resolution.
    Returns {figi, name, ticker, exchCode, marketSector, securityType, isin, cusip}
    """
    if not _buckets["openfigi"].consume(timeout=3): return None
    payload = [{"idType": "TICKER", "idValue": ticker, "exchCode": exchange_code}]
    result = _http_post("https://api.openfigi.com/v3/mapping",
                        payload, headers=_FIGI_HDR, timeout=8)
    if not result or not isinstance(result, list): return None
    data = result[0].get("data", [])
    if not data: return None
    item = data[0]
    return {
        "figi":         item.get("figi",""),
        "name":         item.get("name",""),
        "ticker":       item.get("ticker",""),
        "exchCode":     item.get("exchCode",""),
        "marketSector": item.get("marketSector",""),
        "securityType": item.get("securityType",""),
        "securityType2": item.get("securityType2",""),
        "shareClassFIGI": item.get("shareClassFIGI",""),
        "compositeFIGI":  item.get("compositeFIGI",""),
    }

def openfigi_search(query: str, security_type: str = "Common Stock") -> List[Dict]:
    """OpenFIGI search by company name or ticker keyword."""
    if not _buckets["openfigi"].consume(timeout=3): return []
    payload = [{"idType": "BASE_TICKER", "idValue": query, "securityType": security_type}]
    result = _http_post("https://api.openfigi.com/v3/mapping",
                        payload, headers=_FIGI_HDR, timeout=10)
    if not result or not isinstance(result, list): return []
    return (result[0].get("data") or [])[:10]


# ═══════════════════════════════════════════════════════════════════════════════
# ══ 8. RAPIDAPI ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _rapidapi(host: str, path: str, params: str = "", timeout: int = 10) -> Optional[Any]:
    if not RAPIDAPI_TOKEN: return None
    if not _buckets["rapidapi"].consume(timeout=5): return None
    url  = f"https://{host}/{path}{'?'+params if params else ''}"
    hdrs = {"x-rapidapi-host": host, "x-rapidapi-key": RAPIDAPI_TOKEN}
    return _http_get(url, headers=hdrs, timeout=timeout)

def rapidapi_options_profitability(symbol: str, option_type: str = "call",
                                    strike: Optional[float] = None,
                                    expiration: Optional[str] = None) -> Optional[Dict]:
    """
    RapidAPI Options Profitability API — profitability analysis, breakeven, Greeks summary.
    500 requests/month, 2 req/sec.
    """
    params = f"symbol={symbol}&type={option_type}"
    if strike:     params += f"&strike={strike}"
    if expiration: params += f"&expiration={expiration}"
    return _rapidapi("options-profitability-api.p.rapidapi.com",
                     "options/profitability", params)

def rapidapi_myallies_financials(symbol: str, endpoint: str = "profile") -> Optional[Dict]:
    """
    RapidAPI MyAllies Financials — free-tier financial data.
    Endpoints: profile, financials, news, dividends, splits
    """
    return _rapidapi("myallies-financials.p.rapidapi.com",
                     f"v1/{endpoint}/{symbol}")


# ═══════════════════════════════════════════════════════════════════════════════
# ══ 9. YFINANCE FALLBACK ══════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def yf_quote(symbol: str) -> Optional[Dict]:
    """yfinance quote — universal fallback, no API key."""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        info = t.fast_info
        price = _sf(getattr(info,"last_price",None) or getattr(info,"regularMarketPrice",None) or 0)
        prev  = _sf(getattr(info,"previous_close",None) or 0)
        if price == 0:
            hist = t.history(period="2d", interval="1d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
                prev  = float(hist["Close"].iloc[-2]) if len(hist)>=2 else price
        if price == 0: return None
        ch  = round(price-prev,4) if prev else 0
        chp = round(ch/prev*100,4) if prev else 0
        return {
            "symbol": symbol, "price": price,
            "open": _sf(getattr(info,"open",None) or 0),
            "high": _sf(getattr(info,"day_high",None) or 0),
            "low":  _sf(getattr(info,"day_low",None) or 0),
            "volume": _si(getattr(info,"three_month_average_volume",None) or 0),
            "prevClose": prev, "change": ch, "changePct": chp,
            "source": "yfinance",
        }
    except Exception as e:
        sys.stderr.write(f"yf_quote {symbol}: {e}\n"); return None

def yf_option_chain(symbol: str, expirations: Optional[List[str]] = None) -> Dict:
    """yfinance option chain — OI and volume with clean NaN handling."""
    try:
        import yfinance as yf, math as _m
        def _clean(v):
            if v is None: return 0
            if isinstance(v, float) and _m.isnan(v): return 0
            try: return int(v)
            except: return 0
        t = yf.Ticker(symbol)
        avail = list(t.options)
        targets = [e for e in (expirations or avail) if e in set(avail)][:8]
        result: Dict = {}
        for exp in targets:
            oc = t.option_chain(exp)
            for side, df in [("call", oc.calls), ("put", oc.puts)]:
                for _, row in df.iterrows():
                    k = round(float(row["strike"]), 2)
                    result[(k, exp, side)] = {
                        "oi":    _clean(row.get("openInterest")),
                        "vol":   _clean(row.get("volume")),
                        "iv":    _sf(row.get("impliedVolatility")),
                        "bid":   _sf(row.get("bid")),
                        "ask":   _sf(row.get("ask")),
                        "last":  _sf(row.get("lastPrice")),
                        "inTheMoney": bool(row.get("inTheMoney", False)),
                    }
        return result
    except Exception as e:
        sys.stderr.write(f"yf_option_chain {symbol}: {e}\n"); return {}


# ═══════════════════════════════════════════════════════════════════════════════
# ══ COMPOSITE ENRICHMENT PIPELINE ════════════════════════════════════════════
# ═════════════════════════════════════════════════════════════════════════�����══�����══

class OptionEnrichmentPipeline:
    """
    Combines all option data sources into a single prioritised enrichment map.
    Priority (highest to lowest): Alpaca SDK → InsightSentry → Finnhub → AV →
                                  Massive → TwelveData → Tiingo → yfinance
    """

    def __init__(self, symbol: str, expirations: Optional[List[str]] = None):
        self.symbol      = symbol
        self.expirations = expirations or []
        self._maps: List[Dict] = []
        self._yf_map: Dict = {}
        self._av_pcr: Dict = {}
        self._av_voi: Dict = {}

    def load_all(self, fast: bool = False) -> None:
        """
        Populate enrichment maps from all sources in priority order.
        `fast=True` skips slower sources for latency-critical paths.
        """
        # 1. Alpha Vantage (fast, 10-key rotation)
        try:
            m = av_realtime_options(self.symbol)
            if m: self._maps.append(m)
        except: pass

        # 2. Finnhub option chain
        try:
            m = finnhub_option_chain(self.symbol)
            if m: self._maps.append(m)
        except: pass

        # 3. InsightSentry (if key configured)
        if INSIGHTSENTRY_KEY and not fast:
            try:
                m = insightsentry_option_chain(self.symbol)
                if m: self._maps.append(m)
            except: pass

        # 4. Massive.com
        if not fast:
            try:
                m = massive_option_chain(self.symbol)
                if m: self._maps.append(m)
            except: pass

        # 5. TwelveData
        try:
            m = twelvedata_option_chain(self.symbol)
            if m: self._maps.append(m)
        except: pass

        # 6. Tiingo
        try:
            m = tiingo_option_chain(self.symbol)
            if m: self._maps.append(m)
        except: pass

        # 7. yfinance OI/Volume (most reliable free OI source)
        try:
            self._yf_map = yf_option_chain(self.symbol, self.expirations)
        except: pass

        # 8. AV Put-Call Ratio and Vol/OI
        try:
            self._av_pcr = av_put_call_ratio(self.symbol)
        except: pass
        try:
            self._av_voi = av_volume_oi_ratio(self.symbol)
        except: pass

    def enrich_row(self, row: Dict) -> Dict:
        """
        Merge enrichment data into a single contract row in priority order.
        Fills iv/greeks/bid/ask from enrichment sources; fills oi/vol from yfinance.
        """
        K   = round(row.get("strike", 0), 2)
        exp = row.get("expiration","")[:10]
        cp  = row.get("type","call")
        key = (K, exp, cp)

        # IV + greeks enrichment
        for edict in self._maps:
            hit = edict.get(key)
            if not hit: continue
            if row.get("iv",0) == 0 and hit.get("iv",0) > 0:
                row["iv"] = hit["iv"]; row["ivPct"] = round(hit["iv"]*100, 2)
            for g in ("delta","gamma","theta","vega"):
                if row.get(g,0) == 0 and hit.get(g,0) != 0:
                    row[g] = hit[g]
            if row.get("bid",0) == 0 and hit.get("bid",0) > 0:
                row["bid"] = hit["bid"]
            if row.get("ask",0) == 0 and hit.get("ask",0) > 0:
                row["ask"] = hit["ask"]

        # OI + Volume from yfinance (most reliable)
        yf_hit = self._yf_map.get(key, {})
        if row.get("openInterest",0) == 0 and yf_hit.get("oi",0) > 0:
            row["openInterest"] = yf_hit["oi"]
        if row.get("volume",0) == 0 and yf_hit.get("vol",0) > 0:
            row["volume"] = yf_hit["vol"]

        # Recompute Vol/OI ratio
        oi  = row.get("openInterest",0) or 0
        vol = row.get("volume",0) or 0
        row["volOiRatio"] = round(vol/oi, 4) if oi > 0 else 0

        return row

    @property
    def map_count(self) -> int:
        return len(self._maps)

    @property
    def yf_key_count(self) -> int:
        return len(self._yf_map)

    @property
    def av_pcr(self) -> Dict:
        return self._av_pcr

    @property
    def av_vol_oi(self) -> Dict:
        return self._av_voi


# ═══════════════════════════════════════════════════════════════════════════════
# ══ COMPOSITE QUOTE PIPELINE ═════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def get_best_quote(symbol: str) -> Optional[Dict]:
    """
    Returns the best available quote from all sources in priority order.
    Alpaca → Finnhub → InsightSentry → TwelveData → Tiingo → AV → yfinance
    """
    # Alpaca handled externally (requires SDK) — start with Finnhub
    fh = finnhub_quote(symbol)
    if fh and fh.get("price",0) > 0:
        return fh

    if INSIGHTSENTRY_KEY:
        try:
            q = insightsentry_quote(symbol)
            if q and q.get("price",0) > 0: return q
        except: pass

    td = twelvedata_quote(symbol)
    if td and td.get("price",0) > 0: return td

    ti = tiingo_quote(symbol)
    if ti and ti.get("price",0) > 0: return ti

    av = av_global_quote(symbol)
    if av and av.get("price",0) > 0: return av

    return yf_quote(symbol)


# ═══════════════════════════════════════════════════════════════════════════════
# ══ NYSE PRECISION CLOCK (±1µs) ═══════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def nyse_precision_time() -> Dict:
    """
    Sub-microsecond precision NYSE Eastern-time clock.
    Returns ISO string (ms precision) + raw epoch_ns for HFT calculations.
    """
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
        epoch_ns   = time.time_ns()
        epoch_us   = epoch_ns / 1000
        epoch_ms   = epoch_ns / 1_000_000
        # Format to microsecond precision (6 decimal places)
        iso_us     = now.strftime('%Y-%m-%dT%H:%M:%S.') + f"{now.microsecond:06d}"
        iso_ms     = now.strftime('%Y-%m-%d %H:%M:%S.') + f"{now.microsecond // 1000:03d}"
        session_open  = now.replace(hour=9,minute=30,second=0,microsecond=0)
        session_close = now.replace(hour=16,minute=0,second=0,microsecond=0)
        pre_open      = now.replace(hour=4,minute=0,second=0,microsecond=0)
        post_close    = now.replace(hour=20,minute=0,second=0,microsecond=0)
        is_weekend    = now.weekday() >= 5
        is_regular    = (not is_weekend) and (session_open <= now < session_close)
        is_pre        = (not is_weekend) and (pre_open   <= now < session_open)
        is_post       = (not is_weekend) and (session_close <= now < post_close)
        session = ("regular" if is_regular else "pre-market" if is_pre
                   else "post-market" if is_post else "closed")
        return {
            "nyse_time":      iso_ms,
            "nyse_time_us":   iso_us,
            "epoch_ns":       epoch_ns,
            "epoch_us":       epoch_us,
            "epoch_ms":       epoch_ms,
            "timezone":       "America/New_York",
            "session":        session,
            "is_regular_hours": is_regular,
            "weekday":        now.strftime("%A"),
            "perf_ns":        time.perf_counter_ns(),   # monotonic high-res counter
        }
    except Exception as e:
        sys.stderr.write(f"nyse_precision_time: {e}\n")
        return {"nyse_time": datetime.utcnow().isoformat(), "session": "unknown"}


# ═══════════════════════════════════════════════════════════════════════════════
# ── Aliases for backward-compat and concise imports ───────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

# Finnhub aliases (shorter names used by finnhub_fetch.py)
finnhub_metrics           = finnhub_company_metrics
finnhub_earnings          = finnhub_earnings_calendar
finnhub_sentiment         = finnhub_social_sentiment
finnhub_recommendation    = finnhub_recommendation_trends
finnhub_technical         = finnhub_technical_indicators
finnhub_insider           = finnhub_institutional_ownership

def finnhub_peers(symbol: str) -> List[str]:
    """Return list of peer tickers from Finnhub."""
    if not _buckets["finnhub"].consume(timeout=2): return []
    url = f"https://finnhub.io/api/v1/stock/peers?symbol={symbol}&token={FINNHUB_KEY}"
    data = _http_get(url)
    return data if isinstance(data, list) else []

def finnhub_price_target(symbol: str) -> Dict:
    """Return analyst price target consensus from Finnhub."""
    if not _buckets["finnhub"].consume(timeout=2): return {}
    url = f"https://finnhub.io/api/v1/stock/price-target?symbol={symbol}&token={FINNHUB_KEY}"
    data = _http_get(url)
    if not data: return {}
    return {
        "targetHigh":    _sf(data.get("targetHigh")),
        "targetLow":     _sf(data.get("targetLow")),
        "targetMean":    _sf(data.get("targetMean")),
        "targetMedian":  _sf(data.get("targetMedian")),
        "lastUpdated":   data.get("lastUpdated",""),
        "analystCount":  _si(data.get("analystCount")),
    }

def finnhub_support_resistance(symbol: str) -> Dict:
    """Return support/resistance levels from Finnhub pattern recognition."""
    if not _buckets["finnhub"].consume(timeout=2): return {}
    url = f"https://finnhub.io/api/v1/scan/support-resistance?symbol={symbol}&resolution=D&token={FINNHUB_KEY}"
    data = _http_get(url)
    if not data: return {}
    return {
        "levels": data.get("levels", []),
        "source": "finnhub",
    }

# Massive aliases
massive_snapshot    = massive_option_chain
massive_hist_iv     = massive_historical_options

# ═══════════════════════════════════════════════════════════════════════════════
# ══ 11. LONDON STRATEGIC EDGE (LSE) ══════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
#
# REST API:       https://londonstrategicedge.com/api
# WebSocket:      wss://londonstrategicedge.com/ws
# Rate limit:     100 calls/min, 50 GB/month
# Coverage:       stocks, FX, crypto, ETFs, indices, options — 16,000+ instruments
#
# Functions exposed:
#   lse_quote(symbol)           — single live quote
#   lse_candles(symbol, tf, **) — OHLCV candles (1m/5m/15m/1h/4h/1d)
#   lse_options_chain(symbol)   — live options chain with IV + greeks
#   lse_options_flow(symbol)    — unusual / block options prints
#   lse_insider_trades(symbol)  — insider transactions
#   lse_catalog(category)       — list available instruments

LSE_KEY  = os.environ.get("LSE_API_KEY", "lse_live_8960fdf1f1af3ab76db92734aaaca159")

_buckets["lse"] = TokenBucket(95)   # 100/min, leave 5 as headroom

def _lse_client():
    """Return a fresh lse-data SDK client (import is deferred to avoid startup cost)."""
    from lse import LSE  # type: ignore  # pip install lse-data
    return LSE(api_key=LSE_KEY)

def lse_quote(symbol: str) -> Dict:
    """Live quote via SDK: last 1d candle, descending."""
    if not LSE_KEY or not _buckets["lse"].consume(timeout=2): return {}
    try:
        rows = _lse_client().candles(symbol.upper(), "1d", limit=1, order="desc")
        if not rows: return {}
        r = rows[0]
        return {
            "price":     _sf(r.get("close")),
            "open":      _sf(r.get("open")),
            "high":      _sf(r.get("high")),
            "low":       _sf(r.get("low")),
            "volume":    _si(r.get("volume")),
            "timestamp": str(r.get("timestamp", r.get("updated_at", ""))),
            "source":    "lse",
        }
    except Exception as e:
        sys.stderr.write(f"[lse_quote] {e}\n"); return {}

def lse_candles(symbol: str, timeframe: str = "1d",
                start: Optional[str] = None, limit: int = 200) -> List[Dict]:
    """OHLCV candles via SDK. timeframe: 1m, 5m, 15m, 1h, 4h, 1d.
    Confirmed field names: timestamp, symbol, open, high, low, close, volume, updated_at
    """
    if not LSE_KEY or not _buckets["lse"].consume(timeout=2): return []
    try:
        kwargs: Dict = {"limit": limit, "order": "asc"}
        if start: kwargs["start"] = start
        rows = _lse_client().candles(symbol.upper(), timeframe, **kwargs)
        return [
            {
                "t":      str(r.get("timestamp", "")),
                "open":   _sf(r.get("open")),
                "high":   _sf(r.get("high")),
                "low":    _sf(r.get("low")),
                "close":  _sf(r.get("close")),
                "volume": _sf(r.get("volume")),
            }
            for r in (rows or [])
        ]
    except Exception as e:
        sys.stderr.write(f"[lse_candles] {e}\n"); return []

def lse_options_chain(symbol: str, option_type: Optional[str] = None,
                      max_dte: int = 90) -> Dict:
    """
    Live options chain with IV and greeks via SDK.
    Confirmed field names: ticker, underlying, strike, expiry, contract_type,
    last_price, volume_today, premium_today, underlying_price, dte,
    iv, delta, gamma, theta, vega, rho, last_trade_at, updated_at
    Returns {(strike, expiration, side): {iv, delta, gamma, theta, vega, vol, last, dte}}
    """
    if not LSE_KEY or not _buckets["lse"].consume(timeout=2): return {}
    try:
        kwargs: Dict = {"max_dte": max_dte}
        if option_type: kwargs["type"] = option_type
        rows = _lse_client().options(symbol.upper(), **kwargs)
        result: Dict = {}
        for row in (rows or []):
            K       = _sf(row.get("strike"))
            expDate = str(row.get("expiry", ""))[:10]
            cp_raw  = str(row.get("contract_type", "")).lower()
            cp      = "call" if cp_raw.startswith("c") else "put"
            if K <= 0 or not expDate: continue
            result[(round(K, 2), expDate, cp)] = {
                "iv":    _sf(row.get("iv")),
                "delta": _sf(row.get("delta")),
                "gamma": _sf(row.get("gamma")),
                "theta": _sf(row.get("theta")),
                "vega":  _sf(row.get("vega")),
                "rho":   _sf(row.get("rho")),
                "bid":   0.0,
                "ask":   0.0,
                "oi":    0,
                "vol":   _si(row.get("volume_today")),
                "last":  _sf(row.get("last_price")),
                "dte":   _si(row.get("dte")),
                "source": "lse",
            }
        return result
    except Exception as e:
        sys.stderr.write(f"[lse_options_chain] {e}\n"); return {}

def lse_options_flow(symbol: str, min_premium: int = 0) -> List[Dict]:
    """
    Unusual/block options prints via SDK.
    Confirmed field names: id, ts, underlying, ticker, strike, expiry,
    contract_type, last_price, volume, premium, underlying_price,
    dte, iv, delta, gamma, theta, vega, rho
    """
    if not LSE_KEY or not _buckets["lse"].consume(timeout=2): return []
    try:
        kwargs: Dict = {}
        if min_premium > 0: kwargs["min_premium"] = min_premium
        rows = _lse_client().options_flow(symbol.upper(), **kwargs)
        out = []
        for row in (rows or []):
            cp_raw = str(row.get("contract_type", "")).lower()
            out.append({
                "underlying":      str(row.get("underlying", symbol)),
                "ticker":          str(row.get("ticker", "")),
                "strike":          _sf(row.get("strike")),
                "expiry":          str(row.get("expiry", ""))[:10],
                "type":            "call" if cp_raw.startswith("c") else "put",
                "lastPrice":       _sf(row.get("last_price")),
                "volume":          _si(row.get("volume")),
                "premium":         _sf(row.get("premium")),
                "iv":              _sf(row.get("iv")),
                "delta":           _sf(row.get("delta")),
                "underlyingPrice": _sf(row.get("underlying_price")),
                "dte":             _si(row.get("dte")),
                "timestamp":       str(row.get("ts", "")),
                "source":          "lse",
            })
        return out
    except Exception as e:
        sys.stderr.write(f"[lse_options_flow] {e}\n"); return []

def lse_insider_trades(symbol: str, trade_type: Optional[str] = None,
                       limit: int = 50) -> List[Dict]:
    """
    Insider transactions via SDK.
    Confirmed field names: reporting_name, transaction_type, trade_type,
    acquisition_or_disposition, securities_transacted, securities_owned,
    price, security_name, form_type, filing_date, transaction_date, owner
    """
    if not LSE_KEY or not _buckets["lse"].consume(timeout=2): return []
    try:
        kwargs: Dict = {"limit": limit}
        if trade_type: kwargs["type"] = trade_type
        rows = _lse_client().insider_trades(symbol.upper(), **kwargs)
        return [
            {
                "name":        str(r.get("reporting_name", r.get("owner", ""))),
                "type":        str(r.get("transaction_type", r.get("trade_type", ""))),
                "direction":   str(r.get("acquisition_or_disposition", "")),
                "shares":      _si(r.get("securities_transacted")),
                "sharesOwned": _si(r.get("securities_owned")),
                "price":       _sf(r.get("price")),
                "security":    str(r.get("security_name", "")),
                "formType":    str(r.get("form_type", "")),
                "filingDate":  str(r.get("filing_date", "")),
                "transDate":   str(r.get("transaction_date", "")),
                "source":      "lse",
            }
            for r in (rows or [])
        ]
    except Exception as e:
        sys.stderr.write(f"[lse_insider_trades] {e}\n"); return []

def lse_catalog(category: Optional[str] = None) -> List[Dict]:
    """List available LSE instruments. Works without an API key per SDK docs."""
    try:
        from lse import LSE  # type: ignore
        client = LSE(api_key=LSE_KEY) if LSE_KEY else LSE()
        data = client.catalog(category) if category else client.catalog()
        return data if isinstance(data, list) else []
    except Exception as e:
        sys.stderr.write(f"[lse_catalog] {e}\n"); return []

# ═══════════════════════════════════════════════════════════════════════════════
# ══ 12. EULERPOOL FINANCIAL DATA ══════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
#
# Docs:       https://eulerpool.com/developers
# Auth:       ?token=KEY  or  Authorization: Bearer KEY
# Rate limit: 1,000 requests/month (use very selectively — cache results)
# Coverage:   140+ endpoint categories, 100k+ securities, 90+ exchanges
#
# Functions exposed (all low-frequency — call only for fundamental/institutional data):
#   eulerpool_profile(symbol)         — company profile, description, sector
#   eulerpool_fundamentals(symbol)    — income statement, balance sheet, cash flow
#   eulerpool_analysts(symbol)        — consensus ratings and price targets
#   eulerpool_institutional(symbol)   — institutional holdings
#   eulerpool_sentiment(symbol)       — social/news sentiment
#   eulerpool_screener(**filters)     — equity screener

EULERPOOL_KEY  = os.environ.get("EULERPOOL_API_KEY", "eu_prod_1782933237805_jp4xbr2ag5c")
_EP_BASE       = "https://api.eulerpool.com"
_EP_HDR        = lambda: {"Authorization": f"Bearer {EULERPOOL_KEY}", "Accept": "application/json"}

# Strict bucket: 1000/month ≈ ~1/hr. We allow bursts of up to 5/hr per process.
# In practice each process only calls this for supplemental data.
_buckets["eulerpool"] = TokenBucket(5)    # 5/hr token refill for budget safety

# Module-level in-process cache — Eulerpool responses are relatively static.
_ep_cache: Dict[str, Tuple[float, Any]] = {}
_EP_CACHE_TTL = 3600   # 1 hour

def _ep_get(path: str, params: Optional[Dict] = None, timeout: int = 8) -> Optional[Any]:
    if not EULERPOOL_KEY: return None
    cache_key = f"{path}:{json.dumps(params or {}, sort_keys=True)}"
    now = time.monotonic()
    if cache_key in _ep_cache:
        ts, cached = _ep_cache[cache_key]
        if now - ts < _EP_CACHE_TTL: return cached
    if not _buckets["eulerpool"].consume(timeout=2): return None
    q = ("?" + "&".join(f"{k}={v}" for k, v in {**(params or {}), "token": EULERPOOL_KEY}.items()))
    result = _http_get(f"{_EP_BASE}/{path}{q}", headers=_EP_HDR(), timeout=timeout)
    if result is not None:
        _ep_cache[cache_key] = (now, result)
    return result

def eulerpool_profile(symbol: str) -> Dict:
    """Company profile, description, sector, market cap from Eulerpool."""
    data = _ep_get(f"v1/equity/{symbol.upper()}/profile")
    if not isinstance(data, dict): return {}
    return {
        "name":         str(data.get("name", "")),
        "sector":       str(data.get("sector", "")),
        "industry":     str(data.get("industry", "")),
        "exchange":     str(data.get("exchange", "")),
        "country":      str(data.get("country", "")),
        "description":  str(data.get("description", "")),
        "marketCap":    _sf(data.get("marketCap", data.get("market_cap"))),
        "employees":    _si(data.get("employees", data.get("fullTimeEmployees"))),
        "website":      str(data.get("website", "")),
        "ceo":          str(data.get("ceo", "")),
        "source":       "eulerpool",
    }

def eulerpool_fundamentals(symbol: str) -> Dict:
    """Latest annual fundamental metrics (EPS, P/E, revenue, etc.) from Eulerpool."""
    data = _ep_get(f"v1/equity/{symbol.upper()}/financials")
    if not isinstance(data, dict): return {}
    return {
        "revenueAnnual":   _sf(data.get("revenue")),
        "netIncomeAnnual": _sf(data.get("netIncome")),
        "epsAnnual":       _sf(data.get("eps")),
        "peRatio":         _sf(data.get("peRatio", data.get("pe"))),
        "pbRatio":         _sf(data.get("pbRatio", data.get("pb"))),
        "psRatio":         _sf(data.get("psRatio", data.get("ps"))),
        "dividendYield":   _sf(data.get("dividendYield")),
        "roe":             _sf(data.get("roe")),
        "debtToEquity":    _sf(data.get("debtToEquity")),
        "freeCashFlow":    _sf(data.get("freeCashFlow")),
        "source":          "eulerpool",
    }

def eulerpool_analysts(symbol: str) -> Dict:
    """Analyst consensus ratings and price targets from Eulerpool."""
    data = _ep_get(f"v1/equity/{symbol.upper()}/analyst-ratings")
    if not isinstance(data, dict): return {}
    return {
        "consensus":      str(data.get("consensus", data.get("recommendation", ""))),
        "targetHigh":     _sf(data.get("targetHigh", data.get("priceTargetHigh"))),
        "targetLow":      _sf(data.get("targetLow",  data.get("priceTargetLow"))),
        "targetMean":     _sf(data.get("targetMean", data.get("priceTargetMean"))),
        "targetMedian":   _sf(data.get("targetMedian")),
        "analystCount":   _si(data.get("analystCount", data.get("numberOfAnalysts"))),
        "buyCount":       _si(data.get("buy", data.get("strongBuy"))),
        "holdCount":      _si(data.get("hold")),
        "sellCount":      _si(data.get("sell", data.get("strongSell"))),
        "source":         "eulerpool",
    }

def eulerpool_institutional(symbol: str) -> Dict:
    """Institutional holdings summary from Eulerpool."""
    data = _ep_get(f"v1/equity/{symbol.upper()}/institutional-ownership")
    if not isinstance(data, dict): return {}
    holders = data.get("holders", data.get("topHolders", []))
    return {
        "institutionalOwnership":   _sf(data.get("institutionalOwnership")),
        "institutionalCount":       _si(data.get("institutionalCount")),
        "topHolders": [
            {
                "name":    str(h.get("name", "")),
                "shares":  _si(h.get("shares")),
                "pct":     _sf(h.get("percentage", h.get("pct"))),
                "change":  _sf(h.get("change")),
            }
            for h in (holders[:10] if isinstance(holders, list) else [])
        ],
        "source": "eulerpool",
    }

def eulerpool_sentiment(symbol: str) -> Dict:
    """Social and news sentiment scores from Eulerpool."""
    data = _ep_get(f"v1/equity/{symbol.upper()}/sentiment")
    if not isinstance(data, dict): return {}
    return {
        "sentimentScore":    _sf(data.get("sentimentScore", data.get("score"))),
        "bullishPct":        _sf(data.get("bullish", data.get("bullishPercentage"))),
        "bearishPct":        _sf(data.get("bearish", data.get("bearishPercentage"))),
        "newsCount":         _si(data.get("newsCount")),
        "socialVolume":      _si(data.get("socialVolume")),
        "socialSentiment":   _sf(data.get("socialSentiment")),
        "source":            "eulerpool",
    }

def eulerpool_screener(min_market_cap: Optional[float] = None,
                       sector: Optional[str] = None,
                       min_pe: Optional[float] = None,
                       max_pe: Optional[float] = None,
                       limit: int = 20) -> List[Dict]:
    """Equity screener from Eulerpool. Returns list of matching tickers."""
    params: Dict = {"limit": str(limit)}
    if min_market_cap: params["minMarketCap"] = str(min_market_cap)
    if sector:         params["sector"]       = sector
    if min_pe:         params["minPE"]        = str(min_pe)
    if max_pe:         params["maxPE"]        = str(max_pe)
    data = _ep_get("v1/screener/equity", params, timeout=10)
    if not isinstance(data, (list, dict)): return []
    rows = data if isinstance(data, list) else data.get("results", data.get("data", []))
    return [
        {
            "symbol":    str(r.get("symbol", r.get("ticker", ""))),
            "name":      str(r.get("name", "")),
            "marketCap": _sf(r.get("marketCap")),
            "peRatio":   _sf(r.get("peRatio", r.get("pe"))),
            "sector":    str(r.get("sector", "")),
        }
        for r in (rows[:limit] if isinstance(rows, list) else [])
    ]


if __name__ == "__main__":
    # Quick self-test
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    t   = nyse_precision_time()
    print(f"NYSE Time: {t['nyse_time_us']} ({t['session']})", file=sys.stderr)
    q   = get_best_quote(sym)
    print(json.dumps({"time": t, "quote": q}))
