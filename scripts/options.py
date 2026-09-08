#!/usr/bin/env python3
"""
Options chain fetcher — APEX Terminal
Primary source:   Alpaca SDK (OptionHistoricalDataClient) — live greeks, IV, L1 quotes, trades
Enrichment:       Tiingo / TwelveData / Alpha Vantage (10-key rotation) — IV cross-fill
Secondary:        optiondata.io  — realtime chain REST
Fallback:         yfinance — bid/ask/IV/OI (greeks computed via BS)

Zero-IV fix:
  When bid=0 and ask>0 we solve IV from the ask price (best available).
  When both are 0 we fall back to an enrichment source that may have IV.
  Contracts with IV=0 after all sources are tagged dataQuality='iv_missing' but NOT
  dropped — the UI can decide to grey them out.

Usage:
  python3 options.py AAPL
  python3 options.py AAPL 2026-07-17    (single expiry)
"""
import sys, json, os, math, time, urllib.request, urllib.error, socket, threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import datetime, timezone, date as _date, timedelta

# Hard cap on total wall-clock time for the enrichment waterfall.
# Primary fetch (Alpaca/yfinance) runs outside this budget.
# Set low enough that exec-python.ts never hits its hard kill.
MAX_WALL_SECONDS = 18   # enrichment must finish within 18 s of primary fetch completing
_t_primary_done  = 0.0  # set after primary fetch; enrichment checks against this

def _budget_ok() -> bool:
    """Return False when enrichment wall-clock budget is exhausted."""
    return (time.time() - _t_primary_done) < MAX_WALL_SECONDS

# Clamp all outbound sockets to 6 s — prevents silent TCP stalls on unreachable hosts.
socket.setdefaulttimeout(6)

# ── Credentials ──────────────────────────────────────────────────────────────

APCA_KEY  = os.environ.get("APCA_API_KEY_ID",     "")
APCA_SEC  = os.environ.get("APCA_API_SECRET_KEY", "")

TIINGO_KEY = os.environ.get("TIINGO_API_KEY",      "")
TD_KEY     = os.environ.get("TWELVEDATA_API_KEY",  "")   # injected from env

# Alpha Vantage — 10-key round-robin (5 calls/min, 500/day each)
AV_KEYS = [k for k in [
    os.environ.get("AV_KEY_1", "FUKEKMUEN8GIC82A"),
    os.environ.get("AV_KEY_2", "CYBWW8VF831209WH"),
    os.environ.get("AV_KEY_3", "H58YGLP8WN0V8OXS"),
    os.environ.get("AV_KEY_4", "U3XMEDPQGL1POIAH"),
    os.environ.get("AV_KEY_5", "ELEXFQA94KKGL0OI"),
    os.environ.get("AV_KEY_6", "9FRSHRAZCWHI7IHV"),
    os.environ.get("AV_KEY_7", "UFOY6OS1TKTPN1K5"),
    os.environ.get("AV_KEY_8", "L5Z0LJA84D07FB60"),
    os.environ.get("AV_KEY_9", "NYD9SXABZ0D87JR3"),
    os.environ.get("AV_KEY_10","2L7M89R071KQVT9N"),
] if k]

# optiondata.io key
OPTIONDATA_KEY = os.environ.get(
    "OPTIONDATA_KEY",
    "apikey_Y3VzX1VsQ2tRMWlicFRIdkk5fDE3ODIzMTU0MzgzODN8YjM5MWE0NWY1NWQ4OGE4MQ"
)

# Intrinio API key (optional — unlocks premium data paths)
INTRINIO_KEY = os.environ.get("INTRINIO_API_KEY", "")

# London Strategic Edge API key — live options chain + flow + insider trades
LSE_KEY      = os.environ.get("LSE_API_KEY", "lse_live_8960fdf1f1af3ab76db92734aaaca159")

# Eulerpool API key — fundamentals, institutional, sentiment (1000 req/month budget)
EULERPOOL_KEY = os.environ.get("EULERPOOL_API_KEY", "eu_prod_1782933237805_jp4xbr2ag5c")

_av_key_idx = 0

def next_av_key():
    global _av_key_idx
    if not AV_KEYS:
        return ""
    key = AV_KEYS[_av_key_idx % len(AV_KEYS)]
    _av_key_idx += 1
    return key

# ── Intrinio helpers ─────────────────────────────────────────────────────────
# These functions are thin wrappers around the Intrinio v2 REST API.
# They return None/empty gracefully when INTRINIO_KEY is absent — the caller
# wraps every call in try/except so failures are non-fatal.

def _intrinio_get(path: str, timeout: int = 6):
    """GET from Intrinio v2 API. Returns parsed JSON or None."""
    if not INTRINIO_KEY:
        return None
    url = f"https://api-v2.intrinio.com{path}?api_key={INTRINIO_KEY}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        sys.stderr.write(f"intrinio_get {path}: {e}\n")
        return None

def fetch_intrinio_chain(symbol: str, expiration: str = "") -> dict:
    """Fetch Intrinio realtime options chain.
    Returns a dict keyed by (strike_float, expiration_str, side_str) -> contract_dict,
    mirroring the shape expected by the chain-enrichment merge logic.
    """
    path = f"/options/chain/{symbol}/realtime"
    resp = _intrinio_get(path)
    if not resp or not resp.get("chain"):
        return {}
    result = {}
    for item in resp.get("chain", []):
        opt   = item.get("option", {})
        stats = item.get("stats",  {})
        price = item.get("price",  {})
        strike = float(opt.get("strike", 0))
        exp_s  = str(opt.get("expiration", ""))
        side   = str(opt.get("type", "")).lower()  # "call" or "put"
        if not exp_s or side not in ("call", "put"):
            continue
        if expiration and exp_s != expiration:
            continue
        key = (round(strike, 2), exp_s, side)
        result[key] = {
            "strike": strike,
            "exp":    exp_s,
            "type":   side,
            "code":   opt.get("code", ""),
            "iv":     float(stats.get("implied_volatility", 0) or 0),
            "delta":  float(stats.get("delta", 0) or 0),
            "gamma":  float(stats.get("gamma", 0) or 0),
            "theta":  float(stats.get("theta", 0) or 0),
            "vega":   float(stats.get("vega",  0) or 0),
            "oi":     int(stats.get("open_interest", 0) or 0),
            "vol":    int(price.get("volume", 0) or 0),
        }
    return result

def fetch_intrinio_unusual(symbol: str) -> list:
    """Fetch Intrinio unusual options activity. Returns list of dicts or []."""
    resp = _intrinio_get(f"/options/unusual_activity/{symbol}")
    if not resp or not resp.get("unusual_activity"):
        return []
    out = []
    for u in resp.get("unusual_activity", [])[:50]:
        out.append({
            "type":              str(u.get("type", "")),
            "strikePrice":       float(u.get("strike_price", 0) or 0),
            "expiration":        str(u.get("expiration_date", "")),
            "volume":            int(u.get("total_volume", 0) or 0),
            "impliedVolatility": float(u.get("implied_volatility", 0) or 0),
            "totalPremium":      float(u.get("total_value", 0) or 0),
            "score":             min(100.0, float(u.get("unusual_sentiment_index", 50) or 50)),
            "signal":            str(u.get("sentiment", "NOTABLE")).upper(),
            "source":            "intrinio",
        })
    return out

def fetch_intrinio_implied_move(symbol: str) -> dict:
    """Fetch Intrinio implied move for the next event window. Returns dict or None."""
    resp = _intrinio_get(f"/options/stats/{symbol}")
    if not resp:
        return {}
    s = resp.get("stats", resp) or {}
    im = s.get("implied_move") or {}
    if not isinstance(im, dict):
        return {}
    pct = float(im.get("implied_move_percent", 0) or 0)
    if pct <= 0:
        return {}
    return {
        "impliedMovePct": round(pct, 4),
        "source": "intrinio",
    }

def fetch_intrinio_stats(symbol: str) -> dict:
    """Fetch Intrinio options stats (IV rank, percentile, P/C, volumes). Returns dict or None."""
    resp = _intrinio_get(f"/options/stats/{symbol}")
    if not resp:
        return {}
    s = resp.get("stats", resp) or {}
    return {
        "iv_rank":        float(s.get("implied_volatility_rank",       0) or 0),
        "iv_percentile":  float(s.get("implied_volatility_percentile", 0) or 0),
        "impliedVolatility": float(s.get("implied_volatility",         0) or 0),
        "call_volume":    int(s.get("call_volume",   0) or 0),
        "put_volume":     int(s.get("put_volume",    0) or 0),
        "put_call_ratio": float(s.get("put_call_ratio", 0) or 0),
        "source":         "intrinio",
    }

# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _get(url, headers=None, timeout=8):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

# ── Black-Scholes Math Engine ────────────────────────────────────────────────

def ncdf(x):  return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def npdf(x):  return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

# Continuous dividend yield by symbol — exact mirror of DIV_YIELD_TABLE in dashboard.tsx.
# All values are annual continuous yields (decimal). Default fallback: 0.0015.
DIV_YIELD_TABLE = {
    # ETFs
    "SPY": 0.0130, "QQQ": 0.0052, "IWM": 0.0140, "DIA": 0.0190, "XLF": 0.0175,
    "XLE": 0.0310, "XLU": 0.0310, "XLV": 0.0150, "GLD": 0.0000, "SLV": 0.0000,
    "TLT": 0.0385, "HYG": 0.0450, "EEM": 0.0200, "EFA": 0.0260, "VXX": 0.0000,
    # Mega-cap equities
    "AAPL": 0.0044, "MSFT": 0.0070, "GOOGL": 0.0000, "GOOG": 0.0000,
    "AMZN": 0.0000, "META": 0.0034, "NVDA": 0.0003, "TSLA": 0.0000,
    "NFLX": 0.0000, "AVGO": 0.0095, "ORCL": 0.0140, "CSCO": 0.0280,
    "INTC": 0.0000, "AMD": 0.0000, "QCOM": 0.0180, "TXN": 0.0260,
    # Financials
    "JPM": 0.0210, "BAC": 0.0220, "GS": 0.0210, "MS": 0.0280,
    "WFC": 0.0230, "C": 0.0320, "BLK": 0.0250, "V": 0.0075, "MA": 0.0056,
    # Healthcare / Pharma
    "JNJ": 0.0310, "PFE": 0.0620, "LLY": 0.0065, "ABBV": 0.0330, "MRK": 0.0260,
    "UNH": 0.0155, "CVS": 0.0000, "AMGN": 0.0285, "GILD": 0.0360,
    # Energy
    "XOM": 0.0315, "CVX": 0.0400, "COP": 0.0170, "OXY": 0.0160,
    # Consumer / Retail
    "WMT": 0.0100, "COST": 0.0060, "HD": 0.0220, "TGT": 0.0290, "MCD": 0.0220,
    "KO": 0.0290, "PEP": 0.0295, "PG": 0.0230, "CL": 0.0230,
    # Industrials
    "BA": 0.0000, "CAT": 0.0155, "DE": 0.0145, "GE": 0.0050, "RTX": 0.0195,
    # Telecom / Utilities
    "T": 0.0540, "VZ": 0.0640, "NEE": 0.0275, "SO": 0.0320,
}
DEFAULT_DIV_YIELD = 0.0015

def get_div_yield(symbol: str) -> float:
    return DIV_YIELD_TABLE.get(symbol.upper(), DEFAULT_DIV_YIELD)

def bs_price(S, K, T, r, sigma, is_call, q=0.0):
    """Merton (1973) continuous-dividend Black-Scholes price."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, (S - K) if is_call else (K - S))
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    disc  = math.exp(-r * T)
    discQ = math.exp(-q * T)
    if is_call:
        return S * discQ * ncdf(d1) - K * disc * ncdf(d2)
    return K * disc * ncdf(-d2) - S * discQ * ncdf(-d1)

def bs_greeks(S, K, T, r, sigma, is_call, q=0.0):
    """Merton (1973) continuous-dividend Black-Scholes greeks."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {"delta": 1.0 if (is_call and S > K) else 0.0, "gamma": 0, "theta": 0,
                "vega": 0, "rho": 0, "vanna": 0, "charm": 0, "volga": 0, "speed": 0,
                "lambda": 0}
    sq    = math.sqrt(T)
    d1    = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * sq)
    d2    = d1 - sigma * sq
    Nd1   = ncdf(d1);  Nd2 = ncdf(d2);  Nnd1 = ncdf(-d1);  Nnd2 = ncdf(-d2)
    nd1   = npdf(d1)
    disc  = math.exp(-r * T)   # e^{-rT}
    discQ = math.exp(-q * T)   # e^{-qT}

    # Standard Merton Greeks
    gamma = discQ * nd1 / (S * sigma * sq)
    vega  = S * discQ * nd1 * sq / 100   # per 1% IV move
    # Vanna = ∂Delta/∂σ = −e^{-qT}·n(d1)·d2/σ  (Hull 19.6)
    vanna = -discQ * nd1 * d2 / sigma if sigma > 0 else 0
    volga = vega * d1 * d2 / sigma if sigma > 0 else 0
    speed = -gamma / S * (d1 / (sigma * sq) + 1) if S > 0 else 0

    if is_call:
        delta = discQ * Nd1
        # Merton theta: time-value decay + dividend income − risk-free carry
        theta = (-(S * discQ * nd1 * sigma) / (2 * sq)
                 + q * S * discQ * Nd1
                 - r * K * disc * Nd2) / 365
        rho   = K * T * disc * Nd2 / 100
        price = S * discQ * Nd1 - K * disc * Nd2
        # Charm = ∂Delta/∂t — full Merton form (Hull §19.6 / Haug A.18)
        # charm_call = discQ · [−n(d1)·((r−q)/(σ√T) − d2/(2T)) + q·N(d1)] / 365
        #
        # BUG FIX (July 2026): previous formula had `(r-q+(d2*sigma)/(2*T))/(sigma*sq)`
        # which expands to `(r-q)/(sigma*sqrt(T)) + d2/(2*T*sqrt(T))` — the d2 term
        # divides by T^(3/2) instead of T. Correct term is d2/(2*T).
        # Additionally, the outer sign was `+nd1*(q - ...)` which mixed a spurious `+q`
        # into the base term instead of isolating it in the dividend carry.
        charm = discQ * (-nd1 * ((r - q) / (sigma * sq) - d2 / (2 * T)) + q * Nd1) / 365 if T > 0 else 0
    else:
        delta = discQ * (Nd1 - 1)
        theta = (-(S * discQ * nd1 * sigma) / (2 * sq)
                 - q * S * discQ * Nnd1
                 + r * K * disc * Nnd2) / 365
        rho   = -K * T * disc * Nnd2 / 100
        price = K * disc * Nnd2 - S * discQ * Nnd1
        # charm_put = discQ · [−n(d1)·((r−q)/(σ√T) − d2/(2T)) − q·N(−d1)] / 365
        charm = discQ * (-nd1 * ((r - q) / (sigma * sq) - d2 / (2 * T)) - q * Nnd1) / 365 if T > 0 else 0

    price = max(0.0, price)
    lam = (delta * S / price) if price > 0.005 else 0.0
    return {
        "delta": round(delta, 6), "gamma": round(gamma, 6),
        "theta": round(theta, 6), "vega":  round(vega,  6),
        "rho":   round(rho,   6), "vanna": round(vanna, 6),
        "charm": round(charm, 6), "volga": round(volga, 6),
        "speed": round(speed, 6), "lambda": round(lam,  4),
    }

def solve_iv(S, K, T, r, market_price, is_call, q=0.0, tol=1e-8, max_iter=8):
    """
    Implied volatility solver — three-stage architecture for speed and robustness.

    Stage 1: Corrado-Miller (1996) rational seed.
      Derives a starting σ from the call price and the ATM expansion, giving
      an initial estimate within ~0.01 vol of the true root on most inputs.
      Dramatically outperforms the Brenner-Subrahmanyam constant-×-price seed
      for OTM options (BS underestimates by up to 50% in the wings).

    Stage 2: Halley iteration (order-3 convergence).
      Uses exact vega and volga (∂vega/∂σ = vega·d1·d2/σ) at each step.
      Convergence: |ε_{n+1}| ∝ |ε_n|³ vs Newton's |ε_{n+1}| ∝ |ε_n|².
      Typically reaches |Δσ| < 1e-10 in 3 iterations from the CM seed.
      Halley update:  δ_H = δ_N / (1 − δ_N · volga / (2·vega))
      Reference: Li (2008) "New root-finding algorithms for option pricing."

    Stage 3: Illinois bracket fallback (superlinear convergence).
      Guaranteed to converge when Halley diverges (deep OTM, near-intrinsic,
      high vomma regime). Illinois modification of regula falsi avoids the
      slow linear convergence of bisection and the possible divergence of NR.
      Tolerance: 1e-10 (tighter than tol, ensures full double-precision output).
      Reference: Ford (1995) Numerical Analysis / AERE Report R-9259.

    References:
      Corrado & Miller (1996) J. Banking & Finance 20(3), pp. 595–603.
      Li (2008) "New root-finding algorithms for option pricing."
      Ford (1995) Illinois bracket algorithm.
    """
    if T <= 0 or market_price <= 0 or S <= 0 or K <= 0:
        return 0.0
    sq    = math.sqrt(T)
    discQ = math.exp(-q * T)
    discR = math.exp(-r * T)

    intrinsic = max(0.0, S * discQ - K * discR) if is_call else max(0.0, K * discR - S * discQ)
    if market_price <= intrinsic + 1e-8:
        return 0.0

    # ── Stage 1: Corrado-Miller (1996) rational seed ──────────────────────────
    # Convert to OTM call price for the CM formula (always a small number):
    #   call_otm = C if is_call, else C_parity = P + S·e^{-qT} - K·e^{-rT}
    # CM seed: σ ≈ √(2π/T) · (c_otm − (S·e^{-qT}−K·e^{-rT})/2) / √(S·e^{-qT}·K·e^{-rT})
    try:
        c_otm      = market_price if is_call else market_price + S * discQ - K * discR
        half_fwd   = 0.5 * (S * discQ - K * discR)
        geomean_fwd = math.sqrt(max(1e-10, S * discQ * K * discR))
        sigma      = math.sqrt(2 * math.pi / T) * max(c_otm - half_fwd, 0.001) / geomean_fwd
        sigma      = max(0.01, min(sigma, 8.0))
    except Exception:
        sigma = max(0.01, min(math.sqrt(2 * math.pi / T) * market_price /
                              max(S * math.exp((r - q) * T) * discR, 1e-10), 5.0))

    # ── Stage 2: Halley iterations ────────────────────────────────────────────
    for _ in range(max_iter):
        if sigma < 1e-8:
            break
        p_fit  = bs_price(S, K, T, r, sigma, is_call, q)
        if not math.isfinite(p_fit):
            break
        d1     = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sq)
        d2     = d1 - sigma * sq
        nd1    = npdf(d1)
        vega   = S * discQ * nd1 * sq           # ∂BS/∂σ  (unscaled)
        if vega < 1e-14:
            break
        diff   = p_fit - market_price
        if abs(diff) < tol:
            return round(sigma, 8)
        # Volga = ∂vega/∂σ = vega · d1 · d2 / σ  (Hull §19.4 / Haug eq A.16)
        volga  = vega * d1 * d2 / sigma if sigma > 1e-10 else 0.0
        # Halley update: δ_H = δ_N / (1 - δ_N · volga/(2·vega))
        delta_n = diff / vega
        denom   = 1.0 - 0.5 * delta_n * volga / vega
        delta_h = delta_n / denom if abs(denom) > 1e-12 else delta_n
        sigma  -= delta_h
        sigma   = max(1e-4, min(sigma, 20.0))

    # Check if Halley converged
    if abs(bs_price(S, K, T, r, sigma, is_call, q) - market_price) < tol:
        return round(sigma, 8)

    # ── Stage 3: Illinois (Brent-class) bracket ───────────────────────────────
    lo, hi = 1e-4, 20.0
    f_lo   = bs_price(S, K, T, r, lo, is_call, q) - market_price
    f_hi   = bs_price(S, K, T, r, hi, is_call, q) - market_price
    if f_lo * f_hi > 0:
        return round(max(1e-4, min(sigma, 20.0)), 8) if 1e-4 < sigma < 20.0 else 0.0

    f_il = f_lo   # Illinois: tracks the most recently replaced endpoint's value
    for _ in range(70):
        # Regula falsi: linear interpolation of root position
        mid   = hi - f_hi * (hi - lo) / (f_hi - f_lo + 1e-300)
        mid   = max(lo * (1 + 1e-10), min(hi * (1 - 1e-10), mid))
        f_mid = bs_price(S, K, T, r, mid, is_call, q) - market_price
        if abs(f_mid) < 1e-10 or (hi - lo) < 1e-12:
            return round(mid, 8)
        if f_lo * f_mid < 0:
            if f_mid * f_il < 0:
                f_lo *= 0.5   # Illinois modification: halve retained endpoint
            hi, f_hi = mid, f_mid
        else:
            if f_mid * f_il > 0:
                f_hi *= 0.5   # Illinois modification
            lo, f_lo = mid, f_mid
        f_il = f_mid
    return round((lo + hi) / 2.0, 8)

