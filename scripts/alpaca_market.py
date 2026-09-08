#!/usr/bin/env python3
"""
alpaca_market.py — Comprehensive Alpaca free-tier market data module.
Paper trading keys. NYSE market-hours gate enforced for all real-time calls.
Free-tier constraints honoured:
- IEX feed: real-time (~2% of volume); used for all real-time stock calls.
- delayed_sip: 15-min delay, 100% coverage; used as historical fallback.
- Options: indicative feed (free tier).
- WebSocket subscriptions: capped at 30 symbols.
- Rate limit: 200 req/min (both data + trading APIs).
- Historical data: 200 req/min, no look-back restriction on free tier.

Credentials are read ONLY from environment variables:
  APCA_API_KEY_ID, APCA_API_SECRET_KEY
No hardcoded fallback values are provided — set these in your environment
before running.
"""
from __future__ import annotations

import sys, json, os, math, time, socket, urllib.request, urllib.error, urllib.parse, threading
from datetime import datetime, timezone, timedelta, date as _date

# ── Credentials ───────────────────────────────────────────────────────────────
APCA_KEY = os.environ.get("APCA_API_KEY_ID", "")
APCA_SEC = os.environ.get("APCA_API_SECRET_KEY", "")

DATA_BASE_V2 = "https://data.alpaca.markets/v2"          # stocks market data
DATA_BASE_V1BETA = "https://data.alpaca.markets/v1beta1"  # options + news market data
TRADING_BASE = "https://paper-api.alpaca.markets/v2"      # paper trading / account / clock / contracts

HDRS = {
    "APCA-API-KEY-ID": APCA_KEY,
    "APCA-API-SECRET-KEY": APCA_SEC,
    "Accept": "application/json",
}

# ── Rate-limit budget tracker (200 req/min) ───────────────────────────────────
_rl_lock = threading.Lock()
_rl_window: list[float] = []
MAX_RPM = 200  # requests per minute (sliding 60s window below), not per second


def _rl_gate() -> None:
    with _rl_lock:
        now = time.monotonic()
        _rl_window[:] = [t for t in _rl_window if now - t < 60.0]
        if len(_rl_window) >= MAX_RPM:
            sleep_for = 60.0 - (now - _rl_window[0]) + 0.01
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            _rl_window[:] = [t for t in _rl_window if now - t < 60.0]
        _rl_window.append(time.monotonic())


# ── NYSE market-hours gate ────────────────────────────────────────────────────
_NYSE_TZ_OFFSET = -4
_clock_cache: dict = {}


def _fetch_clock() -> dict:
    global _clock_cache
    now = time.monotonic()
    if _clock_cache and (now - _clock_cache.get("_ts", 0)) < 60:
        return _clock_cache
    try:
        _rl_gate()
        url = f"{TRADING_BASE}/clock"
        req = urllib.request.Request(url, headers=HDRS)
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read().decode())
            _clock_cache = {**d, "_ts": now}
            return _clock_cache
    except Exception as e:
        sys.stderr.write(f"clock fetch: {e}\n")
        utc_now = datetime.now(timezone.utc)
        et_hour = (utc_now.hour + _NYSE_TZ_OFFSET) % 24
        et_dow = utc_now.weekday()
        is_open = (et_dow < 5 and 9 <= et_hour < 16 and not (et_hour == 9 and utc_now.minute < 30))
        fallback = {"is_open": is_open, "_ts": now, "_fallback": True}
        _clock_cache = fallback
        return fallback


def is_market_open() -> bool:
    return bool(_fetch_clock().get("is_open", False))


def market_status() -> dict:
    c = _fetch_clock()
    return {
        "is_open": c.get("is_open", False),
        "next_open": c.get("next_open", ""),
        "next_close": c.get("next_close", ""),
        "timestamp": c.get("timestamp", ""),
        "source": "alpaca_clock" if not c.get("_fallback") else "local_fallback",
    }


# ── HTTP helper ───────────────────────────────────────────────────────────────
_TRANSIENT_ERRORS = (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError)


