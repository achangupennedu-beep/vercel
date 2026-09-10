import { NextRequest } from 'next/server'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const BASE_URL = 'https://api.unusualwhales.com'
const ALLOWED_PREFIXES = [
  '/api/option-trades',
  '/api/option-activity',
  '/api/stock/',
  '/api/market/',
  '/api/darkpool/',
  '/api/lit-flow/',
  '/api/option-contract/',
  '/api/insider/',
  '/api/congress/',
  '/api/volatility/',
  '/api/options-pulse/',
  '/api/net-flow/',
  '/api/news/',
]

function isAllowed(path: string) {
  return ALLOWED_PREFIXES.some((prefix) => path === prefix || path.startsWith(prefix))
}

export async function GET(request: NextRequest, context: { params: Promise<{ path?: string[] }> }) {
  const key = process.env.UNUSUAL_WHALES_API_KEY?.trim()
  if (!key) return Response.json({ error: 'UNUSUAL_WHALES_API_KEY is not configured' }, { status: 503 })

  const { path: segments = [] } = await context.params
  const rawPath = `/${segments.join('/')}`
  const path = rawPath === '/' ? '/' : rawPath.startsWith('/api/') ? rawPath : `/api${rawPath}`
  if (!isAllowed(path)) return Response.json({ error: 'Endpoint is not enabled by this proxy' }, { status: 404 })

  const upstream = new URL(`${BASE_URL}${path}`)
  request.nextUrl.searchParams.forEach((value, name) => upstream.searchParams.set(name, value))

  const response = await fetch(upstream, {
    headers: { Authorization: `Bearer ${key}`, Accept: 'application/json' },
    cache: 'no-store',
    signal: AbortSignal.timeout(15_000),
  })
  const body = await response.arrayBuffer()
  return new Response(body, {
    status: response.status,
    headers: {
      'Content-Type': response.headers.get('content-type') ?? 'application/json',
      'Cache-Control': 'no-store, max-age=0',
      'X-Upstream-Request-Id': response.headers.get('x-request-id') ?? '',
    },
  })
}

export const HEAD = GET
