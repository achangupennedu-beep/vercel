/**
 * /api/alpaca — Unified Alpaca free-tier market data endpoint.
 *
 * Query params:
 *   mode      (required) one of the modes below
 *   symbols   comma-separated stock or option symbols (most modes)
 *   symbol    single underlying (option_chain)
 *   timeframe bar timeframe: 1m | 5m | 15m | 30m | 1h | 1d | 1wk | 1mo
 *   start     YYYY-MM-DD
 *   end       YYYY-MM-DD
 *   feed      iex (default) | delayed_sip
 *   expiration YYYY-MM-DD (option_chain filter)
 *   limit     integer (news)
 *   days      integer (earnings_calibration)
 *   refresh   1 to bypass cache
 *
 * Modes:
 *   market_status        — is market open? next open/close times
 *   stock_quote          — latest IEX bid/ask quotes
 *   stock_bars_latest    — latest 1-min bars (IEX)
 *   stock_bars           — historical OHLCV bars (IEX or delayed_sip)
 *   stock_trades         — latest trade prints (IEX)
 *   stock_snapshots      — full snapshot: quote+trade+bars+prev
 *   option_quotes        — latest option bid/ask (indicative)
 *   option_trades        — latest option trades (indicative)
 *   option_snapshots     — option snapshots with greeks (indicative)
 *   option_bars          — historical option bars
 *   option_chain         — full chain for an underlying
 *   news                 — Alpaca news for symbols
 *   earnings_calibration — historical daily bars for jump-param calibration
 */

import { NextRequest, NextResponse } from 'next/server'
import { execPython } from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const VALID_MODES = new Set([
  'market_status',
  'stock_quote',
  'stock_bars_latest',
  'stock_bars',
  'stock_trades',
  'stock_snapshots',
  'option_quotes',
  'option_trades',
  'option_snapshots',
  'option_bars',
  'option_chain',
  'news',
  'earnings_calibration',
])

const SYM_RE   = /^[A-Z0-9.^-]{1,12}$/
const OCC_RE   = /^[A-Z]{1,6}\d{6}[CP]\d{8}$/   // OCC option symbol
const DATE_RE  = /^\d{4}-\d{2}-\d{2}$/
const TF_RE    = /^(\d+)(Min|Hour|Day|Week|Month|m|h|d|wk|mo)$/i

const VALID_TF   = new Set(['1m','5m','15m','30m','1h','4h','1d','1wk','1mo'])
const VALID_FEED = new Set(['iex', 'delayed_sip', 'sip'])

// Cache-control per mode (seconds)
const CACHE_TTL: Record<string, number> = {
  market_status:        30,
  stock_quote:           5,
  stock_bars_latest:    10,
  stock_bars:         3600,
  stock_trades:          5,
  stock_snapshots:      10,
  option_quotes:         5,
  option_trades:         5,
  option_snapshots:     10,
  option_bars:        3600,
  option_chain:         15,
  news:                300,
  earnings_calibration: 3600,
}

function err(msg: string, status = 400) {
  return NextResponse.json({ success: false, error: msg }, { status })
}

function alpacaEnv() {
  return {
    APCA_API_KEY_ID:     process.env.APCA_API_KEY_ID     ?? '',
    APCA_API_SECRET_KEY: process.env.APCA_API_SECRET_KEY ?? '',
  }
}

