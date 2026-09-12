/**
 * /api/lse/candles — London Strategic Edge OHLCV candles
 *
 * Query params:
 *   symbol     (required) — e.g. "AAPL", "BTC/USD"
 *   timeframe  (optional) — 1m | 5m | 15m | 1h | 4h | 1d, default "1d"
 *   limit      (optional) — number of bars, default 200
 *   start      (optional) — ISO date string
 */
import { NextRequest, NextResponse } from 'next/server'
import { execPython }                from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const SYM_RE = /^[A-Z0-9./^-]{1,16}$/
const TF_RE  = /^(1m|5m|15m|1h|4h|1d)$/

function err(msg: string, status = 400) {
  return NextResponse.json({ success: false, error: msg }, { status })
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)
  const sym = searchParams.get('symbol')?.trim().toUpperCase()
  if (!sym)              return err('symbol is required')
  if (!SYM_RE.test(sym)) return err('symbol contains invalid characters')

  const tf    = searchParams.get('timeframe') ?? '1d'
  if (!TF_RE.test(tf)) return err('timeframe must be one of 1m, 5m, 15m, 1h, 4h, 1d')

  const limit  = Math.min(2000, parseInt(searchParams.get('limit') ?? '200', 10) || 200)
  const bypass = searchParams.get('refresh') === '1'

  const lseKey = process.env.LSE_API_KEY?.trim()
  if (!lseKey) return err('LSE_API_KEY is not configured', 503)
  const env = { LSE_API_KEY: lseKey }
  const args = ['candles', sym, tf, String(limit)]

  const result = await execPython('scripts/lse_source.py', args, env, {
    bypassCache: bypass,
    timeoutMs: 12_000,
  })

  if (!result.ok) {
    return NextResponse.json(
      { success: false, error: process.env.NODE_ENV === 'production'
          ? 'Failed to fetch LSE candles'
          : result.stderr },
      { status: 502, headers: { 'Cache-Control': 'no-store' } }
    )
  }

  return NextResponse.json(
    { success: true, data: result.data },
    {
      headers: {
        'Cache-Control': tf === '1d'
          ? 's-maxage=3600, stale-while-revalidate=7200'
          : 's-maxage=60, stale-while-revalidate=120',
        ...(result.latencyMs != null ? { 'X-Python-Latency-Ms': String(result.latencyMs) } : {}),
      },
    }
  )
}
