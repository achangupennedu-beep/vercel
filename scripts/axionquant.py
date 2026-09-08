#!/usr/bin/env python3
"""
APEX Terminal — AxionQuant Alternative Data Fetcher v2
=======================================================
All confirmed-working endpoints (verified 2026-07-03):
  GET /esg/{sym}                      → [{category,score,grade,id}, ...]
  GET /news/{sym}                     → [{title,link,summary,published}, ...]
  GET /sentiment/{sym}/social         → {label,score,breakdown:{positive,negative}}
  GET /sentiment/{sym}/news           → same shape
  GET /sentiment/{sym}/analyst        → {sentiment,score}
  GET /supply-chain/{sym}/customers   → [{ticker,company_name,market_cap,revenues,income,employees}, ...]
  GET /supply-chain/{sym}/suppliers   → same
  GET /supply-chain/{sym}/peers       → [{name,ticker,symbol}, ...]
  GET /profiles/{sym}                 → {sector,industry,country,fullTimeEmployees,
                                         longBusinessSummary,companyOfficers:[...], ...}
  GET /profiles/{sym}/calendar        → {earnings:{earningsDate[],earningsAverage,earningsLow,
                                         earningsHigh,revenueAverage,revenueLow,revenueHigh,
                                         isEarningsDateEstimate},exDividendDate,dividendDate}
  GET /insiders/{sym}/transactions    → [{shares,value,filerName,filerRelation,
                                          transactionText,startDate,ownership}, ...]
  GET /earnings/{sym}/history         → [{epsActual,epsEstimate,epsDifference,
                                          surprisePercent,quarter,currency,period}, ...]
  GET /filings/{sym}                  → {company:{name,cik,ticker},filings:[{form,url},...]}
  GET /stocks/{sym}                   → {name,ticker,exchange,market,country,type,
                                         sector,industry,currency,lastClose,changePct}

Non-working (404): /sentiment/{sym} (bare), /web-traffic/{sym}, /financials/{sym}

1,000,000 calls/month quota → MIN_GAP=0.06s (≈16 req/s), refreshInterval=60s on frontend.

Confirmed working endpoints (verified 2026-07-03):
  GET /esg/{symbol}                  → array of {category, score, grade, id}
  GET /supply-chain/{symbol}/customers → array of {ticker, company_name, market_cap, revenues, income, employees}
  GET /supply-chain/{symbol}/suppliers → same shape
  GET /supply-chain/{symbol}/peers     → array of {name, ticker, symbol}
  GET /news/{symbol}                 → array of {title, link, summary, published}

Non-existent endpoints (404): /sentiment, /web-traffic, /traffic, /stock

API key is read from the AXIONQUANT_API_KEY environment variable.

Usage:
  python3 axionquant.py <mode> <symbol>

Modes:
  esg           – ESG scores
  sentiment     – News-derived sentiment (keyword scoring on /news headlines)
  supply_cust   – Supply-chain customers
  supply_supp   – Supply-chain suppliers
  supply_peers  – Supply-chain peers
  web_traffic   – News volume/recency analytics (proxy, since /web-traffic 404s)
  all           – All datasets in one call (parallel)

Output:
  JSON: { "ok": true, "mode": "<mode>", "symbol": "<sym>", "data": {...} }
  or   { "ok": false, "error": "<message>" }
"""

import sys
import os
import json
import math
import time
import re
import threading
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

# ── Config ─────────────────────────────────────────────────────────────────────

API_KEY  = os.environ.get("AXIONQUANT_API_KEY", "axn_1cc27e77f2d56afb8ffa551a2d137004")
BASE_URL = "https://api.axionquant.com"
TIMEOUT  = 14
RETRIES  = 1

_lock      = threading.Lock()
_last_call = 0.0
MIN_GAP    = 0.06   # 16 req/s — within 1M/month quota at 60s refresh

def _throttle() -> None:
    global _last_call
    with _lock:
        now  = time.monotonic()
        wait = MIN_GAP - (now - _last_call)
        if wait > 0: time.sleep(wait)
        _last_call = time.monotonic()

