import { Redis } from '@upstash/redis'

/**
 * Shared Upstash Redis client — used as a cross-instance warm cache for real
 * market-data quotes sitting in front of the slower Python data fetchers.
 *
 * The Vercel Upstash integration exposes the same Redis instance under two
 * naming conventions depending on how it was provisioned; accept either.
 */
const REDIS_URL =
  process.env.KV_REST_API_URL ?? process.env.UPSTASH_REDIS_REST_URL ?? ''
const REDIS_TOKEN =
  process.env.KV_REST_API_TOKEN ?? process.env.UPSTASH_REDIS_REST_TOKEN ?? ''

let _redis: Redis | null = null
let _warned = false

/** Returns the shared Redis client, or null if credentials are not configured. */
export function getRedis(): Redis | null {
  if (_redis) return _redis
  if (!REDIS_URL || !REDIS_TOKEN) {
    if (!_warned) {
      _warned = true
      console.warn('[redis] KV_REST_API_URL/TOKEN not set — quote warm-cache disabled')
    }
    return null
  }
  _redis = new Redis({ url: REDIS_URL, token: REDIS_TOKEN })
  return _redis
}

// ── Quote cache ──────────────────────────────────────────────────────────────
// Keyed per-symbol so the cron warmer and on-demand requests share entries.

const QUOTE_KEY_PREFIX = 'quote:v1:'

function quoteKey(symbol: string): string {
  return `${QUOTE_KEY_PREFIX}${symbol.toUpperCase()}`
}

export interface CachedQuote {
  data: any
  warmedAt: number
  source: 'cron' | 'on-demand'
}

/** Fetch one cached quote. Returns null on miss or if Redis is unavailable. */
export async function getCachedQuote(symbol: string): Promise<CachedQuote | null> {
  const redis = getRedis()
  if (!redis) return null
  try {
    return await redis.get<CachedQuote>(quoteKey(symbol))
  } catch (e) {
    console.error('[redis] getCachedQuote failed:', e)
    return null
  }
}

/** Bulk-fetch cached quotes for multiple symbols in a single round trip. */
export async function getCachedQuotesBulk(
  symbols: string[]
): Promise<Map<string, CachedQuote>> {
  const out = new Map<string, CachedQuote>()
  const redis = getRedis()
  if (!redis || symbols.length === 0) return out
  try {
    const keys = symbols.map(quoteKey)
    const results = await redis.mget<CachedQuote[]>(...keys)
    symbols.forEach((sym, i) => {
      const entry = results[i]
      if (entry) out.set(sym.toUpperCase(), entry)
    })
  } catch (e) {
    console.error('[redis] getCachedQuotesBulk failed:', e)
  }
  return out
}

/** Store one quote with a TTL (seconds). */
export async function setCachedQuote(
  symbol: string,
  data: any,
  ttlSeconds: number,
  source: CachedQuote['source'] = 'on-demand'
): Promise<void> {
  const redis = getRedis()
  if (!redis) return
  try {
    const entry: CachedQuote = { data, warmedAt: Date.now(), source }
    await redis.set(quoteKey(symbol), entry, { ex: ttlSeconds })
  } catch (e) {
    console.error('[redis] setCachedQuote failed:', e)
  }
}

/** Store multiple quotes in a single pipelined round trip. */
export async function setCachedQuotesBulk(
  quotes: Record<string, any>,
  ttlSeconds: number,
  source: CachedQuote['source'] = 'cron'
): Promise<number> {
  const redis = getRedis()
  const entries = Object.entries(quotes)
  if (!redis || entries.length === 0) return 0
  try {
    const pipeline = redis.pipeline()
    const warmedAt = Date.now()
    for (const [symbol, data] of entries) {
      const entry: CachedQuote = { data, warmedAt, source }
      pipeline.set(quoteKey(symbol), entry, { ex: ttlSeconds })
    }
    await pipeline.exec()
    return entries.length
  } catch (e) {
    console.error('[redis] setCachedQuotesBulk failed:', e)
    return 0
  }
}
