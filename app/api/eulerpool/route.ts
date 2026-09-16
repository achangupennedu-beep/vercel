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
import { execPython } from '@/lib/exec-python'

const REQUEST_TIMEOUT_MS = 20_000

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const IDENTIFIER_RE = /^(?:[A-Za-z0-9]{1,12}|[A-Za-z0-9]{2,8}:[A-Za-z0-9.^-]{1,12})$/
const VALID_MODES = new Set([
  'profile', 'fundamentals', 'analysts', 'institutional',
  'sentiment', 'derivatives', 'macro', 'screener', 'quote', 'etf-profile',
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

  const symbol = searchParams.get('symbol')?.trim() ?? ''
  const code = searchParams.get('code')?.trim() ?? ''
  const identifier = symbol || code
  const needsIdentifier = !['screener'].includes(mode)
  if (needsIdentifier && (!identifier || !IDENTIFIER_RE.test(identifier))) return err('valid Eulerpool identifier is required (ISIN, ticker, or exchange:ticker)')

  const args = [mode]
  if (mode === 'macro') args.push(identifier)
  else if (mode !== 'screener') args.push(identifier)
  if (mode === 'quote') {
    for (const key of ['startdate', 'enddate']) {
      const value = searchParams.get(key)
      if (value) args.push(`--${key}=${value}`)
    }
  }
  if (mode === 'screener') {
    for (const key of ['sector', 'min-pe', 'max-pe', 'limit']) {
      const value = searchParams.get(key.replace('-', '_')) ?? searchParams.get(key)
      if (value) args.push(`--${key}=${value}`)
    }
  }
  const result = await execPython('scripts/eulerpool_source.py', args, { EULERPOOL_API_KEY: process.env.EULERPOOL_API_KEY ?? '' }, {
    bypassCache: searchParams.get('refresh') === '1',
    timeoutMs: REQUEST_TIMEOUT_MS,
  })
  const headers = {
    ...CACHE_HEADERS,
    'X-Data-Source': 'eulerpool',
    'X-Python-Latency-Ms': String(result.latencyMs ?? Math.round(performance.now() - started)),
  }
  const dataRecord = result.data && typeof result.data === 'object' ? result.data as Record<string, unknown> : null
  const upstreamError = typeof dataRecord?.error === 'string' ? dataRecord.error : null
  if (!result.ok || upstreamError) return NextResponse.json({ success: false, error: upstreamError ?? 'Eulerpool Python data fetch failed', detail: process.env.NODE_ENV === 'production' ? undefined : result.stderr }, { status: 502, headers: { 'Cache-Control': 'no-store' } })
  return NextResponse.json({ success: true, data: result.data, mode, identifier, source: 'eulerpool', fetchedAt: new Date().toISOString(), cached: result.cached === true }, { headers })
}