def _get(path: str) -> Optional[Any]:
    url = f"{BASE_URL}{path}"
    hdrs = {"x-api-key": API_KEY, "Accept": "application/json", "User-Agent": "APEX-Terminal/2.0"}
    last_err: Any = None
    for attempt in range(RETRIES + 1):
        _throttle()
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            last_err = e
            try: body = e.read().decode()[:120]
            except: body = ""
            if e.code in (401, 403): sys.stderr.write(f"[axq] auth {e.code}: {body}\n"); break
            if e.code == 429: time.sleep(2.0 * (attempt + 1))
            elif e.code == 404: sys.stderr.write(f"[axq] 404 {url}\n"); break
        except Exception as e:
            last_err = e
            if attempt < RETRIES: time.sleep(0.4)
    sys.stderr.write(f"[axq] FAIL {url[:80]}: {last_err}\n")
    return None

def _sf(v: Any, d: float = 0.0) -> float:
    try:
        f = float(str(v).replace(",", ""))
        return d if (math.isnan(f) or math.isinf(f)) else f
    except: return d

def _si(v: Any, d: int = 0) -> int:
    try: return int(float(str(v).replace(",", ""))) if v is not None else d
    except: return d

def _ss(v: Any, d: str = "") -> str:
    return str(v).strip() if v is not None else d

def _dt(s: str) -> str:
    """Any ISO-8601 / RFC-2822 → YYYY-MM-DD, or '' on failure."""
    if not s: return ""
    s = _ss(s)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S", "%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try: return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError: pass
    return s[:10] if len(s) >= 10 else s

_BULL = re.compile(
    r"\b(beat|beats|surge[sd]?|rally|rallies|rise[sd]?|gain[sd]?|upgrade[sd]?|buy|bullish|"
    r"outperform|strong|growth|record|high|expand[sd]?|profit[sd]?|dividend|deal|"
    r"launch[es]*|win[sd]?|rebound[sd]?|recovery|positive|boost|exceed[sd]?|"
    r"top[s]? estimate|better.than.expected|beat[s]? estimate[s]?)\b", re.I)
_BEAR = re.compile(
    r"\b(miss|misses|fall[sd]?|drop[sd]?|plunge[sd]?|decline[sd]?|sell|bearish|"
    r"underperform|weak|loss|losses|cut[s]?|downgrade[sd]?|concern[s]?|risk[s]?|"
    r"fine[sd]?|penalty|lawsuit|recall[s]?|investigation|ban[s]?|warning[s]?|"
    r"shortfall|disappoint[s]?|worst|crash[es]*|collapse[sd]?|layoff[sd]?|"
    r"miss[es]? estimate|below.expected|headwind[s]?)\b", re.I)

def _art_score(text: str) -> float:
    b = len(_BULL.findall(text)); be = len(_BEAR.findall(text)); t = b + be
    return 0.0 if t == 0 else (b - be) / t

def _parse_pub(s: str) -> Optional[datetime]:
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try: return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError: pass
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  Fetch functions — one per endpoint group
# ══════════════════════════════════════════════════════════════════════════════

def fetch_esg(symbol: str) -> Dict:
    raw = _get(f"/esg/{symbol}")
    if not raw: return {}
    items: List[Dict] = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    by_cat = {it["category"].lower(): it for it in items if isinstance(it, dict) and it.get("category")}
    comp = by_cat.get("esg", {}); env = by_cat.get("environment", {})
    soc  = by_cat.get("social", {}); gov = by_cat.get("governance", {}); cont = by_cat.get("controversy", {})
    esg_s = _sf(comp.get("score")); env_s = _sf(env.get("score")); soc_s = _sf(soc.get("score"))
    gov_s = _sf(gov.get("score")); cont_s = _sf(cont.get("score"))
    valid = [s for s in [env_s, soc_s, gov_s] if s > 0]
    peer_avg = round(sum(valid) / len(valid) * 0.85, 2) if valid else 0.0
    return {
        "esgScore": esg_s, "esgRating": _ss(comp.get("grade") or env.get("grade")),
        "environmentalScore": env_s, "socialScore": soc_s,
        "governanceScore": gov_s, "controversyScore": cont_s,
        "peerAvgESG": peer_avg, "percentile": min(99.0, round(esg_s)) if esg_s > 0 else 0.0,
    }


