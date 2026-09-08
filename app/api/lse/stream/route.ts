/**
 * /api/lse/stream — London Strategic Edge live tick stream over SSE
 *
 * Streams live price ticks for one or more symbols via Server-Sent Events.
 * Internally calls `scripts/lse_source.py stream` which either uses the
 * lse-data SDK websocket (if installed) or falls back to REST polling.
 *
 * Query params:
 *   symbols  (required) — comma-separated, e.g. "AAPL,SPY,BTC/USD"
 *   dur      (optional) — stream duration in seconds, default 30, max 120
 *
 * SSE event format:
 *   data: {"symbol":"AAPL","price":193.4,"bid":193.38,"ask":193.41,"volume":12340,"timestamp":"...","source":"lse_ws"}
 *
 * Usage (browser):
 *   const es = new EventSource('/api/lse/stream?symbols=AAPL,SPY')
 *   es.onmessage = (e) => { const tick = JSON.parse(e.data); ... }
 */
import { NextRequest } from 'next/server'
import { execPython }  from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const SYM_RE    = /^[A-Z0-9./^-]{1,16}$/
const MAX_SYMS  = 10
const MAX_DUR   = 120
const DEF_DUR   = 30

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)

  const rawSyms = (searchParams.get('symbols') ?? searchParams.get('symbol') ?? 'AAPL')
    .split(',')
    .map(s => s.trim().toUpperCase())
    .filter(s => SYM_RE.test(s))
    .slice(0, MAX_SYMS)

  if (rawSyms.length === 0) {
    return new Response('No valid symbols provided', { status: 400 })
  }

  const dur = Math.min(
    MAX_DUR,
    Math.max(1, parseInt(searchParams.get('dur') ?? String(DEF_DUR), 10) || DEF_DUR)
  )

  const env = {
    LSE_API_KEY: process.env.LSE_API_KEY ?? '',
  }

  // SSE response — stream ticks as they arrive from the Python subprocess.
  // The Python script emits one NDJSON line per tick; we relay each as an SSE event.
  const encoder = new TextEncoder()

  const stream = new ReadableStream({
    async start(controller) {
      const send = (event: string, data: string) => {
        controller.enqueue(encoder.encode(`event: ${event}\ndata: ${data}\n\n`))
      }

      send('connected', JSON.stringify({ symbols: rawSyms, dur, ts: Date.now() }))

      try {
        const args  = ['stream', ...rawSyms, `--dur=${dur}`]
        const result = await execPython('scripts/lse_source.py', args, env, {
          timeoutMs: (dur + 5) * 1000,
          bypassCache: true,
        })

        // The stream script emits NDJSON — parse each line as a tick event.
        // execPython returns the full stdout as result.data (string or parsed JSON).
        const raw = typeof result.data === 'string'
          ? result.data
          : JSON.stringify(result.data ?? '')

        const lines = raw.split('\n').filter(Boolean)
        let tickCount = 0

        for (const line of lines) {
          try {
            const tick = JSON.parse(line)
            if (tick && typeof tick === 'object') {
              send('tick', JSON.stringify(tick))
              tickCount++
            }
          } catch { /* skip malformed lines */ }
        }

        send('done', JSON.stringify({ tickCount, dur, symbols: rawSyms }))
      } catch (err: any) {
        send('error', JSON.stringify({ error: String(err?.message ?? err) }))
      } finally {
        controller.close()
      }
    },
  })

  return new Response(stream, {
    headers: {
      'Content-Type':                'text/event-stream; charset=utf-8',
      'Cache-Control':               'no-cache, no-transform',
      'Connection':                  'keep-alive',
      'X-Accel-Buffering':           'no',
      'Access-Control-Allow-Origin': '*',
    },
  })
}
