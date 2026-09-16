import { NextRequest } from 'next/server'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const BASE_URL = 'https://api.unusualwhales.com'
const TICKER_RE = /^[A-Z][A-Z0-9.-]{0,7}$/
const MAX_SECONDS = 120
const POLL_MS = 2_000

export async function GET(request: NextRequest) {
  const key = process.env.UNUSUAL_WHALES_API_KEY?.trim()
  if (!key) return Response.json({ error: 'UNUSUAL_WHALES_API_KEY is not configured' }, { status: 503 })

  const ticker = (request.nextUrl.searchParams.get('ticker') ?? '').trim().toUpperCase()
  if (!TICKER_RE.test(ticker)) return Response.json({ error: 'A valid ticker is required' }, { status: 400 })
  const seconds = Math.min(MAX_SECONDS, Math.max(5, Number(request.nextUrl.searchParams.get('seconds') ?? 30) || 30))
  const encoder = new TextEncoder()
  const stream = new ReadableStream({
    async start(controller) {
      const send = (event: string, data: unknown) => controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`))
      const started = Date.now()
      let cursor = ''
      let count = 0
      send('connected', { ticker, seconds, pollMs: POLL_MS, source: 'unusual_whales' })
      try {
        while (Date.now() - started < seconds * 1000) {
          const url = new URL(`${BASE_URL}/api/stock/${ticker}/flow-recent`)
          url.searchParams.set('limit', '100')
          if (cursor) url.searchParams.set('newer_than', cursor)
          const response = await fetch(url, { headers: { Authorization: `Bearer ${key}`, Accept: 'application/json' }, cache: 'no-store', signal: AbortSignal.timeout(10_000) })
          if (!response.ok) {
            const text = await response.text()
            send('error', { status: response.status, detail: text.slice(0, 500) })
            break
          }
          const payload = await response.json()
          const rows = Array.isArray(payload) ? payload : (payload.data ?? payload.results ?? payload.rows ?? [])
          if (Array.isArray(rows)) {
            for (const row of rows.reverse()) {
              const id = String(row.id ?? row.trade_id ?? row.timestamp ?? '')
              if (id && id !== cursor) { send('flow', row); count++ }
              if (id) cursor = id
            }
          }
          await new Promise((resolve) => setTimeout(resolve, POLL_MS))
        }
        send('done', { ticker, count, elapsedMs: Date.now() - started })
      } catch (error) {
        send('error', { error: error instanceof Error ? error.message : String(error) })
      } finally { controller.close() }
    },
  })
  return new Response(stream, { headers: { 'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-cache, no-transform', Connection: 'keep-alive', 'X-Accel-Buffering': 'no' } })
}
