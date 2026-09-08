#!/usr/bin/env python3
"""Async multi-source quote fetcher (stdlib-only). Usage: quote_ultrafast.py SYM[,SYM...] [--enrich]"""
from __future__ import annotations
import sys, os, json, time, math, asyncio, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, date, timedelta

try:
    import orjson
    dumps = lambda o: orjson.dumps(o).decode()
except ImportError:
    dumps = lambda o: json.dumps(o, separators=(",", ":"))

APCA_KEY = os.environ.get("APCA_API_KEY_ID", "")
APCA_SEC = os.environ.get("APCA_API_SECRET_KEY", "")
FH_KEY = os.environ.get("FINNHUB_API_KEY", "")
POLY_KEY = os.environ.get("POLYGON_API_KEY", "")
EODHD_KEY = os.environ.get("EODHD_API_KEY", "")
TIINGO_KEY = os.environ.get("TIINGO_API_KEY", "")
TD_KEY = os.environ.get("TWELVEDATA_API_KEY", "")
AV_KEYS = [os.environ[k] for k in (f"AV_KEY_{i}" for i in range(1, 11)) if os.environ.get(k)]

_HEADERS = {"APCA-API-KEY-ID": APCA_KEY, "APCA-API-SECRET-KEY": APCA_SEC, "User-Agent": "quote/2.1"}
_EXECUTOR = ThreadPoolExecutor(max_workers=32)  # backs every async HTTP call below
_clock_cache: tuple[float, bool] = (0.0, False)
_prev_cache: dict[tuple, tuple[float, dict]] = {}
_av_idx = 0  # single-threaded event loop -> no lock needed for round-robin


def sf(v, d=0.0):
    try:
        f = float(v)
        return f if math.isfinite(f) else d
    except (TypeError, ValueError):
        return d


def si(v, d=0):
    try:
        return int(float(v)) if v is not None else d
    except (TypeError, ValueError):
        return d


def next_av_key() -> str:
    global _av_idx
    if not AV_KEYS:
        return ""
    k = AV_KEYS[_av_idx % len(AV_KEYS)]
    _av_idx += 1
    return k


def _blocking_fetch(url, headers, timeout):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


async def get(url, headers=None, timeout=4):
    return await asyncio.get_running_loop().run_in_executor(_EXECUTOR, _blocking_fetch, url, headers, timeout)


async def safe(coro):
    try:
        return await coro
    except Exception:
        return {}


async def is_market_open() -> bool:
    global _clock_cache
    ts, val = _clock_cache
    if time.monotonic() - ts < 60:
        return val
    try:
        d = await get("https://paper-api.alpaca.markets/v2/clock", _HEADERS, 2.5)
        val = bool(d.get("is_open", False))
    except Exception:
        utc = datetime.now(timezone.utc)
        off = 4 if 3 <= utc.month <= 10 else 5
        mins = (utc.hour * 60 + utc.minute - off * 60) % 1440
        val = utc.weekday() < 5 and 570 <= mins < 960
    _clock_cache = (time.monotonic(), val)
    return val


# --- Alpaca primary path (batched, one request per field across all symbols) ---

async def alpaca_quotes(symbols):
    d = await get(f"https://data.alpaca.markets/v2/stocks/quotes/latest?symbols={','.join(symbols)}&feed=iex", _HEADERS)
    return {s: {"bid": sf(q.get("bp")), "ask": sf(q.get("ap")), "bidSize": si(q.get("bs")), "askSize": si(q.get("as"))}
            for s, q in d.get("quotes", {}).items()}


async def alpaca_bars(symbols):
    d = await get(f"https://data.alpaca.markets/v2/stocks/bars/latest?symbols={','.join(symbols)}&feed=iex", _HEADERS)
    return {s: {"open": sf(b.get("o")), "high": sf(b.get("h")), "low": sf(b.get("l")),
                "close": sf(b.get("c")), "volume": si(b.get("v")), "vwap": sf(b.get("vw"))}
            for s, b in d.get("bars", {}).items()}


async def alpaca_prev_close(symbols):
    key = tuple(symbols)
    cached = _prev_cache.get(key)
    if cached and time.monotonic() - cached[0] < 30:
        return cached[1]
    end, start = date.today().isoformat(), (date.today() - timedelta(days=5)).isoformat()
    url = (f"https://data.alpaca.markets/v2/stocks/bars?symbols={','.join(symbols)}"
           f"&timeframe=1Day&start={start}&end={end}&limit=5&sort=desc&feed=iex")
    d = await get(url, _HEADERS)
    out = {s: sf(bars[1]["c"] if len(bars) >= 2 else bars[0]["c"]) for s, bars in d.get("bars", {}).items() if bars}
    _prev_cache[key] = (time.monotonic(), out)
    return out


