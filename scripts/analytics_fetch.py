#!/usr/bin/env python3
"""
APEX Terminal — Analytics Fetch Script
========================================
Calls advanced_analytics.py engines with live options chain data.
Modes: darkpool, ndp, variance, svi, cob, oobi, sweep, gex, vpin, hiro, all
"""

import sys, json, os, math, time
sys.path.insert(0, os.path.dirname(__file__))

from data_sources import (
    av_realtime_options, finnhub_quote, finnhub_option_chain,
    massive_snapshot, massive_hist_iv, twelvedata_options,
    _sf, _si,
)
from advanced_analytics import (
    detect_dark_pool_prints, compute_ndp, compute_oobi,
    compute_variance_swap, fit_svi_surface, detect_cob_patterns,
    compute_toxic_flow, compute_gex_profile, compute_vpin,
    compute_hiro, detect_anomalies, compute_local_vol_surface,
    compute_vanna_surface, compute_vrp, compute_iv_smile_forecast,
)

def _load_chain(symbol: str, expiration: str) -> tuple:
    """Load the options chain from the best available source."""
    calls, puts, spot = [], [], 0.0

    # 1. Alpha Vantage REALTIME_OPTIONS (most complete for analytics)
    try:
        av = av_realtime_options(symbol)
        if av:
            for key, v in av.items():
                strike, exp, side = key
                if expiration and exp != expiration:
                    continue
                row = {**v, "strike": strike, "expiration": exp,
                       "type": side, "symbol": symbol}
                if side == "call":
                    calls.append(row)
                else:
                    puts.append(row)
    except Exception as e:
        sys.stderr.write(f"av chain: {e}\n")

    # 2. Spot price from Finnhub
    try:
        q = finnhub_quote(symbol)
        if q: spot = _sf(q.get("c", 0))
    except Exception as e:
        sys.stderr.write(f"finnhub spot: {e}\n")

    # 3. Fallback: Finnhub options
    if not calls:
        try:
            fh = finnhub_option_chain(symbol)
            if fh and isinstance(fh, dict):
                for item in fh.get("data", []):
                    for c in item.get("options", {}).get("CALL", []):
                        calls.append({
                            "strike": _sf(c.get("strike")),
                            "expiration": item.get("expirationDate",""),
                            "type": "call", "iv": _sf(c.get("impliedVolatility")),
                            "delta": _sf(c.get("delta")), "gamma": _sf(c.get("gamma")),
                            "theta": _sf(c.get("theta")), "vega": _sf(c.get("vega")),
                            "bid": _sf(c.get("bid")), "ask": _sf(c.get("ask")),
                            "volume": _si(c.get("volume")),
                            "openInterest": _si(c.get("openInterest")),
                            "mid": (_sf(c.get("bid")) + _sf(c.get("ask"))) / 2,
                        })
                    for p in item.get("options", {}).get("PUT", []):
                        puts.append({
                            "strike": _sf(p.get("strike")),
                            "expiration": item.get("expirationDate",""),
                            "type": "put", "iv": _sf(p.get("impliedVolatility")),
                            "delta": _sf(p.get("delta")), "gamma": _sf(p.get("gamma")),
                            "theta": _sf(p.get("theta")), "vega": _sf(p.get("vega")),
                            "bid": _sf(p.get("bid")), "ask": _sf(p.get("ask")),
                            "volume": _si(p.get("volume")),
                            "openInterest": _si(p.get("openInterest")),
                            "mid": (_sf(p.get("bid")) + _sf(p.get("ask"))) / 2,
                        })
        except Exception as e:
            sys.stderr.write(f"finnhub options: {e}\n")

    # 4. Final fallback: yfinance
    if not calls:
        try:
            import yfinance as yf
            tk = yf.Ticker(symbol)
            if not spot:
                info = tk.info
                spot = _sf(info.get("currentPrice") or info.get("regularMarketPrice", 0))
            exps = ([expiration] if expiration and expiration in tk.options
                    else list(tk.options[:6]))
            for exp in exps:
                oc = tk.option_chain(exp)
                for _, row in oc.calls.iterrows():
                    calls.append({
                        "strike": float(row["strike"]),
                        "expiration": exp, "type": "call",
                        "iv": float(row.get("impliedVolatility",0) or 0),
                        "bid": float(row.get("bid",0) or 0),
                        "ask": float(row.get("ask",0) or 0),
                        "volume": int(row.get("volume",0) or 0),
                        "openInterest": int(row.get("openInterest",0) or 0),
                        "mid": (float(row.get("bid",0) or 0) + float(row.get("ask",0) or 0)) / 2,
                        "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0,
                    })
                for _, row in oc.puts.iterrows():
                    puts.append({
                        "strike": float(row["strike"]),
                        "expiration": exp, "type": "put",
                        "iv": float(row.get("impliedVolatility",0) or 0),
                        "bid": float(row.get("bid",0) or 0),
                        "ask": float(row.get("ask",0) or 0),
                        "volume": int(row.get("volume",0) or 0),
                        "openInterest": int(row.get("openInterest",0) or 0),
                        "mid": (float(row.get("bid",0) or 0) + float(row.get("ask",0) or 0)) / 2,
                        "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0,
                    })
        except Exception as e:
            sys.stderr.write(f"yfinance fallback: {e}\n")

    return calls, puts, spot