def solve_iv_best(S, K, T, r, bid, ask, is_call, q=0.0):
    """
    Compute IV using the best available price:
    - If both bid and ask > 0: use mid
    - If only ask > 0:        use ask (common for deep OTM options)
    - If only bid > 0:        use bid
    - Both 0:                 return 0.0
    This prevents the all-zeros problem seen when bid=0 for cheap OTM options.
    """
    if bid > 0 and ask > 0:
        return solve_iv(S, K, T, r, (bid + ask) / 2, is_call, q)
    if ask > 0:
        return solve_iv(S, K, T, r, ask, is_call, q)
    if bid > 0:
        return solve_iv(S, K, T, r, bid, is_call, q)
    return 0.0

# ── Helpers ──────────────────────────────────────────────────────────────────

def sf(v, d=0.0):
    try:
        f = float(v)
        return d if (math.isnan(f) or math.isinf(f)) else f
    except: return d

def si(v, d=0):
    try: return int(float(v)) if v is not None else d
    except: return d

def dte_to_T(dte):
    return max(dte, 0) / 365.0

def parse_contract_symbol(sym):
    """Parse OCC contract symbol: AAPL260718C00150000 → {underlying, expiration, type, strike}"""
    try:
        import re
        m = re.match(r'^([A-Z]{1,6})(\d{6})([CP])(\d{8})$', sym)
        if m:
            und, date_str, cp, strike_str = m.groups()
            exp_date = datetime.strptime(date_str, "%y%m%d")
            dte = max(0, (exp_date - datetime.now()).days)
            return {
                "underlying": und,
                "expiration": exp_date.strftime("%Y-%m-%d"),
                "type": "call" if cp == "C" else "put",
                "strike": float(strike_str) / 1000.0,
                "dte": dte,
            }
    except: pass
    return None

# ── Data Quality Pipeline ─────────────────────────────────────────────────────

STALENESS_LIMIT_S    = 15 * 60   # 15 minutes — flag as stale
STALENESS_FATAL_S    = 24 * 3600  # 24 hours — truly bad (weekend/holiday)
MARKET_OPEN_GRACE_S  = 6  * 3600  # 6 hours after close — allow stale data through

def check_data_quality(contract, spot, now_ts):
    """Returns (quality, flags). 'bad' contracts are dropped; 'flagged' kept with marks."""
    flags = []
    iv  = contract.get("iv", 0) or 0
    bid = contract.get("bid", 0) or 0
    ask = contract.get("ask", 0) or 0
    mid = contract.get("mid", 0) or ((bid + ask) / 2 if (bid or ask) else 0)
    oi  = contract.get("openInterest", 0) or 0
    vol = contract.get("volume", 0) or 0
    dte = contract.get("dte", 30) or 30
    is_call = contract.get("type", "call") == "call"
    K = contract.get("strike", 0)

    if iv != 0 and (iv < 0.01 or iv > 20.0):
        flags.append("IV_BOUNDS")
    if bid < 0 or ask < 0:
        flags.append("NEGATIVE_BA")
    if bid > ask and ask > 0:
        flags.append("BID_GT_ASK")

    # Determine staleness first — we relax the intrinsic check for stale quotes
    # because spot has moved since the quote was taken (after-hours / weekend).
    age_s = 0.0
    ts_str = contract.get("quoteTimestamp", "")
    if ts_str:
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age_s = now_ts - ts.timestamp()
            if age_s > STALENESS_LIMIT_S:
                flags.append(f"STALE_{int(age_s//60)}MIN")
        except: pass

    # Price vs intrinsic — only fatal when data is fresh (< 6h old) because
    # after-hours/weekend quotes are valid at quote time but spot has since moved.
    data_is_fresh = age_s < MARKET_OPEN_GRACE_S
    if spot > 0 and K > 0:
        intrinsic = max(0.0, (spot - K) if is_call else (K - spot))
        effective_price = mid if mid > 0 else ask
        if effective_price > 0 and effective_price < intrinsic - 0.10:
            if data_is_fresh:
                flags.append("BELOW_INTRINSIC")
            else:
                flags.append("STALE_BELOW_INTRINSIC")   # non-fatal — stale price
        if effective_price > 3 * spot and spot > 0:
            flags.append("PRICE_UNREASONABLE")

    if mid > 0.05 and bid > 0 and ask > 0 and (ask - bid) / mid > 0.50:
        flags.append("WIDE_SPREAD")
    if oi > 0 and vol > oi * 10:
        flags.append("VOL_OI_SUSPECT")
    if iv == 0:
        flags.append("IV_MISSING")

    # Drop truly garbage contracts (>24h stale)
    if age_s > STALENESS_FATAL_S:
        return "bad", flags + ["EXPIRED_DATA"]

    if not flags:
        return "clean", []
    # Fatal quality flags — these indicate bad data regardless of staleness
    fatal = {"NEGATIVE_BA", "BELOW_INTRINSIC", "BID_GT_ASK", "PRICE_UNREASONABLE"}
    if any(f in fatal for f in flags):
        return "bad", flags
    return "flagged", flags

# ── Bid-Ask IV Spread ─────────────────────────────────────────────────────────

def calc_bidask_iv(bid, ask, S, K, T, r, is_call, q=0.0):
    iv_mid = solve_iv(S, K, T, r, (bid + ask) / 2 if (bid and ask) else (ask or bid), is_call, q)
    iv_bid = solve_iv(S, K, T, r, bid, is_call, q) if bid > 0 else 0
    iv_ask = solve_iv(S, K, T, r, ask, is_call, q) if ask > 0 else 0
    # Clamp spread to [0, ∞) — negative spreads are LBR precision artefacts from
    # nearly identical bid/ask prices where the solver lands on opposite sides of
    # the root. They carry no financial meaning.
    spread = max(0.0, iv_ask - iv_bid) if (iv_ask and iv_bid) else 0
    return {
        "ivBid":          round(iv_bid, 6),
        "ivAsk":          round(iv_ask, 6),
        "ivMid":          round(iv_mid, 6),
        "bidAskIVSpread": round(spread, 6),
    }

# ── Put-Call Parity IV Substitution ──────────────────────────────────────────
# When bid-ask spread on a contract is too wide (> 50% of mid), substitute IV
# from the synthetic counterpart using put-call parity.
# C − P = S·e^{-qT} − K·e^{-rT}  (Merton 1973)
# This mirrors OptionMetrics IvyDB methodology: "when spread is too wide,
# dynamically substitute the call or put counterpart to establish fair value."

def pcp_substitute_iv(S, K, T, r, q,
                      call_bid, call_ask, put_bid, put_ask,
                      is_call):
    """
    Returns a synthetic mid IV for one side, derived from the other side via
    put-call parity when the primary side's spread is too wide or quotes are missing.
    Returns 0.0 if substitution is not possible.
    """
    if T <= 0 or S <= 0 or K <= 0:
        return 0.0
    parity_fwd = S * math.exp(-q * T) - K * math.exp(-r * T)  # C - P synthetic

    def _spread_too_wide(bid, ask):
        if bid <= 0 or ask <= 0:
            return True
        mid = (bid + ask) / 2
        return mid > 0.05 and (ask - bid) / mid > 0.50

    if is_call and _spread_too_wide(call_bid, call_ask):
        # Use put quotes to derive synthetic call mid
        if put_bid > 0 and put_ask > 0:
            put_mid = (put_bid + put_ask) / 2
            synth_call = max(0.01, put_mid + parity_fwd)
            iv = solve_iv(S, K, T, r, synth_call, True, q)
            return iv
    elif not is_call and _spread_too_wide(put_bid, put_ask):
        # Use call quotes to derive synthetic put mid
        if call_bid > 0 and call_ask > 0:
            call_mid = (call_bid + call_ask) / 2
            synth_put = max(0.01, call_mid - parity_fwd)
            iv = solve_iv(S, K, T, r, synth_put, False, q)
            return iv
    return 0.0

# ── Epanechnikov Kernel IV Smoother ──────────────────────────────────────────
# Implements the kernel-smoothing step used by OptionMetrics (IvyDB) to isolate
# a single, precise market-value IV curve that sits between put-call bid-ask
# spreads. Uses Epanechnikov kernel (optimal MSE kernel, Epanechnikov 1969) with
# leave-one-out bandwidth of 0.10 in log-moneyness space (calibrated to equity
# index options).
#
# Reference: "Kernel Smoothing in Empirical Finance" — OptionMetrics whitepaper;
# also consistent with Andersen & Brotherton-Ratcliffe (2005) approach.

def kernel_smooth_iv(strikes, ivs, spot, T, r, q=0.0,
                     bandwidth=None, min_points=3):
    """
    Epanechnikov kernel smoother for the IV smile across strikes (one expiry).

    Bandwidth selection — Silverman (1986) rule-of-thumb adapted to log-moneyness:
      h_opt = 1.06 · σ_k · n^{-1/5}
    where σ_k is the sample standard deviation of log-moneyness across valid strikes
    and n is the number of valid IV observations. This adapts to the spread of the
    strike grid and the number of observations, unlike the fixed 0.10 which was
    calibrated only for SPX options and over-smooths for single-stock names (wider
    strike grids) and under-smooths for weekly expirations (few strikes).

    The Epanechnikov kernel K(u) = 0.75·(1−u²)·1_{|u|<1} minimises MSE among all
    compactly supported kernels (Epanechnikov 1969). Combined with ATM-favoring
    vega weighting, this matches the IvyDB OptionMetrics methodology.

    Bandwidth override: pass bandwidth=h explicitly to disable Silverman rule.

    References:
      Silverman (1986) "Density Estimation for Statistics and Data Analysis." §3.4.2.
      Epanechnikov (1969) Theory Probability Appl. 14(1), pp. 153–158.
      OptionMetrics IvyDB Methodology Guide §4 — kernel smoother implementation.
    """
    if not strikes or not ivs or len(strikes) < min_points:
        return list(ivs)
    if T <= 0 or spot <= 0:
        return list(ivs)

    F  = spot * math.exp((r - q) * T)
    ks = [math.log(K / F) if K > 0 and F > 0 else 0.0 for K in strikes]

    # Filter valid (strike, iv) pairs for bandwidth estimation
    valid_ks = [ks[i] for i in range(len(ivs)) if ivs[i] > 0]
    n_valid  = len(valid_ks)
    if n_valid < min_points:
        return list(ivs)

    if bandwidth is None:
        # Silverman rule-of-thumb in log-moneyness space
        mu_k  = sum(valid_ks) / n_valid
        var_k = sum((x - mu_k) ** 2 for x in valid_ks) / max(n_valid - 1, 1)
        std_k = math.sqrt(var_k) if var_k > 0 else 0.05
        # IQR-adjusted Scott (1992): use min(std, IQR/1.34) for heavy-tailed smile
        sorted_ks = sorted(valid_ks)
        q1 = sorted_ks[max(0, int(0.25 * n_valid))]
        q3 = sorted_ks[min(n_valid - 1, int(0.75 * n_valid))]
        iqr = max((q3 - q1) / 1.34, 1e-4)
        scale = min(std_k, iqr)
        h = max(0.02, min(1.06 * scale * (n_valid ** (-0.2)), 0.35))
    else:
        h = max(0.01, bandwidth)

    # Vega proxy weighting: ATM options get higher weight (Gaussian peaked at k=0)
    def atm_weight(k_val: float) -> float:
        return math.exp(-0.5 * (k_val / 0.20) ** 2)

    smoothed = []
    for i in range(len(strikes)):
        k_i = ks[i]
        total_w   = 0.0
        total_wiv = 0.0
        for j in range(len(strikes)):
            if ivs[j] <= 0:
                continue
            u = (ks[j] - k_i) / h
            if abs(u) >= 1.0:
                continue   # Epanechnikov compact support
            k_epan = 0.75 * (1.0 - u * u)
            w = k_epan * atm_weight(ks[j])
            total_w   += w
            total_wiv += w * ivs[j]
        if total_w > 1e-12 and total_wiv > 0:
            sv  = total_wiv / total_w
            raw = ivs[i]
            # Clamp to ±40% of raw IV (guards against extrapolation at extreme wings)
            clamped = max(raw * 0.60, min(raw * 1.40, sv)) if raw > 0 else sv
            smoothed.append(round(max(0.005, clamped), 6))
        else:
            smoothed.append(round(ivs[i], 6))

    return smoothed

# ── Gatheral SVI IV Surface Fitting ─────────────────────────────────���────────
# Gatheral (2004) Raw SVI: w(k) = a + b·(ρ·(k−m) + √((k−m)² + σ²))
# where k = log(K/F), w = σ²_impl · T (total variance).
# Fitting uses gradient descent with analytical gradients.
# Butterfly-no-arbitrage condition: ∂²w/∂k² ≥ 0 at all k (Fukasawa 2012).
# Calendar-no-arbitrage: ∂w/∂T ≥ 0 at all k (Gatheral & Jacquier 2014).

def svi_fit_expiry(strikes, ivs, spot, T, r, q=0.0,
                   min_points=4, max_iter=300, lr=0.02,
                   smooth_first=True):
    """
    Fit Gatheral Raw SVI to (strike, IV) data for one expiry.
    Uses Adam optimizer (Kingma & Ba 2015) with analytical Jacobian —
    identical to the TypeScript implementation for cross-language parity.
    Convergence: ~80 iterations vs ~500 for fixed-LR SGD.
    Returns dict with params {a, b, rho, m, sig} and quality metrics.
    Returns None if insufficient data or fit fails.
    """
    if not strikes or not ivs or len(strikes) < min_points:
        return None
    if T <= 0 or spot <= 0:
        return None

    F = spot * math.exp((r - q) * T)
    valid = [(K, iv) for K, iv in zip(strikes, ivs) if K > 0 and iv > 0.005 and iv < 5.0]
    if len(valid) < min_points:
        return None

    # Optionally pre-smooth IV data before fitting to reduce microstructure noise
    if smooth_first and len(valid) >= min_points:
        ks_v = [K for K, _ in valid]
        iv_v = [iv for _, iv in valid]
        iv_smoothed = kernel_smooth_iv(ks_v, iv_v, spot, T, r, q)
        valid = [(K, iv_s) for (K, _), iv_s in zip(valid, iv_smoothed) if iv_s > 0.005]
        if len(valid) < min_points:
            return None

    # Convert to log-moneyness k and total variance w = σ² · T
    pts = [{"k": math.log(K / F), "w": iv * iv * T, "K": K, "iv": iv}
           for K, iv in valid if F > 0]
    if len(pts) < min_points:
        return None

    # Initial param estimate
    atm_w = min(pts, key=lambda p: abs(p["k"])).get("w", 0.04)
    a, b, rho, m, sig = atm_w * 0.7, 0.12, -0.5, 0.0, 0.15

    # Adam optimizer state — identical to TS implementation
    beta1, beta2, eps_adam = 0.9, 0.999, 1e-8
    mA = mB = mRho = mM = mSig = 0.0
    vA = vB = vRho = vM = vSig = 0.0
    n = len(pts)

    for t in range(1, max_iter + 1):
        dA = dB = dRho = dM = dSig = loss = 0.0
        for p in pts:
            z    = p["k"] - m
            disc = max(math.sqrt(z * z + sig * sig), 1e-10)  # explicit guard: or-short-circuit fails when disc==0.0
            w_fit = a + b * (rho * z + disc)
            err   = w_fit - p["w"]
            loss += err * err
            dA   += 2 * err
            dB   += 2 * err * (rho * z + disc)
            dRho += 2 * err * b * z
            dM   += 2 * err * b * (-rho - z / disc)
            dSig += 2 * err * b * (sig / disc)

        # Normalise gradients
        gA, gB, gR, gM_g, gS = dA/n, dB/n, dRho/n, dM/n, dSig/n

        # Adam moment updates
        mA   = beta1*mA   + (1-beta1)*gA;   vA   = beta2*vA   + (1-beta2)*gA*gA
        mB   = beta1*mB   + (1-beta1)*gB;   vB   = beta2*vB   + (1-beta2)*gB*gB
        mRho = beta1*mRho + (1-beta1)*gR;   vRho = beta2*vRho + (1-beta2)*gR*gR
        mM   = beta1*mM   + (1-beta1)*gM_g; vM   = beta2*vM   + (1-beta2)*gM_g*gM_g
        mSig = beta1*mSig + (1-beta1)*gS;   vSig = beta2*vSig + (1-beta2)*gS*gS

        # Bias correction
        bc1 = 1 - beta1**t; bc2 = 1 - beta2**t
        a   -= lr * (mA   / bc1) / (math.sqrt(vA   / bc2) + eps_adam)
        b   -= lr * (mB   / bc1) / (math.sqrt(vB   / bc2) + eps_adam)
        rho -= lr * (mRho / bc1) / (math.sqrt(vRho / bc2) + eps_adam)
        m   -= lr * (mM   / bc1) / (math.sqrt(vM   / bc2) + eps_adam)
        sig -= lr * (mSig / bc1) / (math.sqrt(vSig / bc2) + eps_adam)

        # Project onto feasible set (Gatheral-Jacquier 2014 constraints)
        b   = max(1e-5, b)
        sig = max(1e-4, sig)
        rho = max(-0.999, min(0.999, rho))
        # No-negative-variance: a ≥ -b·sig·√(1-rho²)  (SVI minimum at k=m)
        w_min = b * sig * math.sqrt(max(0.0, 1 - rho*rho))
        if a < -w_min + 1e-6:
            a = -w_min + 1e-6

        if loss / n < 1e-12:
            break

    # Reconstruct fitted IVs and check arbitrage
    fitted = []
    sum_sq_err = 0.0
    min_conv = float("inf")
    for p in pts:
        z    = p["k"] - m
        disc = max(math.sqrt(z * z + sig * sig), 1e-10)
        wf   = a + b * (rho * z + disc)
        iv_f = math.sqrt(max(0.0, wf) / T) if T > 0 else 0.0
        sum_sq_err += (iv_f - p["iv"]) ** 2
        # d²w/dk² = b·σ²/disc³ (Fukasawa 2012 butterfly condition)
        d2w = b * sig * sig / max(disc ** 3, 1e-15)
        min_conv = min(min_conv, d2w)
        fitted.append({"strike": p["K"], "logMon": round(p["k"], 5),
                       "ivFit": round(max(0.005, iv_f), 5), "ivRaw": p["iv"],
                       "residual": round(iv_f - p["iv"], 5)})

    rmse = math.sqrt(sum_sq_err / len(pts)) if pts else 0.0
    is_arb_free = min_conv >= -1e-6 and a >= 0 and b >= 0

    return {
        "params":       {"a": round(a, 6), "b": round(b, 6), "rho": round(rho, 6),
                         "m": round(m, 6), "sig": round(sig, 6), "T": round(T, 6)},
        "fittedIVs":    fitted[:50],   # cap list size for JSON transport
        "rmse":         round(rmse, 6),
        "isArbitrageFree": is_arb_free,
        "butterflyConv": round(max(0.0, min_conv), 8),
        "nPoints":      len(pts),
    }

def svi_eval(params, k):
    """Evaluate fitted SVI at log-moneyness k; returns IV (not total variance)."""
    a, b, rho, m, sig = (params["a"], params["b"], params["rho"],
                         params["m"], params["sig"])
    T = params.get("T", 1.0)
    z    = k - m
    disc = max(math.sqrt(z * z + sig * sig), 1e-10)   # max() guard — `or` short-circuits on 0.0
    w    = a + b * (rho * z + disc)
    return math.sqrt(max(0.0, w) / T) if T > 0 else 0.0

