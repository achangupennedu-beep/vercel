import { NextRequest, NextResponse } from 'next/server'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const API_BASE = 'https://console.blueskyapi.com/catalog/api/'
const DATASET_PATH = /^\/[A-Za-z0-9._~:@/-]{1,180}$/
const SYMBOL = /^[A-Z0-9.^-]{1,20}$/
const ALLOWED = new Set(['health', 'datasets', 'categories', 'data'])
const MAX_TIMEOUT_MS = 8_000
const MAX_BODY_BYTES = 2_000_000

function response(status: number, body: Record<string, unknown>, headers?: HeadersInit) {
  return NextResponse.json(body, { status, headers: { 'Cache-Control': 'no-store', ...headers } })
}

function asFinite(value: unknown) {
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function timestampMs(value: unknown) {
  const number = asFinite(value)
  if (number !== null) return number < 10_000_000_000 ? number * 1_000 : number
  if (typeof value === 'string') {
    const parsed = Date.parse(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function freshness(data: unknown) {
  const candidates: unknown[] = []
  const collect = (value: any) => {
    if (!value || typeof value !== 'object') return
    for (const key of ['timestamp', 'time', 't', 'asOf', 'as_of', 'updatedAt', 'updated_at']) {
      if (value[key] !== undefined) candidates.push(value[key])
    }
    for (const key of ['data', 'result', 'results', 'quote', 'quotes']) {
      if (value[key] && typeof value[key] === 'object' && !Array.isArray(value[key])) collect(value[key])
    }
  }
  collect(data)
  const times = candidates.map(timestampMs).filter((value): value is number => value !== null)
  if (!times.length) return { delayed: null, ageMs: null, timestamp: null }
  const timestamp = Math.max(...times)
  const ageMs = Math.max(0, Date.now() - timestamp)
  return { delayed: ageMs > 60_000, ageMs, timestamp: new Date(timestamp).toISOString() }
}

function score(latencyMs: number, freshnessState: ReturnType<typeof freshness>, status: number) {
  if (status !== 200) return 0
  const latencyScore = Math.max(0, 1 - latencyMs / 2_000)
  const ageScore = freshnessState.ageMs == null ? 0.65 : Math.max(0, 1 - freshnessState.ageMs / 300_000)
  return Math.round((0.45 * latencyScore + 0.55 * ageScore) * 1000) / 1000
}

type Freshness = ReturnType<typeof freshness>

type UpstreamResult = {
  ok: boolean
  status: number
  latencyMs: number
  data: unknown
  error: string | null
  fresh: Freshness
}

async function upstream(path: string, params: URLSearchParams): Promise<UpstreamResult> {
  const url = new URL(path.replace(/^\/+/, ''), API_BASE)
  for (const [key, value] of params) {
    if (key !== 'key' && key !== 'endpoint' && key !== 'path') url.searchParams.set(key, value)
  }
  const started = performance.now()
  try {
    const result = await fetch(url, {
      method: 'GET',
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${process.env.VIANEXUS_API_KEY}`,
        'X-API-Key': process.env.VIANEXUS_API_KEY ?? '',
      },
      cache: 'no-store',
      signal: AbortSignal.timeout(MAX_TIMEOUT_MS),
    })
    const latencyMs = Math.round(performance.now() - started)
    const body = await result.text()
    if (body.length > MAX_BODY_BYTES) return { ok: false, status: 502, latencyMs, data: null, error: 'Response exceeded safety limit', fresh: freshness(null) }
    let data: unknown
    try { data = JSON.parse(body) } catch { data = { raw: body.slice(0, 4_000) } }
    const fresh = freshness(data)
    return { ok: result.ok, status: result.status, latencyMs, data, error: result.ok ? null : `viaNexus ${result.status}`, fresh }
  } catch (error) {
    return { ok: false, status: 504, latencyMs: Math.round(performance.now() - started), data: null, error: error instanceof Error ? error.message : 'viaNexus request failed', fresh: freshness(null) }
  }
}

export async function GET(request: NextRequest) {
  const key = process.env.VIANEXUS_API_KEY
  if (!key) return response(503, { success: false, error: 'VIANEXUS_API_KEY is not configured' })

  const params = request.nextUrl.searchParams
  const endpoint = (params.get('endpoint') ?? 'health').toLowerCase()
  if (!ALLOWED.has(endpoint)) return response(400, { success: false, error: 'Unsupported viaNexus endpoint' })

  if (endpoint === 'health') {
    const result = await upstream('/datasets-categories', params)
    return response(result.ok ? 200 : 502, {
      success: result.ok,
      data: result.ok ? result.data : null,
      provider: 'vianexus',
      reliability: { score: score(result.latencyMs, result.fresh, result.status), latencyMs: result.latencyMs, delayed: result.fresh.delayed, ageMs: result.fresh.ageMs },
      provenance: { live: true, apiBase: API_BASE, endpoint: '/datasets-categories' },
      error: result.error,
    }, { 'X-Provider-Latency-Ms': String(result.latencyMs) })
  }

  let path = endpoint === 'datasets' ? '/datasets' : endpoint === 'categories' ? '/datasets-categories' : ''
  if (endpoint === 'data') {
    const requestedPath = params.get('path') ?? ''
    if (!DATASET_PATH.test(requestedPath) || requestedPath.startsWith('//')) return response(400, { success: false, error: 'path must be an allowlisted viaNexus dataset path' })
    path = requestedPath
  }
  const symbol = params.get('symbol')?.trim().toUpperCase()
  if (symbol && !SYMBOL.test(symbol)) return response(400, { success: false, error: 'symbol contains invalid characters' })

  const result = await upstream(path, params)
  const reliability = score(result.latencyMs, result.fresh, result.status)
  return response(result.ok ? 200 : result.status >= 400 && result.status < 500 ? result.status : 502, {
    success: result.ok,
    data: result.ok ? result.data : null,
    provider: 'vianexus',
    reliability: { score: reliability, latencyMs: result.latencyMs, delayed: result.fresh.delayed, ageMs: result.fresh.ageMs, classification: result.fresh.delayed === true ? 'delayed' : 'real-time-or-unknown' },
    provenance: { live: true, apiBase: API_BASE, endpoint: path, symbol: symbol || undefined },
    error: result.error,
  }, { 'X-Provider-Latency-Ms': String(result.latencyMs), 'X-Provider-Reliability': String(reliability) })
}

export async function HEAD() {
  return new NextResponse(null, { status: process.env.VIANEXUS_API_KEY ? 200 : 503, headers: { 'Cache-Control': 'no-store' } })
}

// viaNexus documentation: https://console.blueskyapi.com/docs/core
// Dataset discovery uses /platform/datasets and /platform/datasets-categories.
// Dataset reads use /data/<dataset-path>; credentials remain server-side.
// The response includes latency/freshness metadata so callers can choose a primary
// or secondary source without treating an unavailable or delayed feed as live.
