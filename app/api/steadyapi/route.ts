/**
 * /api/steadyapi — Unified SteadyAPI.com proxy
 * https://docs.steadyapi.com/
 *
 * Auth: Bearer token via STEADYAPI_KEY env var.
 * Rate limit: 15 req/s upstream.
 *
 * ── Modes (query param: mode) ────────────────────────────────────────────────
 *
 * General / Market
 *   search              GET  /v2/markets/search           ?q=AAPL
 *   movers              GET  /v2/markets/movers           ?change_type=PERCENT&direction=UP&page=1
 *   market_info         GET  /v2/markets/market-info
 *   screener            GET  /v2/markets/screener         ?filter=high_volume&metric=overview&page=1
 *   news                GET  /v2/markets/news             ?ticker=AAPL&type=ALL
 *   insider_trades      GET  /v1/markets/insider-trades   ?minValue=10000&type=Buy&page=1
 *
 * Stocks
 *   quote               GET  /v1/markets/quote            ?ticker=AAPL&type=STOCKS
 *   quotes              GET  /v1/markets/stock/quotes     ?ticker=AAPL,TSLA
 *   history             GET  /v2/markets/stock/history    ?ticker=AAPL&interval=1d&limit=100
 *   modules             GET  /v1/markets/stock/modules    ?ticker=AAPL&module=profile
 *   analyst_ratings     GET  /v1/markets/stock/analyst-ratings ?ticker=AAPL&page=1
 *
 * Options
 *   options_chain       GET  /v1/markets/options          ?ticker=AAPL&display=straddle
 *   options_chain_v2    GET  /v2/markets/options          ?ticker=AAPL&type=STOCKS&limit=50
 *   options_chain_v3    GET  /v3/markets/options          ?ticker=AAPL
 *   unusual_options     GET  /v1/markets/options/unusual-options-activity ?type=STOCKS&page=1
 *   iv_rank             GET  /v1/markets/options/iv-rank-percentile       ?type=STOCKS&page=1
 *   iv_change           GET  /v1/markets/options/iv-change                ?type=STOCKS&direction=UP
 *   most_active_options GET  /v1/markets/options/most-active              ?type=STOCKS&page=1
 *   highest_iv          GET  /v1/markets/options/highest-iv               ?sort=HIGHEST&page=1
 *   options_flow        GET  /v1/markets/options/options-flow             ?type=STOCKS&page=1
 *
 * Calendar
 *   earnings_calendar   GET  /v1/markets/calendar/earnings  ?date=YYYY-MM-DD
 *   dividends_calendar  GET  /v1/markets/calendar/dividends ?date=YYYY-MM-DD
 *
 * Technical Indicators
 *   sma                 GET  /v1/markets/indicators/sma     ?ticker=AAPL&period=20
 *   rsi                 GET  /v1/markets/indicators/rsi     ?ticker=AAPL&period=14
 *   macd                GET  /v1/markets/indicators/macd    ?ticker=AAPL
 *
 * ── Cache TTLs (seconds) ─────────────────────────────────────────────────────
 *   Realtime quotes / options tick: 5 s
 *   IV data / screener / movers:   15 s
 *   News / market_info:           120 s
 *   History / calendar / modules: 900 s (15 min)
 *   Analyst ratings:             3600 s (1 hr)
 */

import { NextRequest, NextResponse } from 'next/server'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

// ── Constants ────────────────────────────────────────────────────────────────

const BASE_URL   = 'https://api.steadyapi.com'
const STEADY_KEY = process.env.STEADYAPI_KEY ?? '2441|yoWrSsOS2A3eQjj8NmD2wXwjf4ut7F8G2wblrwFj'

const SYM_RE  = /^[A-Z0-9.\^-]{1,12}$/
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/

// Cache TTL in seconds per mode
const MODE_TTL: Record<string, number> = {
  search:              30,
  movers:              15,
  market_info:        120,
  screener:            15,
  news:               120,
  insider_trades:     300,
  quote:                5,
  quotes:               5,
  history:            900,
  modules:            900,
  analyst_ratings:   3600,
  options_chain:        5,
  options_chain_v2:     5,
  options_chain_v3:     5,
  unusual_options:     15,
  iv_rank:             15,
  iv_change:           15,
  most_active_options: 15,
  highest_iv:          15,
  options_flow:        15,
  earnings_calendar:  900,
  dividends_calendar: 900,
  sma:                 60,
  rsi:                 60,
  macd:                60,
}

