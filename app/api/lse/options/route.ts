/**
 * /api/lse/options — London Strategic Edge live options chain
 *
 * Returns live options contracts with IV, greeks, bid/ask, OI, volume.
 *
 * Query params:
 *   symbol   (required) — underlying, e.g. "AAPL"
 *   max_dte  (optional) — max days-to-expiration, default 90
 *   type     (optional) — "call" | "put", default both
 */
import { NextRequest, NextResponse } from 'next/server'
import { execPython }                from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const SYM_RE = /^[A-Z0-9.^-]{1,12}$/

function err(msg: string, status = 400) {
  return NextResponse.json({ success: false, error: msg }, { status })
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)
  const sym = searchParams.get('symbol')?.trim().toUpperCase()
  if (!sym)               return err('symbol is required')
  if (!SYM_RE.test(sym))  return err('symbol contains invalid characters')

  const maxDte  = searchParams.get('max_dte') ?? '90'
  const optType = searchParams.get('type') ?? ''
  const bypass  = searchParams.get('refresh') === '1'

  const args: string[] = ['options', sym, maxDte]
  if (optType) args.push(optType)

  const env = { LSE_API_KEY: process.env.LSE_API_KEY ?? 'lse_live_8960fdf1f1af3ab76db92734aaaca159' }

  const result = await execPython('scripts/lse_source.py', args, env, {
    bypassCache: bypass,
    timeoutMs: 15_000,
  })

  if (!result.ok) {
    return NextResponse.json(
      { success: false, error: process.env.NODE_ENV === 'production'
          ? 'Failed to fetch LSE options'
          : result.stderr },
      { status: 502, headers: { 'Cache-Control': 'no-store' } }
    )
  }

  return NextResponse.json(
    { success: true, data: result.data },
    {
      headers: {
        'Cache-Control': result.cached ? 's-maxage=30, stale-while-revalidate=60' : 'no-store',
        ...(result.latencyMs != null ? { 'X-Python-Latency-Ms': String(result.latencyMs) } : {}),
      },
    }
  )
}