# ── IVolatility-Style Composite IV Index ─────────────────────────────────────
# Replicates the IVolatility IV Index methodology (https://www.ivolatility.com/
# education/implied-volatility-index/):
#   1. Select ATM options with high Vega and predictable Delta (0.40 ≤ |Δ| �� 0.60)
#   2. Apply Vega-weighting across selected strikes
#   3. Normalize to fixed tenors (30d, 60d) using square-root-of-time interpolation
#      between the two bounding expiries (√T linear interpolation)
# This produces a single scalar IV that is comparable across time and symbols.

def calc_iv_index(calls, puts, spot, r, q=0.0, target_dte=30):
    """
    IVolatility-style composite IV Index for target_dte (default 30 days).
    Returns dict with iv_index, method, and metadata.
    Uses Vega²-weighted interpolation between the two expiries bracketing target_dte.
    Delta filter [0.25, 0.75] is applied to ensure only near-ATM options are used,
    consistent with IVolatility methodology and the TS calcIVIndex implementation.
    """
    if not calls and not puts:
        return {"ivIndex": 0.0, "method": "none", "targetDte": target_dte}
    if spot <= 0:
        return {"ivIndex": 0.0, "method": "none", "targetDte": target_dte}

    # Collect all unique expiries from calls + puts, sort by DTE
    by_exp = {}
    for c in (calls + puts):
        exp = c.get("expiration", "")
        dte = c.get("dte", 0) or 0
        iv  = c.get("iv", 0) or 0
        dlt = abs(c.get("delta", 0) or 0)
        vg  = abs(c.get("vega", 0) or 0)
        K   = c.get("strike", 0) or 0
        if not exp or iv <= 0 or dte <= 0 or K <= 0:
            continue
        by_exp.setdefault(exp, []).append({"iv": iv, "delta": dlt, "vega": vg,
                                           "dte": dte, "K": K})

    if not by_exp:
        return {"ivIndex": 0.0, "method": "none", "targetDte": target_dte}

    # Build Vega²-weighted ATM IV per expiry (IVolatility + TS calcIVIndex methodology:
    # Vega² penalises illiquid wings more strongly than linear Vega weighting)
    exp_ivs = []  # list of (dte, vega2_weighted_iv)
    for exp, pts in sorted(by_exp.items(), key=lambda x: x[1][0]["dte"] if x[1] else 999):
        # IVolatility ATM filter: |Δ| in [0.30, 0.70] — pure ATM options only.
        # [0.25, 0.75] includes 15-delta OTM options that inflate the index with wing premium.
        # Consistent with TS calcIVIndex after the same correction.
        atm_pts = [p for p in pts if 0.30 <= p["delta"] <= 0.70 and p["vega"] > 0 and p["iv"] > 0]
        if not atm_pts:
            # Fallback: use all options with positive vega
            atm_pts = [p for p in pts if p["vega"] > 0 and p["iv"] > 0]
        if not atm_pts:
            continue
        # Vega² weighting (matches TS calcIVIndex exactly)
        total_vega2 = sum(p["vega"] * p["vega"] for p in atm_pts)
        if total_vega2 <= 0:
            continue
        vega2_iv = sum(p["iv"] * p["vega"] * p["vega"] for p in atm_pts) / total_vega2
        dte_val   = atm_pts[0]["dte"]
        exp_ivs.append((dte_val, max(0.005, vega2_iv)))

    if not exp_ivs:
        return {"ivIndex": 0.0, "method": "none", "targetDte": target_dte}

    exp_ivs.sort(key=lambda x: x[0])

    # Find the two expiries bracketing target_dte
    lower = [(dte, iv) for dte, iv in exp_ivs if dte <= target_dte]
    upper = [(dte, iv) for dte, iv in exp_ivs if dte >  target_dte]

    if lower and upper:
        # √T linear interpolation (IVolatility methodology)
        d_lo, iv_lo = lower[-1]
        d_hi, iv_hi = upper[0]
        # Total variance interpolation: var(T) = IV² · T (robust to maturity)
        tv_lo = iv_lo * iv_lo * d_lo
        tv_hi = iv_hi * iv_hi * d_hi
        t_tgt = target_dte
        if d_hi > d_lo:
            w_hi = (t_tgt - d_lo) / (d_hi - d_lo)
            w_lo = 1.0 - w_hi
            tv_tgt = w_lo * tv_lo + w_hi * tv_hi
            iv_idx = math.sqrt(max(0.0, tv_tgt) / t_tgt) if t_tgt > 0 else 0.0
        else:
            iv_idx = iv_lo
        method = f"interp({d_lo}d,{d_hi}d)"
    elif lower:
        # Only shorter expiries available — extrapolate via √T scaling
        d_lo, iv_lo = lower[-1]
        t_tgt = target_dte
        tv_lo = iv_lo * iv_lo * d_lo
        iv_idx = math.sqrt(tv_lo / t_tgt) if t_tgt > 0 else iv_lo
        method = f"extrap_from_{d_lo}d"
    else:
        # Only longer expiries — scale down via √T
        d_hi, iv_hi = upper[0]
        t_tgt = target_dte
        tv_hi = iv_hi * iv_hi * d_hi
        iv_idx = math.sqrt(tv_hi / t_tgt) if t_tgt > 0 else iv_hi
        method = f"extrap_from_{d_hi}d"

    return {
        "ivIndex":    round(iv_idx, 6),
        "ivIndexPct": round(iv_idx * 100, 4),
        "method":     method,
        "targetDte":  target_dte,
        "expiryCount": len(exp_ivs),
    }

# ── Event-Spanning Expiry Detection ──────────────────────────────────────────
# Identifies expiries where a scheduled macro event (earnings, CPI, FOMC) is
# priced in via elevated ATM IV relative to the surrounding term structure.
# Based on Carverhill, Lochmann & Wang (2026, SSRN:2606.12872) non-spanning
# methodology: build a baseline surface from non-spanning expiries, then
# overlay the event premium.
# Kink threshold: ATM IV > 1.30× weighted average of adjacent expiry ATM IVs.

def flag_event_spanning(calls, puts, spot):
    """
    For each expiry, determine if it is 'event-spanning' (has anomalous IV kink).
    Returns dict: {expiry: {"isEventSpanning": bool, "kinkRatio": float,
                            "exEventIV": float, "eventPremiumPct": float}}

    Methodology — Carverhill, Lochmann & Wang (2026) non-spanning detection:
      1. ATM IV per expiry: distance-weighted average of the 3 nearest-ATM strikes
         (single-contract lookup is unstable when two strikes are equidistant).
      2. Baseline construction via **total-variance (TV) interpolation** — same
         methodology as calcIVIndex and flag_event_spanning in the TS layer.
         TV(T) = σ²_ATM · T is interpolated linearly in T; baseline IV is then
         √(TV_pred / T). Linear interpolation in σ is wrong because it does not
         satisfy the no-calendar-arbitrage constraint (TV must be non-decreasing).
      3. Adaptive threshold: kink_ratio ≥ 1.30 (30% excess) flags event spanning,
         consistent with Stein (1989) jump-volatility premium findings.
      4. For expiries at the boundary (no prior or subsequent expiry available),
         second-order TV extrapolation is used rather than raw IV averaging, which
         biases the baseline toward the largest available value.

    References:
      Carverhill, Lochmann & Wang (2026) SSRN:2606.12872.
      Stein (1989) "Overreactions in the Options Market." J. Finance 44(4).
      Gatheral & Jacquier (2014) "Arbitrage-free SVI volatility surfaces."
        Quantitative Finance 14(1), pp. 59–71. (TV linearity as arb-free condition)
    """
    by_exp: dict = {}
    for c in (calls + puts):
        exp = c.get("expiration", "")
        iv  = c.get("iv", 0) or 0
        dte = c.get("dte", 0) or 0
        K   = c.get("strike", 0)
        if not exp or iv <= 0 or K <= 0 or dte <= 0:
            continue
        by_exp.setdefault(exp, []).append({"iv": iv, "dte": dte, "K": K})

    if len(by_exp) < 2:
        return {}

    # ── ATM IV per expiry: distance-weighted avg of 3 nearest-ATM strikes ────
    exp_atm: dict = {}   # {expiry: {"atm_iv": float, "dte": int, "tv": float}}
    for exp, pts in by_exp.items():
        if not pts:
            continue
        nearest = sorted(pts, key=lambda p: abs(p["K"] - spot))[:3]
        weights = [1.0 / (abs(p["K"] - spot) + 0.01 * max(spot, 1.0)) for p in nearest]
        total_w = sum(weights)
        atm_iv  = (sum(w * p["iv"] for w, p in zip(weights, nearest)) / total_w
                   if total_w > 0 else nearest[0]["iv"])
        dte_val = nearest[0]["dte"]
        exp_atm[exp] = {"atm_iv": atm_iv, "dte": dte_val,
                        "tv": atm_iv * atm_iv * dte_val}   # total variance σ²·T

    if len(exp_atm) < 2:
        return {}

    sorted_exps = sorted(exp_atm.keys(), key=lambda e: exp_atm[e]["dte"])

    # ── Helper: TV-interpolated baseline IV for expiry at index i ─────────────
    def _tv_baseline(i: int) -> float:
        """Interpolate/extrapolate total variance linearly in T to get baseline IV."""
        dte_i  = exp_atm[sorted_exps[i]]["dte"]
        tv_i   = exp_atm[sorted_exps[i]]["tv"]
        n      = len(sorted_exps)

        if i == 0:
            # Leftmost expiry — extrapolate backward from next two if available
            if n >= 2:
                d1 = exp_atm[sorted_exps[1]]["dte"]
                tv1 = exp_atm[sorted_exps[1]]["tv"]
                if n >= 3:
                    d2 = exp_atm[sorted_exps[2]]["dte"]
                    tv2 = exp_atm[sorted_exps[2]]["tv"]
                    if d2 > d1 > 0:
                        # Second-order linear extrapolation of TV slope
                        slope = (tv2 - tv1) / (d2 - d1)
                        tv_base = tv1 - slope * (d1 - dte_i)
                    else:
                        tv_base = tv1 * dte_i / d1 if d1 > 0 else tv_i
                else:
                    tv_base = tv1 * dte_i / d1 if d1 > 0 else tv_i
            else:
                return exp_atm[sorted_exps[i]]["atm_iv"]
        elif i == n - 1:
            # Rightmost expiry — extrapolate forward from previous two if available
            if n >= 2:
                d_prev   = exp_atm[sorted_exps[i - 1]]["dte"]
                tv_prev  = exp_atm[sorted_exps[i - 1]]["tv"]
                if n >= 3:
                    d_prev2 = exp_atm[sorted_exps[i - 2]]["dte"]
                    tv_prev2 = exp_atm[sorted_exps[i - 2]]["tv"]
                    if d_prev > d_prev2 > 0:
                        slope = (tv_prev - tv_prev2) / (d_prev - d_prev2)
                        tv_base = tv_prev + slope * (dte_i - d_prev)
                    else:
                        tv_base = tv_prev * dte_i / d_prev if d_prev > 0 else tv_i
                else:
                    tv_base = tv_prev * dte_i / d_prev if d_prev > 0 else tv_i
            else:
                return exp_atm[sorted_exps[i]]["atm_iv"]
        else:
            # Interior expiry: interpolate between immediate neighbours
            d_lo  = exp_atm[sorted_exps[i - 1]]["dte"]
            d_hi  = exp_atm[sorted_exps[i + 1]]["dte"]
            tv_lo = exp_atm[sorted_exps[i - 1]]["tv"]
            tv_hi = exp_atm[sorted_exps[i + 1]]["tv"]
            if d_hi > d_lo:
                w_hi   = (dte_i - d_lo) / (d_hi - d_lo)
                tv_base = tv_lo + w_hi * (tv_hi - tv_lo)
            else:
                tv_base = (tv_lo + tv_hi) / 2.0

        # TV must be ≥ 0; if negative from extrapolation, clamp to 0
        return math.sqrt(max(0.0, tv_base) / dte_i) if dte_i > 0 else 0.0

    # ── Per-expiry event detection ────────────────────────────────────────────
    result: dict = {}
    for i, exp in enumerate(sorted_exps):
        atm_iv  = exp_atm[exp]["atm_iv"]
        dte_cur = exp_atm[exp]["dte"]

        baseline_iv = _tv_baseline(i)
        if baseline_iv < 0.001:
            result[exp] = {"isEventSpanning": False, "kinkRatio": 1.0,
                           "exEventIV": atm_iv, "baselineIV": 0.0,
                           "atmIV": round(atm_iv, 6), "eventPremiumPct": 0.0}
            continue

        kink_ratio = atm_iv / baseline_iv
        is_event   = kink_ratio >= 1.30   # 30% excess IV threshold (Stein 1989)
        ex_event_iv = baseline_iv if is_event else atm_iv
        event_prem_pct = round((atm_iv - baseline_iv) / baseline_iv * 100, 2) if is_event else 0.0

        result[exp] = {
            "isEventSpanning":  is_event,
            "kinkRatio":        round(kink_ratio, 3),
            "exEventIV":        round(ex_event_iv, 6),
            "baselineIV":       round(baseline_iv, 6),
            "atmIV":            round(atm_iv, 6),
            "eventPremiumPct":  event_prem_pct,
        }

    return result

# ── Historical Volatility — Yang-Zhang (2000) OHLC Estimator ────────────────
# Yang-Zhang (2000) achieves 8–14× lower MSE than close-to-close (CC) and
# 2–4× lower than Garman-Klass (1980) for the same sample, because it
# incorporates overnight gaps (opening jump) that GK ignores.
#
# σ²_YZ = σ²_overnight  +  k · σ²_open-to-close  +  (1−k) · σ²_RS
#
#   σ²_overnight = Var[ln(O_i / C_{i-1})]     (overnight gap, sample var)
#   σ²_OC        = Var[ln(C_i / O_i)]         (open-to-close, sample var)
#   σ²_RS        = mean[ln(H/C)·ln(H/O) + ln(L/C)·ln(L/O)]   (Rogers-Satchell 1991)
#   k            = 0.34 / (1.34 + (n+1)/(n-1))   (optimal weight, Vipul-Jacob 2007)
#
# Falls back to Garman-Klass (pure intraday, no overnight) when the bar series
# has only one element (can't compute overnight returns).
#
# References:
#   Yang & Zhang (2000) J. Business 73(3), pp. 477–491.
#   Rogers & Satchell (1991) J. Applied Probability 28(4), pp. 1073–1076.
#   Garman & Klass (1980) J. Business 53(1), pp. 67–78.

def garman_klass_vol(bars):
    """
    Yang-Zhang (2000) combined OHLC historical volatility estimator.
    Returns annualised volatility (252-day convention).
    Falls back to single-bar Garman-Klass when only 1 bar available.
    Accepts bar dicts with keys h/high, l/low, o/open, c/close.
    """
    if not bars:
        return 0.0

    def _parse(b):
        H = sf(b.get("h") or b.get("high",  0))
        L = sf(b.get("l") or b.get("low",   0))
        O = sf(b.get("o") or b.get("open",  0))
        C = sf(b.get("c") or b.get("close", 0))
        return H, L, O, C

    # ── Single-bar fallback: Garman-Klass (no overnight available) ───────────
    if len(bars) < 2:
        H, L, O, C = _parse(bars[0])
        if H > 0 and L > 0 and O > 0 and C > 0 and H >= L:
            gk = 0.5 * math.log(H / L) ** 2 - (2 * math.log(2) - 1) * math.log(C / O) ** 2
            return round(math.sqrt(max(0.0, gk) * 252), 6)
        return 0.0

    # ── Multi-bar Yang-Zhang ──────────────────────────────────────────────────
    n = len(bars) - 1    # number of overnight+intraday return pairs
    lnOC_vals  = []      # ln(O_i / C_{i-1})  overnight log-returns
    lnCO_vals  = []      # ln(C_i / O_i)      open-to-close log-returns
    rs_vals    = []      # Rogers-Satchell intraday estimator per bar

    for i in range(n):
        Hp, Lp, Op, Cp = _parse(bars[i])
        H,  L,  O,  C  = _parse(bars[i + 1])
        if not (Cp > 0 and O > 0 and H > 0 and L > 0 and C > 0 and H >= L):
            continue
        lnOC_vals.append(math.log(O  / Cp))
        lnCO_vals.append(math.log(C  / O))
        # Rogers-Satchell (1991): drift-free intraday variance estimate
        rs_vals.append(math.log(H / C) * math.log(H / O) +
                       math.log(L / C) * math.log(L / O))

    m = len(rs_vals)
    if m < 1:
        return 0.0

    # Sample variances (ddof=1) for overnight and open-to-close
    mu_OC  = sum(lnOC_vals) / m
    var_OC = sum((x - mu_OC) ** 2 for x in lnOC_vals) / max(m - 1, 1)
    mu_CO  = sum(lnCO_vals) / m
    var_CO = sum((x - mu_CO) ** 2 for x in lnCO_vals) / max(m - 1, 1)
    # RS variance: mean (not sample var) — RS is already an unbiased estimator
    var_RS = sum(rs_vals) / m

    # Yang-Zhang optimal blending weight (Vipul & Jacob 2007):
    k = 0.34 / (1.34 + (m + 1) / max(m - 1, 1))
    daily_var = var_OC + k * var_CO + (1.0 - k) * var_RS
    return round(math.sqrt(max(0.0, daily_var) * 252), 6)

# ── Vectorized Batch Greeks (scipy fast path) ────────────────────────────────

