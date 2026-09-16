import 'server-only'

import { Redis } from '@upstash/redis'

const redis = process.env.KV_REST_API_URL && process.env.KV_REST_API_TOKEN
  ? new Redis({
      url: process.env.KV_REST_API_URL,
      token: process.env.KV_REST_API_TOKEN,
    })
  : null

const PREFIX = 'apex:python:v1:'
const MAX_PAYLOAD_BYTES = 2_000_000

export interface DistributedCacheValue {
  data: unknown
  storedAt: number
  expiresAt: number
}

export function isDistributedCacheConfigured() {
  return redis !== null
}

function keyFor(key: string) {
  return `${PREFIX}${key}`
}

export async function getDistributedCache(key: string): Promise<DistributedCacheValue | null> {
  if (!redis) return null
  try {
    const value = await redis.get<DistributedCacheValue>(keyFor(key))
    if (!value || typeof value !== 'object' || typeof value.expiresAt !== 'number') return null
    return value
  } catch (error) {
    console.warn('[redis-cache] read failed:', error instanceof Error ? error.message : String(error))
    return null
  }
}

export async function setDistributedCache(
  key: string,
  value: DistributedCacheValue,
  staleMultiplier: number,
): Promise<void> {
  if (!redis) return
  try {
    const encoded = JSON.stringify(value)
    if (Buffer.byteLength(encoded, 'utf8') > MAX_PAYLOAD_BYTES) return
    const ttlSeconds = Math.max(1, Math.ceil((value.expiresAt - value.storedAt) * staleMultiplier / 1000))
    await redis.set(keyFor(key), value, { ex: ttlSeconds })
  } catch (error) {
    console.warn('[redis-cache] write failed:', error instanceof Error ? error.message : String(error))
  }
}

export async function deleteDistributedCache(key: string): Promise<void> {
  if (!redis) return
  try {
    await redis.del(keyFor(key))
  } catch (error) {
    console.warn('[redis-cache] delete failed:', error instanceof Error ? error.message : String(error))
  }
}

export async function clearDistributedCacheForScript(script: string): Promise<void> {
  if (!redis) return
  // Key scans are intentionally not used on request paths. Targeted invalidation
  // remains local; TTL expiry bounds remote memory and avoids expensive SCAN calls.
  void script
}

export function distributedCacheKey(key: string) {
  return key
}

export function distributedCacheHealth() {
  return { configured: redis !== null, prefix: PREFIX }
}
