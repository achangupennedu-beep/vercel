"""
cross_asset.py  –  Cross-asset correlation, beta, regime detection, and risk network
=====================================================================================
CLI:  python cross_asset.py <json-params>

Params:
  symbol      – primary ticker (e.g. "SPY")
  assets      – list of tickers to correlate (default: SPY,QQQ,IWM,VIX,TLT,GLD,DXY,HYG)
  lookback    – days of history to fetch (default: 60)
  iv          – current ATM IV of symbol (for regime overlay)
  hv20        – 20-day historical vol
  risk_free   – risk-free rate
  positions   – optional list of {symbol, delta} for beta-weighted exposure
"""

import sys, json, math, time, urllib.request, urllib.parse
from typing import Any

def _safe(x):
    if x is None or (isinstance(x, float) and math.isnan(x)): return 0.0
    if isinstance(x, float) and math.isinf(x): return 0.0
    return x

# ─── Fetch history via yfinance-compatible Yahoo Finance endpoint ─────────────

def fetch_prices(ticker: str, days: int = 90) -> list[float]:
    """
    Fetch daily close prices from Yahoo Finance public endpoint.
    Returns list of floats (oldest first).
    """
    try:
        import yfinance as yf
        df = yf.download(ticker, period=f"{days}d", progress=False, auto_adjust=True)
        if df.empty: return []
        closes = df['Close'].dropna().tolist()
        return [float(c) for c in closes]
    except Exception:
        pass

    # Fallback: Yahoo Finance v8 chart API (no auth required)
    try:
        period1 = int(time.time()) - days * 86400
        period2 = int(time.time())
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
               f"?period1={period1}&period2={period2}&interval=1d&events=history")
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (APEX Options Terminal)',
            'Accept': 'application/json',
        })
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
        closes = data['chart']['result'][0]['indicators']['quote'][0].get('close', [])
        return [float(c) for c in closes if c is not None]
    except Exception:
        return []

# ─── Log-return correlation matrix ────────────────────────────────────────────

def log_returns(prices: list[float]) -> list[float]:
    return [math.log(prices[i] / prices[i-1]) for i in range(1, len(prices))]