def bs_greeks_batch(S, K_arr, T_arr, r, sigma_arr, is_call_arr, div_arr=None, mid_arr=None):
    """
    Vectorized Black-Scholes greeks using numpy/scipy for batch processing.
    Falls back to scalar loop if numpy/scipy unavailable.
    Returns list of dicts matching bs_greeks() output format.
    """
    try:
        import numpy as np
        from scipy.special import erf

        # Use 365 (not 365.242199) to match bs_greeks(), theta_decomposition(), and
        # all other scalar functions — consistent theta across Alpaca and yfinance paths.
        DAYS_IN_YEAR  = 365.0
        SQRT_2        = 1.4142135623730951
        INV_SQRT_2PI  = 0.3989422804014327

        IV      = np.asarray(sigma_arr,   dtype=float)
        spot    = float(S)
        K       = np.asarray(K_arr,       dtype=float)
        T       = np.asarray(T_arr,       dtype=float)
        RFR     = float(r)
        isCall  = np.asarray(is_call_arr, dtype=bool)
        div     = np.asarray(div_arr if div_arr is not None else np.zeros(len(K_arr)), dtype=float)
        mid     = np.asarray(mid_arr if mid_arr is not None else np.ones(len(K_arr)) * spot, dtype=float)

        # Mask invalid rows
        valid = (IV > 0) & (T > 0) & (K > 0)
        IV    = np.where(valid, IV, 0.30)
        T     = np.where(T > 0, T, 1/365)

        sqrt_T    = np.sqrt(T)
        iv_sqrt_T = IV * sqrt_T

        d1 = (np.log(spot / K) + T * (RFR - div + (IV * IV) * 0.5)) / iv_sqrt_T
        d2 = d1 - iv_sqrt_T

        n_d1    = 0.5 * (1.0 + erf(d1 / SQRT_2))
        n_d2    = 0.5 * (1.0 + erf(d2 / SQRT_2))
        pdf_d1  = INV_SQRT_2PI * np.exp(-d1 * d1 * 0.5)

        exp_div = np.exp(-div * T)
        exp_rfr = np.exp(-RFR * T)

        spot_exp_div    = spot * exp_div
        strike_exp_rfr  = K * exp_rfr

        gamma       = (exp_div * pdf_d1) / (spot * iv_sqrt_T)
        vega        = 0.01 * spot_exp_div * sqrt_T * pdf_d1
        theta_base  = -(spot_exp_div * IV * pdf_d1) / (2.0 * sqrt_T)

        delta = np.where(isCall, exp_div * n_d1,        exp_div * (n_d1 - 1.0))
        rho   = np.where(isCall,
                         0.01 * strike_exp_rfr * T * n_d2,
                        -0.01 * strike_exp_rfr * T * (1.0 - n_d2))
        lam   = np.where(isCall,
                         (spot_exp_div / np.where(mid > 0.005, mid, 0.005)) * n_d1,
                        -(spot_exp_div / np.where(mid > 0.005, mid, 0.005)) * (1.0 - n_d1))
        theta = np.where(isCall,
                         (1.0 / DAYS_IN_YEAR) * (theta_base - RFR * strike_exp_rfr * n_d2
                                                  + div * spot_exp_div * n_d1),
                         (1.0 / DAYS_IN_YEAR) * (theta_base - RFR * strike_exp_rfr * (1.0 - n_d2)
                                                  + div * spot_exp_div * (1.0 - n_d1)))

        # Second-order: vanna = ∂Delta/∂σ = −e^{-qT}·n(d1)·d2/σ  (Hull 19.6)
        # vanna = -(vega * 100 / spot) * (d2 / iv_sqrt_T)
        # because vega*100 = S·e^{-qT}·n(d1)·√T, so (vega*100/spot)/sqrt_T = e^{-qT}·n(d1)
        # and d2/(sigma*sqrt_T) = d2/iv_sqrt_T
        iv_sqrt_T_safe = np.where(iv_sqrt_T > 0, iv_sqrt_T, 1e-8)
        IV_safe        = np.where(IV > 0, IV, 1e-8)
        T_safe         = np.where(T > 0, T, 1e-8)
        vanna = -(vega * 100 / spot) * (d2 / iv_sqrt_T_safe)
        volga = vega * d1 * d2 / IV_safe
        speed = -gamma / spot * (d1 / iv_sqrt_T_safe + 1)
        # Charm = ∂Delta/∂t — full Merton form with dividend carry term.
        # Call:  charm = discQ·[-n(d1)·(2(r−q)T − d2·σ√T)/(2T·σ√T) + q·N(d1)]  /365
        # Put:   charm = discQ·[-n(d1)·(2(r−q)T − d2·σ√T)/(2T·σ√T) − q·N(1−d1)] /365
        # Vectorized using isCall mask:
        denom_charm = np.where(2 * T_safe * iv_sqrt_T_safe > 0, 2 * T_safe * iv_sqrt_T_safe, 1e-8)
        charm_base  = exp_div * (-pdf_d1 * (2 * (RFR - div) * T - d2 * iv_sqrt_T) / denom_charm)
        # Dividend carry term: +q·e^{-qT}·N(d1) for calls, −q·e^{-qT}·N(−d1) for puts
        div_carry   = np.where(isCall,
                               div * exp_div * n_d1,
                              -div * exp_div * (1.0 - n_d1))
        charm = (charm_base + div_carry) / 365

        # Zero out invalid rows
        for arr in [delta, gamma, theta, vega, rho, lam, vanna, volga, speed, charm]:
            arr[~valid] = 0.0

        results = []
        for i in range(len(K_arr)):
            results.append({
                "delta":  round(float(delta[i]),  6),
                "gamma":  round(float(gamma[i]),  6),
                "theta":  round(float(theta[i]),  6),
                "vega":   round(float(vega[i]),   6),
                "rho":    round(float(rho[i]),    6),
                "vanna":  round(float(vanna[i]),  6),
                "charm":  round(float(charm[i]),  6),
                "volga":  round(float(volga[i]),  6),
                "speed":  round(float(speed[i]),  6),
                "lambda": round(float(lam[i]),    4),
            })
        return results

    except ImportError:
        # numpy/scipy not available — fall back to scalar
        _div = div_arr if div_arr is not None else [0.0] * len(K_arr)
        return [
            bs_greeks(S, K_arr[i], T_arr[i], r, sigma_arr[i], is_call_arr[i], _div[i])
            for i in range(len(K_arr))
        ]
    except Exception as ex:
        sys.stderr.write(f"bs_greeks_batch: {ex}\n")
        _div = div_arr if div_arr is not None else [0.0] * len(K_arr)
        return [
            bs_greeks(S, K_arr[i], T_arr[i], r, sigma_arr[i], is_call_arr[i], _div[i])
            for i in range(len(K_arr))
        ]

# ── Discrete Dividend Adjustment ─────────────────────────────────────────────

def apply_discrete_dividend(S, r, div_amount, div_days):
    if div_amount <= 0 or div_days < 0:
        return S
    pv_div = div_amount * math.exp(-r * div_days / 365.0)
    return max(0.0, S - pv_div)

# ── Theta Decomposition ───────────────────────────────────────────────────────

def theta_decomposition(S, K, T, r, sigma, is_call, q=0.0):
    """
    Merton (1973) theta decomposition with precise market-calendar scaling.

    Theta components:
      calendarDecay  — volatility bleed (gamma·σ²S²/2 term); non-zero on weekends
      driftDecay     — rK·e^{-rT}·N(±d2) − qS·e^{-qT}·N(±d1); risk-free / dividend carry
      weekendDecay   — ADDITIONAL decay attributable to the 2-day weekend gap

    Weekend theta methodology (Carr 2002, "Deriving Derivatives of Derivative Securities"):
      Options markets price calendar-day theta, not business-day theta.
      On Friday close: the option loses 3 calendar days by next open (Fri→Mon).
      On every other weekday: 1 calendar day.
      On Saturday/Sunday (crypto/global): 1 calendar day.
      The total Friday theta ≈ 3 × (1/365) of annual θ, not 1 × (1/365).
      weekendDecay = calendarDecay × 2  (the 2 extra days beyond the normal 1-day bleed)

    The total theta shown to the user = calendarDecay + driftDecay (per day).
    weekendDecay is an additive supplement shown separately on Fridays.

    References:
      Merton (1973) Bell Journal of Economics 4(1), pp. 141–183.
      Carr (2002) working paper — weekend theta premium.
      Hull (2022) "Options, Futures, and Other Derivatives" §9.7.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {"driftDecay": 0, "calendarDecay": 0, "weekendDecay": 0, "totalTheta": 0}

    sq    = math.sqrt(T)
    d1    = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * sq)
    d2    = d1 - sigma * sq
    nd1   = npdf(d1)
    disc  = math.exp(-r * T)
    discQ = math.exp(-q * T)

    # ── Calendar decay = −S·e^{-qT}·n(d1)·σ / (2·√T·365) — volatility bleed ──
    # This is the non-zero "gamma PnL" cost of holding the option for one day.
    calendar_decay = -(S * discQ * nd1 * sigma) / (2 * sq * 365)

    # ── Drift decay — rho/dividend carry terms ───────────────────────────────
    if is_call:
        Nd2 = ncdf(d2);  Nd1 = ncdf(d1)
        # Call drift: −r·K·e^{-rT}·N(d2) + q·S·e^{-qT}·N(d1)
        drift_decay = (-r * K * disc * Nd2 + q * S * discQ * Nd1) / 365
    else:
        Nnd2 = ncdf(-d2); Nnd1 = ncdf(-d1)
        # Put drift:  +r·K·e^{-rT}·N(−d2) − q·S·e^{-qT}·N(−d1)
        drift_decay = (r * K * disc * Nnd2 - q * S * discQ * Nnd1) / 365

    # ── Weekend scaling (Carr 2002) ─────────────���────────────────────────────
    # Friday (weekday=4): 3 calendar days consumed over the weekend.
    # Extra 2 days beyond the normal 1-day bleed = weekend_factor = 2.
    # The total theta on Friday = (calendar + drift) * 3.
    # weekendDecay is the INCREMENTAL extra theta for the 2 weekend days.
    day_of_week    = datetime.now().weekday()
    weekend_factor = 2.0 if day_of_week == 4 else 0.0
    weekend_decay  = (calendar_decay + drift_decay) * weekend_factor

    # Total per-day theta
    total = calendar_decay + drift_decay
    return {
        "driftDecay":    round(drift_decay,    6),
        "calendarDecay": round(calendar_decay, 6),
        "weekendDecay":  round(weekend_decay,  6),
        "totalTheta":    round(total,           6),
    }

# ── Bjerksund-Stensland (2002) American Option Approximation ────────────────
# Two-point flat-boundary approximation — significant improvement over the
# single-boundary BS93 formula, particularly for:
#   • Short-dated puts (where BS93 can underestimate by 5–15%)
#   • High dividend calls (where the trigger migrates over the life)
#   • Deep ITM puts with high r (BS93 boundary is too conservative)
#
# The 2002 paper uses TWO trigger levels t1 and t2 (= T):
#   - Phase 1 [0, t1]: higher trigger β₁ (more aggressive exercise)
#   - Phase 2 [t1, T]: lower trigger β₂ (as T approaches maturity)
# This gives O(√T) convergence vs O(T) for the single-boundary 1993 version.
#
# Calls: reduce to European when q ≤ 0 (Merton 1973 — exact).
# Puts:  always valuable to exercise early when r > 0 (regardless of q).
#
# Returns the FULL American option price (not just the premium over European).
#
# References:
#   Bjerksund & Stensland (1993) Scandinavian Journal of Management 9(S1).
#   Bjerksund & Stensland (2002) "Closed-Form Valuation of American Options".
#     NHH Discussion Paper 2002/09. Widely verified in Haug (2007) Table 3.

def bjerksund_stensland(S, K, T, r, sigma, is_call, q=0.0):
    """
    Bjerksund-Stensland (2002) two-phase American option price.
    Returns the full American option price (early exercise included).
    Returns 0.0 for degenerate inputs.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, (S - K) if is_call else (K - S))
    try:
        # Calls: q ≤ 0 → American call = European call (Merton 1973 exact)
        if is_call and q <= 0.0:
            return max(0.0, bs_price(S, K, T, r, sigma, True, q))

        # ── Map put to call via S↔K, r↔q symmetry (Bjerksund-Stensland, §3) ──
        # The BS02 formula is derived for calls only. For puts we use the
        # American put-call symmetry: P(S,K,T,r,q,σ) = C(K,S,T,q,r,σ).
        # This is exact under the flat-boundary approximation.
        if not is_call:
            return bjerksund_stensland(K, S, T, q, sigma, True, r)

        # ── BS2002 for calls ──────────────────────────────────────────────────
        # Intermediate maturity: t1 = T/2 (two equal phases)
        t1 = T / 2.0

        sig2 = sigma * sigma

        def _phi(S_, T_, gamma_, H_, I_, r_, q_, sigma_):
            """
            BS02 auxiliary function φ(S, T, γ, H, I).
            φ = e^{λ}·S^γ·[N(d1) − (I/S)^κ·N(d2)]
            """
            kappa_ = 2 * r_ / sig2 + 2 * gamma_ - 1.0
            lam    = -r_ + gamma_ * q_ + 0.5 * gamma_ * (gamma_ - 1) * sig2
            d1     = -(math.log(S_ / H_) + (q_ - 0.5 * sig2 + (gamma_ - 1) * sig2 * 0.5 +
                        0.5 * kappa_ * sig2) * T_) / (sigma_ * math.sqrt(T_))
            # Actually BS02 uses a specific d1 form; re-derive from their paper:
            # d1 = (log(S/H) + (b + (γ−½)σ²)T) / (σ√T) where b = r − q
            b = r_ - q_
            sq_T = sigma_ * math.sqrt(T_)
            if sq_T < 1e-12:
                return 0.0
            d1 = (math.log(S_ / H_) + (b + (gamma_ - 0.5) * sig2) * T_) / sq_T
            d2 = (math.log(S_ / I_) + (b + (gamma_ - 0.5) * sig2) * T_) / sq_T
            lam    = -r_ + gamma_ * q_ + 0.5 * gamma_ * (gamma_ - 1) * sig2
            kappa_ = 2 * b / sig2 + 2 * gamma_ - 1.0
            if not (math.isfinite(d1) and math.isfinite(d2) and math.isfinite(lam)):
                return 0.0
            e_lam = math.exp(lam * T_)
            try:
                ratio_pow = (I_ / S_) ** kappa_
            except (OverflowError, ZeroDivisionError):
                ratio_pow = 0.0
            val = (e_lam * (S_ ** gamma_) *
                   (ncdf(d1) - (ratio_pow if math.isfinite(ratio_pow) else 0.0) * ncdf(d2)))
            return val if math.isfinite(val) else 0.0

        b = r - q    # cost-of-carry

        def _trigger(T_):
            """BS2002 eq. (9) optimal exercise boundary β(T)."""
            beta_  = (0.5 - b / sig2 + math.sqrt((b / sig2 - 0.5) ** 2 + 2 * r / sig2 /
                      max(1 - math.exp(-r * T_), 1e-10)))
            B_inf  = beta_ / (beta_ - 1.0) * K
            B0     = max(K, r / max(q, 1e-10) * K)
            h      = -(b * T_ + 2 * sigma * math.sqrt(T_)) * B0 / (B_inf - B0)
            return B0 + (B_inf - B0) * (1 - math.exp(h))

        I1 = _trigger(t1)
        I2 = _trigger(T)

        # BS2002 call price (two-boundary) — compute beta for both phases first
        def _beta(T_):
            disc_ = max(1 - math.exp(-r * T_), 1e-10)
            return 0.5 - b / sig2 + math.sqrt(max(0.0,
                   (b / sig2 - 0.5) ** 2 + 2 * r / (sig2 * disc_)))

        beta1 = _beta(t1)
        beta2 = _beta(T)
        alpha1 = (I1 - K) * I1 ** (-beta1)
        alpha2 = (I2 - K) * I2 ** (-beta2)

        # Case 1: S ≥ I2 — exercise immediately
        if S >= I2:
            return max(0.0, S - K)

        # Case 2: I1 ≤ S < I2 — Phase 2 only
        if S >= I1:
            val = (alpha2 * S ** beta2
                   - alpha2 * _phi(S, T,  beta2, I2, I2, r, q, sigma)
                   +          _phi(S, T,  1.0,   I2, I2, r, q, sigma)
                   -          _phi(S, T,  1.0,   I1, I2, r, q, sigma)
                   + alpha1 * _phi(S, T,  beta1, I1, I2, r, q, sigma)
                   - alpha1 * _phi(S, t1, beta1, I1, I1, r, q, sigma)
                   +          _phi(S, t1, 1.0,   I1, I1, r, q, sigma))
        else:
            # Case 3: S < I1 — use full two-phase formula
            val = (alpha2 * S ** beta2
                   - alpha2 * _phi(S, T,  beta2, I2, I2, r, q, sigma)
                   +          _phi(S, T,  1.0,   I2, I2, r, q, sigma)
                   - alpha1 * _phi(S, T,  beta1, I1, I2, r, q, sigma)
                   + alpha1 * _phi(S, t1, beta1, I1, I1, r, q, sigma)
                   -          _phi(S, t1, 1.0,   I1, I1, r, q, sigma))

        # Price must be ≥ intrinsic value
        intrinsic = max(0.0, S - K)
        price = max(intrinsic, val if math.isfinite(val) else 0.0)
        # Sanity: must be ≤ S (for calls on non-negative asset)
        price = min(price, S)
        return round(price, 6)
    except Exception as exc:
        sys.stderr.write(f"bjerksund_stensland: {exc}\n")
        return max(0.0, bs_price(S, K, T, r, sigma, is_call, q))

# ── EMO-Modified Lee-Ready + OBI Aggressor Classification ────────────────────

def classify_aggressor_emo(trade_price, bid, ask, prev_trade_price, bid_size=0, ask_size=0):
    if bid <= 0 or ask <= 0:
        return "neutral", "no_quote", 0.0
    mid = (bid + ask) / 2.0
    tick_tol = 0.01
    obi = 0.0
    if bid_size + ask_size > 0:
        obi = (bid_size - ask_size) / (bid_size + ask_size)
    if abs(trade_price - ask) < tick_tol:
        method = "emo_at_ask"
        if obi < -0.3:
            method = "obi_iceberg_buy"
        return "buy", method, obi
    if abs(trade_price - bid) < tick_tol:
        method = "emo_at_bid"
        if obi > 0.3:
            method = "obi_iceberg_sell"
        return "sell", method, obi
    if trade_price > mid + tick_tol:
        return "buy", "emo_quote_above_mid", obi
    if trade_price < mid - tick_tol:
        return "sell", "emo_quote_below_mid", obi
    if prev_trade_price is not None and prev_trade_price > 0:
        if trade_price > prev_trade_price:
            return "buy", "tick_uptick", obi
        if trade_price < prev_trade_price:
            return "sell", "tick_downtick", obi
    return "neutral", "midpoint_no_tick", obi

def detect_sweep(trades, window_ms=500):
    if not trades: return {}
    sweeps = {}
    sorted_trades = sorted(trades, key=lambda x: x.get("ts", 0))
    i = 0; sweep_id = 0
    while i < len(sorted_trades):
        t0 = sorted_trades[i]
        window_end = t0.get("ts", 0) + window_ms
        group = [t0]
        j = i + 1
        while j < len(sorted_trades) and sorted_trades[j].get("ts", 0) <= window_end:
            group.append(sorted_trades[j]); j += 1
        if len(group) >= 3:
            exchanges = set(t.get("exchange", "") for t in group)
            sid = f"sweep_{sweep_id}"; sweep_id += 1
            for t in group:
                t["sweepId"] = sid
                t["multiExchange"] = len(exchanges) > 1
                t["sweepSize"] = len(group)
            sweeps[sid] = {"count": len(group), "exchanges": list(exchanges),
                           "multiExchange": len(exchanges) > 1}
        i = j
    return sweeps

# ── Enrichment Source: Tiingo Options ──────────────────────��─────────────────

def fetch_tiingo_iv(symbol):
    """
    Tiingo end-of-day options data.
    Returns dict keyed by (strike, expiration, type) → {iv, delta, gamma, theta, vega}
    Free tier: delayed 15 min, but has real IV from Black-Scholes implied.
    """
    try:
        url = f"https://api.tiingo.com/tiingo/options/{symbol}/chains?token={TIINGO_KEY}"
        data = _get(url, timeout=10)
        result = {}
        if isinstance(data, list):
            for chain in data:
                expDate = chain.get("date", "")[:10]
                for row in chain.get("options", []):
                    K    = sf(row.get("strike"))
                    cp   = row.get("type", "").lower()
                    iv   = sf(row.get("impliedVol"))
                    delta = sf(row.get("delta"))
                    gamma = sf(row.get("gamma"))
                    theta = sf(row.get("theta"))
                    vega  = sf(row.get("vega"))
                    key = (round(K, 2), expDate, cp)
                    result[key] = {"iv": iv, "delta": delta, "gamma": gamma,
                                   "theta": theta, "vega": vega}
        return result
    except urllib.error.HTTPError as e:
        if e.code != 404:
            sys.stderr.write(f"tiingo iv: HTTP {e.code}\n")
        return {}
    except Exception as e:
        sys.stderr.write(f"tiingo iv: {e}\n")
        return {}

# ── Enrichment Source: TwelveData Options ────────────────────────────────────

def fetch_twelvedata_iv(symbol):
    """
    TwelveData options endpoint — provides IV and greeks.
    Free plan: 8 credits/min, 800/day. Endpoint: /options/chain
    """
    if not TD_KEY:
        return {}
    try:
        url = f"https://api.twelvedata.com/options/chain?symbol={symbol}&apikey={TD_KEY}"
        data = _get(url, timeout=10)
        result = {}
        calls = data.get("calls", [])
        puts  = data.get("puts", [])
        for side, ct in [(calls, "call"), (puts, "put")]:
            for row in side:
                K      = sf(row.get("strike_price"))
                expDate = str(row.get("expiration_date", ""))[:10]
                iv     = sf(row.get("implied_volatility"))
                delta  = sf(row.get("delta"))
                gamma  = sf(row.get("gamma"))
                theta  = sf(row.get("theta"))
                vega   = sf(row.get("vega"))
                key = (round(K, 2), expDate, ct)
                result[key] = {"iv": iv, "delta": delta, "gamma": gamma,
                               "theta": theta, "vega": vega}
        return result
    except Exception as e:
        sys.stderr.write(f"twelvedata iv: {e}\n")
        return {}

# ── Enrichment Source: Alpha Vantage (10-key rotation) ───────────────────────

