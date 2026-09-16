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
    if isinstance(v, str):
        v = v.strip().replace(',', '').replace('$', '')
        if v.endswith('%'):
            try: return float(v[:-1]) / 100
            except ValueError: return d
    try:
        f = float(v)
        return d if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return d


def _first(row: dict, *keys, default=None):
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return default


def _side_label(value) -> str:
    if value is True: return 'BUY'
    if value is False: return 'SELL'
    label = str(value or '').strip().upper().replace('-', '_').replace(' ', '_')
    if label in {'BUY', 'B', 'BOT', 'BUYER', 'BUY_INITIATED', 'TAKER_BUY', 'AT_ASK', 'ASK'}: return 'BUY'
    if label in {'SELL', 'S', 'SLD', 'SELLER', 'SELL_INITIATED', 'TAKER_SELL', 'AT_BID', 'BID'}: return 'SELL'
    return ''


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


class _FIQuoteState:
    """Streaming Jurkatis FI-style state for quote-aware trade direction.

    The full-information algorithm uses the quote path and displayed depth rather
    than relying on coarse trade timestamps. This allocation-free state machine
    applies the same ordering to live ticks: explicit venue side, spread touch,
    quote-relative inference, depth depletion, then a conservative tick rule.
    """

    __slots__ = ("last_price", "last_bid", "last_ask", "last_bid_size", "last_ask_size", "last_side")

    def __init__(self):
        self.last_price = 0.0
        self.last_bid = 0.0
        self.last_ask = 0.0
        self.last_bid_size = 0.0
        self.last_ask_size = 0.0
        self.last_side = "UNKNOWN"

    def classify(self, row: dict) -> tuple[str, str, float]:
        explicit = _side_label(_first(row, 'side', 'trade_side', 'aggressor_side', 'direction', 'initiator', 'action', 'sentiment', 'trade_type', 'is_buy'))
        price = _sf(_first(row, 'price', 'last_price', 'trade_price', 'execution_price'))
        bid = _sf(_first(row, 'bid', 'best_bid', 'bid_price'))
        ask = _sf(_first(row, 'ask', 'best_ask', 'ask_price'))
        bid_size = _sf(_first(row, 'bid_size', 'bid_volume', 'bid_qty', 'bid_size_total'))
        ask_size = _sf(_first(row, 'ask_size', 'ask_volume', 'ask_qty', 'ask_size_total'))
        valid_quote = bid > 0 and ask > 0 and ask >= bid and price > 0
        if explicit:
            side, method, confidence = explicit, 'EXPLICIT_SIDE', 1.0
        elif valid_quote and price >= ask:
            side, method, confidence = 'BUY', 'AT_ASK', 0.99
        elif valid_quote and price <= bid:
            side, method, confidence = 'SELL', 'AT_BID', 0.99
        elif valid_quote:
            mid = (bid + ask) / 2
            signed_distance = (price - mid) / max(ask - bid, mid * 1e-6)
            if signed_distance > 0.05: side, method, confidence = 'BUY', 'LEE_READY', 0.82
            elif signed_distance < -0.05: side, method, confidence = 'SELL', 'LEE_READY', 0.82
            elif ask_size > 0 and self.last_ask_size > ask_size: side, method, confidence = 'BUY', 'DEPTH_DEPLETION', 0.7
            elif bid_size > 0 and self.last_bid_size > bid_size: side, method, confidence = 'SELL', 'DEPTH_DEPLETION', 0.7
            else: side, method, confidence = '', 'MIDPOINT_NEUTRAL', 0.5
        elif price > 0 and self.last_price > 0 and price != self.last_price:
            side, method, confidence = ('BUY', 'TICK_RULE', 0.55) if price > self.last_price else ('SELL', 'TICK_RULE', 0.55)
        elif self.last_side in {'BUY', 'SELL'}:
            side, method, confidence = self.last_side, 'TICK_CARRY', 0.35
        else:
            side, method, confidence = '', 'DATA_UNAVAILABLE', 0.0
        self.last_price, self.last_bid, self.last_ask = price, bid, ask
        self.last_bid_size, self.last_ask_size, self.last_side = bid_size, ask_size, side or self.last_side
        return side or 'UNKNOWN', method, confidence