const VALID_MODES = new Set(Object.keys(MODE_TTL))

const VALID_ASSET_CLASS  = new Set(['STOCKS', 'ETF', 'ETFS', 'MUTUALFUNDS', 'FUTURES', 'INDICES', 'INDEX'])
const VALID_DIRECTIONS   = new Set(['UP', 'DOWN'])
const VALID_CHANGE_TYPES = new Set(['PERCENT', 'PRICE', 'GAP'])
const VALID_MODULES      = new Set([
  'profile', 'income-statement', 'income-statement-v2',
  'balance-sheet', 'balance-sheet-v2', 'cashflow-statement', 'cashflow-statement-v2',
  'financial-data', 'statistics', 'ratios', 'calendar-events', 'sec-filings',
  'recommendation-trend', 'upgrade-downgrade-history', 'insider-transactions',
  'insider-holders', 'net-share-purchase-activity', 'earnings',
  'index-trend', 'industry-trend', 'sector-trend',
])
const VALID_HISTORY_INTERVALS = new Set([
  '1min', '5min', '15min', '30min', '60min', '120min', '240min',
  'daily', 'weekly', 'monthly', 'quarterly',
  '1m', '5m', '15m', '30m', '1h', '1d', '1wk', '1mo',
])
const VALID_IV_SORTS = new Set(['HIGHEST', 'LOWEST'])

// ── Helper: build auth headers ───────────────────────────────────────────────

function authHeaders(): Record<string, string> {
  return {
    'Authorization': `Bearer ${STEADY_KEY}`,
    'Accept':        'application/json',
    'User-Agent':    'APEX-Terminal/1.0',
  }
}

// ── Helper: proxy to SteadyAPI ───────────────────────────────────────────────

async function proxyGet(
  endpoint: string,
  params: Record<string, string | number | undefined>,
  ttl: number,
): Promise<{ data: any; error?: string; status: number }> {
  const url = new URL(`${BASE_URL}${endpoint}`)
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== '') url.searchParams.set(k, String(v))
  }

  try {
    const res = await fetch(url.toString(), {
      headers: authHeaders(),
      next: { revalidate: ttl },
    })

    const body = await res.json().catch(() => null)

    if (!res.ok) {
      return {
        data:   null,
        error:  body?.message ?? `SteadyAPI ${res.status}`,
        status: res.status,
      }
    }

    return { data: body, status: 200 }
  } catch (e: any) {
    return { data: null, error: e?.message ?? 'fetch failed', status: 502 }
  }
}

// ── Validation helpers ───────────────────────────────────────────────────────

function err(msg: string, status = 400) {
  return NextResponse.json({ success: false, error: msg }, { status })
}

function validateTicker(ticker: string | null): string | null {
  if (!ticker) return null
  // Accept comma-separated list of up to 10 symbols
  const syms = ticker.split(',').map(s => s.trim().toUpperCase()).filter(Boolean)
  if (syms.length === 0 || syms.length > 10) return null
  if (!syms.every(s => SYM_RE.test(s))) return null
  return syms.join(',')
}

// ── Main handler ─────────────────────────────────────────────────────────────

