import { NextRequest, NextResponse } from 'next/server'
import { execPython } from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const SYM_RE = /^[A-Z0-9.^/-]{1,16}$/

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)
  const symbol = searchParams.get('symbol')?.trim().toUpperCase()
  if (!symbol || !SYM_RE.test(symbol)) {
    return NextResponse.json({ success: false, error: 'valid symbol is required' }, { status: 400 })
  }

  const minPremiumRaw = Number(searchParams.get('min_premium') ?? '0')
  const minPremium = Number.isFinite(minPremiumRaw) && minPremiumRaw >= 0 ? Math.floor(minPremiumRaw) : 0
  const limitRaw = Number(searchParams.get('limit') ?? '200')
  const limit = Number.isFinite(limitRaw) ? Math.min(5000, Math.max(1, Math.floor(limitRaw))) : 200
  const key = process.env.LSE_API_KEY?.trim()
  if (!key) return NextResponse.json({ success: false, error: 'LSE_API_KEY is not configured' }, { status: 503 })

  const result = await execPython('scripts/lse_source.py', ['flow', symbol, String(minPremium), String(limit)], { LSE_API_KEY: key }, {
    timeoutMs: 15_000,
    bypassCache: searchParams.get('refresh') === '1',
  })
  if (!result.ok) return NextResponse.json({ success: false, error: result.stderr || 'Failed to fetch LSE flow' }, { status: 502 })

  return NextResponse.json({ success: true, data: result.data }, {
    headers: {
      'Cache-Control': result.cached ? 's-maxage=5, stale-while-revalidate=10' : 'no-store',
      ...(result.latencyMs != null ? { 'X-Python-Latency-Ms': String(result.latencyMs) } : {}),
    },
  })
}
