#!/usr/bin/env python3
"""
APEX Terminal — Intrinio / iVolatility Data Fetch
==================================================
Handles live intelligence modes:
  unusual     — unusual options activity (Intrinio API or AV synthetic scoring)
  implied_move — 1-event implied move via nearest straddle / straddle index
  stats        — IV rank, IV percentile, P/C ratio, call/put volume
  ivol_rank    — IV rank + percentile from AV historical 52-week data
  ivol_term    — IV term structure (median IV by expiration)
  greeks       — real-time greeks list (Intrinio or AV realtime options)

Usage: python intrinio_fetch.py SYMBOL MODE
Output: JSON to stdout
"""

import sys, json, os, math, time, urllib.request, urllib.error
from datetime import datetime, date as _date, timedelta
from typing import Any, Dict, List, Optional
sys.path.insert(0, os.path.dirname(__file__))

# ── Keys ──────────────────────────────────────────────────────────────────────
INTRINIO_KEY = os.environ.get("INTRINIO_API_KEY", "")
APCA_KEY     = os.environ.get("APCA_API_KEY_ID",    "")
APCA_SEC     = os.environ.get("APCA_API_SECRET_KEY","")

# ── Helpers ───────────────────────────────────────────────────────────────────
def _sf(v, d: float = 0.0) -> float:
    try: return float(v)
    except: return d

def _si(v, d: int = 0) -> int:
    try: return int(v)
    except: return d

def _get(url: str, headers: Optional[Dict] = None, timeout: int = 8) -> Optional[Any]:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        sys.stderr.write(f"http_get {url[:80]}: {e}\n")
        return None

def _intrinio(path: str) -> Optional[Any]:
    """Call Intrinio v2 API. Returns None if key absent or request fails."""
    if not INTRINIO_KEY:
        return None
    url = f"https://api-v2.intrinio.com{path}?api_key={INTRINIO_KEY}"
    return _get(url, timeout=10)

def _dte(exp_str: str) -> int:
    """Days to expiration from a YYYY-MM-DD string."""
    try:
        exp = datetime.strptime(exp_str, "%Y-%m-%d").date()
        return max(0, (exp - _date.today()).days)
    except:
        return 0