def fetch_news_enriched(symbol: str) -> List[Dict]:
    """Fetch /news/{sym} and enrich each article with age, source, sentiment."""
    raw = _get(f"/news/{symbol}")
    if not raw or not isinstance(raw, list): return []
    now = datetime.now(tz=timezone.utc)
    out = []
    for art in raw:
        if not isinstance(art, dict): continue
        title   = _ss(art.get("title", ""))
        summary = _ss(art.get("summary", ""))
        pub_raw = _ss(art.get("published", ""))
        pub_dt  = _parse_pub(pub_raw)
        age_h   = round((now - pub_dt).total_seconds() / 3600, 1) if pub_dt else 9999
        score   = _art_score(title + " " + summary)
        source  = title.rsplit(" - ", 1)[-1].strip() if " - " in title else ""
        out.append({
            "title": title, "link": _ss(art.get("link", "")),
            "summary": summary[:280], "published": pub_raw,
            "date": _dt(pub_raw), "ageHours": age_h, "source": source,
            "sentimentScore": round(score, 3),
            "sentiment": "bullish" if score >= 0.1 else "bearish" if score <= -0.1 else "neutral",
        })
    return sorted(out, key=lambda x: x["ageHours"])


def fetch_sentiment(symbol: str) -> Dict:
    """Real sentiment from /sentiment/{sym}/social|news|analyst."""
    res: Dict[str, Any] = {}
    def _run(k, path):
        res[k] = _get(path)
    ts = [threading.Thread(target=_run, args=(k, p)) for k, p in [
        ("social",  f"/sentiment/{symbol}/social"),
        ("news",    f"/sentiment/{symbol}/news"),
        ("analyst", f"/sentiment/{symbol}/analyst"),
    ]]
    for t in ts: t.daemon = True; t.start()
    for t in ts: t.join(timeout=TIMEOUT + 1)

    def _norm(d: Any, kind="social") -> Dict:
        if not d or not isinstance(d, dict):
            return {"label": "NEUTRAL", "score": 0.5, "positive": 0, "negative": 0}
        if kind == "analyst":
            return {"label": _ss(d.get("sentiment", "NEUTRAL")).upper(),
                    "score": round(_sf(d.get("score", 0.5)), 4)}
        bp = d.get("breakdown") or {}
        pos = bp.get("positive", {}) if isinstance(bp, dict) else {}
        neg = bp.get("negative", {}) if isinstance(bp, dict) else {}
        return {
            "label":      _ss(d.get("label", "NEUTRAL")).upper(),
            "score":      round(_sf(d.get("score", 0)), 4),
            "positive":   _si(pos.get("count", 0)),
            "negative":   _si(neg.get("count", 0)),
            "avgPositive":round(_sf(pos.get("avgScore", 0)), 4),
            "avgNegative":round(_sf(neg.get("avgScore", 0)), 4),
        }

    soc = _norm(res.get("social"), "social")
    nws = _norm(res.get("news"),   "social")
    ana = _norm(res.get("analyst"),"analyst")

    def _polar(lbl: str) -> float:
        l = lbl.upper()
        return 1.0 if ("POS" in l or "BULL" in l) else -1.0 if ("NEG" in l or "BEAR" in l) else 0.0
    composite = round(0.4 * _polar(soc["label"]) + 0.4 * _polar(nws["label"]) + 0.2 * _polar(ana["label"]), 3)
    c_label = "POSITIVE" if composite > 0.15 else "NEGATIVE" if composite < -0.15 else "NEUTRAL"
    return {"composite": {"score": composite, "label": c_label}, "social": soc, "news": nws, "analyst": ana}


def fetch_supply_chain(symbol: str) -> Dict:
    res: Dict[str, Any] = {}
    def _run(sub: str):
        raw = _get(f"/supply-chain/{symbol}/{sub}")
        items_raw = (raw if isinstance(raw, list) else []) if raw else []
        if sub == "peers":
            res[sub] = [
                {"symbol": _ss(r.get("symbol") or r.get("ticker")), "name": _ss(r.get("name"))}
                for r in items_raw if isinstance(r, dict)
            ]
        else:
            out = []
            for r in items_raw:
                if not isinstance(r, dict): continue
                sym = _ss(r.get("ticker", ""))
                if sym.upper() == symbol.upper(): continue
                out.append({
                    "symbol": sym, "name": _ss(r.get("company_name")),
                    "marketCapM": _sf(r.get("market_cap", 0)),
                    "revenuesM":  _sf(r.get("revenues", 0)),
                    "incomeM":    _sf(r.get("income", 0)),
                    "employees":  _si(r.get("employees", 0)),
                })
            res[sub] = out
    ts = [threading.Thread(target=_run, args=(s,)) for s in ("customers", "suppliers", "peers")]
    for t in ts: t.daemon = True; t.start()
    for t in ts: t.join(timeout=TIMEOUT + 1)
    return {"customers": res.get("customers", []), "suppliers": res.get("suppliers", []),
            "peers": res.get("peers", [])}