async def gather_alpaca(symbols):
    return await asyncio.gather(
        safe(alpaca_quotes(symbols)),
        safe(alpaca_bars(symbols)),
        safe(alpaca_prev_close(symbols)),
    )


# --- fallback providers: sym -> dict | None, raced concurrently via thread pool ---

async def p_finnhub(sym):
    if not FH_KEY:
        return None
    d = await get(f"https://finnhub.io/api/v1/quote?symbol={sym}&token={FH_KEY}")
    c = sf(d.get("c"))
    if not c:
        return None
    return {"symbol": sym, "price": c, "open": sf(d.get("o")), "high": sf(d.get("h")),
            "low": sf(d.get("l")), "prevClose": sf(d.get("pc")), "source": "finnhub"}


async def p_tiingo(sym):
    if not TIINGO_KEY:
        return None
    d = await get(f"https://api.tiingo.com/iex?tickers={sym}&token={TIINGO_KEY}")
    if not d:
        return None
    q = d[0]
    price = sf(q.get("last") or q.get("tngoLast"))
    if not price:
        return None
    pc = sf(q.get("prevClose"), price)
    ch = price - pc if pc else 0
    return {"symbol": sym, "price": price, "open": sf(q.get("open")), "high": sf(q.get("high")),
            "low": sf(q.get("low")), "volume": si(q.get("volume")), "prevClose": pc,
            "bid": sf(q.get("bidPrice")), "ask": sf(q.get("askPrice")),
            "change": round(ch, 4), "changePct": round(ch / pc * 100, 4) if pc else 0, "source": "tiingo"}


async def p_twelvedata(sym):
    if not TD_KEY:
        return None
    d = await get(f"https://api.twelvedata.com/quote?symbol={sym}&apikey={TD_KEY}")
    price = sf(d.get("close"))
    if not price:
        return None
    pc = sf(d.get("previous_close"), price)
    ch = price - pc if pc else 0
    return {"symbol": sym, "price": price, "open": sf(d.get("open")), "high": sf(d.get("high")),
            "low": sf(d.get("low")), "volume": si(d.get("volume")), "prevClose": pc, "bid": 0, "ask": 0,
            "change": round(ch, 4), "changePct": round(ch / pc * 100, 4) if pc else 0, "source": "twelvedata"}