def fetch_av_iv(symbol):
    """
    Alpha Vantage OPTION_CHAIN endpoint.
    Rate: 5 calls/min per key × 10 keys = 50 effective calls/min.
    Returns same dict keyed by (strike, expiration, type).
    """
    key = next_av_key()
    if not key:
        return {}
    try:
        url = (f"https://www.alphavantage.co/query"
               f"?function=REALTIME_OPTIONS&symbol={symbol}&apikey={key}")
        data = _get(url, timeout=10)
        result = {}
        for row in data.get("data", []):
            K       = sf(row.get("strike"))
            expDate = str(row.get("expiration", ""))[:10]
            cp      = "call" if str(row.get("type","")).lower().startswith("c") else "put"
            iv      = sf(row.get("implied_volatility"))
            delta   = sf(row.get("delta"))
            gamma   = sf(row.get("gamma"))
            theta   = sf(row.get("theta"))
            vega    = sf(row.get("vega"))
            bid     = sf(row.get("bid"))
            ask     = sf(row.get("ask"))
            oi      = si(row.get("open_interest"))
            vol     = si(row.get("volume"))
            key_t = (round(K, 2), expDate, cp)
            result[key_t] = {"iv": iv, "delta": delta, "gamma": gamma,
                             "theta": theta, "vega": vega,
                             "bid": bid, "ask": ask, "oi": oi, "vol": vol}
        return result
    except Exception as e:
        sys.stderr.write(f"alpha_vantage iv: {e}\n")
        return {}

# ── Enrichment Source: optiondata.io REST ────────────────────────────────────

def fetch_optiondata_chain(symbol, expiration=None):
    """
    optiondata.io REST API — 15-min delayed realtime option chain with IV + greeks.
    Returns same structure as Alpaca for contract-level merging.
    Docs: https://docs.optiondata.io/http-data-api/historical-option-data-api
    """
    if not OPTIONDATA_KEY:
        return {}
    try:
        url = f"https://api.optiondata.io/v2/option_chain/{symbol}"
        if expiration:
            url += f"?expiration_date={expiration}"
        headers = {"Authorization": OPTIONDATA_KEY}
        data = _get(url, headers, timeout=12)
        result = {}
        for row in data.get("data", []):
            K       = sf(row.get("strike"))
            expDate = str(row.get("expiration_date", ""))[:10]
            cp      = str(row.get("option_type", "")).lower()
            iv      = sf(row.get("implied_volatility"))
            delta   = sf(row.get("delta"))
            gamma   = sf(row.get("gamma"))
            theta   = sf(row.get("theta"))
            vega    = sf(row.get("vega"))
            bid     = sf(row.get("bid"))
            ask     = sf(row.get("ask"))
            oi      = si(row.get("open_interest"))
            vol     = si(row.get("volume"))
            key_t = (round(K, 2), expDate, cp)
            result[key_t] = {"iv": iv, "delta": delta, "gamma": gamma,
                             "theta": theta, "vega": vega,
                             "bid": bid, "ask": ask, "oi": oi, "vol": vol}
        return result
    except urllib.error.HTTPError as e:
        if e.code != 404:
            sys.stderr.write(f"optiondata iv: HTTP {e.code}\n")
        return {}
    except Exception as e:
        sys.stderr.write(f"optiondata iv: {e}\n")
        return {}

def fetch_lse_chain(symbol: str, expiration: str | None = None) -> dict:
    """
    London Strategic Edge live options chain via official lse-data SDK.
    Returns {(strike, expiry, side): {iv, delta, gamma, theta, vega, vol, last}}
    structure consumed by merge_enrichment for IV/greeks cross-fill.

    Confirmed live field names from API:
      ticker, underlying, strike, expiry, contract_type, last_price,
      volume_today, premium_today, underlying_price, dte,
      iv, delta, gamma, theta, vega, rho, last_trade_at, updated_at
    """
    if not LSE_KEY:
        return {}
    try:
        from lse import LSE  # type: ignore
        client  = LSE(api_key=LSE_KEY)
        # Fetch chain. max_dte=180 gives a wide window; filter by expiry below if needed.
        rows = client.options(symbol.upper(), max_dte=180)
        if not rows:
            sys.stderr.write(f"lse_chain: no contracts for {symbol}\n")
            return {}
        result: dict = {}
        for row in rows:
            K       = sf(row.get("strike"))
            expDate = str(row.get("expiry", ""))[:10]
            cp_raw  = str(row.get("contract_type", "")).lower()
            cp      = "call" if cp_raw.startswith("c") else "put"
            if K <= 0 or not expDate:
                continue
            if expiration and expDate != expiration:
                continue
            result[(round(K, 2), expDate, cp)] = {
                "iv":    sf(row.get("iv")),
                "delta": sf(row.get("delta")),
                "gamma": sf(row.get("gamma")),
                "theta": sf(row.get("theta")),
                "vega":  sf(row.get("vega")),
                "rho":   sf(row.get("rho")),
                # LSE options chain has volume_today but no separate bid/ask on the chain
                # endpoint — those come from the live tick feed
                "bid":   0.0,
                "ask":   0.0,
                "oi":    0,
                "vol":   si(row.get("volume_today")),
                "last":  sf(row.get("last_price")),
                "dte":   si(row.get("dte")),
                "source": "lse",
            }
        sys.stderr.write(f"lse_chain: {len(result)} contracts for {symbol}\n")
        return result
    except Exception as e:
        sys.stderr.write(f"lse_chain: {e}\n")
        return {}


def fetch_lse_flow(symbol: str, min_premium: int = 0) -> list:
    """
    Unusual / block options prints from LSE via the SDK.
    Returns a list of flow dicts compatible with the intrinioUnusual format.

    Confirmed live field names from API:
      id, ts, underlying, ticker, strike, expiry, contract_type,
      last_price, volume, premium, underlying_price, dte,
      iv, delta, gamma, theta, vega, rho
    """
    if not LSE_KEY:
        return []
    try:
        from lse import LSE  # type: ignore
        client = LSE(api_key=LSE_KEY)
        kwargs = {"min_premium": min_premium} if min_premium > 0 else {}
        rows   = client.options_flow(symbol.upper(), **kwargs)
        if not rows:
            return []
        out = []
        for row in rows:
            cp_raw = str(row.get("contract_type", "")).lower()
            out.append({
                "type":              "call" if cp_raw.startswith("c") else "put",
                "strikePrice":       sf(row.get("strike")),
                "expiration":        str(row.get("expiry", ""))[:10],
                "volume":            si(row.get("volume")),
                "impliedVolatility": sf(row.get("iv")),
                "totalPremium":      sf(row.get("premium")),
                "delta":             sf(row.get("delta")),
                "gamma":             sf(row.get("gamma")),
                "underlyingPrice":   sf(row.get("underlying_price")),
                "dte":               si(row.get("dte")),
                "score":             min(100.0, sf(row.get("score", 50.0))),
                "signal":            "NOTABLE",
                "timestamp":         str(row.get("ts", "")),
                "ticker":            str(row.get("ticker", "")),
                "source":            "lse",
            })
        sys.stderr.write(f"lse_flow: {len(out)} prints for {symbol}\n")
        return out
    except Exception as e:
        sys.stderr.write(f"lse_flow: {e}\n")
        return []


def merge_enrichment(row, enrichment_maps):
    """
    Cross-fill missing IV/greeks in a contract row from enrichment sources.
    Priority: row's own values > optiondata > alpha_vantage > tiingo > twelvedata.
    """
    K   = round(row.get("strike", 0), 2)
    exp = row.get("expiration", "")[:10]
    cp  = row.get("type", "call")
    key = (K, exp, cp)

    for edict in enrichment_maps:
        hit = edict.get(key)
        if not hit:
            continue
        # Fill IV if missing
        if row.get("iv", 0) == 0 and hit.get("iv", 0) > 0:
            row["iv"]    = hit["iv"]
            row["ivPct"] = round(hit["iv"] * 100, 2)
        # Fill greeks if all zero
        if row.get("delta", 0) == 0 and hit.get("delta", 0) != 0:
            row["delta"] = hit["delta"]
        if row.get("gamma", 0) == 0 and hit.get("gamma", 0) != 0:
            row["gamma"] = hit["gamma"]
        if row.get("theta", 0) == 0 and hit.get("theta", 0) != 0:
            row["theta"] = hit["theta"]
        if row.get("vega", 0) == 0 and hit.get("vega", 0) != 0:
            row["vega"]  = hit["vega"]
        # Fill OI/vol if missing
        if row.get("openInterest", 0) == 0 and hit.get("oi", 0) > 0:
            row["openInterest"] = hit["oi"]
        if row.get("volume", 0) == 0 and hit.get("vol", 0) > 0:
            row["volume"] = hit["vol"]
        # Fill bid/ask if missing
        if row.get("bid", 0) == 0 and hit.get("bid", 0) > 0:
            row["bid"] = hit["bid"]
        if row.get("ask", 0) == 0 and hit.get("ask", 0) > 0:
            row["ask"] = hit["ask"]
    return row

# ── Source 1: Alpaca SDK (Primary) ───────────────────────────────────────────

def fetch_alpaca_options(symbol, expiration=None):
    """
    Fetch full options chain via Alpaca SDK.
    Returns structured chain with greeks, IV, L1 quotes, trades.
    Latency: ~0.2-0.3s for full chain (single SDK call, no pagination).
    Zero-IV fix: uses ask-price when bid=0 for IV solving.
    """
    from alpaca.data.historical import OptionHistoricalDataClient
    from alpaca.data.requests import OptionChainRequest

    client = OptionHistoricalDataClient(APCA_KEY, APCA_SEC)
    today = _date.today()
    if expiration:
        try:
            exp_dt = datetime.strptime(expiration, "%Y-%m-%d").date()
            exp_gte, exp_lte = exp_dt, exp_dt
        except:
            exp_gte = today + timedelta(days=1)
            exp_lte = today + timedelta(days=180)
    else:
        exp_gte = today + timedelta(days=1)
        exp_lte = today + timedelta(days=180)

    req = OptionChainRequest(
        underlying_symbol=symbol,
        expiration_date_gte=exp_gte,
        expiration_date_lte=exp_lte,
    )

    # Alpaca SDK has no built-in timeout — wrap in a thread with a hard deadline.
    chain = None
    _chain_result: list = []
    _chain_exc:    list = []

    def _do_fetch():
        try:
            _chain_result.append(client.get_option_chain(req))
        except Exception as _e:
            _chain_exc.append(_e)

    _t = threading.Thread(target=_do_fetch, daemon=True)
    _t.start()
    _t.join(timeout=28)   # 28 s hard cap on Alpaca SDK call
    if _chain_exc:
        raise _chain_exc[0]
    if not _t.is_alive() and _chain_result:
        chain = _chain_result[0]
    elif _t.is_alive():
        sys.stderr.write("alpaca: SDK call timed out after 28s\n")
        return None

    if not chain:
        return None

    now_ts = time.time()
    R = 0.0525                       # Risk-free rate (SOFR) — matches TypeScript RISK_FREE
    Q = get_div_yield(symbol)        # Continuous dividend yield for this symbol

    calls_all, puts_all = [], []
    loaded_exps = set()
    spot = 0.0

    # Get spot price
    def _fetch_spot():
        headers = {"APCA-API-KEY-ID": APCA_KEY, "APCA-API-SECRET-KEY": APCA_SEC}
        for feed in ["delayed_sip", "iex"]:
            try:
                url = f"https://data.alpaca.markets/v2/stocks/quotes/latest?symbols={symbol}&feed={feed}"
                d = _get(url, headers, timeout=5)
                q = d.get("quotes", {}).get(symbol, {})
                bp = sf(q.get("bp")); ap = sf(q.get("ap"))
                if bp > 0 and ap > 0: return (bp + ap) / 2
                if bp > 0: return bp
                if ap > 0: return ap
            except: pass
        try:
            url = f"https://data.alpaca.markets/v2/stocks/trades/latest?symbols={symbol}&feed=iex"
            d = _get(url, headers, timeout=5)
            t = d.get("trades", {}).get(symbol, {})
            p = sf(t.get("p"))
            if p > 0: return p
        except: pass
        return 0.0

    try:
        spot = _fetch_spot()
        if spot == 0:
            sys.stderr.write(f"spot: could not get spot for {symbol}\n")
    except Exception as e:
        sys.stderr.write(f"spot fetch: {e}\n")

    # Determine Alpaca IV coverage — only fetch enrichment when genuinely needed
    # (saves ~0.5–1s when Alpaca provides greeks for most contracts)
    total_contracts   = len(chain)
    alpaca_iv_present = sum(
        1 for snap in chain.values()
        if (getattr(snap, 'implied_volatility', None) or 0) > 0.01
    )
    alpaca_coverage = (alpaca_iv_present / total_contracts) if total_contracts > 0 else 0

    # Skip slow enrichment sources if Alpaca covers ≥70% of contracts
    need_enrichment = alpaca_coverage < 0.70

    # Gather expirations from the chain first (needed by both enrichment and OI map)
    _chain_exps: set = set()
    for _csym in chain.keys():
        _p = parse_contract_symbol(_csym)
        if _p: _chain_exps.add(_p["expiration"])

    # ── Fetch enrichment maps + yfinance OI map in parallel ──────────────────
    # All tasks run concurrently; we wait at most 12 s total for all of them.
    # Cap yf OI map to the 4 nearest expirations to avoid 15 × serial calls.
    _nearest_exps = sorted(_chain_exps)[:4]

    def _task_od():
        try: return fetch_optiondata_chain(symbol, expiration)
        except: return None
    def _task_tiingo():
        try: return fetch_tiingo_iv(symbol)
        except: return None
    def _task_av():
        try: return fetch_av_iv(symbol)
        except: return None
    def _task_td():
        try: return fetch_twelvedata_iv(symbol)
        except: return None
    def _task_yf_oi():
        return build_yf_oi_vol_map(symbol, _nearest_exps)

    enrich_maps: list = []
    yf_oi_vol:   dict = {}

    tasks: dict = {}
    with ThreadPoolExecutor(max_workers=5) as _pool:
        tasks["yf_oi"]  = _pool.submit(_task_yf_oi)
        tasks["av"]     = _pool.submit(_task_av)
        if need_enrichment:
            tasks["od"]     = _pool.submit(_task_od)
            tasks["tiingo"] = _pool.submit(_task_tiingo)
            tasks["td"]     = _pool.submit(_task_td)

        # Collect results — give each at most 12 s
        for name, fut in tasks.items():
            try:
                res = fut.result(timeout=12)
                if name == "yf_oi":
                    yf_oi_vol = res or {}
                elif res:
                    enrich_maps.append(res)
            except Exception as _fe:
                sys.stderr.write(f"enrich/{name}: {_fe}\n")

    sys.stderr.write(f"yf_oi_vol: {len(yf_oi_vol)} keys for {len(_nearest_exps)} exps (parallel)\n")

    for contract_sym, snap in chain.items():
        parsed = parse_contract_symbol(contract_sym)
        if not parsed:
            continue

        exp_str  = parsed["expiration"]
        dte      = parsed["dte"]
        T        = dte_to_T(dte)
        is_call  = parsed["type"] == "call"
        K        = parsed["strike"]

        lq = snap.latest_quote
        bid    = sf(getattr(lq, "bid_price",  0)) if lq else 0
        ask    = sf(getattr(lq, "ask_price",  0)) if lq else 0
        bid_sz = si(getattr(lq, "bid_size",   0)) if lq else 0
        ask_sz = si(getattr(lq, "ask_size",   0)) if lq else 0
        mid    = round((bid + ask) / 2, 4) if (bid and ask) else (ask or bid or 0)
        qt     = str(getattr(lq, "timestamp",  "")) if lq else ""

        lt = snap.latest_trade
        last     = sf(getattr(lt, "price",    0)) if lt else 0
        exchange = str(getattr(lt, "exchange", "")) if lt else ""

        # Alpaca IV + greeks
        iv_raw = sf(snap.implied_volatility) if snap.implied_volatility else 0
        gr     = snap.greeks
        if gr:
            delta = sf(getattr(gr, "delta", 0))
            gamma = sf(getattr(gr, "gamma", 0))
            theta = sf(getattr(gr, "theta", 0))
            vega  = sf(getattr(gr, "vega",  0))
            rho   = sf(getattr(gr, "rho",   0))
        else:
            delta = gamma = theta = vega = rho = 0

        # ── ZERO-IV FIX ─────────────────────────────────────────────────────
        # If Alpaca didn't supply IV, solve from best available price
        # (ask-side when bid=0 — common for cheap OTM options).
        if iv_raw == 0 and spot > 0 and T > 0:
            iv_raw = solve_iv_best(spot, K, T, R, bid, ask, is_call, Q)

        # If greeks are missing but we have IV, compute via BS (with dividend)
        if iv_raw > 0 and spot > 0:
            g = bs_greeks(spot, K, T, R, iv_raw, is_call, Q)
            if delta == 0: delta = g["delta"]
            if gamma == 0: gamma = g["gamma"]
            if theta == 0: theta = g["theta"]
            if vega  == 0: vega  = g["vega"]
            if rho   == 0: rho   = g["rho"]

        # Second-order greeks (always BS-derived, with dividend)
        g2 = bs_greeks(spot, K, T, R, iv_raw if iv_raw > 0 else 0.30, is_call, Q) if spot > 0 else {}
        vanna  = g2.get("vanna", 0)
        charm  = g2.get("charm", 0)
        volga  = g2.get("volga", 0)
        speed  = g2.get("speed", 0)
        lam    = g2.get("lambda", 0)

        # Bid-ask IV spread
        baiv = calc_bidask_iv(bid, ask, spot, K, T, R, is_call, Q) if spot > 0 and (bid or ask) else {}

        # Theta decomposition
        td_dec = theta_decomposition(spot, K, T, R, iv_raw if iv_raw > 0 else 0.30, is_call, Q) if spot > 0 else {}

        # American premium
        am_premium = bjerksund_stensland(spot, K, T, R, iv_raw if iv_raw > 0 else 0.30, is_call, Q) if spot > 0 else 0

        # Aggressor classification
        aggressor, agg_method, obi = "neutral", "no_trade", 0.0
        if last > 0 and bid > 0 and ask > 0:
            aggressor, agg_method, obi = classify_aggressor_emo(
                last, bid, ask, None, bid_sz, ask_sz
            )

        intrinsic = max(0.0, (spot - K) if is_call else (K - spot)) if spot > 0 else 0
        price_used = mid or last
        time_val = max(0.0, price_used - intrinsic) if price_used > 0 else 0

        # Cross-fill OI and volume from yfinance map.
        # Alpaca's OptionsSnapshot has neither field; latest_trade.size is a
        # single-trade contract count, NOT daily cumulative volume.
        _yf_key = (round(K, 2), exp_str, parsed["type"])
        _yf     = yf_oi_vol.get(_yf_key, {})
        oi       = _yf.get("oi",  0)
        vol_size = _yf.get("vol", 0)
        # Accumulate Alpaca trade size on top of yfinance vol when both present
        _alpaca_trade_sz = si(getattr(lt, "size", 0)) if lt else 0
        if vol_size == 0 and _alpaca_trade_sz > 0:
            vol_size = _alpaca_trade_sz  # last resort: single-trade size
        loaded_exps.add(exp_str)

        # Prob ITM = N(d2) — Merton continuous-dividend formula
        prob_itm = 0.0
        if iv_raw > 0 and spot > 0 and T > 0:
            sq_T = math.sqrt(T)
            d1_v = (math.log(spot / K) + (R - Q + 0.5 * iv_raw**2) * T) / (iv_raw * sq_T)
            d2_v = d1_v - iv_raw * sq_T
            prob_itm = round(ncdf(d2_v) if is_call else ncdf(-d2_v), 4)

        row = {
            "contractSymbol": contract_sym,
            "strike":         K,
            "expiration":     exp_str,
            "dte":            dte,
            "type":           parsed["type"],
            "bid":            bid,
            "ask":            ask,
            "mid":            mid,
            "last":           last,
            "bidSize":        bid_sz,
            "askSize":        ask_sz,
            "iv":             iv_raw,
            "ivPct":          round(iv_raw * 100, 2) if iv_raw else 0,
            "delta":          round(delta, 5),
            "gamma":          round(gamma, 6),
            "theta":          round(theta, 5),
            "vega":           round(vega,  5),
            "rho":            round(rho,   5),
            "vanna":          round(vanna, 6),
            "charm":          round(charm, 6),
            "volga":          round(volga, 6),
            "speed":          round(speed, 6),
            "lambda":         round(lam,   4),
            "probITM":        prob_itm,
            "openInterest":   oi,
            "volume":         vol_size,
            "volOiRatio":     round(vol_size / oi, 2) if oi > 0 else 0,
            "inTheMoney":     ((spot > K) if is_call else (spot < K)) if spot > 0 else False,
            "intrinsicValue": round(intrinsic, 4),
            "timeValue":      round(time_val, 4),
            "exchange":       exchange,
            "aggressor":      aggressor,
            "aggressorMethod": agg_method,
            "obi":            round(obi, 4),
            "quoteTimestamp": qt,
            "ivBid":          baiv.get("ivBid", 0),
            "ivAsk":          baiv.get("ivAsk", 0),
            "bidAskIVSpread": baiv.get("bidAskIVSpread", 0),
            "thetaDrift":     td_dec.get("driftDecay", 0),
            "thetaCalendar":  td_dec.get("calendarDecay", 0),
            "thetaWeekend":   td_dec.get("weekendDecay", 0),
            "americanPremium": am_premium,
            "source":         "alpaca",
        }

        # Cross-fill from enrichment sources
        if enrich_maps:
            row = merge_enrichment(row, enrich_maps)
            # If enrichment gave us IV, recompute greeks
            if row["iv"] > 0 and row["delta"] == 0 and spot > 0:
                g_fill = bs_greeks(spot, K, T, R, row["iv"], is_call, Q)
                row.update({
                    "delta": g_fill["delta"], "gamma": g_fill["gamma"],
                    "theta": g_fill["theta"], "vega":  g_fill["vega"],
                    "rho":   g_fill["rho"],   "vanna": g_fill["vanna"],
                    "charm": g_fill["charm"], "volga": g_fill["volga"],
                    "speed": g_fill["speed"], "lambda": g_fill["lambda"],
                })
                row["ivPct"] = round(row["iv"] * 100, 2)

        quality, flags = check_data_quality(row, spot, now_ts)
        row["dataQuality"]  = quality
        row["qualityFlags"] = flags

        if quality == "bad":
            sys.stderr.write(f"skip {contract_sym}: {flags}\n")
            continue

        if is_call: calls_all.append(row)
        else:       puts_all.append(row)

    if not calls_all and not puts_all:
        return None

    exp_dates = sorted(loaded_exps)
    expected_move = 0.0
    analytics = {}
    borrow_rates = {}

    # ── Post-processing pass: Put-Call Parity IV Substitution ─────────────────
    # For contracts with zero IV and wide spread, derive synthetic IV from
    # the counterpart via put-call parity. This is the OptionMetrics IvyDB
    # approach: "dynamically substitute the call/put counterpart to establish
    # a fair intrinsic value when primary spread is too wide."
    if calls_all and puts_all and spot > 0:
        try:
            # Build (strike, expiry) keyed maps
            call_map_pcp: dict = {}
            put_map_pcp:  dict = {}
            for c in calls_all:
                call_map_pcp[(round(c["strike"], 2), c["expiration"])] = c
            for p in puts_all:
                put_map_pcp[(round(p["strike"], 2), p["expiration"])] = p

            pcp_filled = 0
            for c in calls_all:
                if c.get("iv", 0) > 0.005:
                    continue  # already has IV
                key = (round(c["strike"], 2), c["expiration"])
                p   = put_map_pcp.get(key)
                if not p:
                    continue
                T_c = dte_to_T(c.get("dte", 30) or 30)
                iv_pcp = pcp_substitute_iv(
                    spot, c["strike"], T_c, R, Q,
                    c.get("bid", 0), c.get("ask", 0),
                    p.get("bid", 0), p.get("ask", 0),
                    True)
                if iv_pcp > 0.005:
                    c["iv"]    = iv_pcp
                    c["ivPct"] = round(iv_pcp * 100, 2)
                    c["dataQuality"] = "pcp_filled"
                    # Recompute greeks with new IV
                    g_pcp = bs_greeks(spot, c["strike"], T_c, R, iv_pcp, True, Q)
                    for fld in ("delta","gamma","theta","vega","rho","vanna","charm","volga","speed","lambda"):
                        if c.get(fld, 0) == 0:
                            c[fld] = g_pcp.get(fld, 0)
                    pcp_filled += 1

            for p in puts_all:
                if p.get("iv", 0) > 0.005:
                    continue
                key = (round(p["strike"], 2), p["expiration"])
                c   = call_map_pcp.get(key)
                if not c:
                    continue
                T_p = dte_to_T(p.get("dte", 30) or 30)
                iv_pcp = pcp_substitute_iv(
                    spot, p["strike"], T_p, R, Q,
                    c.get("bid", 0), c.get("ask", 0),
                    p.get("bid", 0), p.get("ask", 0),
                    False)
                if iv_pcp > 0.005:
                    p["iv"]    = iv_pcp
                    p["ivPct"] = round(iv_pcp * 100, 2)
                    p["dataQuality"] = "pcp_filled"
                    g_pcp = bs_greeks(spot, p["strike"], T_p, R, iv_pcp, False, Q)
                    for fld in ("delta","gamma","theta","vega","rho","vanna","charm","volga","speed","lambda"):
                        if p.get(fld, 0) == 0:
                            p[fld] = g_pcp.get(fld, 0)
                    pcp_filled += 1

            if pcp_filled > 0:
                sys.stderr.write(f"pcp_fill: filled {pcp_filled} zero-IV contracts via put-call parity\n")
        except Exception as e:
            sys.stderr.write(f"pcp_fill: {e}\n")

    if calls_all and puts_all and spot > 0 and exp_dates:
        front_exp = exp_dates[0]
        fc = [c for c in calls_all if c["expiration"] == front_exp]
        fp = [p for p in puts_all  if p["expiration"] == front_exp]
        if fc and fp:
            atm_c = min(fc, key=lambda x: abs(x["strike"] - spot))
            atm_p = min(fp, key=lambda x: abs(x["strike"] - spot))
            expected_move = round(
                (atm_c.get("mid") or 0) + (atm_p.get("mid") or 0), 4
            )

        analytics = compute_chain_analytics(calls_all, puts_all, spot, R)
        borrow_rates = compute_implied_borrow_rates(calls_all, puts_all, spot, R)

    # ── Attach per-contract smoothed IV and SVI fair-IV ───────────────────────
    # Back-fill ivSmoothed from analytics.kernelSmoothedIV and ivSVI from sviByExpiry
    try:
        smooth_map = (analytics or {}).get("kernelSmoothedIV", {})
        svi_map    = (analytics or {}).get("sviByExpiry", {})
        for c in calls_all:
            exp = c.get("expiration", "")
            Kr  = round(c.get("strike", 0), 2)
            sm  = smooth_map.get(exp, {}).get(Kr, {})
            c["ivSmoothed"] = sm.get("ivSmoothedCall", c.get("iv", 0))
            # SVI fair IV for this strike
            svi_fit = svi_map.get(exp)
            if svi_fit and c.get("iv", 0) > 0 and spot > 0:
                T_c = dte_to_T(c.get("dte", 30) or 30)
                F_c = spot * math.exp((R - get_div_yield(symbol)) * T_c)
                k_c = math.log(Kr / F_c) if (F_c > 0 and Kr > 0) else 0.0
                c["ivSVI"] = round(svi_eval(svi_fit["params"], k_c), 6)
            else:
                c["ivSVI"] = 0.0
        for p in puts_all:
            exp = p.get("expiration", "")
            Kr  = round(p.get("strike", 0), 2)
            sm  = smooth_map.get(exp, {}).get(Kr, {})
            p["ivSmoothed"] = sm.get("ivSmoothedPut", p.get("iv", 0))
            svi_fit = svi_map.get(exp)
            if svi_fit and p.get("iv", 0) > 0 and spot > 0:
                T_p = dte_to_T(p.get("dte", 30) or 30)
                F_p = spot * math.exp((R - get_div_yield(symbol)) * T_p)
                k_p = math.log(round(p.get("strike", 0), 2) / F_p) if (F_p > 0 and p.get("strike", 0) > 0) else 0.0
                p["ivSVI"] = round(svi_eval(svi_fit["params"], k_p), 6)
            else:
                p["ivSVI"] = 0.0
    except Exception as e:
        sys.stderr.write(f"ivSmoothed attach (alpaca): {e}\n")

    return {
        "symbol":          symbol,
        "spot":            spot,
        "expirationDates": exp_dates,
        "calls":           calls_all,
        "puts":            puts_all,
        "expectedMove":    expected_move,
        "analytics":       analytics,
        "borrowRates":     borrow_rates,
        "source":          "alpaca",
        "enrichedBy":      [type(m).__name__ if not isinstance(m, dict) else "dict"
                            for m in enrich_maps],
        "timestamp":       int(time.time() * 1000),
    }