def _norm_cdf(x: float) -> float:
    """A&S normal CDF approximation."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    d = 0.3989423 * math.exp(-0.5 * x * x)
    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.7814779 + t * (-1.8212560 + t * 1.3302744))))
    return 1.0 - p if x >= 0 else p

# ── AV chain loader (reuses data_sources module) ─────────────────────────────
def _load_av_chain(symbol: str):
    """Load AV realtime options. Returns dict keyed by (strike, exp, side) or {}."""
    try:
        from data_sources import av_realtime_options
        return av_realtime_options(symbol) or {}
    except Exception as e:
        sys.stderr.write(f"av_chain: {e}\n")
        return {}

def _load_av_hist(symbol: str):
    """Load AV historical options (for IV rank). Returns list of dicts."""
    try:
        from data_sources import av_historical_options
        result = av_historical_options(symbol)
        if isinstance(result, dict):
            return list(result.values())
        return result or []
    except Exception as e:
        sys.stderr.write(f"av_hist: {e}\n")
        return []

def _load_finnhub_quote(symbol: str) -> float:
    try:
        from data_sources import finnhub_quote
        q = finnhub_quote(symbol)
        return _sf(q.get("c", 0)) if q else 0.0
    except:
        return 0.0

def _load_alpaca_chain(symbol: str) -> List[Dict]:
    """Alpaca options snapshot as a flat list of contract dicts."""
    try:
        url = f"https://data.alpaca.markets/v1beta1/options/snapshots?symbols={symbol}&feed=opra&limit=100"
        headers = {"APCA-API-KEY-ID": APCA_KEY, "APCA-API-SECRET-KEY": APCA_SEC}
        data = _get(url, headers=headers, timeout=10)
        if not data:
            return []
        snaps = data.get("snapshots", {})
        results = []
        for sym_key, snap in snaps.items():
            gd = snap.get("greeks", {}) or {}
            qt = snap.get("latestQuote", {}) or {}
            det = snap.get("details", {}) or {}
            results.append({
                "symbol":  sym_key,
                "strike":  _sf(det.get("strikePrice", 0)),
                "exp":     det.get("expirationDate", ""),
                "type":    det.get("optionType", "").lower(),
                "oi":      _si(det.get("openInterest", 0)),
                "vol":     _si(snap.get("dailyBar", {}).get("v", 0)),
                "iv":      _sf(det.get("impliedVolatility", 0)),
                "delta":   _sf(gd.get("delta", 0)),
                "gamma":   _sf(gd.get("gamma", 0)),
                "theta":   _sf(gd.get("theta", 0)),
                "vega":    _sf(gd.get("vega", 0)),
                "bid":     _sf(qt.get("bp", 0)),
                "ask":     _sf(qt.get("ap", 0)),
                "mid":     (_sf(qt.get("bp", 0)) + _sf(qt.get("ap", 0))) / 2,
            })
        return results
    except Exception as e:
        sys.stderr.write(f"alpaca_chain: {e}\n")
        return []

# ── Mode: unusual ─────────────────────────────────────────────────────────────
def _score_unusual(contract: dict, avg_vol: float, avg_oi: float) -> float:
    """Synthetic unusual activity score 0-100."""
    vol = _sf(contract.get("vol", contract.get("volume", 0)))
    oi  = _sf(contract.get("oi",  contract.get("openInterest", 0)))
    iv  = _sf(contract.get("iv",  contract.get("impliedVolatility", 0)))
    mid = _sf(contract.get("mid", 0)) or (_sf(contract.get("bid",0)) + _sf(contract.get("ask",0))) / 2

    vol_ratio = vol / max(avg_vol, 1)
    oi_ratio  = vol / max(oi, 1)
    prem      = vol * mid * 100  # total premium in $

    # Weighted scoring: vol_ratio dominates, premium adds, oi_ratio secondary
    score = min(100.0, (
        min(vol_ratio / 10, 1) * 45 +   # 0-45: volume vs average
        min(oi_ratio / 2, 1) * 25 +      # 0-25: vol/OI (directional intent)
        min(math.log10(max(prem, 1)) / 7, 1) * 20 +  # 0-20: premium size
        (1 if iv > 0.4 else 0) * 10       # 0-10: elevated IV bonus
    ))
    return round(score, 1)

def mode_unusual(symbol: str) -> Dict:
    """Intrinio unusual activity or synthetic scoring from AV/Alpaca chain."""
    # 1. Try Intrinio
    if INTRINIO_KEY:
        resp = _intrinio(f"/options/unusual_activity/{symbol}")
        if resp and resp.get("unusual_activity"):
            items = resp["unusual_activity"]
            out = []
            for u in items[:50]:
                out.append({
                    "type":           u.get("type", ""),
                    "strikePrice":    _sf(u.get("strike_price", 0)),
                    "expiration":     u.get("expiration_date", ""),
                    "dte":            _dte(u.get("expiration_date", "")),
                    "volume":         _si(u.get("total_volume", 0)),
                    "size":           _si(u.get("total_volume", 0)),
                    "impliedVolatility": _sf(u.get("implied_volatility", 0)),
                    "iv":             _sf(u.get("implied_volatility", 0)),
                    "premium":        _sf(u.get("total_value", 0)),
                    "totalPremium":   _sf(u.get("total_value", 0)),
                    "bid":            _sf(u.get("bid", 0)),
                    "ask":            _sf(u.get("ask", 0)),
                    "score":          min(100, _sf(u.get("unusual_sentiment_index", 50))),
                    "signal":         u.get("sentiment", "NOTABLE").upper(),
                    "source":         "intrinio",
                })
            return {"unusual": out, "source": "intrinio"}

    # 2. Fallback: AV or Alpaca chain, synthetic scoring
    chain_flat: List[Dict] = []
    av = _load_av_chain(symbol)
    if av:
        for (strike, exp, side), v in av.items():
            chain_flat.append({
                "type":   side,
                "strike": strike,
                "exp":    exp,
                "dte":    _dte(str(exp)),
                "vol":    _si(v.get("volume", 0)),
                "oi":     _si(v.get("open_interest", 0)),
                "iv":     _sf(v.get("iv", 0)),
                "bid":    _sf(v.get("bid", 0)),
                "ask":    _sf(v.get("ask", 0)),
                "mid":    _sf(v.get("mid", (_sf(v.get("bid",0)) + _sf(v.get("ask",0)))/2)),
            })
    else:
        chain_flat = _load_alpaca_chain(symbol)

    if not chain_flat:
        return {"unusual": [], "source": "none"}

    vols = [c["vol"] for c in chain_flat if c["vol"] > 0]
    ois  = [c["oi"]  for c in chain_flat if c["oi"]  > 0]
    avg_vol = sum(vols) / max(len(vols), 1)
    avg_oi  = sum(ois)  / max(len(ois),  1)

    scored = []
    for c in chain_flat:
        score = _score_unusual(c, avg_vol, avg_oi)
        if score < 20:
            continue
        mid = c.get("mid", 0) or (c.get("bid", 0) + c.get("ask", 0)) / 2
        prem = c["vol"] * mid * 100
        signal = "AGGRESSIVE" if score > 80 else "ELEVATED" if score > 60 else "NOTABLE" if score > 40 else "WATCH"
        scored.append({
            "type":              c["type"],
            "strikePrice":       c["strike"],
            "expiration":        str(c["exp"]),
            "dte":               c["dte"],
            "volume":            c["vol"],
            "size":              c["vol"],
            "impliedVolatility": c["iv"],
            "iv":                c["iv"],
            "premium":           round(prem, 0),
            "totalPremium":      round(prem, 0),
            "bid":               c["bid"],
            "ask":               c["ask"],
            "score":             score,
            "signal":            signal,
            "source":            "synthetic",
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return {"unusual": scored[:50], "source": "synthetic"}

# ── Mode: implied_move ────────────────────────────────────────────────────────
def mode_implied_move(symbol: str) -> Dict:
    """Compute expected 1-event move from nearest ATM straddle price."""
    # 1. Try Intrinio implied move
    if INTRINIO_KEY:
        resp = _intrinio(f"/options/stats/{symbol}")
        if resp:
            im_pct = _sf(resp.get("implied_move", {}).get("implied_move_percent", 0)) if isinstance(resp.get("implied_move"), dict) else 0
            if im_pct > 0:
                spot = _load_finnhub_quote(symbol)
                return {
                    "implied_move": {
                        "impliedMovePct": im_pct,
                        "low":  round(spot * (1 - im_pct / 100), 2) if spot else None,
                        "high": round(spot * (1 + im_pct / 100), 2) if spot else None,
                        "source": "intrinio",
                    }
                }

    # 2. Compute from AV/Alpaca chain: find nearest ATM straddle
    spot = _load_finnhub_quote(symbol)
    if not spot:
        return {"implied_move": None}

    av = _load_av_chain(symbol)
    if av:
        # Find near-dated expiration (7-45 DTE)
        exps: Dict[str, List] = {}
        for (strike, exp, side), v in av.items():
            dte = _dte(str(exp))
            if not (7 <= dte <= 45):
                continue
            key = str(exp)
            if key not in exps:
                exps[key] = []
            exps[key].append((strike, side, v))

        if exps:
            nearest_exp = min(exps.keys())
            contracts   = exps[nearest_exp]
            dte         = _dte(nearest_exp)

            # Find ATM strike (nearest to spot)
            strikes = sorted(set(c[0] for c in contracts))
            atm_strike = min(strikes, key=lambda k: abs(k - spot))

            call_mid = put_mid = 0.0
            for (strike, side, v) in contracts:
                if strike != atm_strike:
                    continue
                mid = _sf(v.get("mid", 0)) or (_sf(v.get("bid",0)) + _sf(v.get("ask",0))) / 2
                if side == "call":
                    call_mid = mid
                else:
                    put_mid  = mid

            straddle = call_mid + put_mid
            if straddle > 0:
                im_pct = round(straddle / spot * 100, 2)
                return {
                    "implied_move": {
                        "impliedMovePct": im_pct,
                        "low":    round(spot - straddle, 2),
                        "high":   round(spot + straddle, 2),
                        "strike": atm_strike,
                        "exp":    nearest_exp,
                        "dte":    dte,
                        "straddle": round(straddle, 2),
                        "source": "straddle",
                    }
                }

    # 3. Last resort: VIX-based approximation (sqrt(VIX/100 * T) * spot)
    # Use AV or just return None
    return {"implied_move": None}

# ── Mode: stats ────────────────────────────────────────────────────────────────
def mode_stats(symbol: str) -> Dict:
    """IV rank, percentile, P/C ratio, volumes."""
    # 1. Try Intrinio options stats
    if INTRINIO_KEY:
        resp = _intrinio(f"/options/stats/{symbol}")
        if resp:
            s = resp.get("stats", resp) or {}
            return {
                "stats": {
                    "iv_rank":       _sf(s.get("implied_volatility_rank", s.get("iv_rank", None))),
                    "iv_percentile": _sf(s.get("implied_volatility_percentile", s.get("iv_pct", None))),
                    "impliedVolatility": _sf(s.get("implied_volatility", None)),
                    "call_volume":   _si(s.get("call_volume", 0)),
                    "put_volume":    _si(s.get("put_volume", 0)),
                    "put_call_ratio":_sf(s.get("put_call_ratio", 0)),
                    "source":        "intrinio",
                }
            }

    # 2. Compute from AV chain
    av = _load_av_chain(symbol)
    call_vol = put_vol = 0
    ivs = []
    for (strike, exp, side), v in av.items():
        vol = _si(v.get("volume", 0))
        iv  = _sf(v.get("iv", 0))
        if side == "call":
            call_vol += vol
        else:
            put_vol  += vol
        if 0.01 < iv < 5.0:
            ivs.append(iv)

    # IV rank needs historical context — use AV put_call_ratio endpoint if available
    pcr = put_vol / call_vol if call_vol > 0 else None
    avg_iv = sum(ivs) / len(ivs) if ivs else None

    # 3. Try AV put_call_ratio for enriched P/C
    try:
        from data_sources import av_put_call_ratio
        pc_resp = av_put_call_ratio(symbol, "3month")
        if pc_resp:
            latest = list(pc_resp.values())[0] if isinstance(pc_resp, dict) else None
            if latest:
                pcr = _sf(latest.get("put_call_ratio", pcr))
    except:
        pass

    return {
        "stats": {
            "impliedVolatility": avg_iv,
            "call_volume":   call_vol,
            "put_volume":    put_vol,
            "put_call_ratio": round(pcr, 4) if pcr else None,
            "source":        "av_computed",
        }
    }

# ── Mode: ivol_rank ────────────────────────────────────────────────────────────
def mode_ivol_rank(symbol: str) -> Dict:
    """IV rank and percentile from 52-week AV historical options data."""
    # 1. Try Intrinio
    if INTRINIO_KEY:
        resp = _intrinio(f"/options/stats/{symbol}")
        if resp:
            s = resp.get("stats", resp) or {}
            iv_r = _sf(s.get("implied_volatility_rank", 0))
            iv_p = _sf(s.get("implied_volatility_percentile", 0))
            iv_c = _sf(s.get("implied_volatility", 0))
            if iv_r or iv_p:
                return {
                    "ivrank": {
                        "iv_rank":   round(iv_r, 2),
                        "iv_pct":    round(iv_p, 2),
                        "currentIV": iv_c,
                        "source":    "intrinio",
                    }
                }

    # 2. AV historical options: compute IV rank from ATM IV over rolling year
    hist = _load_av_hist(symbol)

    # Also get current IV from realtime chain
    av_rt = _load_av_chain(symbol)
    current_ivs = [_sf(v.get("iv",0)) for v in av_rt.values() if 0.01 < _sf(v.get("iv",0)) < 5]
    current_iv  = sum(current_ivs) / len(current_ivs) if current_ivs else 0.0

    # Collect historical IV observations (use daily ATM IV proxy)
    hist_ivs: List[float] = []
    for item in hist:
        iv = _sf(item.get("implied_volatility", item.get("iv", 0)) if isinstance(item, dict) else 0)
        if 0.01 < iv < 5.0:
            hist_ivs.append(iv)

    if not hist_ivs or current_iv <= 0:
        return {"ivrank": {"iv_rank": None, "iv_pct": None, "currentIV": current_iv, "source": "av_partial"}}

    iv_min  = min(hist_ivs)
    iv_max  = max(hist_ivs)
    iv_rank = (current_iv - iv_min) / (iv_max - iv_min) * 100 if iv_max > iv_min else 50.0
    iv_pct  = sum(1 for x in hist_ivs if x < current_iv) / len(hist_ivs) * 100

    return {
        "ivrank": {
            "iv_rank":   round(iv_rank, 2),
            "iv_pct":    round(iv_pct, 2),
            "currentIV": round(current_iv, 4),
            "iv52wLow":  round(iv_min, 4),
            "iv52wHigh": round(iv_max, 4),
            "source":    "av_computed",
        }
    }

# ── Mode: ivol_term ────────────────────────────────────────────────────────────
def mode_ivol_term(symbol: str) -> Dict:
    """IV term structure — median IV grouped by expiration."""
    # 1. Try Intrinio chain (has per-exp IV)
    if INTRINIO_KEY:
        resp = _intrinio(f"/options/chain/{symbol}/realtime")
        if resp and resp.get("chain"):
            by_exp: Dict[str, List[float]] = {}
            for item in resp["chain"]:
                exp = item.get("option", {}).get("expiration", "")
                iv  = _sf(item.get("stats", {}).get("implied_volatility", 0))
                if exp and 0.01 < iv < 5.0:
                    by_exp.setdefault(exp, []).append(iv)
            if by_exp:
                term = []
                for exp, ivs in sorted(by_exp.items()):
                    ivs_sorted = sorted(ivs)
                    mid_iv = ivs_sorted[len(ivs_sorted) // 2]
                    term.append({
                        "expiration": exp,
                        "dte":       _dte(exp),
                        "impliedVolatility": round(mid_iv, 4),
                        "iv":        round(mid_iv, 4),
                    })
                return {"term": term, "source": "intrinio"}

    # 2. AV/Alpaca chain: group by expiration
    av = _load_av_chain(symbol)
    by_exp: Dict[str, List[float]] = {}
    if av:
        for (strike, exp, side), v in av.items():
            iv = _sf(v.get("iv", 0))
            if 0.01 < iv < 5.0:
                by_exp.setdefault(str(exp), []).append(iv)
    else:
        chain_flat = _load_alpaca_chain(symbol)
        for c in chain_flat:
            iv = c.get("iv", 0)
            if 0.01 < iv < 5.0:
                by_exp.setdefault(c["exp"], []).append(iv)

    if not by_exp:
        return {"term": [], "source": "none"}

    term = []
    for exp, ivs in sorted(by_exp.items()):
        dte = _dte(exp)
        if dte < 0:
            continue
        ivs_sorted = sorted(ivs)
        mid_iv = ivs_sorted[len(ivs_sorted) // 2]
        term.append({
            "expiration": exp,
            "dte":       dte,
            "impliedVolatility": round(mid_iv, 4),
            "iv":        round(mid_iv, 4),
        })

    return {"term": term, "source": "av_computed"}

# ── Mode: greeks ────────────────────────────────────────────────────────────────
def mode_greeks(symbol: str) -> Dict:
    """Real-time greeks list — Intrinio or AV/Alpaca chain."""
    # 1. Intrinio realtime chain
    if INTRINIO_KEY:
        resp = _intrinio(f"/options/chain/{symbol}/realtime")
        if resp and resp.get("chain"):
            greeks = []
            for item in resp["chain"]:
                opt   = item.get("option", {})
                stats = item.get("stats",  {})
                price = item.get("price",  {})
                greeks.append({
                    "contract":          opt.get("code", ""),
                    "type":              opt.get("type", ""),
                    "strikePrice":       _sf(opt.get("strike", 0)),
                    "expiration":        opt.get("expiration", ""),
                    "dte":               _dte(opt.get("expiration", "")),
                    "impliedVolatility": _sf(stats.get("implied_volatility", 0)),
                    "iv":                _sf(stats.get("implied_volatility", 0)),
                    "delta":             _sf(stats.get("delta", 0)),
                    "gamma":             _sf(stats.get("gamma", 0)),
                    "theta":             _sf(stats.get("theta", 0)),
                    "vega":              _sf(stats.get("vega",  0)),
                    "openInterest":      _si(stats.get("open_interest", 0)),
                    "oi":                _si(stats.get("open_interest", 0)),
                    "volume":            _si(price.get("volume", 0)),
                    "vol":               _si(price.get("volume", 0)),
                })
            return {"greeks": greeks, "source": "intrinio"}

    # 2. AV chain (already has greeks)
    av = _load_av_chain(symbol)
    greeks = []
    if av:
        for (strike, exp, side), v in av.items():
            greeks.append({
                "contract":          f"{symbol}_{exp}_{side}_{strike}",
                "type":              side,
                "strikePrice":       strike,
                "expiration":        str(exp),
                "dte":               _dte(str(exp)),
                "impliedVolatility": _sf(v.get("iv", 0)),
                "iv":                _sf(v.get("iv", 0)),
                "delta":             _sf(v.get("delta", 0)),
                "gamma":             _sf(v.get("gamma", 0)),
                "theta":             _sf(v.get("theta", 0)),
                "vega":              _sf(v.get("vega",  0)),
                "openInterest":      _si(v.get("open_interest", 0)),
                "oi":                _si(v.get("open_interest", 0)),
                "volume":            _si(v.get("volume", 0)),
                "vol":               _si(v.get("volume", 0)),
            })
    else:
        # Alpaca
        chain_flat = _load_alpaca_chain(symbol)
        for c in chain_flat:
            greeks.append({
                "contract":          c.get("symbol", ""),
                "type":              c.get("type", ""),
                "strikePrice":       c["strike"],
                "expiration":        c["exp"],
                "dte":               c["dte"],
                "impliedVolatility": c["iv"],
                "iv":                c["iv"],
                "delta":             c["delta"],
                "gamma":             c["gamma"],
                "theta":             c["theta"],
                "vega":              c["vega"],
                "openInterest":      c["oi"],
                "oi":                c["oi"],
                "volume":            c["vol"],
                "vol":               c["vol"],
            })

    return {"greeks": greeks, "source": "av_computed" if av else "alpaca"}

# ── Dispatch ──────────────────────────────────────────────────────────────────
MODES = {
    "unusual":      mode_unusual,
    "implied_move": mode_implied_move,
    "stats":        mode_stats,
    "ivol_rank":    mode_ivol_rank,
    "ivol_term":    mode_ivol_term,
    "greeks":       mode_greeks,
}

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: intrinio_fetch.py SYMBOL MODE"}))
        sys.exit(1)

    symbol = sys.argv[1].strip().upper()
    mode   = sys.argv[2].strip().lower()

    if not symbol or len(symbol) > 12:
        print(json.dumps({"error": "Invalid symbol"}))
        sys.exit(1)

    fn = MODES.get(mode)
    if not fn:
        print(json.dumps({"error": f"Unknown mode: {mode}. Valid: {', '.join(MODES)}"}))
        sys.exit(1)

    try:
        result = fn(symbol)
        # Flatten: spread mode result directly at top level so the API route's
        # { success: true, data: result.data } gives data.unusual / data.stats / etc.
        out = {"success": True, "symbol": symbol, "mode": mode}
        out.update(result)
        print(json.dumps(out))
    except Exception as e:
        sys.stderr.write(f"intrinio_fetch {mode}: {e}\n")
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