async def p_av(sym):
    key = next_av_key()
    if not key:
        return None
    d = await get(f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={sym}&apikey={key}", timeout=5)
    q = d.get("Global Quote", {})
    price = sf(q.get("05. price"))
    if not price:
        return None
    return {"symbol": sym, "price": price, "open": sf(q.get("02. open")), "high": sf(q.get("03. high")),
            "low": sf(q.get("04. low")), "volume": si(q.get("06. volume")), "prevClose": sf(q.get("08. previous close")),
            "change": sf(q.get("09. change")), "changePct": sf(str(q.get("10. change percent", "0")).rstrip("%")),
            "bid": 0, "ask": 0, "source": "alpha_vantage"}


async def p_eodhd(sym):
    if not EODHD_KEY:
        return None
    d = await get(f"https://eodhd.com/api/real-time/{sym}.US?api_token={EODHD_KEY}&fmt=json")
    c = sf(d.get("close"))
    if not c:
        return None
    pc = sf(d.get("previousClose"), c)
    ch = c - pc if pc else 0
    return {"symbol": sym, "price": c, "open": sf(d.get("open")), "high": sf(d.get("high")),
            "low": sf(d.get("low")), "volume": si(d.get("volume")), "prevClose": pc,
            "change": round(ch, 6), "changePct": round(ch / pc * 100, 6) if pc else 0,
            "bid": sf(d.get("bid")), "ask": sf(d.get("ask")), "source": "eodhd"}


async def p_polygon(sym):
    if not POLY_KEY:
        return None
    d = await get(f"https://api.polygon.io/v2/aggs/ticker/{sym}/prev?adjusted=true&apiKey={POLY_KEY}")
    r = (d.get("results") or [{}])[0]
    c = sf(r.get("c"))
    if not c:
        return None
    return {"symbol": sym, "price": c, "open": sf(r.get("o")), "high": sf(r.get("h")), "low": sf(r.get("l")),
            "volume": si(r.get("v")), "vwap": sf(r.get("vw")), "prevClose": c, "source": "polygon"}


def yfinance_quote(sym):  # sync, last resort — already thread-offloaded by caller
    try:
        import yfinance as yf
        t = yf.Ticker(sym)
        info = t.fast_info
        price = sf(getattr(info, "last_price", None) or getattr(info, "regularMarketPrice", None))
        prev = sf(getattr(info, "previous_close", None) or getattr(info, "regularMarketPreviousClose", None))
        if not price:
            hist = t.history(period="2d", interval="1d")
            if hist.empty:
                return None
            price = sf(hist["Close"].iloc[-1])
            prev = sf(hist["Close"].iloc[-2]) if len(hist) >= 2 else price
        ch = price - prev if prev else 0
        return {"symbol": sym, "price": price, "open": sf(getattr(info, "open", None)),
                "high": sf(getattr(info, "day_high", None)), "low": sf(getattr(info, "day_low", None)),
                "volume": si(getattr(info, "volume", None)), "prevClose": prev,
                "change": round(ch, 4), "changePct": round(ch / prev * 100, 4) if prev else 0,
                "bid": 0, "ask": 0, "source": "yfinance"}
    except Exception:
        return None


FALLBACKS = (p_finnhub, p_tiingo, p_twelvedata, p_av, p_eodhd, p_polygon)


async def fallback_one(sym):
    tasks = [asyncio.create_task(fn(sym)) for fn in FALLBACKS]
    try:
        for coro in asyncio.as_completed(tasks):
            try:
                r = await coro
            except Exception:
                continue
            if r:
                return r
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    return await asyncio.get_running_loop().run_in_executor(_EXECUTOR, yfinance_quote, sym)


async def av_overview(sym):
    key = next_av_key()
    if not key:
        return {}
    d = await safe(get(f"https://www.alphavantage.co/query?function=OVERVIEW&symbol={sym}&apikey={key}", timeout=5))
    if not d.get("Symbol"):
        return {}
    return {"marketCap": si(d.get("MarketCapitalization")), "pe": sf(d.get("TrailingPE")),
            "forwardPE": sf(d.get("ForwardPE")), "eps": sf(d.get("EPS")), "beta": sf(d.get("Beta")),
            "week52High": sf(d.get("52WeekHigh")), "week52Low": sf(d.get("52WeekLow")),
            "divYield": sf(d.get("DividendYield")), "sector": d.get("Sector", ""),
            "industry": d.get("Industry", ""), "name": d.get("Name", ""),
            "description": d.get("Description", "")[:200]}


def build(sym, quotes, bars, prev, fund=None):
    q, b = quotes.get(sym, {}), bars.get(sym, {})
    bid, ask = q.get("bid", 0), q.get("ask", 0)
    close = b.get("close", 0) or ((bid + ask) / 2 if bid and ask else 0)
    pc = prev.get(sym) or b.get("open", 0)
    change = close - pc if pc else 0
    spread = ask - bid if bid and ask else 0
    out = {"symbol": sym, "price": close, "bid": bid, "ask": ask, "spread": round(spread, 4),
           "spreadPct": round(spread / close * 100, 4) if close else 0,
           "bidSize": q.get("bidSize", 0), "askSize": q.get("askSize", 0),
           "open": b.get("open", 0), "high": b.get("high", 0), "low": b.get("low", 0), "close": close,
           "volume": b.get("volume", 0), "vwap": b.get("vwap", 0), "prevClose": pc,
           "change": round(change, 4), "changePct": round(change / pc * 100, 4) if pc else 0,
           "source": "alpaca", "timestamp": int(time.time() * 1000)}
    if fund:
        out.update(fund)
    return out


async def run(symbols, enrich):
    aq, ab, prev = {}, {}, {}
    if APCA_KEY and APCA_SEC:
        if await is_market_open():
            aq, ab, prev = await gather_alpaca(symbols)
        else:
            prev = await safe(alpaca_prev_close(symbols))

    results, missing = {}, []
    for sym in symbols:
        if aq.get(sym) or ab.get(sym):
            fund = await av_overview(sym) if enrich and len(symbols) == 1 else {}
            results[sym] = build(sym, aq, ab, prev, fund)
        else:
            missing.append(sym)

    if missing:
        fetched = await asyncio.gather(*(fallback_one(s) for s in missing))
        for sym, fb in zip(missing, fetched):
            if fb:
                price, pc = fb.get("price", 0), fb.get("prevClose", 0)
                fb.setdefault("change", round(price - pc, 4) if pc else 0)
                fb.setdefault("changePct", round((price - pc) / pc * 100, 4) if pc else 0)
                fb.setdefault("bid", 0)
                fb.setdefault("ask", 0)
                fb["timestamp"] = int(time.time() * 1000)
                results[sym] = fb
            else:
                results[sym] = {"symbol": sym, "price": 0, "source": "none", "error": "unavailable"}

    return {s: results.get(s, {"symbol": s, "price": 0, "source": "none", "error": "unavailable"}) for s in symbols}


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "AAPL"
    enrich = "--enrich" in sys.argv
    symbols = list(dict.fromkeys(s.strip().upper() for s in raw.split(",") if s.strip())) or ["AAPL"]
    out = asyncio.run(run(symbols, enrich))
    sys.stdout.write(dumps(out[symbols[0]] if len(symbols) == 1 else out))


if __name__ == "__main__":
    main()