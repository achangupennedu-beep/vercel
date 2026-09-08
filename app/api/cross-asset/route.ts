import { NextRequest, NextResponse } from 'next/server'
import { execPython } from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const SYM_RE = /^[A-Z0-9.^-]{1,12}$/

function errorResp(msg: string, status = 400) {
  return NextResponse.json({ success: false, error: msg }, { status })
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)
  const symbol   = searchParams.get('symbol')?.trim().toUpperCase() ?? 'SPY'
  const lookback = parseInt(searchParams.get('lookback') ?? '60', 10)
  const bypassCache = searchParams.get('refresh') === '1'

  if (!SYM_RE.test(symbol)) return errorResp('Invalid symbol')
  if (lookback < 5 || lookback > 365) return errorResp('lookback must be 5–365')

  const rawAssets = searchParams.get('assets')
  const assets = rawAssets
    ? rawAssets.split(',').map(s => s.trim().toUpperCase()).filter(s => SYM_RE.test(s)).slice(0, 10)
    : ['SPY', 'QQQ', 'IWM', 'TLT', 'GLD', 'HYG']

  const payload = JSON.stringify({ symbol, assets, lookback })

  const result = await execPython('scripts/cross_asset.py', [payload], {
    POLYGON_API_KEY:    process.env.POLYGON_API_KEY    ?? '',
    MARKETDATA_API_KEY: process.env.MARKETDATA_API_KEY ?? '',
    EODHD_API_KEY:      process.env.EODHD_API_KEY      ?? '6a3ac9d808bda9.37141543',
    FINNHUB_API_KEY:    process.env.FINNHUB_API_KEY    ?? '',
  }, { bypassCache })

  if (!result.ok) {
    const isProd = process.env.NODE_ENV === 'production'
    return NextResponse.json(
      { success: false, error: isProd ? 'Cross-asset fetch error' : result.stderr },
      { status: 502, headers: { 'Cache-Control': 'no-store' } }
    )
  }

  return NextResponse.json({ success: true, data: result.data }, {
    headers: {
      'Cache-Control': result.cached ? 's-maxage=120, stale-while-revalidate=240' : 'no-store',
      ...(result.latencyMs != null ? { 'X-Python-Latency-Ms': String(result.latencyMs) } : {}),
      ...(result.cached ? { 'X-Cache': 'HIT' } : {}),
    },
  })
}