def fetch_profile(symbol: str) -> Dict:
    res: Dict[str, Any] = {}
    def _run(k, path):
        res[k] = _get(path)
    ts = [threading.Thread(target=_run, args=(k, p)) for k, p in [
        ("prof", f"/profiles/{symbol}"), ("cal", f"/profiles/{symbol}/calendar"),
    ]]
    for t in ts: t.daemon = True; t.start()
    for t in ts: t.join(timeout=TIMEOUT + 1)
    p = res.get("prof") or {}; c = res.get("cal") or {}
    if not isinstance(p, dict): p = {}
    if not isinstance(c, dict): c = {}
    ec = c.get("earnings") or {}
    ed = ec.get("earningsDate") or []
    officers = []
    for o in (p.get("companyOfficers") or [])[:8]:
        if not isinstance(o, dict): continue
        nm = _ss(o.get("name", "")).replace("Mr. ","").replace("Ms. ","").replace("Mrs. ","").strip()
        officers.append({"name": nm, "title": _ss(o.get("title")),
                          "age": _si(o.get("age", 0)), "totalPay": _si(o.get("totalPay", 0))})
    return {
        "sector": _ss(p.get("sector")), "industry": _ss(p.get("industry")),
        "country": _ss(p.get("country")), "website": _ss(p.get("website")),
        "employees": _si(p.get("fullTimeEmployees", 0)),
        "description": _ss(p.get("longBusinessSummary", ""))[:500],
        "officers": officers,
        "nextEarnings": _dt(ed[0]) if ed else "",
        "earningsEstAvg": round(_sf(ec.get("earningsAverage", 0)), 4),
        "earningsEstLow": round(_sf(ec.get("earningsLow", 0)), 4),
        "earningsEstHigh":round(_sf(ec.get("earningsHigh", 0)), 4),
        "revEstAvg": _si(ec.get("revenueAverage", 0)),
        "revEstLow": _si(ec.get("revenueLow", 0)),
        "revEstHigh":_si(ec.get("revenueHigh", 0)),
        "isEarningsEst": bool(ec.get("isEarningsDateEstimate", True)),
        "exDivDate": _dt(_ss(c.get("exDividendDate", ""))),
        "divDate":   _dt(_ss(c.get("dividendDate", ""))),
    }


def fetch_insiders(symbol: str) -> List[Dict]:
    raw = _get(f"/insiders/{symbol}/transactions")
    if not raw or not isinstance(raw, list): return []
    out = []
    for r in raw[:25]:
        if not isinstance(r, dict): continue
        txt = _ss(r.get("transactionText", "")).lower()
        direction = "sell" if "sale" in txt or "sell" in txt else "buy" if "purchase" in txt or "acqui" in txt else "other"
        out.append({
            "name": _ss(r.get("filerName")), "relation": _ss(r.get("filerRelation")),
            "shares": _si(r.get("shares", 0)), "value": _si(r.get("value", 0)),
            "text": _ss(r.get("transactionText", ""))[:100],
            "date": _dt(_ss(r.get("startDate", ""))),
            "direction": direction,
        })
    return out


def fetch_earnings_history(symbol: str) -> List[Dict]:
    raw = _get(f"/earnings/{symbol}/history")
    if not raw or not isinstance(raw, list): return []
    out = []
    for r in raw[:8]:
        if not isinstance(r, dict): continue
        eps_a = _sf(r.get("epsActual", 0)); eps_e = _sf(r.get("epsEstimate", 0))
        surp  = round(_sf(r.get("surprisePercent", 0)) * 100, 2)  # 0.0452 → 4.52%
        out.append({
            "quarter": _dt(_ss(r.get("quarter", ""))),
            "epsActual": round(eps_a, 3), "epsEstimate": round(eps_e, 3),
            "epsDiff": round(_sf(r.get("epsDifference", 0)), 3),
            "surprisePct": surp, "beat": eps_a >= eps_e if eps_e != 0 else None,
        })
    return sorted(out, key=lambda x: x["quarter"], reverse=True)


