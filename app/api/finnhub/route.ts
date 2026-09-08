import { NextRequest, NextResponse } from 'next/server'
import { execPython } from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const SYM_RE = /^[A-Z0-9.^-]{1,12}$/
const ENDPOINT_RE = /^[a-z_]{2,40}$/

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
  AV_KEY_3: process.env.AV_KEY_3 ?? '', AV_KEY_4: process.env.AV_KEY_4 ?? '',
  AV_KEY_5: process.env.AV_KEY_5 ?? '', AV_KEY_6: process.env.AV_KEY_6 ?? '',
  AV_KEY_7: process.env.AV_KEY_7 ?? '', AV_KEY_8: process.env.AV_KEY_8 ?? '',
  AV_KEY_9: process.env.AV_KEY_9 ?? '', AV_KEY_10: process.env.AV_KEY_10 ?? '',
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)
  const symbol = searchParams.get('symbol')?.trim().toUpperCase()
  const endpoint = searchParams.get('endpoint')?.trim().toLowerCase() ?? 'quote'

  if (!symbol || !SYM_RE.test(symbol)) return NextResponse.json({ error: 'Invalid symbol' }, { status: 400 })
  if (!ENDPOINT_RE.test(endpoint)) return NextResponse.json({ error: 'Invalid endpoint' }, { status: 400 })

  const result = await execPython('scripts/finnhub_fetch.py', [symbol, endpoint], SHARED_ENV)
  if (!result.ok) return NextResponse.json({ error: result.stderr }, { status: 502 })

  return NextResponse.json({ success: true, data: result.data }, {
    headers: { 'Cache-Control': 's-maxage=15, stale-while-revalidate=30', 'X-Source': 'finnhub' },
  })
}