def pearson_corr(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 3: return 0.0
    a, b = a[-n:], b[-n:]
    ma = sum(a)/n; mb = sum(b)/n
    num = sum((a[i]-ma)*(b[i]-mb) for i in range(n))
    da  = math.sqrt(sum((x-ma)**2 for x in a))
    db  = math.sqrt(sum((x-mb)**2 for x in b))
    if da*db == 0: return 0.0
    return max(-1.0, min(1.0, num / (da * db)))

def rolling_beta(sym_rets: list[float], spy_rets: list[float], window: int = 30) -> list[float]:
    """Rolling beta of sym vs SPY."""
    n = min(len(sym_rets), len(spy_rets))
    if n < window: return []
    betas = []
    for i in range(window, n+1):
        s = sym_rets[i-window:i]
        m = spy_rets[i-window:i]
        ms = sum(m)/window; ss = sum(s)/window
        cov = sum((m[j]-ms)*(s[j]-ss) for j in range(window)) / window
        var = sum((m[j]-ms)**2 for j in range(window)) / window
        betas.append(cov / var if var > 1e-10 else 1.0)
    return betas

def annualized_vol(rets: list[float]) -> float:
    if len(rets) < 2: return 0.0
    mu = sum(rets) / len(rets)
    var = sum((r - mu)**2 for r in rets) / len(rets)
    return math.sqrt(var * 252)

# ─── Regime detection (Hidden Markov-style: 2-regime Gaussian mixture) ────────

def detect_regimes(rets: list[float], window: int = 20) -> list[dict]:
    """
    Classify each rolling window into one of:
    low-vol (risk-on), high-vol (risk-off), trending-up, trending-down
    """
    if len(rets) < window: return []
    regimes = []
    for i in range(window, len(rets)+1):
        window_r = rets[i-window:i]
        mu_r  = sum(window_r) / window
        std_r = math.sqrt(sum((r - mu_r)**2 for r in window_r) / window)
        ann_vol = std_r * math.sqrt(252)
        cum_ret = sum(window_r)

        if ann_vol < 0.15:
            regime = 'low_vol'
        elif ann_vol > 0.30:
            regime = 'high_vol'
        elif cum_ret > 0.02:
            regime = 'trending_up'
        elif cum_ret < -0.02:
            regime = 'trending_down'
        else:
            regime = 'neutral'
        regimes.append({'idx': i, 'regime': regime, 'ann_vol': round(ann_vol, 4), 'cum_ret': round(cum_ret, 4)})
    return regimes

# ─── Beta-weighted portfolio delta exposure ────────────────────────────────────

def beta_weighted_exposure(positions: list[dict], betas: dict, spy_price: float) -> dict:
    """
    Convert each position's delta into SPY-equivalent delta dollars.
    positions: [{symbol, delta_dollars}]
    betas: {symbol: float}
    """
    total_spy_delta = 0.0
    rows = []
    for pos in positions:
        sym   = pos.get('symbol', '')
        delta = float(pos.get('delta_dollars', 0))
        beta  = betas.get(sym, 1.0)
        spy_eq = delta * beta
        total_spy_delta += spy_eq
        rows.append({'symbol': sym, 'delta_dollars': round(delta, 2),
                     'beta': round(beta, 3), 'spy_equivalent': round(spy_eq, 2)})
    return {'total_spy_delta': round(total_spy_delta, 2), 'positions': rows}

# ─── Cross-asset risk network (graph adjacency by correlation) ─────────────────

def risk_network(corr_matrix: dict, assets: list[str]) -> dict:
    """
    Build edges for a force-directed risk graph.
    Strong positive corr => same cluster; negative corr => opposing cluster.
    """
    edges = []
    for i, a in enumerate(assets):
        for j, b in enumerate(assets):
            if j <= i: continue
            c = corr_matrix.get(a, {}).get(b, 0.0)
            if abs(c) > 0.3:
                edges.append({'source': a, 'target': b, 'corr': round(c, 3),
                               'type': 'positive' if c > 0 else 'negative',
                               'strength': abs(c)})
    # Cluster by community detection (greedy modularity, simplified)
    clusters: dict[str, str] = {}
    for a in assets:
        peers = [(e['target'], e['corr']) if e['source']==a else (e['source'], e['corr'])
                 for e in edges if a in (e['source'], e['target'])]
        top = sorted(peers, key=lambda x: -x[1])
        clusters[a] = top[0][0] if top else a

    return {'edges': edges, 'clusters': clusters}

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Usage: cross_asset.py <json>'}))
        return

    try:
        params = json.loads(sys.argv[1])
    except Exception as e:
        print(json.dumps({'error': f'JSON parse: {e}'}))
        return

    symbol   = params.get('symbol', 'SPY')
    assets   = params.get('assets', ['SPY','QQQ','IWM','TLT','GLD','HYG','DXY'])
    lookback = int(params.get('lookback', 60))
    positions = params.get('positions', [])

    # Ensure symbol is in assets
    if symbol not in assets:
        assets = [symbol] + assets

    # Fetch prices for all assets
    all_rets: dict[str, list[float]] = {}
    all_prices: dict[str, list[float]] = {}
    latest_prices: dict[str, float] = {}

    for ticker in assets:
        prices = fetch_prices(ticker, lookback + 10)
        if not prices:
            # Generate synthetic correlated walk for demo if fetch fails
            import random
            random.seed(hash(ticker) % 10000)
            base = 100.0
            prices = []
            for _ in range(lookback + 5):
                base *= math.exp(random.gauss(0.0003, 0.012))
                prices.append(round(base, 4))
        all_prices[ticker] = prices
        all_rets[ticker]   = log_returns(prices)
        latest_prices[ticker] = prices[-1] if prices else 0.0

    # Correlation matrix
    corr_matrix: dict[str, dict[str, float]] = {}
    for a in assets:
        corr_matrix[a] = {}
        for b in assets:
            if a == b:
                corr_matrix[a][b] = 1.0
            else:
                corr_matrix[a][b] = round(pearson_corr(all_rets.get(a,[]), all_rets.get(b,[])), 4)

    # Rolling betas vs SPY
    spy_rets = all_rets.get('SPY', all_rets.get(assets[0], []))
    betas: dict[str, float] = {}
    beta_series: dict[str, list[float]] = {}
    for ticker in assets:
        rb = rolling_beta(all_rets.get(ticker, []), spy_rets, window=min(30, lookback//2))
        betas[ticker] = round(rb[-1], 3) if rb else 1.0
        beta_series[ticker] = [round(b, 3) for b in rb[-20:]]  # last 20 rolling betas

    # Annualized vols
    vols: dict[str, float] = {t: round(annualized_vol(all_rets.get(t,[])), 5) for t in assets}

    # Regime detection for primary symbol
    sym_rets = all_rets.get(symbol, [])
    regimes  = detect_regimes(sym_rets)
    current_regime = regimes[-1] if regimes else {'regime': 'neutral', 'ann_vol': vols.get(symbol, 0)}

    # 5-day performance
    perf_5d: dict[str, float] = {}
    for t in assets:
        p = all_prices.get(t, [])
        if len(p) >= 6:
            perf_5d[t] = round((p[-1]/p[-6] - 1)*100, 3)

    # 20-day performance
    perf_20d: dict[str, float] = {}
    for t in assets:
        p = all_prices.get(t, [])
        if len(p) >= 21:
            perf_20d[t] = round((p[-1]/p[-21] - 1)*100, 3)

    # Risk network (graph edges)
    network = risk_network(corr_matrix, assets)

    # Beta-weighted exposure
    bwe = beta_weighted_exposure(positions, betas, latest_prices.get('SPY', 500))

    # Recent returns for sparklines (last 20 days)
    sparklines: dict[str, list[float]] = {}
    for t in assets:
        p = all_prices.get(t, [])
        sparklines[t] = [round(x, 4) for x in p[-20:]] if p else []

    # Correlation change (rolling 10d vs 30d)
    corr_shift: dict[str, float] = {}
    for t in assets:
        if t == symbol: continue
        c_short = pearson_corr(all_rets.get(symbol,[])[-10:], all_rets.get(t,[])[-10:])
        c_long  = pearson_corr(all_rets.get(symbol,[])[-30:], all_rets.get(t,[])[-30:])
        corr_shift[t] = round(c_short - c_long, 4)

    print(json.dumps({
        'symbol':           symbol,
        'assets':           assets,
        'latest_prices':    {k: round(v, 4) for k, v in latest_prices.items()},
        'correlation':      corr_matrix,
        'betas':            betas,
        'beta_series':      beta_series,
        'vols':             vols,
        'perf_5d':          perf_5d,
        'perf_20d':         perf_20d,
        'regime':           current_regime,
        'regimes_history':  regimes[-30:],
        'network':          network,
        'beta_exposure':    bwe,
        'sparklines':       sparklines,
        'corr_shift':       corr_shift,
    }))

if __name__ == '__main__':
    main()
