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
    EODHD_API_KEY:       process.env.EODHD_API_KEY       ?? '6a3ac9d808bda9.37141543',
    APCA_API_KEY_ID:     process.env.APCA_API_KEY_ID     ?? 'PKUJ3JTPEIFN5KY2CMCCYSBG25',
    APCA_API_SECRET_KEY: process.env.APCA_API_SECRET_KEY ?? 'GepZj2TWF386pTxHJfMWDgfnUZ7ykvor7svvo8K9nxwY',
    TIINGO_API_KEY:      process.env.TIINGO_API_KEY      ?? '641295bf53a9841702e86b0bae7a15cd5bd6adf9',
    TWELVEDATA_API_KEY:  process.env.TWELVEDATA_API_KEY  ?? '',
    LSE_API_KEY:           process.env.LSE_API_KEY               ?? 'lse_live_8960fdf1f1af3ab76db92734aaaca159',
    EULERPOOL_API_KEY:     process.env.EULERPOOL_API_KEY         ?? 'eu_prod_1782933237805_jp4xbr2ag5c',
    AV_KEY_1:  'FUKEKMUEN8GIC82A', AV_KEY_2:  'CYBWW8VF831209WH',
    AV_KEY_3:  'H58YGLP8WN0V8OXS', AV_KEY_4:  'U3XMEDPQGL1POIAH',
    AV_KEY_5:  'ELEXFQA94KKGL0OI', AV_KEY_6:  '9FRSHRAZCWHI7IHV',
    AV_KEY_7:  'UFOY6OS1TKTPN1K5', AV_KEY_8:  'L5Z0LJA84D07FB60',
    AV_KEY_9:  'NYD9SXABZ0D87JR3', AV_KEY_10: '2L7M89R071KQVT9N',
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
