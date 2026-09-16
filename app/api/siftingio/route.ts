import { NextRequest, NextResponse } from 'next/server'

const BASE_URL = 'https://api.sifting.io/v1'
const ENDPOINTS = new Set([
  'search', 'last-trade', 'snapshot', 'bars', 'forex-bars', 'crypto-bars',
  'filings', 'filing', 'filing-text', 'risk-factors-diff', 'financials',
  'concept', 'xbrl', 'news', 'pool',
])

function symbol(value: string | null) {
  const result = String(value ?? '').trim().toUpperCase()
  return /^[A-Z0-9_.:/-]{1,40}$/.test(result) ? result : ''
}

function date(value: string | null) {
  const result = String(value ?? '').trim()
  return /^\d{4}-\d{2}-\d{2}(?:T[^ ]+)?$/.test(result) ? result : ''
}

function add(url: URL, params: URLSearchParams, ...keys: string[]) {
  for (const key of keys) {
    const value = params.get(key)
    if (value) url.searchParams.set(key, value)
  }
}

export async function GET(request: NextRequest) {
  const key = process.env.SIFTINGIO_API_KEY
  if (!key) return NextResponse.json({ success: false, error: 'SIFTINGIO_API_KEY is not configured' }, { status: 503 })

  const params = request.nextUrl.searchParams
  const endpoint = params.get('endpoint') ?? 'snapshot'
  const venue = (params.get('venue') ?? 'stocks').toLowerCase()
  const ticker = symbol(params.get('symbol') ?? params.get('ticker'))
  if (!ENDPOINTS.has(endpoint)) return NextResponse.json({ success: false, error: 'Unsupported SiftingIO endpoint' }, { status: 400 })

  let path: string
  if (endpoint === 'search') path = '/fnd/stocks/search'
  else if (endpoint === 'snapshot') path = `/snapshot/${venue}`
  else if (endpoint === 'last-trade') path = `/last/trade/${venue}/${ticker}`
  else if (endpoint === 'bars' || endpoint === 'forex-bars' || endpoint === 'crypto-bars') {
    if (!ticker) return NextResponse.json({ success: false, error: 'A valid symbol is required' }, { status: 400 })
    const market = endpoint === 'bars' ? 'stocks' : endpoint === 'forex-bars' ? 'forex' : 'crypto'
    path = `/hist/${market}/${ticker}/bars`
  } else {
    if (!ticker) return NextResponse.json({ success: false, error: 'A valid ticker is required' }, { status: 400 })
    path = endpoint === 'filings' ? `/fnd/stocks/${ticker}/filings`
      : endpoint === 'financials' ? `/fnd/stocks/${ticker}/financials`
      : endpoint === 'concept' ? `/fnd/stocks/${ticker}/financials/${encodeURIComponent(params.get('concept') ?? '')}`
      : endpoint === 'risk-factors-diff' ? `/fnd/stocks/${ticker}/risk-factors-diff`
      : `/fnd/stocks/${ticker}/${endpoint}`
  }

  const upstream = new URL(`${BASE_URL}${path}`)
  add(upstream, params, 'q', 'limit', 'start', 'end', 'interval', 'symbols', 'cursor', 'form', 'year', 'period', 'concept')
  if (upstream.searchParams.has('start') && !date(upstream.searchParams.get('start'))) upstream.searchParams.delete('start')
  if (upstream.searchParams.has('end') && !date(upstream.searchParams.get('end'))) upstream.searchParams.delete('end')

  try {
    const response = await fetch(upstream, {
      headers: { 'X-API-Key': key, Accept: 'application/json', 'Accept-Encoding': 'gzip' },
      cache: 'no-store',
      signal: AbortSignal.timeout(15_000),
    })
    const body = await response.text()
    let data: unknown
    try { data = JSON.parse(body) } catch { data = { raw: body.slice(0, 2000) } }
    return NextResponse.json({
      success: response.ok,
      data: response.ok ? data : null,
      error: response.ok ? undefined : `SiftingIO ${response.status}`,
      provenance: { source: 'siftingio', endpoint: path, live: true, venue, symbol: ticker || undefined },
    }, { status: response.ok ? 200 : response.status, headers: { 'Cache-Control': endpoint === 'snapshot' || endpoint === 'last-trade' ? 's-maxage=1, stale-while-revalidate=2' : 's-maxage=15, stale-while-revalidate=60' } })
  } catch (error) {
    return NextResponse.json({ success: false, error: error instanceof Error ? error.message : 'SiftingIO request failed', provenance: { source: 'siftingio', endpoint: path, live: true } }, { status: 502 })
  }
}

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'
