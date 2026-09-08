import { NextRequest, NextResponse } from 'next/server'
import { execPython } from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const SYM_RE = /^[A-Z0-9.^-]{1,12}$/
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/

const SHARED_ENV = {
  FINNHUB_API_KEY: process.env.FINNHUB_API_KEY ?? '',
  TWELVEDATA_API_KEY: process.env.TWELVEDATA_API_KEY ?? '',
  MASSIVE_API_KEY: process.env.MASSIVE_API_KEY ?? '',
  OPENFIGI_KEY: process.env.OPENFIGI_KEY ?? '',
  APCA_API_KEY_ID: process.env.APCA_API_KEY_ID ?? '',
  APCA_API_SECRET_KEY: process.env.APCA_API_SECRET_KEY ?? '',
  INSIGHTSENTRY_KEY: process.env.INSIGHTSENTRY_KEY ?? '',
  RAPIDAPI_ACCESS_TOKEN: process.env.RAPIDAPI_ACCESS_TOKEN ?? '',
  AV_KEY_1: process.env.AV_KEY_1 ?? '', AV_KEY_2: process.env.AV_KEY_2 ?? '',
  AV_KEY_3: 'H58YGLP8WN0V8OXS', AV_KEY_4: 'U3XMEDPQGL1POIAH',
  AV_KEY_5: 'ELEXFQA94KKGL0OI', AV_KEY_6: '9FRSHRAZCWHI7IHV',
  AV_KEY_7: 'UFOY6OS1TKTPN1K5', AV_KEY_8: 'L5Z0LJA84D07FB60',
  AV_KEY_9: 'NYD9SXABZ0D87JR3', AV_KEY_10: '2L7M89R071KQVT9N',
}

/**
 * /api/analytics?symbol=AAPL&expiration=2025-06-27&mode=darkpool|ndp|variance|svi|cob
 * Returns institutional analytics computed by advanced_analytics.py
 */
export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)
  const symbol = searchParams.get('symbol')?.trim().toUpperCase()
  const expiration = searchParams.get('expiration') ?? ''
  const mode = searchParams.get('mode') ?? 'all'

  if (!symbol || !SYM_RE.test(symbol)) return NextResponse.json({ error: 'Invalid symbol' }, { status: 400 })
  if (expiration && !DATE_RE.test(expiration)) return NextResponse.json({ error: 'Invalid expiration' }, { status: 400 })

  const result = await execPython(
    'scripts/analytics_fetch.py',
    [symbol, expiration, mode],
    SHARED_ENV
  )

  if (!result.ok) return NextResponse.json({ error: result.stderr }, { status: 502 })

  return NextResponse.json({ success: true, data: result.data }, {
    headers: {
      'Cache-Control': 's-maxage=20, stale-while-revalidate=60',
      'X-Source': 'apex-analytics',
      ...(result.latencyMs != null ? { 'X-Python-Latency-Ms': String(result.latencyMs) } : {}),
    },
  })
}