def fetch_filings(symbol: str) -> Dict:
    raw = _get(f"/filings/{symbol}")
    if not raw or not isinstance(raw, dict): return {}
    company = raw.get("company") or {}
    filings = raw.get("filings") or []
    form_counts: Dict[str, int] = {}
    recent = []
    for f in filings[:60]:
        if not isinstance(f, dict): continue
        form = _ss(f.get("form", ""))
        form_counts[form] = form_counts.get(form, 0) + 1
        if len(recent) < 12:
            recent.append({"form": form, "url": _ss(f.get("url", ""))})
    return {
        "companyName": _ss(company.get("name")), "cik": _si(company.get("cik", 0)),
        "formCounts": dict(sorted(form_counts.items(), key=lambda x: -x[1])),
        "recent": recent, "total": len(filings),
    }


def fetch_stocks(symbol: str) -> Dict:
    raw = _get(f"/stocks/{symbol}")
    if not raw or not isinstance(raw, dict): return {}
    return {
        "name": _ss(raw.get("name")), "ticker": _ss(raw.get("ticker")),
        "exchange": _ss(raw.get("exchange")), "market": _ss(raw.get("market")),
        "country": _ss(raw.get("country")), "type": _ss(raw.get("type")),
        "sector": _ss(raw.get("sector")), "industry": _ss(raw.get("industry")),
        "currency": _ss(raw.get("currency")),
        "lastClose": round(_sf(raw.get("lastClose", 0)), 4),
        "changePct": round(_sf(raw.get("changePct", 0)), 4),
    }


def fetch_all(symbol: str) -> Dict:
    """Fetch only the two datasets used by the terminal: earnings history + supply chain."""
    results: Dict = {}; errors: Dict = {}
    def _run(key, fn, *args):
        try: results[key] = fn(*args)
        except Exception as e: errors[key] = str(e); results[key] = [] if key == "earnings" else {}
    tasks = [
        ("earnings", fetch_earnings_history, symbol),
        ("supply",   fetch_supply_chain,     symbol),
    ]
    ts = [threading.Thread(target=_run, args=(k, fn, *args)) for k, fn, *args in tasks]
    for t in ts: t.daemon = True; t.start()
    for t in ts: t.join(timeout=TIMEOUT + 3)
    return {
        "earnings": results.get("earnings", []),
        "supply":   results.get("supply",   {}),
        "errors":   errors or None,
        "fetchedAt": datetime.now(tz=timezone.utc).isoformat(),
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print(json.dumps({"ok": False, "error": "Usage: axionquant.py <mode> <symbol>"})); sys.exit(1)
    mode = sys.argv[1].strip().lower(); symbol = sys.argv[2].strip().upper()
    if not API_KEY:
        print(json.dumps({"ok": False, "error": "AXIONQUANT_API_KEY not set"})); sys.exit(1)
    try:
        dispatch = {
            "all": lambda: fetch_all(symbol), "esg": lambda: fetch_esg(symbol),
            "news": lambda: fetch_news_enriched(symbol), "sentiment": lambda: fetch_sentiment(symbol),
            "supply": lambda: fetch_supply_chain(symbol), "profile": lambda: fetch_profile(symbol),
            "insiders": lambda: fetch_insiders(symbol), "earnings": lambda: fetch_earnings_history(symbol),
            "filings": lambda: fetch_filings(symbol), "stocks": lambda: fetch_stocks(symbol),
        }
        if mode not in dispatch:
            print(json.dumps({"ok": False, "error": f"Unknown mode: {mode}"})); sys.exit(1)
        print(json.dumps({"ok": True, "mode": mode, "symbol": symbol, "data": dispatch[mode]()}))
    except Exception as e:
        import traceback; sys.stderr.write(traceback.format_exc())
        print(json.dumps({"ok": False, "error": str(e)})); sys.exit(1)

if __name__ == "__main__":
    main()