export async function GET(req: NextRequest) {
  const sp   = new URL(req.url).searchParams
  const mode = sp.get('mode')?.toLowerCase()

  if (!mode)                    return err('mode is required')
  if (!VALID_MODES.has(mode))   return err(`unknown mode. Valid: ${[...VALID_MODES].join(', ')}`)

  const ttl = MODE_TTL[mode]

  // ── search ─────────────���──────────────────────────────────────────────────
  if (mode === 'search') {
    const q = sp.get('q')?.trim()
    if (!q || q.length < 1) return err('q is required')
    if (q.length > 50)       return err('q too long')
    const { data, error, status } = await proxyGet('/v2/markets/search', { search: q }, ttl)
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({ success: true, data: data?.body ?? data }, { headers: cacheHeaders(ttl) })
  }

  // ── market_info ───────────────────────────────────────────────────────────
  if (mode === 'market_info') {
    const { data, error, status } = await proxyGet('/v2/markets/market-info', {}, ttl)
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({ success: true, data: data?.body ?? data }, { headers: cacheHeaders(ttl) })
  }

  // ── movers ────────────────────────────────────────────────────────────────
  if (mode === 'movers') {
    const change_type = (sp.get('change_type') ?? 'PERCENT').toUpperCase()
    const direction   = sp.get('direction')?.toUpperCase()
    const page        = sp.get('page') ?? '1'
    const price_min   = sp.get('price_min')

    if (!VALID_CHANGE_TYPES.has(change_type))
      return err(`change_type must be one of: ${[...VALID_CHANGE_TYPES].join(', ')}`)
    if (direction && !VALID_DIRECTIONS.has(direction))
      return err('direction must be UP or DOWN')

    const { data, error, status } = await proxyGet('/v2/markets/movers', {
      change_type, ...(direction ? { direction } : {}), page, ...(price_min ? { price_min } : {}),
    }, ttl)
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({
      success: true,
      data:    data?.body ?? data,
      meta:    data?.meta,
    }, { headers: cacheHeaders(ttl) })
  }

  // ── screener ──────────────────────────────────────────────────────────────
  if (mode === 'screener') {
    const filter = sp.get('filter') ?? 'high_volume'
    const metric = sp.get('metric') ?? sp.get('metricType') ?? 'overview'
    const page   = sp.get('page') ?? '1'
    const { data, error, status } = await proxyGet('/v2/markets/screener', {
      metricType: metric, filter, page,
    }, ttl)
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({
      success: true,
      data:    data?.body ?? data,
      meta:    data?.meta,
    }, { headers: cacheHeaders(ttl) })
  }

  // ── news ──────────────────────────────────────────────────────────────────
  if (mode === 'news') {
    const ticker = sp.get('ticker')
    const type   = sp.get('type') ?? 'ALL'
    const params: Record<string, string | undefined> = { type }
    if (ticker) {
      const validated = validateTicker(ticker)
      if (!validated) return err('invalid ticker format')
      params.ticker = validated
    }
    const { data, error, status } = await proxyGet('/v2/markets/news', params, ttl)
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({
      success: true,
      data:    data?.body ?? data,
      meta:    data?.meta,
    }, { headers: cacheHeaders(ttl) })
  }

  // ── insider_trades ────────────────────────────────────────────────────────
  if (mode === 'insider_trades') {
    const ticker         = sp.get('ticker')
    const type_filter    = sp.get('type')
    const minValue       = sp.get('minValue')
    const politiciansOnly = sp.get('politiciansOnly')
    const page           = sp.get('page') ?? '1'

    const params: Record<string, string | undefined> = { page }
    if (ticker) {
      const validated = validateTicker(ticker)
      if (!validated) return err('invalid ticker format')
      params.ticker = validated
    }
    if (type_filter && ['Buy', 'Sell', 'Transfer'].includes(type_filter))
      params.type = type_filter
    if (minValue && /^\d+$/.test(minValue)) params.minValue = minValue
    if (politiciansOnly === 'true' || politiciansOnly === 'false')
      params.politiciansOnly = politiciansOnly

    const { data, error, status } = await proxyGet('/v1/markets/insider-trades', params, ttl)
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({
      success: true,
      data:    data?.body ?? data,
      meta:    data?.meta,
    }, { headers: cacheHeaders(ttl) })
  }

  // ── quote (single ticker, real-time) ──────────────────────────────────────
  if (mode === 'quote') {
    const ticker = sp.get('ticker')?.trim().toUpperCase()
    const type   = (sp.get('type') ?? 'STOCKS').toUpperCase()
    if (!ticker)             return err('ticker is required')
    if (!SYM_RE.test(ticker)) return err('ticker contains invalid characters')
    if (!VALID_ASSET_CLASS.has(type)) return err(`type must be one of: ${[...VALID_ASSET_CLASS].join(', ')}`)

    const { data, error, status } = await proxyGet('/v1/markets/quote', { ticker, type }, ttl)
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({ success: true, data: data?.body ?? data }, { headers: cacheHeaders(ttl) })
  }

  // ── quotes (multi-ticker) ─────────────────────────────────────────────────
  if (mode === 'quotes') {
    const ticker = sp.get('ticker')
    if (!ticker) return err('ticker is required')
    const validated = validateTicker(ticker)
    if (!validated) return err('invalid ticker(s): max 10 comma-separated symbols')

    const { data, error, status } = await proxyGet('/v1/markets/stock/quotes', { ticker: validated }, ttl)
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({ success: true, data: data?.body ?? data }, { headers: cacheHeaders(ttl) })
  }

  // ── history ───────────────────────────────────────────────────────────────
  if (mode === 'history') {
    const ticker   = sp.get('ticker')?.trim().toUpperCase()
    const interval = sp.get('interval') ?? '1d'
    const limit    = sp.get('limit')    ?? '100'
    const start    = sp.get('start')    ?? sp.get('startDate')
    const end      = sp.get('end')      ?? sp.get('endDate')

    if (!ticker)              return err('ticker is required')
    if (!SYM_RE.test(ticker)) return err('ticker contains invalid characters')
    if (!VALID_HISTORY_INTERVALS.has(interval))
      return err(`interval must be one of: ${[...VALID_HISTORY_INTERVALS].join(', ')}`)
    if (start && !DATE_RE.test(start)) return err('start must be YYYY-MM-DD')
    if (end   && !DATE_RE.test(end))   return err('end must be YYYY-MM-DD')

    const params: Record<string, string | undefined> = { ticker, interval, limit }
    if (start) params.startDate = start.replace(/-/g, '')  // API uses YYYYMMDD
    if (end)   params.endDate   = end.replace(/-/g, '')

    const { data, error, status } = await proxyGet('/v2/markets/stock/history', params, ttl)
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({
      success: true,
      data:    data?.body ?? data,
      meta:    data?.meta,
    }, { headers: cacheHeaders(ttl) })
  }

  // ── modules (profile, financials, etc.) ──────────────────────────────────
  if (mode === 'modules') {
    const ticker    = sp.get('ticker')?.trim().toUpperCase()
    const module    = sp.get('module')    ?? 'profile'
    const timeframe = sp.get('timeframe') ?? 'annually'

    if (!ticker)              return err('ticker is required')
    if (!SYM_RE.test(ticker)) return err('ticker contains invalid characters')
    if (!VALID_MODULES.has(module))
      return err(`module must be one of: ${[...VALID_MODULES].join(', ')}`)

    const { data, error, status } = await proxyGet('/v1/markets/stock/modules', {
      ticker, module, timeframe,
    }, ttl)
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({ success: true, data: data?.body ?? data }, { headers: cacheHeaders(ttl) })
  }

  // ── analyst_ratings ───────────────────────────────────────────────────────
  if (mode === 'analyst_ratings') {
    const ticker = sp.get('ticker')
    const page   = sp.get('page') ?? '1'
    if (!ticker) return err('ticker is required')
    const validated = validateTicker(ticker)
    if (!validated) return err('invalid ticker(s): max 10 comma-separated symbols')

    const { data, error, status } = await proxyGet('/v1/markets/stock/analyst-ratings', {
      ticker: validated, page,
    }, ttl)
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({
      success: true,
      data:    data?.body ?? data,
      meta:    data?.meta,
    }, { headers: cacheHeaders(ttl) })
  }

  // ── options_chain (v1) ────────────────────────────────────────────────────
  if (mode === 'options_chain') {
    const ticker     = sp.get('ticker')?.trim().toUpperCase()
    const expiration = sp.get('expiration')
    const display    = sp.get('display') ?? 'list'

    if (!ticker)              return err('ticker is required')
    if (!SYM_RE.test(ticker)) return err('ticker contains invalid characters')
    if (display && !['list', 'straddle'].includes(display))
      return err('display must be list or straddle')

    const params: Record<string, string | undefined> = { ticker, display }
    if (expiration) params.expiration = expiration

    const { data, error, status } = await proxyGet('/v1/markets/options', params, ttl)
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({ success: true, data: data?.body ?? data }, { headers: cacheHeaders(ttl) })
  }

  // ── options_chain_v2 ──────────────────────────────────────────────────────
  if (mode === 'options_chain_v2') {
    const ticker    = sp.get('ticker')?.trim().toUpperCase()
    const type      = (sp.get('type') ?? 'STOCKS').toUpperCase()
    const from_date = sp.get('from_date')
    const to_date   = sp.get('to_date')
    const limit     = sp.get('limit') ?? '50'

    if (!ticker)              return err('ticker is required')
    if (!SYM_RE.test(ticker)) return err('ticker contains invalid characters')
    if (!VALID_ASSET_CLASS.has(type)) return err(`type must be one of: ${[...VALID_ASSET_CLASS].join(', ')}`)
    if (from_date && !DATE_RE.test(from_date)) return err('from_date must be YYYY-MM-DD')
    if (to_date   && !DATE_RE.test(to_date))   return err('to_date must be YYYY-MM-DD')

    const params: Record<string, string | undefined> = { ticker, type, limit }
    if (from_date) params.from_date = from_date
    if (to_date)   params.to_date   = to_date

    const { data, error, status } = await proxyGet('/v2/markets/options', params, ttl)
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({
      success: true,
      data:    data?.body ?? data,
      meta:    data?.meta,
    }, { headers: cacheHeaders(ttl) })
  }

  // ── options_chain_v3 ──────────────────────────────────────────────────────
  if (mode === 'options_chain_v3') {
    const ticker = sp.get('ticker')?.trim().toUpperCase()
    if (!ticker)              return err('ticker is required')
    if (!SYM_RE.test(ticker)) return err('ticker contains invalid characters')

    const { data, error, status } = await proxyGet('/v3/markets/options', { ticker }, ttl)
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({
      success: true,
      data:    data?.body ?? data,
      meta:    data?.meta,
    }, { headers: cacheHeaders(ttl) })
  }

  // ── unusual_options ───────────────────────────────────────────────────────
  if (mode === 'unusual_options') {
    const type      = (sp.get('type') ?? 'STOCKS').toUpperCase()
    const ticker    = sp.get('ticker')
    const date      = sp.get('date')
    const price_min = sp.get('price_min')
    const page      = sp.get('page') ?? '1'

    if (!VALID_ASSET_CLASS.has(type)) return err(`type must be STOCKS, ETFS, or INDICES`)
    if (date && !DATE_RE.test(date))  return err('date must be YYYY-MM-DD')

    const params: Record<string, string | undefined> = { type, page }
    if (ticker) {
      const v = validateTicker(ticker)
      if (!v) return err('invalid ticker')
      params.ticker = v
    }
    if (date)      params.date      = date
    if (price_min) params.price_min = price_min

    const { data, error, status } = await proxyGet(
      '/v1/markets/options/unusual-options-activity', params, ttl,
    )
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({
      success: true,
      data:    data?.body ?? data,
      meta:    data?.meta,
    }, { headers: cacheHeaders(ttl) })
  }

  // ── iv_rank ───────────────────────────────────────────────────────────────
  if (mode === 'iv_rank') {
    const type      = (sp.get('type') ?? 'STOCKS').toUpperCase()
    const price_min = sp.get('price_min')
    const page      = sp.get('page') ?? '1'

    if (!VALID_ASSET_CLASS.has(type)) return err('type must be STOCKS, ETFS, or INDICES')

    const params: Record<string, string | undefined> = { type, page }
    if (price_min) params.price_min = price_min

    const { data, error, status } = await proxyGet(
      '/v1/markets/options/iv-rank-percentile', params, ttl,
    )
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({
      success: true,
      data:    data?.body ?? data,
      meta:    data?.meta,
    }, { headers: cacheHeaders(ttl) })
  }

  // ── iv_change ─────────────────────────────────────────────────────────────
  if (mode === 'iv_change') {
    const type      = (sp.get('type') ?? 'STOCKS').toUpperCase()
    const direction = sp.get('direction')?.toUpperCase()
    const price_min = sp.get('price_min')
    const page      = sp.get('page') ?? '1'

    if (!VALID_ASSET_CLASS.has(type)) return err('type must be STOCKS, ETFS, or INDICES')
    if (direction && !VALID_DIRECTIONS.has(direction)) return err('direction must be UP or DOWN')

    const params: Record<string, string | undefined> = { type, page }
    if (direction) params.direction = direction
    if (price_min) params.price_min = price_min

    const { data, error, status } = await proxyGet(
      '/v1/markets/options/iv-change', params, ttl,
    )
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({
      success: true,
      data:    data?.body ?? data,
      meta:    data?.meta,
    }, { headers: cacheHeaders(ttl) })
  }

  // ── most_active_options ───────────────────────────────────────────────────
  if (mode === 'most_active_options') {
    const type = (sp.get('type') ?? 'STOCKS').toUpperCase()
    const page = sp.get('page') ?? '1'

    if (!VALID_ASSET_CLASS.has(type)) return err('type must be STOCKS, ETFS, or INDICES')

    const { data, error, status } = await proxyGet(
      '/v1/markets/options/most-active', { type, page }, ttl,
    )
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({
      success: true,
      data:    data?.body ?? data,
      meta:    data?.meta,
    }, { headers: cacheHeaders(ttl) })
  }

  // ── highest_iv ────────────────────────────────────────────────────────────
  if (mode === 'highest_iv') {
    const sort = (sp.get('sort') ?? 'HIGHEST').toUpperCase()
    const page = sp.get('page') ?? '1'

    if (!VALID_IV_SORTS.has(sort)) return err('sort must be HIGHEST or LOWEST')

    const { data, error, status } = await proxyGet(
      '/v1/markets/options/highest-iv', { sort, page }, ttl,
    )
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({
      success: true,
      data:    data?.body ?? data,
      meta:    data?.meta,
    }, { headers: cacheHeaders(ttl) })
  }

  // ── options_flow ──────────────────────────────────────────────────────────
  if (mode === 'options_flow') {
    const type = (sp.get('type') ?? 'STOCKS').toUpperCase()
    const page = sp.get('page') ?? '1'

    if (!VALID_ASSET_CLASS.has(type)) return err('type must be STOCKS, ETFS, or INDICES')

    const { data, error, status } = await proxyGet(
      '/v1/markets/options/options-flow', { type, page }, ttl,
    )
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({
      success: true,
      data:    data?.body ?? data,
      meta:    data?.meta,
    }, { headers: cacheHeaders(ttl) })
  }

  // ── earnings_calendar ─────────────────────────────────────────────────────
  if (mode === 'earnings_calendar') {
    const date = sp.get('date')
    if (date && !DATE_RE.test(date)) return err('date must be YYYY-MM-DD')

    const params: Record<string, string | undefined> = {}
    if (date) params.date = date

    const { data, error, status } = await proxyGet(
      '/v1/markets/calendar/earnings', params, ttl,
    )
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({ success: true, data: data?.body ?? data }, { headers: cacheHeaders(ttl) })
  }

  // ── dividends_calendar ────────────────────────────────────────────────────
  if (mode === 'dividends_calendar') {
    const date = sp.get('date')
    if (date && !DATE_RE.test(date)) return err('date must be YYYY-MM-DD')

    const params: Record<string, string | undefined> = {}
    if (date) params.date = date

    const { data, error, status } = await proxyGet(
      '/v1/markets/calendar/dividends', params, ttl,
    )
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({ success: true, data: data?.body ?? data }, { headers: cacheHeaders(ttl) })
  }

  // ── sma ───────────────────────────────────────────────────────────────────
  if (mode === 'sma') {
    const ticker = sp.get('ticker')?.trim().toUpperCase()
    const period = sp.get('period') ?? '20'
    if (!ticker)              return err('ticker is required')
    if (!SYM_RE.test(ticker)) return err('ticker contains invalid characters')
    if (!/^\d{1,4}$/.test(period)) return err('period must be a positive integer')

    const { data, error, status } = await proxyGet(
      '/v1/markets/indicators/sma', { ticker, period }, ttl,
    )
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({ success: true, data: data?.body ?? data }, { headers: cacheHeaders(ttl) })
  }

  // ── rsi ───────────────────────────────────────────────────────────────────
  if (mode === 'rsi') {
    const ticker = sp.get('ticker')?.trim().toUpperCase()
    const period = sp.get('period') ?? '14'
    if (!ticker)              return err('ticker is required')
    if (!SYM_RE.test(ticker)) return err('ticker contains invalid characters')
    if (!/^\d{1,4}$/.test(period)) return err('period must be a positive integer')

    const { data, error, status } = await proxyGet(
      '/v1/markets/indicators/rsi', { ticker, period }, ttl,
    )
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({ success: true, data: data?.body ?? data }, { headers: cacheHeaders(ttl) })
  }

  // ── macd ──────────────────────────────────────────────────────────────────
  if (mode === 'macd') {
    const ticker = sp.get('ticker')?.trim().toUpperCase()
    if (!ticker)              return err('ticker is required')
    if (!SYM_RE.test(ticker)) return err('ticker contains invalid characters')

    const { data, error, status } = await proxyGet(
      '/v1/markets/indicators/macd', { ticker }, ttl,
    )
    if (error) return NextResponse.json({ success: false, error }, { status })
    return NextResponse.json({ success: true, data: data?.body ?? data }, { headers: cacheHeaders(ttl) })
  }

  // Should never reach here given the VALID_MODES guard above
  return err(`mode '${mode}' is not implemented`)
}

// ── Cache-control header builder ─────────────────────────────────────────────

function cacheHeaders(ttl: number): HeadersInit {
  return {
    'Cache-Control': `s-maxage=${ttl}, stale-while-revalidate=${ttl * 2}`,
  }
}
