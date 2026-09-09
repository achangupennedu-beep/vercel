/**
 * /api/cron/warm-quotes — scheduled job that refreshes real Alpaca quotes
 * and stores them in Redis so on-demand requests hit a warm, shared cache
 * instead of spawning a Python fetch on every request.
 *
 * Triggered by Vercel Cron (see vercel.json). Vercel automatically sends
 * `Authorization: Bearer <CRON_SECRET>` for cron-invoked requests when
 * CRON_SECRET is set on the project — this route verifies that header.
 * The secret is never placed in a URL query string, so it can't leak into
 * git history or access logs.
 */
import { NextRequest, NextResponse } from 'next/server'
import { execPython } from '@/lib/exec-python'
import { setCachedQuotesBulk } from '@/lib/redis'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

// Default watchlist kept in sync with components/dashboard.tsx DEFAULT_SYMBOLS
const WATCHLIST = ['AAPL', 'TSLA', 'NVDA', 'SPY', 'QQQ', 'MSFT', 'AMZN', 'META']

const TTL_MARKET_OPEN_SECONDS = 90    // quotes move fast — short TTL while trading
const TTL_MARKET_CLOSED_SECONDS = 1800 // last price barely changes after hours

function unauthorized() {
  return NextResponse.json({ success: false, error: 'Unauthorized' }, { status: 401 })
}

function isAuthorized(req: NextRequest): boolean {
  const secret = process.env.CRON_SECRET
  if (!secret) return false // fail closed if not configured
  const auth = req.headers.get('authorization') ?? ''
  return auth === `Bearer ${secret}`
}

async function handle(req: NextRequest) {
  if (!isAuthorized(req)) return unauthorized()

  const env = {
    APCA_API_KEY_ID: process.env.APCA_API_KEY_ID ?? '',
    APCA_API_SECRET_KEY: process.env.APCA_API_SECRET_KEY ?? '',
  }

  // Real market-status check — determines TTL, not whether we fetch.
  const statusResult = await execPython('scripts/alpaca_market.py', ['market_status'], env, {
    bypassCache: true,
    timeoutMs: 8_000,
  })
  const isOpen = Boolean(statusResult.ok && statusResult.data?.is_open)

  // Bypass the in-memory TTL cache so we get genuinely fresh data from Alpaca.
  const result = await execPython('scripts/quote.py', [WATCHLIST.join(',')], env, {
    bypassCache: true,
    timeoutMs: 20_000,
  })

  if (!result.ok || !result.data) {
    return NextResponse.json(
      { success: false, error: result.stderr || 'quote fetch failed', warmed: 0 },
      { status: 502 }
    )
  }

  const ttl = isOpen ? TTL_MARKET_OPEN_SECONDS : TTL_MARKET_CLOSED_SECONDS
  const warmed = await setCachedQuotesBulk(result.data, ttl, 'cron')

  return NextResponse.json({
    success: true,
    warmed,
    marketOpen: isOpen,
    ttlSeconds: ttl,
    symbols: WATCHLIST,
    timestamp: new Date().toISOString(),
  })
}

export async function GET(req: NextRequest) {
  return handle(req)
}

export async function POST(req: NextRequest) {
  return handle(req)
}
