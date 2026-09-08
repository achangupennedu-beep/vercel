import { NextRequest, NextResponse } from 'next/server'
import { execPython } from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const SYM_RE       = /^[A-Z0-9.^-]{1,12}$/
const VALID_INTERVALS = new Set(['1m','5m','15m','30m','1h','1d','1wk','1mo'])
const VALID_RANGES    = new Set(['1d','5d','1mo','3mo','6mo','1y','2y','5y','10y','ytd','max'])

function errorResp(msg: string, status = 400) {
  return NextResponse.json({ success: false, error: msg }, { status })
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)

  const rawSym   = searchParams.get('symbol')?.trim().toUpperCase()
  const interval = searchParams.get('interval') ?? '1d'
  const range    = searchParams.get('range') ?? searchParams.get('period') ?? '1y'

  if (!rawSym)              return errorResp('symbol is required')
  if (!SYM_RE.test(rawSym)) return errorResp('symbol contains invalid characters')
  if (!VALID_INTERVALS.has(interval)) return errorResp(`interval must be one of: ${[...VALID_INTERVALS].join(', ')}`)
  if (!VALID_RANGES.has(range))       return errorResp(`range must be one of: ${[...VALID_RANGES].join(', ')}`)

  const bypassCache = searchParams.get('refresh') === '1'

  const env = {
    APCA_API_KEY_ID:     process.env.APCA_API_KEY_ID     ?? 'PKUJ3JTPEIFN5KY2CMCCYSBG25',
    APCA_API_SECRET_KEY: process.env.APCA_API_SECRET_KEY ?? 'GepZj2TWF386pTxHJfMWDgfnUZ7ykvor7svvo8K9nxwY',
    POLYGON_API_KEY:     process.env.POLYGON_API_KEY     ?? '',
    EODHD_API_KEY:       process.env.EODHD_API_KEY       ?? '6a3ac9d808bda9.37141543',
  }

  const result = await execPython('scripts/history.py', [rawSym, interval, range], env, {
    bypassCache,
    timeoutMs: 25_000,
  })

  if (!result.ok) {
    const isProd = process.env.NODE_ENV === 'production'
    return NextResponse.json(
      { success: false, error: isProd ? 'Failed to fetch historical data' : result.stderr },
      { status: 502, headers: { 'Cache-Control': 'no-store' } }
    )
  }

  const headers: HeadersInit = {
    'Cache-Control': result.cached
      ? 's-maxage=3600, stale-while-revalidate=7200'
      : 'no-store',
  }
  if (result.latencyMs != null) headers['X-Python-Latency-Ms'] = String(result.latencyMs)
  if (result.cached)             headers['X-Cache'] = 'HIT'

  return NextResponse.json(
    {
      success: true,
      data:    result.data.bars,
      meta: {
        source:  result.data.source,
        count:   result.data.count,
        symbol:  rawSym,
        interval,
        range,
      },
    },
    { headers }
  )
}
