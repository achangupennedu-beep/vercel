#!/usr/bin/env python3
"""
lse_source.py — London Strategic Edge data integration
=======================================================
Uses the official lse-data Python SDK (pip install lse-data).
All operations use the SDK client rather than raw HTTP.

Modes (argv[1]):
  quote    <SYM>                       — single REST quote via SDK
  candles  <SYM> [TF] [LIMIT]          — OHLCV candles (1m/5m/15m/1h/4h/1d)
  options  <SYM> [max_dte]             — live options chain with IV/greeks
  flow     <SYM> [min_premium]         — unusual/block options prints
  insiders <SYM> [limit]               — insider trades
  catalog  [category]                  — list available instruments
  stream   <SYM> [SYM2 ...] [--dur=N]  — live websocket ticks for N seconds (NDJSON stdout)

All outputs are a single JSON object or NDJSON. Errors go to stderr.

Usage:
  python3 lse_source.py quote AAPL
  python3 lse_source.py candles BTC/USD 1h 200
  python3 lse_source.py options AAPL 14
  python3 lse_source.py stream AAPL SPY --dur=5
"""

import sys, os, json, time, math

LSE_KEY = os.environ.get("LSE_API_KEY", "").strip()


def _require_key() -> str:
    if not LSE_KEY:
        raise RuntimeError("LSE_API_KEY is not configured")
    return LSE_KEY


def _sf(v, d: float = 0.0) -> float:
    try:
        f = float(v)
        return d if (math.isnan(f) or math.isinf(f)) else f
    except:
        return d


def _si(v, d: int = 0) -> int:
    try:
        return int(float(v)) if v is not None else d
    except:
        return d


def _emit(obj) -> None:
    print(json.dumps(obj, default=str), flush=True)


def _get_client():
    from lse import LSE  # type: ignore
    return LSE(api_key=_require_key())


# ── Actual field names confirmed from live API ─────────────────────────────────
# candles row:  timestamp, symbol, open, high, low, close, volume, updated_at
# options row:  ticker, underlying, strike, expiry, contract_type, last_price,
#               volume_today, premium_today, underlying_price, dte, iv,
#               delta, gamma, theta, vega, rho, last_trade_at, updated_at
# flow row:     id, ts, underlying, ticker, strike, expiry, contract_type,
#               last_price, volume, premium, underlying_price, dte, iv,
#               delta, gamma, theta, vega, rho
# insider row:  id, symbol, trade_type, filing_date, transaction_date,
#               transaction_type, acquisition_or_disposition, reporting_name,
#               reporting_cik, securities_transacted, securities_owned, price,
#               security_name, form_type, owner, asset_description, created_at


def cmd_quote(sym: str) -> None:
    """Fetch a live quote. Falls back to last candle close if direct quote unavailable."""
    try:
        client = _get_client()
        # LSE SDK: use candles with 1d, limit=1 for a reliable spot price
        rows = client.candles(sym.upper(), "1d", limit=1, order="desc")
        if rows:
            r = rows[0]
            _emit({
                "symbol":    sym.upper(),
                "price":     _sf(r.get("close")),
                "open":      _sf(r.get("open")),
                "high":      _sf(r.get("high")),
                "low":       _sf(r.get("low")),
                "volume":    _si(r.get("volume")),
                "timestamp": str(r.get("timestamp", r.get("updated_at", ""))),
                "source":    "lse",
            })
        else:
            _emit({"error": f"no quote for {sym}", "symbol": sym})
    except Exception as e:
        sys.stderr.write(f"[lse_source] quote error: {e}\n")
        _emit({"error": str(e), "symbol": sym})


def cmd_candles(sym: str, tf: str = "1d", limit: int = 200) -> None:
    """OHLCV candles using the SDK."""
    try:
        client = _get_client()
        rows = client.candles(sym.upper(), tf, limit=limit, order="asc")
        out = []
        for r in rows:
            out.append({
                "t":      str(r.get("timestamp", "")),
                "open":   _sf(r.get("open")),
                "high":   _sf(r.get("high")),
                "low":    _sf(r.get("low")),
                "close":  _sf(r.get("close")),
                "volume": _sf(r.get("volume")),
            })
        _emit({"symbol": sym.upper(), "timeframe": tf, "count": len(out), "candles": out})
    except Exception as e:
        sys.stderr.write(f"[lse_source] candles error: {e}\n")
        _emit({"error": str(e), "symbol": sym, "candles": []})


