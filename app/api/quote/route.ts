import { NextRequest, NextResponse } from 'next/server'
import { execPython } from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const SYM_RE  = /^[A-Z0-9.^-]{1,12}$/
const MAX_BATCH = 20

function errorResp(msg: string, status = 400) {
  return NextResponse.json({ success: false, error: msg }, { status })
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)

  const symbolParam  = searchParams.get('symbol')?.trim().toUpperCase()
  const symbolsParam = searchParams.get('symbols')

  // Build deduplicated, validated target list
  let symbols: string[]
  if (symbolsParam) {
    symbols = symbolsParam
      .split(',')
      .map(s => s.trim().toUpperCase())
      .filter(s => SYM_RE.test(s))
      .slice(0, MAX_BATCH)
  } else if (symbolParam) {
    if (!SYM_RE.test(symbolParam)) return errorResp('symbol contains invalid characters')
    symbols = [symbolParam]
  } else {
    return errorResp('symbol or symbols is required')
  }

  if (symbols.length === 0) return errorResp('No valid symbols provided')

  const target = symbols.join(',')
  const bypassCache = searchParams.get('refresh') === '1'

  const env = {
    POLYGON_API_KEY:     process.env.POLYGON_API_KEY     ?? '',
    FINNHUB_API_KEY:     process.env.FINNHUB_API_KEY     ?? '',
    EODHD_API_KEY:       process.env.EODHD_API_KEY       ?? '',
    APCA_API_KEY_ID:     process.env.APCA_API_KEY_ID     ?? '',
    APCA_API_SECRET_KEY: process.env.APCA_API_SECRET_KEY ?? '',
    TIINGO_API_KEY:      process.env.TIINGO_API_KEY      ?? '',
    TWELVEDATA_API_KEY:  process.env.TWELVEDATA_API_KEY  ?? '',
    LSE_API_KEY:           process.env.LSE_API_KEY               ?? '',
    EULERPOOL_API_KEY:     process.env.EULERPOOL_API_KEY         ?? '',
    AV_KEY_1:  process.env.AV_KEY_1 ?? '', AV_KEY_2:  process.env.AV_KEY_2 ?? '',
    AV_KEY_3:  process.env.AV_KEY_3 ?? '', AV_KEY_4:  process.env.AV_KEY_4 ?? '',
    AV_KEY_5:  process.env.AV_KEY_5 ?? '', AV_KEY_6:  process.env.AV_KEY_6 ?? '',
    AV_KEY_7:  process.env.AV_KEY_7 ?? '', AV_KEY_8:  process.env.AV_KEY_8 ?? '',
    AV_KEY_9:  process.env.AV_KEY_9 ?? '', AV_KEY_10: process.env.AV_KEY_10 ?? '',
  }

  const result = await execPython('scripts/quote.py', [target], env, {
    bypassCache,
    timeoutMs: 20_000,
  })

  if (!result.ok) {
    const isProd = process.env.NODE_ENV === 'production'
    return NextResponse.json(
      { success: false, error: isProd ? 'Failed to fetch quote data' : result.stderr },
      { status: 502, headers: { 'Cache-Control': 'no-store' } }
    )
  }

  const headers: HeadersInit = {
    'Cache-Control': result.cached
      ? 's-maxage=10, stale-while-revalidate=20'
      : 'no-store',
  }
  if (result.latencyMs != null) headers['X-Python-Latency-Ms'] = String(result.latencyMs)
  if (result.cached)             headers['X-Cache'] = 'HIT'

  return NextResponse.json({ success: true, data: result.data }, { headers })
}
