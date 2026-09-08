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
import { execPython }                from '@/lib/exec-python'

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
  const { searchParams } = new URL(req.url)

  const mode = searchParams.get('mode')?.toLowerCase() ?? ''
  if (!mode)                    return err('mode is required')
  if (!VALID_MODES.has(mode))   return err(`mode must be one of: ${[...VALID_MODES].join(', ')}`)

  const sym     = searchParams.get('symbol')?.trim().toUpperCase() ?? ''
  const code    = searchParams.get('code')?.trim().toUpperCase() ?? ''
  const sector  = searchParams.get('sector') ?? ''
  const minPe   = searchParams.get('min_pe') ?? ''
  const maxPe   = searchParams.get('max_pe') ?? ''
  const limit   = searchParams.get('limit')  ?? '20'

  // Validate symbol for modes that require it
  const needsSym = ['profile','fundamentals','analysts','institutional','sentiment','derivatives']
  if (needsSym.includes(mode)) {
    if (!sym)              return err('symbol is required for this mode')
    if (!SYM_RE.test(sym)) return err('symbol contains invalid characters')
  }

  // Build args for eulerpool_source.py
  let args: string[]
  if (mode === 'macro') {
    if (!code) return err('code is required for mode=macro')
    args = ['macro', code]
  } else if (mode === 'screener') {
    args = ['screener']
    if (sector) args.push(`--sector=${sector}`)
    if (minPe)  args.push(`--min-pe=${minPe}`)
    if (maxPe)  args.push(`--max-pe=${maxPe}`)
    if (limit)  args.push(`--limit=${limit}`)
  } else {
    args = [mode, sym]
  }

  const env = {
    EULERPOOL_API_KEY: process.env.EULERPOOL_API_KEY ?? 'eu_prod_1782933237805_jp4xbr2ag5c',
  }

  const result = await execPython('scripts/eulerpool_source.py', args, env, {
    bypassCache: searchParams.get('refresh') === '1',
    timeoutMs: 20_000,
  })

  if (!result.ok) {
    return NextResponse.json(
      {
        success: false,
        error: process.env.NODE_ENV === 'production'
          ? 'Failed to fetch Eulerpool data'
          : result.stderr,
      },
      { status: 502, headers: { 'Cache-Control': 'no-store' } }
    )
  }

  return NextResponse.json(
    { success: true, data: result.data, mode, symbol: sym || code },
    {
      headers: {
        ...CACHE_HEADERS,
        ...(result.cached       ? { 'X-Cache': 'HIT' }               : {}),
        ...(result.latencyMs != null ? { 'X-Python-Latency-Ms': String(result.latencyMs) } : {}),
      },
    }
  )
}