def _get(path: str, params: dict | None = None, base: str = DATA_BASE_V2, timeout: int = 10, raise_on_429: bool = True, max_attempts: int = 3) -> dict:
    qs = ("?" + "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in (params or {}).items())) if params else ""
    url = f"{base}{path}{qs}"
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        _rl_gate()
        try:
            req = urllib.request.Request(url, headers=HDRS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                if raise_on_429 and attempt == max_attempts - 1:
                    raise
                wait = min(2 ** attempt * 0.5, 4.0)
                sys.stderr.write(f"429 on {path} — retry in {wait:.1f}s\n")
                time.sleep(wait)
                continue
            if 500 <= e.code < 600 and attempt < max_attempts - 1:
                wait = min(2 ** attempt * 0.5, 4.0)
                sys.stderr.write(f"{e.code} on {path} — retry in {wait:.1f}s\n")
                time.sleep(wait)
                continue
            raise
        except _TRANSIENT_ERRORS as e:
            last_err = e
            if attempt == max_attempts - 1:
                raise
            wait = min(2 ** attempt * 0.5, 4.0)
            sys.stderr.write(f"network error on {path} ({e}) — retry in {wait:.1f}s\n")
            time.sleep(wait)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"bad JSON from {path}: {e}\n")
            return {}
    if last_err:
        raise last_err
    return {}


# ── Safe converters ───────────────────────────────────────────────────────────
def sf(v, d: float = 0.0) -> float:
    try:
        f = float(v)
        return d if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return d


def si(v, d: int = 0) -> int:
    try:
        return int(float(v)) if v is not None else d
    except (TypeError, ValueError):
        return d


def _bar(b: dict) -> dict:
    ts = b.get("t", "") or ""
    ms = 0
    if ts:
        try:
            ms = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            pass
    return {
        "t": ms, "ts": ts,
        "o": sf(b.get("o")), "h": sf(b.get("h")), "l": sf(b.get("l")), "c": sf(b.get("c")),
        "v": si(b.get("v")), "vw": sf(b.get("vw")), "n": si(b.get("n")),
    }


def _chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# ── 1. STOCK QUOTES (latest) ─────────────────────────────────────────────────
def stock_quotes_latest(symbols: list[str], gate: bool = True) -> dict:
    if not symbols:
        return {}
    if gate and not is_market_open():
        return {"_market_closed": True, "symbols": symbols}
    out = {}
    for chunk in _chunks(symbols, 30):
        syms = ",".join(chunk)
        try:
            d = _get("/stocks/quotes/latest", {"symbols": syms, "feed": "iex"})
            for sym, q in d.get("quotes", {}).items():
                out[sym] = {
                    "bid": sf(q.get("bp")), "ask": sf(q.get("ap")),
                    "bidSize": si(q.get("bs")), "askSize": si(q.get("as")),
                    "bidExch": q.get("bx", ""), "askExch": q.get("ax", ""),
                    "condition": q.get("c", []), "timestamp": q.get("t", ""), "feed": "iex",
                }
        except Exception as e:
            sys.stderr.write(f"stock_quotes_latest {syms}: {e}\n")
    return out


# ── 2. STOCK BARS (latest intraday) ──────────────────────────────────────────
def stock_bars_latest(symbols: list[str], gate: bool = True) -> dict:
    if not symbols:
        return {}
    if gate and not is_market_open():
        return {"_market_closed": True}
    out = {}
    for chunk in _chunks(symbols, 30):
        syms = ",".join(chunk)
        try:
            d = _get("/stocks/bars/latest", {"symbols": syms, "feed": "iex"})
            for sym, b in d.get("bars", {}).items():
                out[sym] = _bar(b)
        except Exception as e:
            sys.stderr.write(f"stock_bars_latest {syms}: {e}\n")
    return out


# ── 3. STOCK BARS (historical) ────────────────────────────────────────────────
APCA_TF = {
    "1m": "1Min", "5m": "5Min", "15m": "15Min", "30m": "30Min",
    "1h": "1Hour", "4h": "4Hour", "1d": "1Day", "1wk": "1Week", "1mo": "1Month",
}
_MAX_PAGES = 50


def stock_bars_historical(symbols: list[str], timeframe: str, start: str, end: str, feed: str = "iex", limit: int = 1000, adjustment: str = "raw") -> dict:
    if not symbols:
        return {}
    tf = APCA_TF.get(timeframe, timeframe)
    out: dict[str, list] = {s: [] for s in symbols}
    syms = ",".join(symbols)
    params: dict = {
        "symbols": syms, "timeframe": tf, "start": start, "end": end,
        "limit": min(limit, 10000), "sort": "asc", "feed": feed, "adjustment": adjustment,
    }
    for _page in range(_MAX_PAGES):
        try:
            d = _get("/stocks/bars", params)
        except Exception as e:
            sys.stderr.write(f"stock_bars_historical: {e}\n")
            break
        for sym, bars in d.get("bars", {}).items():
            if sym in out:
                for b in bars:
                    if len(out[sym]) >= 5000:
                        break
                    out[sym].append(_bar(b))
        npt = d.get("next_page_token")
        if not npt or all(len(v) >= 5000 for v in out.values()):
            break
        params["page_token"] = npt
    else:
        sys.stderr.write(f"stock_bars_historical: stopped after {_MAX_PAGES} pages\n")
    params.pop("page_token", None)
    return out


# ── 4. STOCK TRADES (latest) ──────────────────────────────────────────────────
def stock_trades_latest(symbols: list[str], gate: bool = True) -> dict:
    if not symbols:
        return {}
    if gate and not is_market_open():
        return {"_market_closed": True}
    out = {}
    for chunk in _chunks(symbols, 30):
        syms = ",".join(chunk)
        try:
            d = _get("/stocks/trades/latest", {"symbols": syms, "feed": "iex"})
            for sym, t in d.get("trades", {}).items():
                out[sym] = {
                    "price": sf(t.get("p")), "size": si(t.get("s")), "exchange": t.get("x", ""),
                    "timestamp": t.get("t", ""), "conditions": t.get("c", []), "tape": t.get("z", ""), "id": si(t.get("i")),
                }
        except Exception as e:
            sys.stderr.write(f"stock_trades_latest {syms}: {e}\n")
    return out


# ── 5. STOCK SNAPSHOTS ────────────────────────────────────────────────────────
def stock_snapshots(symbols: list[str], gate: bool = True) -> dict:
    if not symbols:
        return {}
    if gate and not is_market_open():
        return {"_market_closed": True}
    out = {}
    for chunk in _chunks(symbols, 30):
        syms = ",".join(chunk)
        try:
            d = _get("/stocks/snapshots", {"symbols": syms, "feed": "iex"})
            for sym, snap in d.get("snapshots", {}).items():
                lt = snap.get("latestTrade") or {}
                lq = snap.get("latestQuote") or {}
                mb = snap.get("minuteBar") or {}
                db = snap.get("dailyBar") or {}
                pb = snap.get("prevDailyBar") or {}
                price = sf(lt.get("p") or ((sf(lq.get("bp")) + sf(lq.get("ap"))) / 2 if lq.get("bp") else 0) or sf(db.get("c")))
                prev = sf(pb.get("c"))
                out[sym] = {
                    "price": price, "bid": sf(lq.get("bp")), "ask": sf(lq.get("ap")),
                    "bidSize": si(lq.get("bs")), "askSize": si(lq.get("as")),
                    "lastTradePrice": sf(lt.get("p")), "lastTradeSize": si(lt.get("s")),
                    "open": sf(db.get("o")), "high": sf(db.get("h")), "low": sf(db.get("l")), "close": sf(db.get("c")),
                    "volume": si(db.get("v")), "vwap": sf(db.get("vw")), "prevClose": prev,
                    "change": round(price - prev, 4) if prev else 0,
                    "changePct": round((price - prev) / prev * 100, 4) if prev else 0,
                    "minuteBar": _bar(mb) if mb else None, "timestamp": lt.get("t", ""),
                }
        except Exception as e:
            sys.stderr.write(f"stock_snapshots {syms}: {e}\n")
    return out


# ── 6. OPTION SNAPSHOTS (indicative feed) ────────────────────────────────────
def option_snapshots(symbols: list[str], gate: bool = True) -> dict:
    if not symbols:
        return {}
    if gate and not is_market_open():
        return {"_market_closed": True}
    out = {}
    for chunk in _chunks(symbols, 100):
        syms = ",".join(chunk)
        try:
            d = _get("/options/snapshots", {"symbols": syms, "feed": "indicative"}, base=DATA_BASE_V1BETA)
            for sym, snap in d.get("snapshots", {}).items():
                lq = snap.get("latestQuote") or {}
                lt = snap.get("latestTrade") or {}
                gr = snap.get("greeks") or {}
                out[sym] = {
                    "bid": sf(lq.get("bp")), "ask": sf(lq.get("ap")), "bidSize": si(lq.get("bs")), "askSize": si(lq.get("as")),
                    "mid": round((sf(lq.get("bp")) + sf(lq.get("ap"))) / 2, 4), "lastTradePrice": sf(lt.get("p")), "lastTradeSize": si(lt.get("s")),
                    "iv": sf(snap.get("impliedVolatility")), "delta": sf(gr.get("delta")), "gamma": sf(gr.get("gamma")),
                    "theta": sf(gr.get("theta")), "vega": sf(gr.get("vega")), "rho": sf(gr.get("rho")), "openInterest": si(snap.get("openInterest")),
                    "timestamp": lq.get("t", ""),
                }
        except Exception as e:
            sys.stderr.write(f"option_snapshots {syms[:80]}: {e}\n")
    return out


# ── 7. OPTION QUOTES (latest) ──────────────────────────────────────────��──────
def option_quotes_latest(symbols: list[str], gate: bool = True) -> dict:
    if not symbols:
        return {}
    if gate and not is_market_open():
        return {"_market_closed": True}
    out = {}
    for chunk in _chunks(symbols, 100):
        syms = ",".join(chunk)
        try:
            d = _get("/options/quotes/latest", {"symbols": syms, "feed": "indicative"}, base=DATA_BASE_V1BETA)
            for sym, q in d.get("quotes", {}).items():
                out[sym] = {
                    "bid": sf(q.get("bp")), "ask": sf(q.get("ap")), "bidSize": si(q.get("bs")),
                    "askSize": si(q.get("as")), "timestamp": q.get("t", ""), "condition": q.get("c", ""),
                }
        except Exception as e:
            sys.stderr.write(f"option_quotes_latest: {e}\n")
    return out


# ── 8. OPTION TRADES (latest) ─────────────────────────────────────────────────
def option_trades_latest(symbols: list[str], gate: bool = True) -> dict:
    if not symbols:
        return {}
    if gate and not is_market_open():
        return {"_market_closed": True}
    out = {}
    for chunk in _chunks(symbols, 100):
        syms = ",".join(chunk)
        try:
            d = _get("/options/trades/latest", {"symbols": syms, "feed": "indicative"}, base=DATA_BASE_V1BETA)
            for sym, t in d.get("trades", {}).items():
                out[sym] = {
                    "price": sf(t.get("p")), "size": si(t.get("s")), "exchange": t.get("x", ""),
                    "timestamp": t.get("t", ""), "condition": t.get("c", ""),
                }
        except Exception as e:
            sys.stderr.write(f"option_trades_latest: {e}\n")
    return out


# ── 9. OPTION BARS (historical) ───────────────────────────────────────────────
def option_bars_historical(symbols: list[str], timeframe: str, start: str, end: str, limit: int = 1000) -> dict:
    if not symbols:
        return {}
    tf = APCA_TF.get(timeframe, timeframe)
    out: dict[str, list] = {s: [] for s in symbols}
    syms = ",".join(symbols)
    params: dict = {
        "symbols": syms, "timeframe": tf, "start": start, "end": end,
        "limit": min(limit, 10000), "sort": "asc",
    }
    for _page in range(_MAX_PAGES):
        try:
            d = _get("/options/bars", params, base=DATA_BASE_V1BETA)
        except Exception as e:
            sys.stderr.write(f"option_bars_historical: {e}\n")
            break
        for sym, bars in d.get("bars", {}).items():
            if sym in out:
                for b in bars:
                    if len(out[sym]) >= 2000:
                        break
                    out[sym].append(_bar(b))
        npt = d.get("next_page_token")
        if not npt or all(len(v) >= 2000 for v in out.values()):
            break
        params["page_token"] = npt
    else:
        sys.stderr.write(f"option_bars_historical: stopped after {_MAX_PAGES} pages\n")
    params.pop("page_token", None)
    return out


# ── 10. OPTION CHAIN (from snapshots) ─────────────────────────────────────────
def option_chain(underlying: str, expiration: str = "", gate: bool = True) -> dict:
    if not underlying:
        return {"underlying": underlying, "expirations": {}}
    if gate and not is_market_open():
        return {"_market_closed": True, "underlying": underlying}
    params: dict = {"underlying_symbols": underlying, "status": "active", "limit": 1000}
    if expiration:
        params["expiration_date"] = expiration
    contracts = []
    for _page in range(_MAX_PAGES):
        try:
            d = _get("/options/contracts", params, base=TRADING_BASE)
        except Exception as e:
            sys.stderr.write(f"option_contracts {underlying}: {e}\n")
            break
        batch = d.get("option_contracts", [])
        contracts.extend(batch)
        npt = d.get("next_page_token")
        if not npt or len(contracts) >= 3000:
            break
        params["page_token"] = npt
    else:
        sys.stderr.write(f"option_chain: stopped after {_MAX_PAGES} pages\n")
    params.pop("page_token", None)
    if not contracts:
        return {"underlying": underlying, "expirations": {}}
    occ_syms = [c["symbol"] for c in contracts if c.get("symbol")]
    snaps: dict = {}
    for chunk in _chunks(occ_syms, 100):
        try:
            snaps.update(option_snapshots(chunk, gate=False))
        except Exception as e:
            sys.stderr.write(f"chain snapshot chunk: {e}\n")
    by_exp: dict[str, dict] = {}
    for c in contracts:
        sym = c.get("symbol", "")
        exp = c.get("expiration_date", "")
        strike = sf(c.get("strike_price"))
        side = (c.get("type") or "").lower()
        if not sym or not exp:
            continue
        snap = snaps.get(sym, {})
        row = {
            "symbol": sym, "strike": strike, "side": side, "bid": snap.get("bid", 0), "ask": snap.get("ask", 0),
            "mid": snap.get("mid", 0), "iv": snap.get("iv", 0), "oi": snap.get("openInterest", si(c.get("open_interest"))),
            "delta": snap.get("delta", 0), "gamma": snap.get("gamma", 0), "theta": snap.get("theta", 0),
            "vega": snap.get("vega", 0), "rho": snap.get("rho", 0), "size": si(c.get("size")), "expiresAt": exp,
        }
        if exp not in by_exp:
            by_exp[exp] = {"calls": [], "puts": []}
        key = "calls" if side == "call" else "puts"
        by_exp[exp][key].append(row)
    for exp in by_exp:
        by_exp[exp]["calls"].sort(key=lambda r: r["strike"])
        by_exp[exp]["puts"].sort(key=lambda r: r["strike"])
    return {
        "underlying": underlying, "contractCount": len(contracts), "expirationCount": len(by_exp),
        "expirations": by_exp, "feed": "indicative", "source": "alpaca",
    }


# ── 11. NEWS ──────────────────────────────────────────────────────────────────
def news(symbols: list[str] | None = None, limit: int = 10, start: str = "", end: str = "") -> list:
    params: dict = {"limit": max(1, min(limit, 50)), "sort": "desc"}
    if symbols:
        params["symbols"] = ",".join(symbols)
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    try:
        d = _get("/news", params, base=DATA_BASE_V1BETA)
        articles = []
        for a in d.get("news", []):
            images = a.get("images") or [{}]
            articles.append({
                "id": str(a.get("id", "")), "headline": a.get("headline", ""), "summary": a.get("summary", ""),
                "author": a.get("author", ""), "source": a.get("source", ""), "url": a.get("url", ""),
                "imageUrl": (images[0] or {}).get("url", ""), "symbols": a.get("symbols", []),
                "createdAt": a.get("created_at", ""), "updatedAt": a.get("updated_at", ""),
            })
        return articles
    except Exception as e:
        sys.stderr.write(f"news: {e}\n")
        return []


# ── 12. HISTORICAL BARS FOR EARNINGS CALIBRATION ─────────────────────────────
def earnings_jump_calibration(symbol: str, days_back: int = 90) -> dict:
    if not symbol:
        return {"symbol": symbol, "error": "no_symbol"}
    end_dt = _date.today()
    start_dt = end_dt - timedelta(days=days_back + 10)
    bars_map = stock_bars_historical([symbol], "1d", start_dt.isoformat(), end_dt.isoformat(), feed="iex", limit=200)
    bars = bars_map.get(symbol, [])
    if len(bars) < 5:
        return {"symbol": symbol, "error": "insufficient_data"}
    closes = [b["c"] for b in bars if b["c"] > 0]
    if len(closes) < 5:
        return {"symbol": symbol, "error": "no_closes"}
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(rets)
    if n == 0:
        return {"symbol": symbol, "error": "no_returns"}
    mu = sum(rets) / n
    var = sum((r - mu) ** 2 for r in rets) / max(n - 1, 1)
    std = math.sqrt(var)
    ann_vol = std * math.sqrt(252)
    skew = (sum((r - mu) ** 3 for r in rets) / n / var ** 1.5) if var > 0 else 0
    kurt = (sum((r - mu) ** 4 for r in rets) / n / var ** 2 - 3) if var > 0 else 0
    threshold = 2 * std
    large = [{"date": bars[i + 1].get("ts", ""), "ret": round(r, 6)} for i, r in enumerate(rets) if abs(r) > threshold]
    lam_est = len(large) / n * 252
    return {
        "symbol": symbol, "barCount": n + 1, "annualVol": round(ann_vol, 6), "dailyVol": round(std, 6),
        "skewness": round(skew, 6), "excessKurt": round(kurt, 6), "jumpFreqAnn": round(lam_est, 4),
        "jumpSizeStd": round(std * 2.5, 6), "largeMoves": large, "source": "alpaca_iex",
    }


# ── CLI dispatcher ────────────────────────────────────────────────────────────
def _syms(arg: str) -> list[str]:
    return [s.strip().upper() for s in arg.split(",") if s.strip()]


def _arg(argv: list[str], i: int, default: str | None = None) -> str | None:
    return argv[i] if len(argv) > i else default


def _int_arg(argv: list[str], i: int, default: int) -> int:
    raw = _arg(argv, i)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"expected an integer for argument {i}, got {raw!r}")


