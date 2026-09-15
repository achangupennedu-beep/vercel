/**
 * /api/eulerpool — Eulerpool financial data API proxy
 *
 * Proxies requests to eulerpool_source.py with caching and budget protection.
 * Eulerpool has a 1,000 req/month budget — responses are cached aggressively.
 *
 * Query params:
 *   mode     (required) — profile | fundamentals | analysts | institutional |
 *                         sentiment | derivatives | macro | screener
 *   symbol   (required for most modes) — e.g. "AAPL"
 *   code     (required for mode=macro)  — e.g. "GDP", "CPI"
 *   sector   (optional, screener)
 *   min_pe   (optional, screener)
 *   max_pe   (optional, screener)
 *   limit    (optional, screener, default 20)
 *
 * Example:
 *   GET /api/eulerpool?mode=fundamentals&symbol=AAPL
 *   GET /api/eulerpool?mode=screener&sector=Technology&max_pe=25&limit=10
 *   GET /api/eulerpool?mode=macro&code=CPI
 */
import { NextRequest, NextResponse } from 'next/server'

const EULERPOOL_BASE = 'https://api.eulerpool.com/api/1'
const REQUEST_TIMEOUT_MS = 8_000

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const SYM_RE   = /^[A-Z0-9.^-]{1,12}$/
const VALID_MODES = new Set([
  'profile', 'fundamentals', 'analysts', 'institutional',
  'sentiment', 'derivatives', 'macro', 'screener',
])

function err(msg: string, status = 400) {
  return NextResponse.json({ success: false, error: msg }, { status })
}

// Eulerpool responses are cached for 1 hour server-side (budget protection).
// The Python script also caches on-disk; this is an additional HTTP layer cache.
const CACHE_HEADERS: Record<string, string> = {
  'Cache-Control': 's-maxage=3600, stale-while-revalidate=7200',
}

export async function GET(req: NextRequest) {
  const started = performance.now()
  const { searchParams } = new URL(req.url)
  const mode = searchParams.get('mode')?.toLowerCase() ?? ''
  if (!mode) return err('mode is required')
  if (!VALID_MODES.has(mode)) return err(`mode must be one of: ${[...VALID_MODES].join(', ')}`)

  const symbol = searchParams.get('symbol')?.trim().toUpperCase() ?? ''
  const code = searchParams.get('code')?.trim().toUpperCase() ?? ''
  const identifier = symbol || code
  const needsIdentifier = !['screener'].includes(mode)
  if (needsIdentifier && (!identifier || !SYM_RE.test(identifier))) return err('valid symbol or code is required')

  const paths: Record<string, string> = {
    profile: `/equity/profile/${encodeURIComponent(identifier)}`,
    fundamentals: `/equity/incomestatement/${encodeURIComponent(identifier)}`,
    analysts: `/equity/estimates/${encodeURIComponent(identifier)}`,
    institutional: `/equity/ownership/${encodeURIComponent(identifier)}`,
    derivatives: `/equity/quotes/${encodeURIComponent(identifier)}`,
    sentiment: `/equity/quotes/${encodeURIComponent(identifier)}`,
    macro: `/macro/${encodeURIComponent(identifier)}`,
  }
  const path = mode === 'screener' ? '/equity/list/0/200' : paths[mode]
  const upstream = new URL(`${EULERPOOL_BASE}${path}`)
  upstream.searchParams.set('token', process.env.EULERPOOL_API_KEY ?? '')
  for (const key of ['startdate', 'enddate', 'language']) {
    const value = searchParams.get(key)
    if (value) upstream.searchParams.set(key, value)
  }

  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  try {
    const response = await fetch(upstream, {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
      cache: searchParams.get('refresh') === '1' ? 'no-store' : 'force-cache',
      next: searchParams.get('refresh') === '1' ? undefined : { revalidate: 900 },
    })
    const text = await response.text()
    let data: unknown
    try { data = JSON.parse(text) } catch { data = text.slice(0, 2000) }
    const headers = {
      ...CACHE_HEADERS,
      'X-Data-Source': 'eulerpool',
      'X-Upstream-Latency-Ms': String(Math.round(performance.now() - started)),
    }
    if (!response.ok) return NextResponse.json({ success: false, error: 'Eulerpool upstream request failed', status: response.status, data }, { status: response.status, headers: { 'Cache-Control': 'no-store' } })
    return NextResponse.json({ success: true, data, mode, symbol: identifier, source: 'eulerpool', fetchedAt: new Date().toISOString() }, { headers })
  } catch (error) {
    const message = error instanceof Error && error.name === 'AbortError' ? 'Eulerpool request timed out' : 'Eulerpool request failed'
    return err(message, 504)
  } finally {
    clearTimeout(timeout)
  }
}

