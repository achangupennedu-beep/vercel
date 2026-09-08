import { NextRequest, NextResponse } from 'next/server'
import { execPython, warmCache } from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

// Kick off cache warming on first import (server cold-start).
// This runs in the background and does not block requests.
warmCache(['AAPL', 'SPY', 'QQQ'])

const SYM_RE = /^[A-Z0-9.^-]{1,12}$/
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/
const MAX_SYM_LEN = 12

function errorResp(msg: string, status = 400) {
  return NextResponse.json({ success: false, error: msg }, { status })
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)

  // ── Input validation ────────────────────────────────────────────────────────
  const rawSym = searchParams.get('symbol')?.trim().toUpperCase()
  if (!rawSym) return errorResp('symbol is required')
  if (!SYM_RE.test(rawSym)) return errorResp('symbol contains invalid characters')
  if (rawSym.length > MAX_SYM_LEN) return errorResp('symbol too long')

  const expiration = searchParams.get('expiration')
  if (expiration && !DATE_RE.test(expiration)) return errorResp('expiration must be YYYY-MM-DD')

  // Optional: bypass server cache for a hard refresh
  const bypassCache = searchParams.get('refresh') === '1'

  const args = expiration ? [rawSym, expiration] : [rawSym]
  const env = {
    APCA_API_KEY_ID: process.env.APCA_API_KEY_ID ?? '',
    APCA_API_SECRET_KEY: process.env.APCA_API_SECRET_KEY ?? '',
    POLYGON_API_KEY: process.env.POLYGON_API_KEY ?? '',
    MARKETDATA_API_KEY: process.env.MARKETDATA_API_KEY ?? '',
    EODHD_API_KEY: process.env.EODHD_API_KEY ?? '',
    FINNHUB_API_KEY: process.env.FINNHUB_API_KEY ?? '',
    TIINGO_API_KEY: process.env.TIINGO_API_KEY ?? '',
    TWELVEDATA_API_KEY: process.env.TWELVEDATA_API_KEY ?? '',
    MASSIVE_API_KEY: process.env.MASSIVE_API_KEY ?? '',
    OPENFIGI_KEY: process.env.OPENFIGI_KEY ?? '',
    INSIGHTSENTRY_KEY: process.env.INSIGHTSENTRY_KEY ?? '',
    RAPIDAPI_ACCESS_TOKEN: process.env.RAPIDAPI_ACCESS_TOKEN ?? '',
    OPTIONDATA_KEY: process.env.OPTIONDATA_KEY ?? '',
    LSE_API_KEY: process.env.LSE_API_KEY ?? '',
    EULERPOOL_API_KEY: process.env.EULERPOOL_API_KEY ?? '',
    AV_KEY_1: process.env.AV_KEY_1 ?? '', AV_KEY_2: process.env.AV_KEY_2 ?? '',
    AV_KEY_3: process.env.AV_KEY_3 ?? '', AV_KEY_4: process.env.AV_KEY_4 ?? '',
    AV_KEY_5: process.env.AV_KEY_5 ?? '', AV_KEY_6: process.env.AV_KEY_6 ?? '',
    AV_KEY_7: process.env.AV_KEY_7 ?? '', AV_KEY_8: process.env.AV_KEY_8 ?? '',
    AV_KEY_9: process.env.AV_KEY_9 ?? '', AV_KEY_10: process.env.AV_KEY_10 ?? '',
  }

  const result = await execPython('scripts/options.py', args, env, { bypassCache })

  if (!result.ok) {
    // Return 502 with detail for debugging; do NOT expose raw stderr in prod
    const isProd = process.env.NODE_ENV === 'production'
    return NextResponse.json(
      { success: false, error: isProd ? 'Failed to fetch options data' : result.stderr },
      {
        status: 502,
        headers: { 'Cache-Control': 'no-store' },
      }
    )
  }

  // ── Response ─────────────────────────────────────────────────────────────────
  const headers: HeadersInit = {
    'Cache-Control': result.cached
      ? 's-maxage=30, stale-while-revalidate=120'
      : 'no-store',
  }
  if (result.latencyMs != null) headers['X-Python-Latency-Ms'] = String(result.latencyMs)
  if (result.cached) headers['X-Cache'] = result.stale ? 'STALE' : 'HIT'

  return NextResponse.json(
    { success: true, data: result.data, stale: result.stale ?? false },
    { headers }
  )
}
