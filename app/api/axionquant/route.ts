/**
 * /api/axionquant — AxionQuant Alternative Data proxy  v2
 *
 * Confirmed working endpoints used by axionquant.py:
 *   /esg, /news, /sentiment/{sym}/social, /sentiment/{sym}/news, /sentiment/{sym}/analyst
 *   /supply-chain/{sym}/customers, /supply-chain/{sym}/suppliers, /supply-chain/{sym}/peers
 *   /profiles/{sym}, /profiles/{sym}/calendar
 *   /insiders/{sym}/transactions, /earnings/{sym}/history
 *   /filings/{sym}, /stocks/{sym}
 *
 * Cache strategy (1M calls/month → can refresh every 60s on frontend):
 *   - all:       60s  (live-grade freshness)
 *   - sentiment: 60s
 *   - news:      60s
 *   - esg:       1h   (rare changes)
 *   - supply:    2h
 *   - profile:   1h
 *   - insiders:  5m
 *   - earnings:  1h
 *   - filings:   30m
 *   - stocks:    30s
 */
import { NextRequest, NextResponse } from 'next/server'
import { execPython }                from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const SYM_RE = /^[A-Z0-9.^-]{1,12}$/

const VALID_MODES = new Set([
  'all', 'esg', 'news', 'sentiment', 'supply',
  'profile', 'insiders', 'earnings', 'filings', 'stocks',
])

const CACHE_SECONDS: Record<string, number> = {
  all:       60,
  news:      60,
  sentiment: 60,
  stocks:    30,
  insiders:  5 * 60,
  filings:   30 * 60,
  esg:       3600,
  profile:   3600,
  earnings:  3600,
  supply:    2 * 3600,
}

function err(msg: string, status = 400) {
  return NextResponse.json({ success: false, error: msg }, { status })
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)

  const mode = searchParams.get('mode')?.toLowerCase().trim() ?? ''
  if (!mode)                  return err('mode is required')
  if (!VALID_MODES.has(mode)) return err(`mode must be one of: ${[...VALID_MODES].join(', ')}`)

  const sym = searchParams.get('symbol')?.trim().toUpperCase() ?? ''
  if (!sym)              return err('symbol is required')
  if (!SYM_RE.test(sym)) return err('symbol contains invalid characters')

  const bypass = searchParams.get('refresh') === '1'

  const result = await execPython(
    'scripts/axionquant.py',
    [mode, sym],
    { AXIONQUANT_API_KEY: process.env.AXIONQUANT_API_KEY ?? 'axn_1cc27e77f2d56afb8ffa551a2d137004' },
    { bypassCache: bypass, timeoutMs: 25_000 },
  )

  if (!result.ok) {
    return NextResponse.json(
      {
        success: false,
        error:
          process.env.NODE_ENV === 'production'
            ? 'Failed to fetch AxionQuant data'
            : result.stderr,
      },
      { status: 502, headers: { 'Cache-Control': 'no-store' } },
    )
  }

  const ttl = CACHE_SECONDS[mode] ?? 900

  return NextResponse.json(
    { success: true, mode, symbol: sym, data: result.data?.data ?? result.data },
    {
      headers: {
        'Cache-Control': `s-maxage=${ttl}, stale-while-revalidate=${ttl * 2}`,
        ...(result.cached       ? { 'X-Cache':             'HIT'                      } : {}),
        ...(result.stale        ? { 'X-Cache-Stale':        'true'                     } : {}),
        ...(result.latencyMs != null
          ? { 'X-Python-Latency-Ms': String(result.latencyMs) } : {}),
      },
    },
  )
}
