#!/usr/bin/env python3
"""Small, dependency-free Alpaca IEX market-data adapter.

The Basic plan permits real-time IEX quotes. The adapter keeps source fields
intact so callers can distinguish a missing quote from a real zero value.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://data.alpaca.markets/v2/stocks"


def request(path: str, params: dict[str, str]) -> dict:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{BASE}/{path}?{query}", headers={
        "APCA-API-KEY-ID": os.environ.get("APCA_API_KEY_ID", ""),
        "APCA-API-SECRET-KEY": os.environ.get("APCA_API_SECRET_KEY", ""),
        "Accept": "application/json",
        "User-Agent": "apex-options-terminal/1.0",
    })
    with urllib.request.urlopen(req, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def symbols_arg(raw: str) -> list[str]:
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def clean_trade(symbol: str, row: dict) -> dict:
    return {"symbol": symbol, "time": row.get("t"), "price": row.get("p"),
            "size": row.get("s"), "exchange": row.get("x"),
            "tradeId": row.get("i"), "conditions": row.get("c", []),
            "tape": row.get("z"), "source": "alpaca_iex"}


def clean_quote(symbol: str, row: dict) -> dict:
    return {"symbol": symbol, "time": row.get("t"), "bidPrice": row.get("bp"),
            "askPrice": row.get("ap"), "bidSize": row.get("bs"),
            "askSize": row.get("as"), "bidExchange": row.get("bx"),
            "askExchange": row.get("ax"), "conditions": row.get("c", []),
            "tape": row.get("z"), "source": "alpaca_iex"}


def main() -> None:
    if len(sys.argv) < 3:
        raise ValueError("usage: alpaca_market.py <stock_trades|stock_quotes|stock_quote|stock_snapshots|snapshot> SYMBOLS")
    mode = sys.argv[1].lower()
    symbols = symbols_arg(sys.argv[2])
    if not symbols:
        raise ValueError("symbols is required")
    joined = ",".join(symbols)
    result: dict = {"feed": "iex", "source": "alpaca", "retrievedAt": datetime.now(timezone.utc).isoformat()}

    if mode in {"stock_quotes", "stock_quote", "quotes"}:
        payload = request("quotes/latest", {"symbols": joined, "feed": "iex"})
        result["quotes"] = {s: clean_quote(s, payload.get("quotes", {}).get(s, {})) for s in symbols}
    elif mode in {"stock_trades", "trades"}:
        payload = request("trades/latest", {"symbols": joined, "feed": "iex"})
        result["trades"] = {s: clean_trade(s, payload.get("trades", {}).get(s, {})) for s in symbols}
    elif mode in {"stock_snapshots", "snapshot", "snapshots"}:
        payload = request("snapshots", {"symbols": joined, "feed": "iex"})
        snapshots = {}
        for symbol in symbols:
            raw = payload.get("snapshots", {}).get(symbol, {})
            snapshots[symbol] = {
                "symbol": symbol,
                "latestTrade": clean_trade(symbol, raw.get("latestTrade", {})),
                "latestQuote": clean_quote(symbol, raw.get("latestQuote", {})),
                "minuteBar": raw.get("minuteBar"), "dailyBar": raw.get("dailyBar"),
                "prevDailyBar": raw.get("prevDailyBar"), "source": "alpaca_iex",
            }
        result["snapshots"] = snapshots
    else:
        raise ValueError(f"unsupported mode: {mode}")
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": str(exc), "source": "alpaca"}), file=sys.stderr)
        raise SystemExit(1)