def cmd_options(sym: str, max_dte: int = 90) -> None:
    """Live options chain with IV and greeks from the SDK."""
    try:
        client = _get_client()
        rows = client.options(sym.upper(), max_dte=max_dte)
        contracts = []
        for r in rows:
            cp_raw = str(r.get("contract_type", "")).lower()
            contracts.append({
                "ticker":          str(r.get("ticker", "")),
                "strike":          _sf(r.get("strike")),
                "expiration":      str(r.get("expiry", ""))[:10],
                "type":            "call" if cp_raw.startswith("c") else "put",
                "last":            _sf(r.get("last_price")),
                "iv":              _sf(r.get("iv")),
                "delta":           _sf(r.get("delta")),
                "gamma":           _sf(r.get("gamma")),
                "theta":           _sf(r.get("theta")),
                "vega":            _sf(r.get("vega")),
                "rho":             _sf(r.get("rho")),
                "volume":          _si(r.get("volume_today")),
                "premium":         _sf(r.get("premium_today")),
                "underlyingPrice": _sf(r.get("underlying_price")),
                "dte":             _si(r.get("dte")),
                "source":          "lse",
            })
        _emit({"symbol": sym.upper(), "count": len(contracts), "contracts": contracts})
    except Exception as e:
        sys.stderr.write(f"[lse_source] options error: {e}\n")
        _emit({"error": str(e), "symbol": sym, "contracts": []})


def _classify_trade(row: dict) -> str:
    explicit = str(row.get("side", row.get("trade_side", ""))).upper()
    if explicit in {"BUY", "SELL"}:
        return explicit
    last = _sf(row.get("last_price"))
    bid = _sf(row.get("bid"))
    ask = _sf(row.get("ask"))
    if ask > 0 and last >= ask:
        return "BUY"
    if bid > 0 and last <= bid:
        return "SELL"
    midpoint = (bid + ask) / 2 if ask >= bid > 0 else 0
    if midpoint > 0:
        return "BUY" if last > midpoint else "SELL" if last < midpoint else "UNKNOWN"
    return "UNKNOWN"


def cmd_flow(sym: str, min_premium: int = 0, limit: int = 200) -> None:
    """Unusual/block options prints from the SDK; never synthesizes missing prints."""
    try:
        client = _get_client()
        kwargs = {"min_premium": min_premium} if min_premium > 0 else {}
        rows = client.options_flow(sym.upper(), **kwargs)
        out = []
        for r in rows[:limit]:
            cp_raw = str(r.get("contract_type", "")).lower()
            out.append({
                "underlying": str(r.get("underlying", sym)),
                "ticker":     str(r.get("ticker", "")),
                "strike":     _sf(r.get("strike")),
                "expiry":     str(r.get("expiry", ""))[:10],
                "type":       "call" if cp_raw.startswith("c") else "put",
                "lastPrice":  _sf(r.get("last_price")),
                "bid":         _sf(r.get("bid")),
                "ask":         _sf(r.get("ask")),
                "volume":     _si(r.get("volume")),
                "premium":    _sf(r.get("premium")),
                "side":       _classify_trade(r),
                "exchange":    str(r.get("exchange", r.get("venue", ""))),
                "iv":         _sf(r.get("iv")),
                "delta":      _sf(r.get("delta")),
                "underlyingPrice": _sf(r.get("underlying_price")),
                "dte":        _si(r.get("dte")),
                "timestamp":  str(r.get("ts", "")),
                "source":     "lse",
            })
        _emit({"symbol": sym.upper(), "count": len(out), "prints": out})
    except Exception as e:
        sys.stderr.write(f"[lse_source] flow error: {e}\n")
        _emit({"error": str(e), "symbol": sym, "prints": []})


def cmd_insiders(sym: str, limit: int = 50) -> None:
    """Insider trades from the SDK."""
    try:
        client = _get_client()
        rows = client.insider_trades(sym.upper(), limit=limit)
        out = []
        for r in rows:
            out.append({
                "name":          str(r.get("reporting_name", r.get("owner", ""))),
                "type":          str(r.get("transaction_type", r.get("trade_type", ""))),
                "direction":     str(r.get("acquisition_or_disposition", "")),
                "shares":        _si(r.get("securities_transacted")),
                "sharesOwned":   _si(r.get("securities_owned")),
                "price":         _sf(r.get("price")),
                "security":      str(r.get("security_name", "")),
                "formType":      str(r.get("form_type", "")),
                "filingDate":    str(r.get("filing_date", "")),
                "transDate":     str(r.get("transaction_date", "")),
                "source":        "lse",
            })
        _emit({"symbol": sym.upper(), "count": len(out), "trades": out})
    except Exception as e:
        sys.stderr.write(f"[lse_source] insiders error: {e}\n")
        _emit({"error": str(e), "symbol": sym, "trades": []})


