import { NextRequest, NextResponse } from 'next/server'
import { execPython } from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

function errorResp(msg: string, status = 400) {
  return NextResponse.json({ success: false, error: msg }, { status })
}

export async function POST(req: NextRequest) {
  let body: Record<string, unknown>
  try {
    body = await req.json()
  } catch {
    return errorResp('Invalid JSON body')
  }

  const mode = String(body.mode ?? 'price')
  const allowedModes = ['price','gex','flow','calibrate','surface','term','borrow','montecarlo','greeks','research_audit','rqmc_audit','illiqar','spectrum_sync','roughness_audit','edge_audit','informed_flow','sentiment_regime','expiry_pressure','diversification_cost','debt_beta','hawkes_clock','cvar_threshold','option_implied_crash_index','calendar_factor_overlay','ambiguity_adjusted_option_signal','marginal_diversification_cost_multifactor','jump_leverage_premium','rate_insurance','option_liquidity_crash','hawkes_markov_quote']
  if (!allowedModes.includes(mode)) return errorResp(`Unknown mode: ${mode}`)

  // Basic numeric validation for price/greeks modes
  if (['price','greeks','montecarlo'].includes(mode)) {
    const S = Number(body.S); const K = Number(body.K)
    if (!isFinite(S) || S <= 0) return errorResp('S must be a positive number')
    if (!isFinite(K) || K <= 0) return errorResp('K must be a positive number')
    const T = Number(body.T)
    if (!isFinite(T) || T < 0 || T > 36500) return errorResp('T (days) must be 0–36500')
  }

  const payload = JSON.stringify({ ...body, mode: undefined })

  const result = await execPython('scripts/pricing_models.py', [mode, payload], {
    POLYGON_API_KEY:    process.env.POLYGON_API_KEY    ?? '',
    MARKETDATA_API_KEY: process.env.MARKETDATA_API_KEY ?? '',
  }, { bypassCache: true })

  if (!result.ok) {
    const isProd = process.env.NODE_ENV === 'production'
    return NextResponse.json(
      { success: false, error: isProd ? 'Pricing engine error' : result.stderr },
      { status: 502, headers: { 'Cache-Control': 'no-store' } }
    )
  }

  return NextResponse.json({ success: true, data: result.data }, {
    headers: {
      'Cache-Control': 'no-store',
      ...(result.latencyMs != null ? { 'X-Python-Latency-Ms': String(result.latencyMs) } : {}),
    },
  })
}