def main():
    t0 = time.perf_counter()
    symbol     = sys.argv[1].upper() if len(sys.argv) > 1 else "AAPL"
    expiration = sys.argv[2]         if len(sys.argv) > 2 else ""
    mode       = sys.argv[3].lower() if len(sys.argv) > 3 else "all"

    calls, puts, spot = _load_chain(symbol, expiration)
    all_contracts = calls + puts

    result = {
        "symbol": symbol, "expiration": expiration, "mode": mode,
        "spot": spot, "fetchMs": round((time.perf_counter() - t0) * 1000),
    }

    # ── Dark Pool Detection ────────────────────────────────────────────────────
    if mode in ("darkpool", "all"):
        try:
            result["darkPool"] = detect_dark_pool_prints(all_contracts, spot)
        except Exception as e:
            sys.stderr.write(f"darkpool: {e}\n")
            result["darkPool"] = []

    # ── Net Dealer Position ────────────────────────────────────────────────────
    if mode in ("ndp", "all"):
        try:
            result["ndp"] = compute_ndp(calls, puts, spot)
        except Exception as e:
            sys.stderr.write(f"ndp: {e}\n")
            result["ndp"] = {}

    # ── OOBI (Order Book Imbalance) ────────────────────────────────────────────
    if mode in ("oobi", "all"):
        try:
            result["oobi"] = compute_oobi(calls, puts, spot)
        except Exception as e:
            sys.stderr.write(f"oobi: {e}\n")
            result["oobi"] = {}

    # ── Variance Swap Pricing ──────────────────────────────────────────────────
    if mode in ("variance", "all"):
        try:
            result["variance"] = compute_variance_swap(calls, puts, spot)
        except Exception as e:
            sys.stderr.write(f"variance: {e}\n")
            result["variance"] = {}

    # ── SVI Surface Fit ────────────────────────────────────────────────────────
    if mode in ("svi", "all"):
        try:
            result["svi"] = fit_svi_surface(calls + puts, spot)
        except Exception as e:
            sys.stderr.write(f"svi: {e}\n")
            result["svi"] = {}

    # ── COB Multi-leg Patterns ─────────────────────────────────────────────────
    if mode in ("cob", "all"):
        try:
            result["cob"] = detect_cob_patterns(all_contracts, spot)
        except Exception as e:
            sys.stderr.write(f"cob: {e}\n")
            result["cob"] = []

    # ── Toxic Flow ────────────────────────────────────────────────────────────
    if mode in ("toxic", "all"):
        try:
            result["toxicFlow"] = compute_toxic_flow(all_contracts, spot)
        except Exception as e:
            sys.stderr.write(f"toxic: {e}\n")
            result["toxicFlow"] = {}

    # ── GEX Profile ───────────────────────────────────────────────────────────
    if mode in ("gex", "all"):
        try:
            result["gex"] = compute_gex_profile(calls, puts, spot)
        except Exception as e:
            sys.stderr.write(f"gex: {e}\n")
            result["gex"] = {}

    # ── VPIN ──────────────────────────────────────────────────────────────────
    if mode in ("vpin", "all"):
        try:
            result["vpin"] = compute_vpin(all_contracts)
        except Exception as e:
            sys.stderr.write(f"vpin: {e}\n")
            result["vpin"] = {}

    # ── HIRO ──────────────────────────────────────────────────────────────────
    if mode in ("hiro", "all"):
        try:
            result["hiro"] = compute_hiro(calls, puts, spot)
        except Exception as e:
            sys.stderr.write(f"hiro: {e}\n")
            result["hiro"] = {}

    # ── Anomalies (Banushev) ──────────────────────────────────────────────────
    if mode in ("anomalies", "all"):
        try:
            result["anomalies"] = detect_anomalies(all_contracts, spot)
        except Exception as e:
            sys.stderr.write(f"anomalies: {e}\n")
            result["anomalies"] = []

    # ── Local Vol Surface ─────────────────────────────────────────────────────
    if mode in ("localvol", "all"):
        try:
            result["localVol"] = compute_local_vol_surface(calls + puts, spot)
        except Exception as e:
            sys.stderr.write(f"localvol: {e}\n")
            result["localVol"] = {}

    # ── Variance Risk Premium ─────────────────────────────────────────────────
    if mode in ("vrp", "all"):
        try:
            result["vrp"] = compute_vrp(all_contracts, spot)
        except Exception as e:
            sys.stderr.write(f"vrp: {e}\n")
            result["vrp"] = {}

    # ── IV Smile Forecast ────────────────────────────────────────────────────
    if mode in ("ivforecast", "all"):
        try:
            result["ivSmileForecast"] = compute_iv_smile_forecast(all_contracts, spot)
        except Exception as e:
            sys.stderr.write(f"ivforecast: {e}\n")
            result["ivSmileForecast"] = {}

    result["computeMs"] = round((time.perf_counter() - t0) * 1000)
    print(json.dumps(result))

if __name__ == "__main__":
    main()