export async function GET(req: NextRequest) {
  const sp = new URL(req.url).searchParams

  const mode = sp.get('mode')?.toLowerCase()
  if (!mode) return err('mode is required')
  if (!VALID_MODES.has(mode)) return err(`unknown mode: ${mode}. Valid: ${[...VALID_MODES].join(', ')}`)

  const bypassCache = sp.get('refresh') === '1'

  // ── market_status ──────────────────────────────────────────────────────────
  if (mode === 'market_status') {
    const result = await execPython('scripts/alpaca_market.py', ['market_status'], alpacaEnv(), {
      bypassCache,
      timeoutMs: 8_000,
    })
    if (!result.ok) return err('market_status fetch failed', 502)
    return NextResponse.json(
      { success: true, data: result.data },
      { headers: cacheHeaders(mode, result.cached) }
    )
  }

  // ── symbol-based modes ─────────────────────────────────────────────────────
  const rawSymbols = sp.get('symbols') ?? sp.get('symbol') ?? ''
  const rawSymbol  = sp.get('symbol')?.trim().toUpperCase() ?? ''

  // For option_chain: single underlying symbol
  if (mode === 'option_chain') {
    if (!rawSymbol) return err('symbol is required for option_chain')
    if (!SYM_RE.test(rawSymbol)) return err('symbol contains invalid characters')
    const expiration = sp.get('expiration') ?? ''
    if (expiration && !DATE_RE.test(expiration)) return err('expiration must be YYYY-MM-DD')
    const args = ['option_chain', rawSymbol, ...(expiration ? [expiration] : [])]
    const result = await execPython('scripts/alpaca_market.py', args, alpacaEnv(), {
      bypassCache,
      timeoutMs: 30_000,
    })
    if (!result.ok) return err('option_chain fetch failed', 502)
    return NextResponse.json(
      { success: true, data: result.data },
      { headers: cacheHeaders(mode, result.cached) }
    )
  }

  // news: symbols optional
  if (mode === 'news') {
    const symbols = rawSymbols
      ? rawSymbols.split(',').map(s => s.trim().toUpperCase()).filter(s => SYM_RE.test(s))
      : []
    const limit = Math.min(parseInt(sp.get('limit') ?? '10', 10) || 10, 50)
    const args = ['news', ...(symbols.length ? [symbols.join(',')] : ['']), String(limit)]
    const result = await execPython('scripts/alpaca_market.py', args, alpacaEnv(), {
      bypassCache,
      timeoutMs: 10_000,
    })
    if (!result.ok) return err('news fetch failed', 502)
    return NextResponse.json(
      { success: true, data: result.data },
      { headers: cacheHeaders(mode, result.cached) }
    )
  }

  // earnings_calibration: single symbol
  if (mode === 'earnings_calibration') {
    if (!rawSymbol) return err('symbol is required')
    if (!SYM_RE.test(rawSymbol)) return err('symbol contains invalid characters')
    const days = Math.min(parseInt(sp.get('days') ?? '90', 10) || 90, 365)
    const result = await execPython(
      'scripts/alpaca_market.py',
      ['earnings_calibration', rawSymbol, String(days)],
      alpacaEnv(),
      { bypassCache, timeoutMs: 20_000 }
    )
    if (!result.ok) return err('earnings_calibration failed', 502)
    return NextResponse.json(
      { success: true, data: result.data },
      { headers: cacheHeaders(mode, result.cached) }
    )
  }

  // All remaining modes require at least one symbol
  if (!rawSymbols) return err('symbols is required')

  // Determine if we're in an options mode (OCC symbols) or stock mode
  const isOptionMode = mode.startsWith('option_')
  const symbolList = rawSymbols
    .split(',')
    .map(s => s.trim().toUpperCase())
    .filter(s => isOptionMode ? (OCC_RE.test(s) || SYM_RE.test(s)) : SYM_RE.test(s))
    .slice(0, isOptionMode ? 100 : 30)  // cap at 30 for stock (WS limit), 100 for options

  if (symbolList.length === 0) return err('no valid symbols provided')

  // Bar modes need timeframe + date range
  if (mode === 'stock_bars' || mode === 'option_bars') {
    const tf    = sp.get('timeframe') ?? '1d'
    const start = sp.get('start') ?? ''
    const end   = sp.get('end')   ?? ''
    const feed  = sp.get('feed')  ?? 'iex'

    if (!VALID_TF.has(tf) && !TF_RE.test(tf)) return err(`invalid timeframe: ${tf}`)
    if (start && !DATE_RE.test(start)) return err('start must be YYYY-MM-DD')
    if (end   && !DATE_RE.test(end))   return err('end must be YYYY-MM-DD')
    if (feed && !VALID_FEED.has(feed)) return err(`feed must be one of: ${[...VALID_FEED].join(', ')}`)

    const args = [mode, symbolList.join(','), tf, start, end, ...(mode === 'stock_bars' ? [feed] : [])]
    const result = await execPython('scripts/alpaca_market.py', args, alpacaEnv(), {
      bypassCache,
      timeoutMs: 25_000,
    })
    if (!result.ok) return err(`${mode} fetch failed`, 502)
    return NextResponse.json(
      { success: true, data: result.data },
      { headers: cacheHeaders(mode, result.cached) }
    )
  }

  // Remaining real-time modes: stock_quote, stock_bars_latest, stock_trades,
  // stock_snapshots, option_quotes, option_trades, option_snapshots
  const result = await execPython(
    'scripts/alpaca_market.py',
    [mode, symbolList.join(',')],
    alpacaEnv(),
    { bypassCache, timeoutMs: 12_000 }
  )

  if (!result.ok) {
    const isProd = process.env.NODE_ENV === 'production'
    return NextResponse.json(
      { success: false, error: isProd ? `${mode} fetch failed` : result.stderr },
      { status: 502, headers: { 'Cache-Control': 'no-store' } }
    )
  }

  return NextResponse.json(
    { success: true, data: result.data, cached: result.cached ?? false },
    { headers: cacheHeaders(mode, result.cached) }
  )
}

function cacheHeaders(mode: string, cached?: boolean): HeadersInit {
  const ttl = CACHE_TTL[mode] ?? 30
  const headers: HeadersInit = {
    'Cache-Control': cached
      ? `s-maxage=${ttl}, stale-while-revalidate=${ttl * 2}`
      : 'no-store',
  }
  if (cached) headers['X-Cache'] = 'HIT'
  return headers
}
