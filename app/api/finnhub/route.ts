import { NextRequest, NextResponse } from 'next/server'
import { execPython } from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const SYM_RE = /^[A-Z0-9.^-]{1,12}$/
const ENDPOINT_RE = /^[a-z_]{2,40}$/
const BIDASK_ENDPOINTS = new Set(['bidask', 'bid_ask'])

const SHARED_ENV = {
  FINNHUB_API_KEY: 'd8tbcp9r01qhcnk1ft60d8tbcp9r01qhcnk1ft6g',
  TWELVEDATA_API_KEY: 'db352fe2024b489b9c57ea36297aa217',
  MASSIVE_API_KEY: 'Ns0BKHdMyS7tNaAQ_RREHtCpJ1x49FNi',
  OPENFIGI_KEY: '2052d5d0-cd5d-4863-83fc-083e56e68663',
  APCA_API_KEY_ID: process.env.APCA_API_KEY_ID ?? '',
  APCA_API_SECRET_KEY: process.env.APCA_API_SECRET_KEY ?? '',
  INSIGHTSENTRY_KEY: process.env.INSIGHTSENTRY_KEY ?? '',
  RAPIDAPI_ACCESS_TOKEN: process.env.RAPIDAPI_ACCESS_TOKEN ?? '',
  AV_KEY_1: 'FUKEKMUEN8GIC82A', AV_KEY_2: 'CYBWW8VF831209WH',
  AV_KEY_3: 'H58YGLP8WN0V8OXS', AV_KEY_4: 'U3XMEDPQGL1POIAH',
  AV_KEY_5: 'ELEXFQA94KKGL0OI', AV_KEY_6: '9FRSHRAZCWHI7IHV',
  AV_KEY_7: 'UFOY6OS1TKTPN1K5', AV_KEY_8: 'L5Z0LJA84D07FB60',
  AV_KEY_9: 'NYD9SXABZ0D87JR3', AV_KEY_10: '2L7M89R071KQVT9N',
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)
  const symbol = searchParams.get('symbol')?.trim().toUpperCase()
  const endpoint = searchParams.get('endpoint')?.trim().toLowerCase() ?? 'quote'

  if (!symbol || !SYM_RE.test(symbol)) return NextResponse.json({ error: 'Invalid symbol' }, { status: 400 })
  if (!ENDPOINT_RE.test(endpoint)) return NextResponse.json({ error: 'Invalid endpoint' }, { status: 400 })

  const result = await execPython('scripts/finnhub_fetch.py', [symbol, endpoint], SHARED_ENV)
  if (!result.ok) return NextResponse.json({ error: result.stderr }, { status: 502 })

  return NextResponse.json({
    success: true,
    data: result.data,
    provenance: {
      source: 'finnhub',
      endpoint: BIDASK_ENDPOINTS.has(endpoint) ? '/stock/bidask' : `/api/v1/${endpoint}`,
      realtime: BIDASK_ENDPOINTS.has(endpoint),
      freshness: BIDASK_ENDPOINTS.has(endpoint) ? 'real-time-US' : 'endpoint-dependent',
    },
  }, {
    headers: {
      'Cache-Control': BIDASK_ENDPOINTS.has(endpoint) ? 's-maxage=1, stale-while-revalidate=2' : 's-maxage=15, stale-while-revalidate=30',
      'X-Source': 'finnhub',
      'X-Finnhub-Endpoint': BIDASK_ENDPOINTS.has(endpoint) ? '/stock/bidask' : endpoint,
    },
  })
}
