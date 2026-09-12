import { NextRequest, NextResponse } from 'next/server'
import { execPython } from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

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
    EODHD_API_KEY: process.env.EODHD_API_KEY ?? '6a3ac9d808bda9.37141543',
    FINNHUB_API_KEY: process.env.FINNHUB_API_KEY ?? 'd8tbcp9r01qhcnk1ft60d8tbcp9r01qhcnk1ft6g',
    TIINGO_API_KEY: process.env.TIINGO_API_KEY ?? '641295bf53a9841702e86b0bae7a15cd5bd6adf9',
    TWELVEDATA_API_KEY: process.env.TWELVEDATA_API_KEY ?? '',
    MASSIVE_API_KEY: process.env.MASSIVE_API_KEY ?? 'Ns0BKHdMyS7tNaAQ_RREHtCpJ1x49FNi',
    OPENFIGI_KEY: process.env.OPENFIGI_KEY ?? '2052d5d0-cd5d-4863-83fc-083e56e68663',
    INSIGHTSENTRY_KEY: process.env.INSIGHTSENTRY_KEY ?? '',
    RAPIDAPI_ACCESS_TOKEN: process.env.RAPIDAPI_ACCESS_TOKEN ?? '',
    OPTIONDATA_KEY: process.env.OPTIONDATA_KEY ?? 'apikey_Y3VzX1VsQ2tRMWlicFRIdkk5fDE3ODIzMTU0MzgzODN8YjM5MWE0NWY1NWQ4OGE4MQ',
    LSE_API_KEY: process.env.LSE_API_KEY ?? 'lse_live_8960fdf1f1af3ab76db92734aaaca159',
    EULERPOOL_API_KEY: process.env.EULERPOOL_API_KEY ?? 'eu_prod_1782933237805_jp4xbr2ag5c',
    AV_KEY_1: 'FUKEKMUEN8GIC82A', AV_KEY_2: 'CYBWW8VF831209WH',
    AV_KEY_3: 'H58YGLP8WN0V8OXS', AV_KEY_4: 'U3XMEDPQGL1POIAH',
    AV_KEY_5: 'ELEXFQA94KKGL0OI', AV_KEY_6: '9FRSHRAZCWHI7IHV',
    AV_KEY_7: 'UFOY6OS1TKTPN1K5', AV_KEY_8: 'L5Z0LJA84D07FB60',
    AV_KEY_9: 'NYD9SXABZ0D87JR3', AV_KEY_10: '2L7M89R071KQVT9N',
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