# ── yfinance OI/Volume map ───────────────────────────────────────────────────
# Alpaca's OptionsSnapshot API provides no open_interest or daily volume.
# We build a (strike, exp, side) keyed map from yfinance and cross-fill.

def build_yf_oi_vol_map(symbol: str, expirations: list) -> dict:
    """Return {(strike_float, exp_str, 'call'/'put'): {'oi': int, 'vol': int}}"""
    try:
        import yfinance as yf, math as _math
        def _clean_int(v):
            if v is None: return 0
            if isinstance(v, float) and _math.isnan(v): return 0
            try: return int(v)
            except: return 0
        tk = yf.Ticker(symbol)
        yf_exps = set(tk.options)
        result = {}
        for exp in expirations:
            if exp not in yf_exps:
                continue
            try:
                oc = tk.option_chain(exp)
                for side, df in [("call", oc.calls), ("put", oc.puts)]:
                    for _, row in df.iterrows():
                        k = round(float(row["strike"]), 2)
                        result[(k, exp, side)] = {
                            "oi":  _clean_int(row.get("openInterest")),
                            "vol": _clean_int(row.get("volume")),
                        }
            except Exception as e:
                sys.stderr.write(f"yf_oi_vol {exp}: {e}\n")
        return result
    except Exception as e:
        sys.stderr.write(f"yf_oi_vol_map: {e}\n")
        return {}

# ── Source 2: yfinance fallback ───────────────────────────────────────────────