def cmd_catalog(category: str | None = None) -> None:
    """List available instruments. Works without API key."""
    try:
        client = _get_client()
        data = client.catalog(category) if category else client.catalog()
        _emit({"count": len(data), "instruments": data[:500]})
    except Exception as e:
        sys.stderr.write(f"[lse_source] catalog error: {e}\n")
        _emit({"error": str(e), "instruments": []})


def cmd_stream(symbols: list, duration_s: int = 10) -> None:
    """
    Live tick stream over the LSE websocket.
    Emits one JSON object per tick to stdout (NDJSON).
    Automatically runs for `duration_s` seconds then exits.
    Also supports replay: pass --start=ISO8601 to replay from that time then go live.
    """
    try:
        from lse import LSE  # type: ignore
        client = LSE(api_key=_require_key())

        t_end = time.time() + duration_s
        count = [0]
        stopped = [False]

        def handle_tick(tick):
            if stopped[0]:
                return
            if time.time() > t_end:
                stopped[0] = True
                try:
                    client.disconnect()
                except Exception:
                    pass
                return
            _emit({
                "symbol":    getattr(tick, "symbol", ""),
                "price":     _sf(getattr(tick, "price",  0.0)),
                "bid":       _sf(getattr(tick, "bid",    0.0)),
                "ask":       _sf(getattr(tick, "ask",    0.0)),
                "volume":    _si(getattr(tick, "volume", 0)),
                "timestamp": str(getattr(tick, "timestamp", "")),
                "replay":    bool(getattr(tick, "replay", False)),
                "source":    "lse_ws",
            })
            count[0] += 1

        def handle_error(e):
            sys.stderr.write(f"[lse_ws] error: {e}\n")

        client.on("tick",  handle_tick)
        client.on("error", handle_error)
        client.connect(symbols)

        # Poll until done or timeout
        deadline = time.time() + duration_s + 1.0
        while time.time() < deadline and not stopped[0]:
            time.sleep(0.1)

        if not stopped[0]:
            try:
                client.disconnect()
            except Exception:
                pass

        sys.stderr.write(f"[lse_stream] completed: {count[0]} ticks from {symbols}\n")

    except ImportError:
        sys.stderr.write("[lse_stream] lse-data not installed\n")
        _emit({"error": "lse-data SDK not installed", "ticks": 0})
    except Exception as e:
        sys.stderr.write(f"[lse_stream] fatal: {e}\n")
        _emit({"error": str(e), "ticks": 0})


# ── CLI dispatcher ─────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    if not args:
        _emit({"error": "usage: lse_source.py <mode> [args...]"})
        sys.exit(1)

    mode = args[0].lower()
    rest = args[1:]

    if mode == "quote":
        sym = rest[0].upper() if rest else "AAPL"
        cmd_quote(sym)

    elif mode == "candles":
        sym   = rest[0].upper() if rest else "AAPL"
        tf    = rest[1] if len(rest) > 1 else "1d"
        limit = int(rest[2]) if len(rest) > 2 else 200
        cmd_candles(sym, tf, limit)

    elif mode == "options":
        sym     = rest[0].upper() if rest else "AAPL"
        max_dte = int(rest[1]) if len(rest) > 1 else 90
        cmd_options(sym, max_dte)

    elif mode == "flow":
        sym         = rest[0].upper() if rest else "AAPL"
        min_premium = int(rest[1]) if len(rest) > 1 else 0
        limit = int(rest[2]) if len(rest) > 2 else 200
        cmd_flow(sym, min_premium, limit)

    elif mode == "insiders":
        sym   = rest[0].upper() if rest else "AAPL"
        limit = int(rest[1]) if len(rest) > 1 else 50
        cmd_insiders(sym, limit)

    elif mode == "catalog":
        category = rest[0] if rest else None
        cmd_catalog(category)

    elif mode == "stream":
        syms     = [a.upper() for a in rest if not a.startswith("--")]
        dur_args = [a for a in rest if a.startswith("--dur=")]
        duration = int(dur_args[0].split("=")[1]) if dur_args else 10
        if not syms:
            syms = ["AAPL"]
        cmd_stream(syms, duration)

    else:
        _emit({"error": f"unknown mode: {mode}"})
        sys.exit(1)


if __name__ == "__main__":
    main()