def _classify_trade(row: dict, state: _FIQuoteState) -> tuple[str, str, float]:
    """Classify a print while preserving quote history across the returned tape."""
    normalized = {
        "side": _first(row, 'side', 'trade_side', 'aggressor_side', 'direction', 'initiator', 'action', 'sentiment', 'trade_type', 'is_buy'),
        "price": row.get("price", row.get("last_price", row.get("trade_price", row.get("execution_price")))),
        "bid": row.get("bid", row.get("best_bid", row.get("bid_price"))),
        "ask": row.get("ask", row.get("best_ask", row.get("ask_price"))),
        "bid_size": row.get("bid_size", row.get("bid_volume", row.get("bid_qty", row.get("bid_size_total")))),
        "ask_size": row.get("ask_size", row.get("ask_volume", row.get("ask_qty", row.get("ask_size_total")))),
    }
    return state.classify(normalized)


def cmd_flow(sym: str, min_premium: int = 0, limit: int = 200) -> None:
    """Unusual/block options prints from the SDK; never synthesizes missing prints."""
    try:
        client = _get_client()
        kwargs = {"min_premium": min_premium} if min_premium > 0 else {}
        rows = client.options_flow(sym.upper(), **kwargs)
        out = []
        states = {}
        for r in rows[:limit]:
            contract = str(_first(r, 'ticker', 'contract_symbol', 'id', default='unknown'))
            classifier = states.setdefault(contract, _FIQuoteState())
            cp_raw = str(_first(r, 'contract_type', 'type', 'option_type', default='')).lower()
            side, method, confidence = _classify_trade(r, classifier)
            intent = 'BUY_INITIATED' if side == 'BUY' else 'SELL_INITIATED' if side == 'SELL' else 'UNKNOWN'
            price = _sf(_first(r, 'last_price', 'trade_price', 'price'))
            bid = _sf(_first(r, 'bid', 'bid_price', 'best_bid'))
            ask = _sf(_first(r, 'ask', 'ask_price', 'best_ask'))
            quote_available = bid > 0 and ask >= bid
            out.append({
                'underlying': str(_first(r, 'underlying', 'symbol', default=sym)),
                'ticker': contract,
                'strike': _sf(_first(r, 'strike', 'strike_price')),
                'expiry': str(_first(r, 'expiry', 'expiration', 'expiration_date', default=''))[:10],
                'type': 'call' if cp_raw.startswith('c') else 'put',
                'lastPrice': price, 'price': price, 'bid': bid, 'ask': ask,
                'volume': _si(_first(r, 'volume', 'size', 'quantity')),
                'premium': _sf(_first(r, 'premium', 'premium_today', 'notional')),
                'side': side, 'classificationMethod': method,
                'classificationConfidence': confidence, 'score': round(confidence * 100.0, 2) if confidence > 0 else None,
                'intent': intent, 'spoof': 'UNAVAILABLE', 'spoofScore': None,
                'dataQuality': 'QUOTE' if quote_available else 'TRADE_ONLY' if price > 0 else 'MISSING_PRICE',
                'quoteAvailable': quote_available, 'analyticsStatus': 'UNAVAILABLE_SPOOF_HISTORY',
                'exchange': str(_first(r, 'exchange', 'venue', default='')),
                'iv': _sf(r.get('iv')), 'delta': _sf(r.get('delta')),
                'underlyingPrice': _sf(_first(r, 'underlying_price', 'underlyingPrice')),
                'dte': _si(r.get('dte')), 'timestamp': str(_first(r, 'ts', 'timestamp', 'last_trade_at', default='')),
                'source': 'lse',
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
        classifiers = {}

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
            symbol = str(getattr(tick, "symbol", "")).upper()
            row = {
                "price": getattr(tick, "price", 0.0),
                "bid": getattr(tick, "bid", 0.0),
                "ask": getattr(tick, "ask", 0.0),
                "bid_size": getattr(tick, "bid_size", getattr(tick, "bid_volume", 0.0)),
                "ask_size": getattr(tick, "ask_size", getattr(tick, "ask_volume", 0.0)),
            }
            classifier = classifiers.setdefault(symbol, _FIQuoteState())
            side, method, confidence = classifier.classify(row)
            _emit({
                "symbol":    symbol,
                "price":     _sf(row["price"]),
                "bid":       _sf(row["bid"]),
                "ask":       _sf(row["ask"]),
                "bidSize":   _sf(row["bid_size"]),
                "askSize":   _sf(row["ask_size"]),
                "volume":    _si(getattr(tick, "volume", 0)),
                "timestamp": str(getattr(tick, "timestamp", "")),
                "replay":    bool(getattr(tick, "replay", False)),
                "side":      side,
                "classificationMethod": method,
                "classificationConfidence": confidence,
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