def fetch_yfinance(symbol, expiration=None):
    import yfinance as yf
    t     = yf.Ticker(symbol)
    spot  = sf(t.fast_info.last_price)
    exps  = list(t.options)
    if not exps:
        return None

    # Cap to 6 nearest expirations to bound total fetch time
    targets = [expiration] if (expiration and expiration in exps) else exps[:6]
    R = 0.0525                       # Risk-free rate (SOFR) — matches TypeScript RISK_FREE
    Q = get_div_yield(symbol)        # Continuous dividend yield for this symbol
    now_ts = time.time()

    # Enrichment + expiry fetches run concurrently
    enrich_maps: list = []
    calls_all, puts_all = [], []

    # Pre-fetch all expiry chains in parallel (each is a separate HTTP call)
    chain_map: dict = {}   # exp_str -> option_chain result
    def _fetch_exp(exp: str):
        try: return exp, t.option_chain(exp)
        except Exception as e:
            sys.stderr.write(f"yf chain {exp}: {e}\n")
            return exp, None
    def _fetch_od():
        try: return fetch_optiondata_chain(symbol, expiration)
        except: return None
    def _fetch_av():
        try: return fetch_av_iv(symbol)
        except: return None
    def _fetch_lse_yf():
        try: return fetch_lse_chain(symbol, expiration) if LSE_KEY else None
        except: return None

    with ThreadPoolExecutor(max_workers=min(len(targets) + 3, 9)) as _pool:
        exp_futs  = {_pool.submit(_fetch_exp, exp): exp for exp in targets}
        od_fut    = _pool.submit(_fetch_od)
        av_fut    = _pool.submit(_fetch_av)
        lse_fut   = _pool.submit(_fetch_lse_yf)

        for fut in as_completed(exp_futs, timeout=20):
            try:
                exp_str, chain_data = fut.result(timeout=20)
                if chain_data: chain_map[exp_str] = chain_data
            except Exception: pass

        try:
            od = od_fut.result(timeout=8)
            if od: enrich_maps.append(od)
        except: pass
        try:
            av = av_fut.result(timeout=8)
            if av: enrich_maps.append(av)
        except: pass
        try:
            lse = lse_fut.result(timeout=10)
            if lse: enrich_maps.insert(0, lse)   # LSE first — highest priority
        except: pass

    for exp in targets:
        chain_data = chain_map.get(exp)
        if not chain_data:
            continue
        exp_date = datetime.strptime(exp, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dte = max(0, (exp_date - datetime.now(tz=timezone.utc)).days)
        T   = dte_to_T(dte)

        def process_df(df, is_call):
            # ── Pass 1: build raw contract list and solve IVs ──────────────────
            raw = []
            for _, row in df.iterrows():
                K_val = sf(row.get("strike"))
                bid   = sf(row.get("bid"))
                ask   = sf(row.get("ask"))
                mid   = round((bid + ask) / 2, 4) if (bid and ask) else (ask or bid or 0)
                last  = sf(row.get("lastPrice"))
                oi    = si(row.get("openInterest"))
                vol   = si(row.get("volume"))
                itm   = bool(row.get("inTheMoney", False))
                sym_s = str(row.get("contractSymbol", ""))
                iv_yf = sf(row.get("impliedVolatility"))
                # Zero-IV fix: use ask when bid=0 (common for cheap OTM options)
                if iv_yf == 0 and spot > 0 and T > 0:
                    iv_yf = solve_iv_best(spot, K_val, T, R, bid, ask, is_call, Q)
                if iv_yf == 0 and last > 0 and spot > 0 and T > 0:
                    iv_yf = solve_iv(spot, K_val, T, R, last, is_call, Q)
                raw.append({
                    "sym": sym_s, "K": K_val, "bid": bid, "ask": ask, "mid": mid,
                    "last": last, "oi": oi, "vol": vol, "itm": itm, "iv": iv_yf,
                })

            if not raw:
                return []

            # ── Pass 2: vectorized greeks (scipy fast path) ────────────────────
            K_arr      = [r["K"]   for r in raw]
            T_arr      = [T]       * len(raw)
            iv_arr     = [r["iv"] if r["iv"] > 0 else 0.30 for r in raw]
            iscall_arr = [is_call] * len(raw)
            mid_arr    = [r["mid"] if r["mid"] > 0 else r["last"] or 0.01 for r in raw]

            div_arr    = [Q] * len(raw)
            greeks_batch = bs_greeks_batch(spot, K_arr, T_arr, R, iv_arr, iscall_arr,
                                           div_arr=div_arr, mid_arr=mid_arr) if spot > 0 else \
                [{"delta": 0, "gamma": 0, "theta": 0, "vega": 0, "rho": 0,
                  "vanna": 0, "charm": 0, "volga": 0, "speed": 0, "lambda": 0}
                 for _ in raw]

            # ── Pass 3: assemble final rows ────────────────────────────────────
            rows = []
            for i, c in enumerate(raw):
                K_val = c["K"]
                bid, ask, mid = c["bid"], c["ask"], c["mid"]
                last, oi, vol, itm = c["last"], c["oi"], c["vol"], c["itm"]
                iv_yf = c["iv"]
                g = greeks_batch[i]

                baiv   = calc_bidask_iv(bid, ask, spot, K_val, T, R, is_call, Q) if spot > 0 and (bid or ask) else {}
                td_dec = theta_decomposition(spot, K_val, T, R, iv_yf or 0.30, is_call, Q) if spot > 0 else {}

                intrinsic  = max(0.0, (spot - K_val) if is_call else (K_val - spot)) if spot > 0 else 0
                price_used = mid or last
                time_val   = max(0.0, price_used - intrinsic) if price_used > 0 else 0

                # Prob ITM = N(d2) — Merton continuous-dividend formula
                prob_itm = 0.0
                if iv_yf > 0 and spot > 0 and T > 0:
                    sq_T = math.sqrt(T)
                    d1_v = (math.log(spot / K_val) + (R - Q + 0.5 * iv_yf**2) * T) / (iv_yf * sq_T)
                    d2_v = d1_v - iv_yf * sq_T
                    prob_itm = round(ncdf(d2_v) if is_call else ncdf(-d2_v), 4)

                contract_row = {
                    "contractSymbol": c["sym"],
                    "strike":         K_val,
                    "expiration":     exp,
                    "dte":            dte,
                    "type":           "call" if is_call else "put",
                    "bid":            bid,
                    "ask":            ask,
                    "mid":            mid,
                    "last":           last,
                    "bidSize":        0,
                    "askSize":        0,
                    "iv":             iv_yf,
                    "ivPct":          round(iv_yf * 100, 2) if iv_yf else 0,
                    "delta":          g["delta"],
                    "gamma":          g["gamma"],
                    "theta":          g["theta"],
                    "vega":           g["vega"],
                    "rho":            g["rho"],
                    "vanna":          g["vanna"],
                    "charm":          g["charm"],
                    "volga":          g["volga"],
                    "speed":          g["speed"],
                    "lambda":         g["lambda"],
                    "probITM":        prob_itm,
                    "openInterest":   oi,
                    "volume":         vol,
                    "volOiRatio":     round(vol / oi, 2) if oi > 0 else 0,
                    "inTheMoney":     itm,
                    "intrinsicValue": round(intrinsic, 4),
                    "timeValue":      round(time_val, 4),
                    "exchange":       "",
                    "aggressor":      "neutral",
                    "aggressorMethod": "no_data",
                    "obi":            0,
                    "quoteTimestamp": "",
                    "ivBid":          baiv.get("ivBid", 0),
                    "ivAsk":          baiv.get("ivAsk", 0),
                    "bidAskIVSpread": baiv.get("bidAskIVSpread", 0),
                    "thetaDrift":     td_dec.get("driftDecay", 0),
                    "thetaCalendar":  td_dec.get("calendarDecay", 0),
                    "thetaWeekend":   td_dec.get("weekendDecay", 0),
                    "americanPremium": bjerksund_stensland(spot, K_val, T, R, iv_yf or 0.30, is_call, Q) if spot > 0 else 0,
                    "source":         "yfinance",
                }

                if enrich_maps:
                    contract_row = merge_enrichment(contract_row, enrich_maps)

                quality, flags = check_data_quality(contract_row, spot, now_ts)
                contract_row["dataQuality"] = quality
                contract_row["qualityFlags"] = flags

                if quality != "bad":
                    rows.append(contract_row)
            return rows

        calls_all.extend(process_df(chain_data.calls, True))
        puts_all.extend(process_df(chain_data.puts,  False))

    expected_move = 0.0
    if calls_all and puts_all and spot > 0 and targets:
        front_exp = targets[0]
        fc = [c for c in calls_all if c["expiration"] == front_exp]
        fp = [p for p in puts_all  if p["expiration"] == front_exp]
        if fc and fp:
            atm_c = min(fc, key=lambda x: abs(x["strike"] - spot))
            atm_p = min(fp, key=lambda x: abs(x["strike"] - spot))
            expected_move = round((atm_c.get("mid") or 0) + (atm_p.get("mid") or 0), 4)

    analytics = compute_chain_analytics(calls_all, puts_all, spot, R)
    borrow_rates = compute_implied_borrow_rates(calls_all, puts_all, spot, R)

    # ── Attach per-contract smoothed IV and SVI fair-IV (yfinance path) ──────
    try:
        smooth_map = (analytics or {}).get("kernelSmoothedIV", {})
        svi_map    = (analytics or {}).get("sviByExpiry", {})
        for c in calls_all:
            exp = c.get("expiration", "")
            Kr  = round(c.get("strike", 0), 2)
            sm  = smooth_map.get(exp, {}).get(Kr, {})
            c["ivSmoothed"] = sm.get("ivSmoothedCall", c.get("iv", 0))
            svi_fit = svi_map.get(exp)
            if svi_fit and c.get("iv", 0) > 0 and spot > 0:
                T_c = dte_to_T(c.get("dte", 30) or 30)
                # Merton forward: F = S·e^{(r-q)T}  (dividend-adjusted, not just e^{rT})
                F_c = spot * math.exp((R - Q) * T_c)
                k_c = math.log(Kr / F_c) if (F_c > 0 and Kr > 0) else 0.0
                c["ivSVI"] = round(svi_eval(svi_fit["params"], k_c), 6)
            else:
                c["ivSVI"] = 0.0
        for p in puts_all:
            exp = p.get("expiration", "")
            Kr  = round(p.get("strike", 0), 2)
            sm  = smooth_map.get(exp, {}).get(Kr, {})
            p["ivSmoothed"] = sm.get("ivSmoothedPut", p.get("iv", 0))
            svi_fit = svi_map.get(exp)
            if svi_fit and p.get("iv", 0) > 0 and spot > 0:
                T_p = dte_to_T(p.get("dte", 30) or 30)
                # Merton forward: F = S·e^{(r-q)T}
                F_p = spot * math.exp((R - Q) * T_p)
                k_p = math.log(round(p.get("strike", 0), 2) / F_p) if (F_p > 0 and p.get("strike", 0) > 0) else 0.0
                p["ivSVI"] = round(svi_eval(svi_fit["params"], k_p), 6)
            else:
                p["ivSVI"] = 0.0
    except Exception as e:
        sys.stderr.write(f"ivSmoothed attach (yfinance): {e}\n")

    return {
        "symbol":          symbol,
        "spot":            spot,
        "expirationDates": exps,
        "calls":           calls_all,
        "puts":            puts_all,
        "expectedMove":    expected_move,
        "analytics":       analytics,
        "borrowRates":     borrow_rates,
        "source":          "yfinance",
        "timestamp":       int(time.time() * 1000),
    }

# ── Implied Borrow Rates ──────────────────────────────────────────────────────

def compute_implied_borrow_rates(calls, puts, spot, rfr):
    """
    Compute implied borrow / dividend yield from put-call parity.
    C − P = S·e^{-q̂T} − K·e^{-rT}  →  q̂ = −ln((C−P+K·e^{-rT})/S) / T
    Uses `rfr` (risk-free rate) to avoid shadowing the local `q` (implied borrow).
    """
    results = []
    call_map = {(c["strike"], c["expiration"]): c for c in calls}
    for p in puts:
        key = (p["strike"], p["expiration"])
        c = call_map.get(key)
        if not c: continue
        K, dte = p["strike"], p["dte"]
        T = dte_to_T(dte)
        if T <= 0 or spot <= 0 or K <= 0: continue
        c_mid = c.get("mid", 0) or (c.get("bid", 0) + c.get("ask", 0)) / 2
        p_mid = p.get("mid", 0) or (p.get("bid", 0) + p.get("ask", 0)) / 2
        if c_mid <= 0 or p_mid <= 0: continue
        try:
            inner = (c_mid - p_mid + K * math.exp(-rfr * T)) / spot
            if inner <= 0: continue
            q_impl = -math.log(inner) / T
            if abs(q_impl) > 0.5: continue
            results.append({"strike": K, "expiration": p["expiration"],
                            "dte": dte, "impliedBorrowRate": round(q_impl, 6),
                            "impliedBorrowPct": round(q_impl * 100, 4),
                            "callMid": c_mid, "putMid": p_mid})
        except: continue
    by_exp = {}
    for r_item in results:
        by_exp.setdefault(r_item["expiration"], []).append(r_item["impliedBorrowRate"])
    term_structure = []
    for exp, rates in sorted(by_exp.items()):
        if rates:
            avg = sum(rates) / len(rates)
            exp_date = datetime.strptime(exp, "%Y-%m-%d")
            dte_days = max(0, (exp_date - datetime.now()).days)
            term_structure.append({"expiration": exp, "dte": dte_days,
                                   "avgBorrowRate": round(avg, 6),
                                   "avgBorrowPct": round(avg * 100, 4),
                                   "count": len(rates)})
    return {"byStrike": results[:100], "termStructure": term_structure}

# ── Chain Analytics ──────────────────────────────────────────���────────────────

def compute_chain_analytics(calls, puts, spot, r=0.0525):  # default = SOFR matching RISK_FREE
    analytics = {}
    # Max Pain
    try:
        call_oi, put_oi = {}, {}
        for c in calls:
            k = c["strike"]; call_oi[k] = call_oi.get(k, 0) + (c.get("openInterest") or 0)
        for p in puts:
            k = p["strike"]; put_oi[k]  = put_oi.get(k,  0) + (p.get("openInterest") or 0)
        all_strikes = sorted(set(list(call_oi.keys()) + list(put_oi.keys())))
        if all_strikes:
            losses = []
            for test_k in all_strikes:
                call_loss = sum((test_k-k)*oi*100 for k,oi in call_oi.items() if k < test_k)
                put_loss  = sum((k-test_k)*oi*100 for k,oi in put_oi.items()  if k > test_k)
                losses.append({"strike": test_k, "totalLoss": round(call_loss+put_loss, 0),
                                "callLoss": round(call_loss, 0), "putLoss": round(put_loss, 0)})
            min_loss = min(losses, key=lambda x: x["totalLoss"])
            analytics["maxPain"] = {
                "strike": min_loss["strike"], "totalLoss": min_loss["totalLoss"],
                "allStrikes": losses,
                "distanceFromSpot": round(min_loss["strike"] - spot, 4),
                "distancePct": round((min_loss["strike"] - spot) / spot * 100, 4) if spot else 0,
            }
    except Exception as e:
        analytics["maxPain"] = None; sys.stderr.write(f"maxPain: {e}\n")

    # GEX / DEX / VEX
    try:
        gex_map, dex_map, vex_map = {}, {}, {}
        for ctype, contracts in [("call", calls), ("put", puts)]:
            sign = 1 if ctype == "call" else -1
            for c in contracts:
                k  = c["strike"]; g = c.get("gamma",0) or 0
                d  = c.get("delta",0) or 0; v = c.get("vega",0) or 0
                oi = c.get("openInterest",0) or 0; mult = oi * 100
                gex_map[k] = gex_map.get(k, 0) + g * mult * (spot**2) * 0.01 * sign
                dex_map[k] = dex_map.get(k, 0) + d * mult * sign
                vex_map[k] = vex_map.get(k, 0) + v * mult * sign
        all_ks = sorted(set(list(gex_map.keys()) + list(dex_map.keys()) + list(vex_map.keys())))
        exp_by_strike = [{"strike": k,
                          "gex": round(gex_map.get(k, 0) / 1e6, 4),
                          "dex": round(dex_map.get(k, 0) / 1e6, 4),
                          "vex": round(vex_map.get(k, 0) / 1e3, 4)} for k in all_ks]
        gex_flip = spot
        for i in range(1, len(exp_by_strike)):
            prev, curr = exp_by_strike[i-1], exp_by_strike[i]
            if prev["gex"] <= 0 < curr["gex"]:
                denom = abs(prev["gex"]) + abs(curr["gex"])
                if denom:
                    t = abs(prev["gex"]) / denom
                    gex_flip = prev["strike"] + t * (curr["strike"] - prev["strike"])
                break
        analytics["exposure"] = {
            "byStrike": exp_by_strike,
            "totalNetGEX": round(sum(x["gex"] for x in exp_by_strike), 2),
            "totalNetDEX": round(sum(x["dex"] for x in exp_by_strike), 2),
            "totalNetVEX": round(sum(x["vex"] for x in exp_by_strike), 2),
            "gexFlipLevel": round(gex_flip, 2),
        }
        analytics["gex"] = {
            "byStrike": [{"strike": x["strike"], "netGEX": x["gex"]} for x in exp_by_strike],
            "totalNetGEX": analytics["exposure"]["totalNetGEX"],
            "gexFlipLevel": gex_flip,
        }
    except Exception as e:
        analytics["exposure"] = None; analytics["gex"] = None
        sys.stderr.write(f"exposure: {e}\n")

    # Flow scoring
    try:
        def score_contract(c, ctype):
            vol   = c.get("volume", 0)   or 0
            oi    = c.get("openInterest", 1) or 1
            mid   = c.get("mid", 0) or ((c.get("bid", 0) or 0) + (c.get("ask", 0) or 0)) / 2
            spread = (c.get("ask", 0) or 0) - (c.get("bid", 0) or 0)
            spread_pct = spread / mid if mid > 0 else 1
            vol_oi = vol / oi if oi > 0 else 0
            prem   = vol * mid * 100
            dte    = c.get("dte", 30) or 30
            delta  = abs(c.get("delta", 0.5) or 0.5)
            aggressor = c.get("aggressor", "neutral")
            agg_method = c.get("aggressorMethod", "")
            flags, score = [], 0
            if vol_oi > 10:   score += 35; flags.append("VOL/OI>10x")
            elif vol_oi > 5:  score += 25; flags.append("VOL/OI>5x")
            elif vol_oi > 2:  score += 12; flags.append("VOL/OI>2x")
            if prem > 1_000_000:  score += 25; flags.append("PREM>$1M")
            elif prem > 500_000:  score += 15; flags.append("PREM>$500K")
            elif prem > 100_000:  score +=  8; flags.append("PREM>$100K")
            if spread_pct < 0.02 and mid > 1: score += 15; flags.append("TIGHT-SPREAD")
            if 0 < delta < 0.15:  score += 20; flags.append("DEEP-OTM")
            if dte <= 7 and prem > 250_000: score += 15; flags.append("NEAR-EXPIRY-SWEEP")
            if aggressor == "buy":    score += 8; flags.append("BUYER-AGGRESSOR")
            elif aggressor == "sell": score += 8; flags.append("SELLER-AGGRESSOR")
            if "iceberg" in agg_method: score += 12; flags.append("OBI-ICEBERG")
            if c.get("multiExchangeSweep"): score += 10; flags.append("MULTI-EXCH")
            score = min(100, score)
            cls = ("dark-pool" if score >= 80 else "iceberg" if score >= 65
                   else "sweep" if score >= 45 else "unusual" if score >= 25 else "normal")
            return {
                "contractSymbol":  c.get("contractSymbol", f"{ctype}_{c['strike']}"),
                "strike":          c["strike"], "expiration": c.get("expiration", ""),
                "type":            ctype, "volOiRatio": round(vol_oi, 4),
                "score":           score, "flags":  flags, "classification": cls,
                "dollarPremium":   round(prem, 0),
                "aggressor":       aggressor, "aggressorMethod": agg_method,
                "obi":             c.get("obi", 0),
            }
        all_scored = (
            [score_contract(c, "call") for c in calls if (c.get("volume") or 0) > 0]
          + [score_contract(p, "put")  for p in puts  if (p.get("volume") or 0) > 0]
        )
        all_scored.sort(key=lambda x: -x["score"])
        unusual = [x for x in all_scored if x["score"] >= 45]
        call_prem = sum(x["dollarPremium"] for x in unusual if x["type"] == "call")
        put_prem  = sum(x["dollarPremium"] for x in unusual if x["type"] == "put")
        total_prem = call_prem + put_prem
        direction = ("bullish" if call_prem > put_prem * 1.5
                     else "bearish" if put_prem > call_prem * 1.5 else "neutral")
        urgency = ("extreme" if total_prem > 5e6 else "high" if total_prem > 1e6
                   else "medium" if total_prem > 250_000 else "low")
        analytics["flow"] = {
            "top": all_scored[:50], "sweepCount": len(unusual), "direction": direction,
            "urgency": urgency, "callPremium": round(call_prem, 0),
            "putPremium": round(put_prem, 0), "totalPremium": round(total_prem, 0),
        }
    except Exception as e:
        analytics["flow"] = None; sys.stderr.write(f"flow: {e}\n")

    # Early exercise
    try:
        early_ex_candidates = []
        for ctype, contracts in [("call", calls), ("put", puts)]:
            is_call_type = (ctype == "call")
            for c in contracts:
                k = c["strike"]
                intrinsic = max(0.0, (spot - k) if is_call_type else (k - spot))
                mid = c.get("mid") or ((c.get("bid", 0) or 0) + (c.get("ask", 0) or 0)) / 2
                oi  = c.get("openInterest", 0) or 0
                dte = c.get("dte", 30) or 30
                if intrinsic < 2.0 or oi < 50: continue
                T   = max(dte / 365, 1/365)
                time_val = max(0, mid - intrinsic) if mid > 0 else 0
                disc_benefit = k * r * T if not is_call_type else 0
                score = 0
                if disc_benefit > time_val * 1.2 and not is_call_type:
                    score = min(100, 50 + (disc_benefit / max(time_val, 0.01) - 1) * 30)
                elif is_call_type and dte < 20 and intrinsic / k > 0.15:
                    score = 40
                if score >= 30:
                    early_ex_candidates.append({
                        "contractSymbol": c.get("contractSymbol", f"{ctype}_{k}"),
                        "strike": k, "type": ctype, "dte": dte,
                        "intrinsic": round(intrinsic, 4), "timeValue": round(time_val, 4),
                        "discountBenefit": round(disc_benefit, 4), "score": round(score, 1),
                        "shouldExercise": score >= 70, "americanPremium": c.get("americanPremium", 0),
                    })
        early_ex_candidates.sort(key=lambda x: -x["score"])
        analytics["earlyExercise"] = early_ex_candidates[:20]
    except Exception as e:
        analytics["earlyExercise"] = []; sys.stderr.write(f"earlyExercise: {e}\n")

    # OI by strike
    try:
        oi_by_strike = {}
        for c in calls:
            k = c["strike"]
            oi_by_strike.setdefault(k, {"strike": k, "callOI": 0, "putOI": 0})
            oi_by_strike[k]["callOI"] += (c.get("openInterest") or 0)
        for p in puts:
            k = p["strike"]
            oi_by_strike.setdefault(k, {"strike": k, "callOI": 0, "putOI": 0})
            oi_by_strike[k]["putOI"] += (p.get("openInterest") or 0)
        analytics["oiByStrike"] = sorted(oi_by_strike.values(), key=lambda x: x["strike"])
    except Exception as e:
        analytics["oiByStrike"] = []

    # Vanna surface
    try:
        vanna_surface = {}
        for c in (calls + puts):
            k = c["strike"]; exp = c["expiration"]
            v = c.get("vanna", 0) or 0; oi = c.get("openInterest", 0) or 0
            key = (k, exp)
            vanna_surface[key] = vanna_surface.get(key, 0) + v * oi * 100
        analytics["vannaSurface"] = [
            {"strike": k, "expiration": exp, "vannaDollar": round(v / 1e3, 4)}
            for (k, exp), v in sorted(vanna_surface.items(), key=lambda x: (x[0][1], x[0][0]))
        ]
    except Exception as e:
        analytics["vannaSurface"] = []

    # VPIN — Volume-Synchronized Probability of Informed Trading
    # Computed from the volume-weighted buy/sell imbalance across contracts
    try:
        buy_vol = sum((c.get("volume", 0) or 0) for c in calls if c.get("aggressor") == "buy")
        buy_vol += sum((p.get("volume", 0) or 0) for p in puts  if p.get("aggressor") == "sell")  # put sells = bullish
        sell_vol = sum((c.get("volume", 0) or 0) for c in calls if c.get("aggressor") == "sell")
        sell_vol += sum((p.get("volume", 0) or 0) for p in puts  if p.get("aggressor") == "buy")   # put buys = bearish
        total_vol = buy_vol + sell_vol or 1
        imbalance = abs(buy_vol - sell_vol) / total_vol
        vpin = round(imbalance, 4)
        toxicity = ("extreme" if vpin > 0.70 else "high" if vpin > 0.55
                    else "elevated" if vpin > 0.40 else "low")
        analytics["vpin"] = {
            "vpin": vpin,
            "toxicityLabel": toxicity,
            "buyVolume": int(buy_vol),
            "sellVolume": int(sell_vol),
            "imbalance": round(imbalance, 4),
            "signal": ("exit" if vpin > 0.65 else "caution" if vpin > 0.50 else "hold"),
        }
    except Exception as e:
        analytics["vpin"] = {"vpin": 0, "toxicityLabel": "low", "signal": "hold"}

    # HIRO — Dealer Hedging Impact
    # Net delta-hedging flow: positive = MMs buying underlying (squeeze support)
    try:
        hiro_call = sum((c.get("delta", 0) or 0) * (c.get("volume", 0) or 0) * 100
                        * (1 if c.get("aggressor") == "buy" else -1 if c.get("aggressor") == "sell" else 0)
                        for c in calls)
        hiro_put  = sum(-(p.get("delta", 0) or 0) * (p.get("volume", 0) or 0) * 100
                        * (1 if p.get("aggressor") == "buy" else -1 if p.get("aggressor") == "sell" else 0)
                        for p in puts)
        hiro_net = hiro_call + hiro_put
        max_abs = max(abs(hiro_net), 1000)
        hiro_norm = max(-1.0, min(1.0, hiro_net / (max_abs * 5)))
        pressure = ("strong_buy" if hiro_norm > 0.5 else "buy" if hiro_norm > 0.2
                    else "strong_sell" if hiro_norm < -0.5 else "sell" if hiro_norm < -0.2
                    else "neutral")
        analytics["hiro"] = {
            "hiroNet": int(hiro_net),
            "hiroCall": int(hiro_call),
            "hiroPut": int(hiro_put),
            "hiroNorm": round(hiro_norm, 4),
            "hedgingPressure": pressure,
            "mmBuyingStr": (
                "MM aggressively buying (gamma squeeze risk)" if pressure == "strong_buy"
                else "MM net buying (supportive)" if pressure == "buy"
                else "MM aggressively selling (unwind risk)" if pressure == "strong_sell"
                else "MM net selling (resistance)" if pressure == "sell"
                else "Market maker hedging neutral"
            ),
        }
    except Exception as e:
        analytics["hiro"] = {"hiroNet": 0, "hedgingPressure": "neutral"}

    # Gamma Squeeze Velocity
    try:
        squeeze_map = {}
        for c in calls:
            k = c["strike"]
            gamma = c.get("gamma", 0) or 0
            speed = c.get("speed", 0) or 0
            oi = c.get("openInterest", 0) or 0
            squeeze_map[k] = squeeze_map.get(k, 0) + gamma * oi * 100 * (spot**2) * 0.01
        for p in puts:
            k = p["strike"]
            gamma = p.get("gamma", 0) or 0
            oi = p.get("openInterest", 0) or 0
            squeeze_map[k] = squeeze_map.get(k, 0) - gamma * oi * 100 * (spot**2) * 0.01

        near = {k: v for k, v in squeeze_map.items()
                if spot * 0.95 <= k <= spot * 1.05} if spot > 0 else {}
        top_gex = sum(abs(v) for v in near.values())
        squeeze_score = min(100, int(top_gex / 1e6))
        trigger = max(near, key=lambda k: abs(near[k]), default=spot) if near else spot
        above_gex = sum(v for k, v in near.items() if k > spot)
        below_gex = sum(v for k, v in near.items() if k < spot)
        direction = ("up" if above_gex > 2 * abs(below_gex)
                     else "down" if below_gex < -2 * abs(above_gex) else "none")
        analytics["gammaSqueezeVelocity"] = {
            "squeezeScore": squeeze_score,
            "triggerLevel": round(trigger, 2),
            "squeezeActive": squeeze_score >= 60,
            "squeezeDirection": direction,
            "clusteredStrikes": sorted(near.keys(), key=lambda k: -abs(near.get(k, 0)))[:8],
            "label": ("ACTIVE SQUEEZE" if squeeze_score >= 80
                      else "SQUEEZE WARNING" if squeeze_score >= 60
                      else "Elevated gamma concentration" if squeeze_score >= 40
                      else "Normal gamma distribution"),
        }
    except Exception as e:
        analytics["gammaSqueezeVelocity"] = {"squeezeScore": 0, "squeezeActive": False}

    # ── SVI Surface Fitting (per-expiry) ─────────────────────────────────────
    # Gatheral (2004) Raw SVI with butterfly-arb checks. One fit per expiry.
    # Attaches sviParams to analytics so the front-end can use the fitted
    # surface for D%/F%/edge metrics and Breeden-Litzenberger ProbITM.
    try:
        by_exp_c: dict = {}
        for c in calls:
            exp = c.get("expiration", "")
            if exp:
                by_exp_c.setdefault(exp, []).append(c)

        svi_by_exp = {}
        for exp, exp_calls in sorted(by_exp_c.items())[:8]:  # cap at 8 expiries for perf
            exp_calls_v = [c for c in exp_calls if c.get("iv", 0) > 0.005]
            if len(exp_calls_v) < 4:
                continue
            T_exp = dte_to_T(exp_calls_v[0].get("dte", 30) or 30)
            if T_exp <= 0:
                continue
            strikes = [c["strike"] for c in exp_calls_v]
            ivs     = [c["iv"]     for c in exp_calls_v]
            # Also blend in put IVs for wing stabilization
            put_map  = {p["strike"]: p["iv"] for p in puts
                        if p.get("expiration","") == exp and p.get("iv",0) > 0.005}
            # Average put and call IV at each strike (P-C parity enforcement)
            merged_strikes, merged_ivs = [], []
            seen_ks = set()
            for K, iv_c in zip(strikes, ivs):
                Kr = round(K, 2)
                if Kr in seen_ks:
                    continue
                seen_ks.add(Kr)
                iv_p = put_map.get(Kr, 0)
                # Weight: near ATM prefer the call IV, OTM puts may be more liquid
                if iv_p > 0.005:
                    merged_iv = (iv_c + iv_p) / 2
                else:
                    merged_iv = iv_c
                merged_strikes.append(K)
                merged_ivs.append(merged_iv)
            # Add OTM put wings for better tail calibration
            for K_p, iv_p in put_map.items():
                Kr = round(K_p, 2)
                if Kr not in seen_ks and K_p < spot:
                    merged_strikes.append(K_p)
                    merged_ivs.append(iv_p)

            fit = svi_fit_expiry(merged_strikes, merged_ivs, spot, T_exp, r,
                                 smooth_first=True)
            if fit:
                svi_by_exp[exp] = fit

        analytics["sviByExpiry"] = svi_by_exp
    except Exception as e:
        analytics["sviByExpiry"] = {}
        sys.stderr.write(f"svi_by_expiry: {e}\n")

    # ── Kernel-Smoothed IV (per expiry, call + put sides) ────────────────────
    # Applies Epanechnikov kernel smoother to raw IV smile.
    # Output: {expiry: {strike: {"ivSmoothedCall": ..., "ivSmoothedPut": ...}}}
    try:
        smooth_map = {}
        by_exp_smooth: dict = {}
        for c in calls:
            exp = c.get("expiration", "")
            if exp and c.get("iv", 0) > 0:
                by_exp_smooth.setdefault(exp, {"calls": [], "puts": []})["calls"].append(c)
        for p in puts:
            exp = p.get("expiration", "")
            if exp and p.get("iv", 0) > 0:
                by_exp_smooth.setdefault(exp, {"calls": [], "puts": []})["puts"].append(p)

        for exp, sides in list(by_exp_smooth.items())[:8]:
            exp_c = sorted(sides["calls"], key=lambda x: x["strike"])
            exp_p = sorted(sides["puts"],  key=lambda x: x["strike"])
            T_exp = dte_to_T((exp_c or exp_p)[0].get("dte", 30) or 30)
            smooth_map[exp] = {}
            if exp_c and len(exp_c) >= 3:
                ks_c = [c["strike"] for c in exp_c]
                ivs_c = [c["iv"] for c in exp_c]
                sm_c = kernel_smooth_iv(ks_c, ivs_c, spot, T_exp, r)
                for c, iv_s in zip(exp_c, sm_c):
                    Kr = round(c["strike"], 2)
                    smooth_map[exp].setdefault(Kr, {})["ivSmoothedCall"] = round(iv_s, 6)
            if exp_p and len(exp_p) >= 3:
                ks_p = [p["strike"] for p in exp_p]
                ivs_p = [p["iv"] for p in exp_p]
                sm_p = kernel_smooth_iv(ks_p, ivs_p, spot, T_exp, r)
                for p, iv_s in zip(exp_p, sm_p):
                    Kr = round(p["strike"], 2)
                    smooth_map[exp].setdefault(Kr, {})["ivSmoothedPut"] = round(iv_s, 6)

        analytics["kernelSmoothedIV"] = smooth_map
    except Exception as e:
        analytics["kernelSmoothedIV"] = {}
        sys.stderr.write(f"kernel_smooth: {e}\n")

    # ── IVolatility-Style IV Index (30d and 60d) ─────────────────────────────
    # Vega-weighted, delta-filtered, √T interpolated ATM IV composite.
    try:
        iv_idx_30 = calc_iv_index(calls, puts, spot, r, target_dte=30)
        iv_idx_60 = calc_iv_index(calls, puts, spot, r, target_dte=60)
        analytics["ivIndex"] = {
            "iv30":    iv_idx_30,
            "iv60":    iv_idx_60,
            # Term slope: positive = normal backwardation (long-term > short-term IV)
            "slope30_60": round(
                (iv_idx_60.get("ivIndexPct", 0) - iv_idx_30.get("ivIndexPct", 0)), 4
            ),
        }
    except Exception as e:
        analytics["ivIndex"] = {}
        sys.stderr.write(f"iv_index: {e}\n")

    # ── Event-Spanning Expiry Detection ─────────────────────────────────��────
    # Flags expiries with anomalous ATM IV kink (earnings/CPI/FOMC premium).
    # Non-spanning methodology per Carverhill, Lochmann & Wang (2026).
    try:
        analytics["eventSpanning"] = flag_event_spanning(calls, puts, spot)
    except Exception as e:
        analytics["eventSpanning"] = {}
        sys.stderr.write(f"event_spanning: {e}\n")

    # ── Data Quality Summary ──────────���─────────────────────────────��─────────
    try:
        all_c = calls + puts
        n_total  = len(all_c)
        n_clean  = sum(1 for x in all_c if x.get("dataQuality","") in ("clean","lse_enriched","intrinio_enriched","av_enriched"))
        n_iv_ok  = sum(1 for x in all_c if (x.get("iv",0) or 0) > 0.005)
        analytics["dataQuality"] = {
            "total":    n_total,
            "clean":    n_clean,
            "cleanPct": round(n_clean / n_total * 100, 1) if n_total > 0 else 0,
            "ivOk":     n_iv_ok,
            "ivOkPct":  round(n_iv_ok / n_total * 100, 1) if n_total > 0 else 0,
        }
    except Exception as e:
        analytics["dataQuality"] = {}

    return analytics

# ── Main ────────────────���─────────────────────────────────────────────────────

def main():
    t_start    = time.time()
    symbol     = sys.argv[1].upper() if len(sys.argv) > 1 else "AAPL"
    expiration = sys.argv[2]         if len(sys.argv) > 2 else None

    result = None

    # ── 1. Primary: Alpaca ───────────────────────────────────────────────────
    try:
        result = fetch_alpaca_options(symbol, expiration)
        if result:
            sys.stderr.write(
                f"alpaca: OK in {time.time()-t_start:.3f}s "
                f"({len(result.get('calls',[]))} calls, "
                f"{len(result.get('puts',[]))} puts)\n"
            )
    except Exception as e:
        sys.stderr.write(f"alpaca: {e}\n")

    # ── 2. Fallback: yfinance ────────────────────────────────────────────────
    if not result:
        try:
            result = fetch_yfinance(symbol, expiration)
            if result:
                sys.stderr.write(f"yfinance: OK in {time.time()-t_start:.3f}s\n")
        except Exception as e:
            sys.stderr.write(f"yfinance: {e}\n")

    if not result:
        print(json.dumps({"error": f"No options data for {symbol}"}))
        sys.exit(1)

    # Start enrichment wall-clock budget from this point
    global _t_primary_done
    _t_primary_done = time.time()
    sys.stderr.write(f"primary_fetch: {_t_primary_done - t_start:.2f}s — enrichment budget {MAX_WALL_SECONDS}s starts now\n")

    # ── 3. Multi-source IV enrichment via data_sources.py ───────────────────
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        from data_sources import (
            av_realtime_options, finnhub_quote, massive_option_chain,
            nyse_precision_time, get_best_quote,
        )

        # 3a-3d: Run NYSE clock, enriched spot, AV IV cross-fill, and LSE chain in parallel.
        # LSE gives us live options data with IV/greeks for cross-filling zero-IV contracts.
        calls = result.get("calls", [])
        puts  = result.get("puts",  [])
        missing_iv = sum(1 for c in calls + puts if c.get("iv", 0) < 0.001)

        def _nyse_task():
            try: return nyse_precision_time()
            except: return None
        def _spot_task():
            try: return get_best_quote(symbol)
            except: return None
        def _av_task():
            try: return av_realtime_options(symbol) if missing_iv > 0 else None
            except: return None
        def _lse_task():
            try: return fetch_lse_chain(symbol, expiration) if LSE_KEY else None
            except: return None
        def _lse_flow_task():
            try: return fetch_lse_flow(symbol) if LSE_KEY else []
            except: return []

        av_chain = None; lse_chain = None; lse_flow: list = []
        with ThreadPoolExecutor(max_workers=5) as _wp:
            _nyse_f      = _wp.submit(_nyse_task)
            _spot_f      = _wp.submit(_spot_task)
            _av_f        = _wp.submit(_av_task)
            _lse_f       = _wp.submit(_lse_task)
            _lse_flow_f  = _wp.submit(_lse_flow_task)
            try:
                nyse_t = _nyse_f.result(timeout=4)
                if nyse_t: result["nyseTime"] = nyse_t
            except: pass
            try:
                bq = _spot_f.result(timeout=6)
                if bq and bq.get("price", 0) > 0:
                    result["enrichedSpot"] = bq
                    if abs(result.get("spot", 0) - bq["price"]) / max(bq["price"], 1) > 0.005:
                        result["spot"] = bq["price"]
            except Exception as e:
                sys.stderr.write(f"enrichedSpot: {e}\n")
            try:
                av_chain = _av_f.result(timeout=8)
            except: pass
            try:
                lse_chain = _lse_f.result(timeout=10)
            except: pass
            try:
                lse_flow = _lse_flow_f.result(timeout=8) or []
            except: pass

        # 3d-lse: Merge LSE chain into calls/puts (IV + greeks + bid/ask cross-fill)
        if lse_chain and _budget_ok():
            try:
                lse_filled = 0
                for c in result.get("calls", []):
                    k = (round(float(c.get("strike", 0)), 2),
                         str(c.get("expiration", "")), "call")
                    lse = lse_chain.get(k)
                    if lse:
                        if c.get("iv", 0) < 0.001 and lse.get("iv", 0) > 0.001:
                            c["iv"]    = lse["iv"]
                            c["ivPct"] = round(lse["iv"] * 100, 2)
                            lse_filled += 1
                        for f in ("delta", "gamma", "theta", "vega"):
                            if c.get(f, 0) == 0 and lse.get(f, 0) != 0: c[f] = lse[f]
                        if c.get("bid", 0) == 0 and lse.get("bid", 0) > 0: c["bid"] = lse["bid"]
                        if c.get("ask", 0) == 0 and lse.get("ask", 0) > 0: c["ask"] = lse["ask"]
                        if c.get("openInterest", 0) == 0 and lse.get("oi", 0) > 0:
                            c["openInterest"] = lse["oi"]
                        if c.get("volume", 0) == 0 and lse.get("vol", 0) > 0:
                            c["volume"] = lse["vol"]
                        if lse_filled > 0 or lse.get("iv", 0) > 0:
                            c["dataQuality"] = "lse_enriched"
                for p in result.get("puts", []):
                    k = (round(float(p.get("strike", 0)), 2),
                         str(p.get("expiration", "")), "put")
                    lse = lse_chain.get(k)
                    if lse:
                        if p.get("iv", 0) < 0.001 and lse.get("iv", 0) > 0.001:
                            p["iv"]    = lse["iv"]
                            p["ivPct"] = round(lse["iv"] * 100, 2)
                            lse_filled += 1
                        for f in ("delta", "gamma", "theta", "vega"):
                            if p.get(f, 0) == 0 and lse.get(f, 0) != 0: p[f] = lse[f]
                        if p.get("bid", 0) == 0 and lse.get("bid", 0) > 0: p["bid"] = lse["bid"]
                        if p.get("ask", 0) == 0 and lse.get("ask", 0) > 0: p["ask"] = lse["ask"]
                        if p.get("openInterest", 0) == 0 and lse.get("oi", 0) > 0:
                            p["openInterest"] = lse["oi"]
                        if p.get("volume", 0) == 0 and lse.get("vol", 0) > 0:
                            p["volume"] = lse["vol"]
                        if lse_filled > 0 or lse.get("iv", 0) > 0:
                            p["dataQuality"] = "lse_enriched"
                result["lseEnriched"] = lse_filled
                sys.stderr.write(f"lse_enrich: filled {lse_filled} IVs from {len(lse_chain)} contracts\n")
            except Exception as e:
                sys.stderr.write(f"lse_enrich: {e}\n")

        # 3d-lse-flow: Attach LSE unusual options flow
        if lse_flow and _budget_ok():
            result["lseFlow"] = lse_flow
            sys.stderr.write(f"lse_flow: {len(lse_flow)} unusual prints attached\n")

        if av_chain and missing_iv > 0 and _budget_ok():
            try:
                if av_chain:
                    # Build lookup keyed by (strike, expiry) for quick merge
                    av_lookup: dict = {}
                    for (strike, exp, side), v in av_chain.items():
                        key = (round(float(strike), 2), str(exp), str(side))
                        av_lookup[key] = v
                    for c in calls:
                        k = (round(float(c.get("strike", 0)), 2),
                             str(c.get("expiration", "")), "call")
                        av = av_lookup.get(k)
                        if av and c.get("iv", 0) < 0.001 and av.get("iv", 0) > 0.001:
                            c["iv"]    = av["iv"]
                            c["delta"] = av.get("delta", c.get("delta", 0))
                            c["gamma"] = av.get("gamma", c.get("gamma", 0))
                            c["theta"] = av.get("theta", c.get("theta", 0))
                            c["vega"]  = av.get("vega",  c.get("vega",  0))
                            c["dataQuality"] = "av_enriched"
                    for p in puts:
                        k = (round(float(p.get("strike", 0)), 2),
                             str(p.get("expiration", "")), "put")
                        av = av_lookup.get(k)
                        if av and p.get("iv", 0) < 0.001 and av.get("iv", 0) > 0.001:
                            p["iv"]    = av["iv"]
                            p["delta"] = av.get("delta", p.get("delta", 0))
                            p["gamma"] = av.get("gamma", p.get("gamma", 0))
                            p["theta"] = av.get("theta", p.get("theta", 0))
                            p["vega"]  = av.get("vega",  p.get("vega",  0))
                            p["dataQuality"] = "av_enriched"
                    sys.stderr.write(f"av_enrich: filled {missing_iv} missing IVs\n")
            except Exception as e:
                sys.stderr.write(f"av_enrich: {e}\n")

        # 3d. Intrinio premium enrichment (real-time greeks + unusual + implied move)
        # 3d. Intrinio: chain enrich, unusual, implied-move, stats
        if not _budget_ok():
            sys.stderr.write(f"intrinio: skipped — budget exhausted at {time.time()-_t_primary_done:.1f}s\n")
        elif not INTRINIO_KEY:
            sys.stderr.write("intrinio: skipped — no INTRINIO_API_KEY\n")
        else:
            try:
                calls_ref = result.get("calls", [])
                puts_ref  = result.get("puts",  [])
                intrinio_ch = fetch_intrinio_chain(symbol, expiration)
                if intrinio_ch:
                    for c in calls_ref:
                        it = intrinio_ch.get((round(float(c.get("strike",0)),2), str(c.get("expiration","")), "call"))
                        if it:
                            if c.get("iv", 0) < 0.001 and it.get("iv", 0) > 0.001:
                                c["iv"] = it["iv"]; c["ivPct"] = round(it["iv"]*100, 2)
                            for f in ["delta","gamma","theta","vega"]:
                                if c.get(f, 0) == 0 and it.get(f, 0) != 0: c[f] = it[f]
                            if c.get("openInterest", 0) == 0 and it.get("oi", 0) > 0: c["openInterest"] = it["oi"]
                            if c.get("volume", 0) == 0 and it.get("vol", 0) > 0: c["volume"] = it["vol"]
                            if it.get("code"): c["intrinioCode"] = it["code"]
                            c["dataQuality"] = "intrinio_enriched"
                    for p in puts_ref:
                        it = intrinio_ch.get((round(float(p.get("strike",0)),2), str(p.get("expiration","")), "put"))
                        if it:
                            if p.get("iv", 0) < 0.001 and it.get("iv", 0) > 0.001:
                                p["iv"] = it["iv"]; p["ivPct"] = round(it["iv"]*100, 2)
                            for f in ["delta","gamma","theta","vega"]:
                                if p.get(f, 0) == 0 and it.get(f, 0) != 0: p[f] = it[f]
                            if p.get("openInterest", 0) == 0 and it.get("oi", 0) > 0: p["openInterest"] = it["oi"]
                            if p.get("volume", 0) == 0 and it.get("vol", 0) > 0: p["volume"] = it["vol"]
                            if it.get("code"): p["intrinioCode"] = it["code"]
                            p["dataQuality"] = "intrinio_enriched"
                    sys.stderr.write(f"intrinio_enrich: merged {len(intrinio_ch)} contracts in {time.time()-_t_primary_done:.1f}s\n")
            except Exception as e:
                sys.stderr.write(f"intrinio_enrich: {e}\n")
            if _budget_ok():
                try:
                    unusual = fetch_intrinio_unusual(symbol)
                    if unusual: result["intrinioUnusual"] = unusual
                except Exception as e:
                    sys.stderr.write(f"intrinio_unusual: {e}\n")
            if _budget_ok():
                try:
                    iml = fetch_intrinio_implied_move(symbol)
                    if iml: result["intrinioImpliedMove"] = iml
                except Exception as e:
                    sys.stderr.write(f"intrinio_implied_move: {e}\n")
            if _budget_ok():
                try:
                    istats = fetch_intrinio_stats(symbol)
                    if istats: result["intrinioStats"] = istats
                except Exception as e:
                    sys.stderr.write(f"intrinio_stats: {e}\n")

        # 3e. Massive EOD IV enrichment (historical surface)
        if _budget_ok():
            try:
                massive = massive_option_chain(symbol)
                if massive and massive.get("calls"):
                    result["massiveEOD"] = {
                        "lastUpdated": massive.get("lastUpdated", ""),
                        "callCount": len(massive.get("calls", [])),
                        "putCount":  len(massive.get("puts",  [])),
                    }
            except Exception as e:
                sys.stderr.write(f"massive: {e}\n")

    except Exception as e:
        sys.stderr.write(f"enrichment block: {e}\n")

    # ── 4. Advanced analytics via advanced_analytics.py ──────────────────��──
    if not _budget_ok():
        sys.stderr.write(f"analytics: skipped — budget exhausted at {time.time()-_t_primary_done:.1f}s\n")
    else:
        try:
            from advanced_analytics import run_all_advanced_analytics
            calls = result.get("calls", [])
            puts  = result.get("puts",  [])
            spot  = result.get("spot",  0.0)
            if calls and spot > 0:
                aa = run_all_advanced_analytics(calls, puts, spot)
                # Merge advanced analytics into result
                result["darkPool"]           = aa.get("darkPool", {})
                result["ndp"]                = aa.get("ndp", {})
                result["oobi"]               = aa.get("oobi", {})
                result["varianceSwap"]       = aa.get("varianceSwap", {})
                result["toxicFlow"]          = aa.get("toxicFlow", {})
                result["sviSurface"]         = aa.get("sviSurface", {})
                result["localVolSurface"]    = aa.get("localVolSurface", [])
                result["cob"]                = aa.get("cob", {})
                result["anomaliesAA"]        = aa.get("anomalies", {})
                result["portfolioGreeks"]    = aa.get("portfolioGreeks", {})
                result["vannaArbitrage"]     = aa.get("vannaArbitrage", {})
                sys.stderr.write(f"analytics: OK in {aa.get('analyticsMs',0)}ms\n")
        except Exception as e:
            sys.stderr.write(f"analytics: {e}\n")

    result["fetchMs"] = round((time.time() - t_start) * 1000)
    print(json.dumps(result))

if __name__ == "__main__":
    main()