def main() -> int:
    argv = sys.argv
    if len(argv) < 2:
        print(json.dumps({"error": "mode required"}))
        return 1
    if not APCA_KEY or not APCA_SEC:
        print(json.dumps({"error": "APCA_API_KEY_ID / APCA_API_SECRET_KEY not set in environment"}))
        return 1
    mode = argv[1].lower()
    try:
        if mode == "market_status":
            print(json.dumps(market_status()))
        elif mode == "stock_quote":
            symbols = _syms(_arg(argv, 2, "AAPL"))
            print(json.dumps(stock_quotes_latest(symbols)))
        elif mode == "stock_bars_latest":
            symbols = _syms(_arg(argv, 2, "AAPL"))
            print(json.dumps(stock_bars_latest(symbols)))
        elif mode == "stock_bars":
            symbols = _syms(_arg(argv, 2, "AAPL"))
            tf = _arg(argv, 3, "1d")
            start = _arg(argv, 4, (_date.today() - timedelta(30)).isoformat())
            end = _arg(argv, 5, _date.today().isoformat())
            feed = _arg(argv, 6, "iex")
            print(json.dumps(stock_bars_historical(symbols, tf, start, end, feed=feed)))
        elif mode == "stock_trades":
            symbols = _syms(_arg(argv, 2, "AAPL"))
            print(json.dumps(stock_trades_latest(symbols)))
        elif mode == "stock_snapshots":
            symbols = _syms(_arg(argv, 2, "AAPL"))
            print(json.dumps(stock_snapshots(symbols)))
        elif mode == "option_quotes":
            symbols = _syms(_arg(argv, 2, ""))
            print(json.dumps(option_quotes_latest(symbols)))
        elif mode == "option_trades":
            symbols = _syms(_arg(argv, 2, ""))
            print(json.dumps(option_trades_latest(symbols)))
        elif mode == "option_snapshots":
            symbols = _syms(_arg(argv, 2, ""))
            print(json.dumps(option_snapshots(symbols)))
        elif mode == "option_bars":
            symbols = _syms(_arg(argv, 2, ""))
            tf = _arg(argv, 3, "1d")
            start = _arg(argv, 4, (_date.today() - timedelta(30)).isoformat())
            end = _arg(argv, 5, _date.today().isoformat())
            print(json.dumps(option_bars_historical(symbols, tf, start, end)))
        elif mode == "option_chain":
            underlying = _arg(argv, 2, "AAPL").upper()
            expiration = _arg(argv, 3, "")
            print(json.dumps(option_chain(underlying, expiration)))
        elif mode == "news":
            symbols = _syms(_arg(argv, 2, ""))
            limit = _int_arg(argv, 3, 10)
            print(json.dumps(news(symbols, limit)))
        elif mode == "earnings_calibration":
            sym = _arg(argv, 2, "AAPL").upper()
            days_back = _int_arg(argv, 3, 90)
            print(json.dumps(earnings_jump_calibration(sym, days_back)))
        else:
            print(json.dumps({"error": f"unknown mode: {mode}"}))
            return 1
    except ValueError as e:
        print(json.dumps({"error": f"invalid argument: {e}"}))
        return 1
    except Exception as e:
        sys.stderr.write(f"{mode}: unhandled error: {e}\n")
        print(json.dumps({"error": f"{mode} failed: {e}"}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
