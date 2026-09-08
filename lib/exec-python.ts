import { execFile, execFileSync } from 'child_process'
import path from 'path'
import fs from 'fs'

// ── Types ──────────────────────────────────────────────────────────────────────

export interface PythonResult {
  ok: boolean
  data: any
  stderr: string
  cached?: boolean
  stale?: boolean        // true when we returned cache and kicked a background refresh
  latencyMs?: number
}

// ── Venv bootstrap ─────────────────────────────────────────────────────────────
// The .venv is ephemeral in the Vercel Sandbox; rebuild it on-demand.

const PROJECT_ROOT = process.cwd()
// Keep generated virtualenv files outside the source tree. Besides avoiding repository churn,
// this prevents Next/Turbopack from traversing broken host-specific symlinks during builds.
const VENV_DIR = process.env.APEX_VENV_DIR ?? path.join('/tmp', 'apex-options-venv')
const VENV_PYTHON = path.join(VENV_DIR, 'bin', 'python')
const VENV_PIP = path.join(VENV_DIR, 'bin', 'pip')
const VENV_SENTINEL = path.join(VENV_DIR, '.apex_ready')   // written after packages install
const REQUIRED_PKGS = [
  'alpaca-py>=0.38.0',
  'yfinance>=0.2.61',
  'lse-data>=0.1.0',    // London Strategic Edge live tick + options SDK
]

let _venvReady = false   // in-memory flag — avoid redundant fs checks
let _buildingVenv = false
let _buildQueue: Array<(ok: boolean) => void> = []

function venvIsBuilt(): boolean {
  if (_venvReady) return true
  const ok = fs.existsSync(VENV_SENTINEL)
  if (ok) _venvReady = true
  return ok
}

/**
 * Ensure the venv exists and has packages installed.
 * Safe to call concurrently — subsequent callers wait for the first to finish.
 */
async function ensureVenv(): Promise<boolean> {
  if (venvIsBuilt()) return true

  // Queue if already building
  if (_buildingVenv) {
    return new Promise<boolean>((res) => _buildQueue.push(res))
  }

  _buildingVenv = true

  try {
    // Prefer uv (faster), fall back to python3 -m venv
    const uvBin = '/root/.local/bin/uv'
    const hasUv = fs.existsSync(uvBin)

    if (hasUv) {
      execFileSync(uvBin, ['venv', '--python', '3.13', VENV_DIR], {
        stdio: 'ignore', timeout: 30_000,
      })
      execFileSync(uvBin, ['pip', 'install', '--quiet', ...REQUIRED_PKGS], {
        stdio: 'ignore', timeout: 120_000,
        env: { ...process.env, VIRTUAL_ENV: VENV_DIR },
      })
    } else {
      execFileSync('python3', ['-m', 'venv', VENV_DIR], {
        stdio: 'ignore', timeout: 30_000,
      })
      execFileSync(VENV_PIP, ['install', '--quiet', ...REQUIRED_PKGS], {
        stdio: 'ignore', timeout: 120_000,
      })
    }

    fs.writeFileSync(VENV_SENTINEL, new Date().toISOString())
    _venvReady = true
    console.log('[exec-python] venv ready')
    _buildQueue.forEach((cb) => cb(true))
    return true
  } catch (e) {
    console.error('[exec-python] venv build failed:', e)
    _buildQueue.forEach((cb) => cb(false))
    return false
  } finally {
    _buildingVenv = false
    _buildQueue = []
  }
}

function resolvePythonBin(): string {
  if (fs.existsSync(VENV_PYTHON)) return VENV_PYTHON
  // Hard fallback — system python3 (may lack alpaca/yfinance)
  return '/usr/bin/python3'
}

// ── TTL-based LRU cache ────────────────────────────────────────────────────────

const CACHE_TTL: Record<string, number> = {
  'scripts/options.py': 30_000,   // 30 s
  'scripts/quote.py': 8_000,   // 8 s
  'scripts/history.py': 3_600_000,   // 1 h
  'scripts/finnhub_fetch.py': 20_000,   // 20 s
  'scripts/analytics_fetch.py': 30_000,   // 30 s
  'scripts/pricing_models.py': 60_000,   // 1 min
  'scripts/cross_asset.py': 60_000,
  'scripts/data_sources.py': 30_000,
  // AxionQuant alternative data — slow-changing, generous TTL
  'scripts/axionquant.py': 900_000,      // 15 min (sentiment); ESG/supply/traffic longer but route handles it
}
const DEFAULT_TTL = 30_000

// Extended "stale" window — we can return stale data while refreshing in background
const STALE_MULTIPLIER = 4  // data is usable up to TTL * 4 for SWR purposes

interface CacheEntry {
  data: any
  storedAt: number
  expiresAt: number
}

const cache = new Map<string, CacheEntry>()

function cacheKey(script: string, args: string[]): string {
  return `${script}:${args.join('|')}`
}

function getCacheTTL(scriptName: string) {
  return CACHE_TTL[scriptName] ?? DEFAULT_TTL
}

function isFresh(entry: CacheEntry): boolean {
  return Date.now() <= entry.expiresAt
}

function isUsable(entry: CacheEntry): boolean {
  // Usable (for stale-while-revalidate) up to STALE_MULTIPLIER × TTL
  const ttl = entry.expiresAt - entry.storedAt
  return Date.now() <= entry.storedAt + ttl * STALE_MULTIPLIER
}

function setCached(key: string, data: any, scriptName: string) {
  const ttl = getCacheTTL(scriptName)
  const now = Date.now()
  cache.set(key, { data, storedAt: now, expiresAt: now + ttl })
  if (cache.size > 300) {
    const oldest = cache.keys().next().value
    if (oldest) cache.delete(oldest)
  }
}

// ── In-flight deduplication ────────────────────────────────────────────────────
// Prevents multiple simultaneous spawns for the same script+args combination.

const inFlight = new Map<string, Promise<PythonResult>>()

// ── Circuit-breaker ────────────────────────────────────────────────────────────
// More lenient: threshold=5 failures, window=45s (was 3/60s).
// After circuit opens we still try once per 20s (probe mode).

interface CBState { count: number; openUntil: number; lastProbeAt: number }
const failures = new Map<string, CBState>()
const CB_THRESHOLD = 5
const CB_OPEN_MS = 45_000
const CB_PROBE_MS = 20_000  // try a probe every 20 s while open

function circuitState(script: string): 'closed' | 'open' | 'probe' {
  const f = failures.get(script)
  if (!f || f.count < CB_THRESHOLD) return 'closed'
  const now = Date.now()
  if (now >= f.openUntil) { failures.delete(script); return 'closed' }
  if (now - f.lastProbeAt >= CB_PROBE_MS) return 'probe'
  return 'open'
}

function recordFailure(script: string) {
  const f = failures.get(script) ?? { count: 0, openUntil: 0, lastProbeAt: 0 }
  f.count++
  if (f.count >= CB_THRESHOLD) f.openUntil = Date.now() + CB_OPEN_MS
  failures.set(script, f)
}

function recordSuccess(script: string) {
  failures.delete(script)
}

function recordProbeAttempt(script: string) {
  const f = failures.get(script)
  if (f) { f.lastProbeAt = Date.now(); failures.set(script, f) }
}

// ── Environment forwarding ─────────────────────────────────────────────────────

function buildEnv(extra: Record<string, string> = {}): NodeJS.ProcessEnv {
  return {
    ...process.env,
    // Alpaca
    APCA_API_KEY_ID: process.env.APCA_API_KEY_ID ?? 'PKJ7QRP6GBRDN3UKP2XX34NG2H',
    APCA_API_SECRET_KEY: process.env.APCA_API_SECRET_KEY ?? 'G9dcUtYbNMx2dzQssxekHj9XGP5bgfEJYJuFVjCmv7qF',
    // Data providers
    MARKETDATA_API_KEY: process.env.MARKETDATA_API_KEY ?? '',
    POLYGON_API_KEY: process.env.POLYGON_API_KEY ?? '',
    EODHD_API_KEY: process.env.EODHD_API_KEY ?? '6a3ac9d808bda9.37141543',
    FINNHUB_API_KEY: process.env.FINNHUB_API_KEY ?? 'd8tbcp9r01qhcnk1ft60d8tbcp9r01qhcnk1ft6g',
    TIINGO_API_KEY: process.env.TIINGO_API_KEY ?? '641295bf53a9841702e86b0bae7a15cd5bd6adf9',
    TWELVEDATA_API_KEY: process.env.TWELVEDATA_API_KEY ?? '',
    MASSIVE_API_KEY: process.env.MASSIVE_API_KEY ?? 'Ns0BKHdMyS7tNaAQ_RREHtCpJ1x49FNi',
    OPENFIGI_KEY: process.env.OPENFIGI_KEY ?? '2052d5d0-cd5d-4863-83fc-083e56e68663',
    INSIGHTSENTRY_KEY: process.env.INSIGHTSENTRY_KEY ?? '',
    RAPIDAPI_ACCESS_TOKEN: process.env.RAPIDAPI_ACCESS_TOKEN ?? '',
    OPTIONDATA_KEY: process.env.OPTIONDATA_KEY ?? 'apikey_Y3VzX1VsQ2tRMWlicFRIdkk5fDE3ODIzMTU0MzgzODN8YjM5MWE0NWY1NWQ4OGE4MQ',
    INTRINIO_API_KEY: process.env.INTRINIO_API_KEY ?? '',
    // AxionQuant alternative data
    AXIONQUANT_API_KEY: process.env.AXIONQUANT_API_KEY ?? 'axn_1cc27e77f2d56afb8ffa551a2d137004',
    // Alpha Vantage 10-key pool
    AV_KEY_1: 'FUKEKMUEN8GIC82A', AV_KEY_2: 'CYBWW8VF831209WH',
    AV_KEY_3: 'H58YGLP8WN0V8OXS', AV_KEY_4: 'U3XMEDPQGL1POIAH',
    AV_KEY_5: 'ELEXFQA94KKGL0OI', AV_KEY_6: '9FRSHRAZCWHI7IHV',
    AV_KEY_7: 'UFOY6OS1TKTPN1K5', AV_KEY_8: 'L5Z0LJA84D07FB60',
    AV_KEY_9: 'NYD9SXABZ0D87JR3', AV_KEY_10: '2L7M89R071KQVT9N',
    ...extra,
  } as NodeJS.ProcessEnv
}

// ── Core spawn ─────────────────────────────────────────────────────────────────

function spawnPython(
  scriptRelPath: string,
  args: string[],
  env: NodeJS.ProcessEnv,
  timeoutMs: number,
): Promise<PythonResult> {
  const t0 = Date.now()
  const scriptPath = path.join(PROJECT_ROOT, scriptRelPath)
  const pythonBin = resolvePythonBin()

  return new Promise((resolve) => {
    let settled = false

    const child = execFile(
      pythonBin,
      [scriptPath, ...args],
      {
        env,
        maxBuffer: 64 * 1024 * 1024,
        timeout: timeoutMs,
        killSignal: 'SIGTERM',
      },
      (err, stdout, stderr) => {
        if (settled) return
        settled = true
        const latencyMs = Date.now() - t0

        // Log meaningful stderr (skip yfinance deprecation noise)
        if (stderr?.length > 0) {
          const trimmed = stderr.slice(0, 1200)
          if (!trimmed.includes('DeprecationWarning') && !trimmed.includes('FutureWarning')) {
            console.error(`[python:${path.basename(scriptRelPath)}] ${trimmed}`)
          }
        }

        if (err && !stdout) {
          recordFailure(scriptRelPath)
          const msg = err.killed
            ? `Timeout ${timeoutMs}ms exceeded`
            : (stderr?.slice(0, 400) ?? err.message)
          resolve({ ok: false, data: null, stderr: msg, latencyMs })
          return
        }

        const rawOut = stdout?.trim()
        if (!rawOut) {
          recordFailure(scriptRelPath)
          resolve({ ok: false, data: null, stderr: 'Empty output', latencyMs })
          return
        }

        try {
          const data = JSON.parse(rawOut)
          if (data?.error && !data?.calls && !data?.price) {
            recordFailure(scriptRelPath)
            resolve({ ok: false, data: null, stderr: String(data.error), latencyMs })
            return
          }
          recordSuccess(scriptRelPath)
          resolve({ ok: true, data, stderr: stderr?.slice(0, 300) ?? '', latencyMs })
        } catch {
          recordFailure(scriptRelPath)
          resolve({
            ok: false, data: null,
            stderr: `JSON parse error: ${rawOut.slice(0, 300)}`,
            latencyMs,
          })
        }
      }
    )

    // Hard kill: SIGKILL 3 s after timeout
    setTimeout(() => {
      if (!settled) {
        settled = true
        try { child.kill('SIGKILL') } catch { }
        recordFailure(scriptRelPath)
        resolve({
          ok: false, data: null,
          stderr: `Hard kill at ${timeoutMs + 3000}ms`,
          latencyMs: Date.now() - t0,
        })
      }
    }, timeoutMs + 3000)
  })
}

// ── Public execPython ──────────────────────────────────────────────────────────

export async function execPython(
  scriptRelPath: string,
  args: string[] = [],
  extraEnv: Record<string, string> = {},
  options: { bypassCache?: boolean; timeoutMs?: number } = {}
): Promise<PythonResult> {
  const t0 = Date.now()
  const key = cacheKey(scriptRelPath, args)
  const timeoutMs = options.timeoutMs ?? (scriptRelPath.includes('options') ? 48_000 : 28_000)
  const env = buildEnv(extraEnv)

  // ── 1. Cache check ─────────────────────────────────────────────────────────
  if (!options.bypassCache) {
    const entry = cache.get(key)
    if (entry) {
      if (isFresh(entry)) {
        return { ok: true, data: entry.data, stderr: '', cached: true, latencyMs: Date.now() - t0 }
      }
      if (isUsable(entry)) {
        // Return stale data immediately, trigger background refresh
        triggerBackgroundRefresh(scriptRelPath, args, extraEnv, timeoutMs, key)
        return { ok: true, data: entry.data, stderr: '', cached: true, stale: true, latencyMs: Date.now() - t0 }
      }
    }
  }

  // ── 2. Circuit-breaker ─────────────────────────────────────────────────────
  const cs = circuitState(scriptRelPath)
  if (cs === 'open') {
    // Return stale if we have any usable data
    const entry = cache.get(key)
    if (entry) {
      return { ok: true, data: entry.data, stderr: '[circuit-open] returning stale', cached: true, stale: true, latencyMs: 0 }
    }
    return { ok: false, data: null, stderr: `[circuit-open] ${scriptRelPath}`, latencyMs: 0 }
  }
  if (cs === 'probe') {
    recordProbeAttempt(scriptRelPath)
  }

  // ── 3. In-flight deduplication ─────────────────────────────────────────────
  const existing = inFlight.get(key)
  if (existing) {
    return existing
  }

  // ── 4. Ensure venv ─────────────────────────────────────────────────────────
  const venvOk = await ensureVenv()
  if (!venvOk) {
    console.error('[exec-python] venv unavailable — attempting system python fallback')
    // Do not abort: resolvePythonBin() falls back to /usr/bin/python3
  }

  // ── 5. Spawn ───────────────────────────────────────────────────────────────
  const promise = spawnPython(scriptRelPath, args, env, timeoutMs)
    .then((result) => {
      inFlight.delete(key)
      if (result.ok && !options.bypassCache) {
        setCached(key, result.data, scriptRelPath)
      }
      return result
    })
    .catch((err) => {
      inFlight.delete(key)
      return { ok: false, data: null, stderr: String(err), latencyMs: Date.now() - t0 } as PythonResult
    })

  inFlight.set(key, promise)
  return promise
}

// ── Background stale-while-revalidate refresh ──────────────────────────────────

const refreshing = new Set<string>()

function triggerBackgroundRefresh(
  scriptRelPath: string,
  args: string[],
  extraEnv: Record<string, string>,
  timeoutMs: number,
  key: string,
) {
  if (refreshing.has(key)) return  // already in-flight
  refreshing.add(key)

  setImmediate(async () => {
    try {
      const env = buildEnv(extraEnv)
      const result = await spawnPython(scriptRelPath, args, env, timeoutMs)
      if (result.ok) {
        setCached(key, result.data, scriptRelPath)
        recordSuccess(scriptRelPath)
      } else {
        recordFailure(scriptRelPath)
      }
    } catch {
      recordFailure(scriptRelPath)
    } finally {
      refreshing.delete(key)
    }
  })
}

// ── Cache-warming: pre-load AAPL on server cold-start ─────────────────────────

let _warmed = false
export function warmCache(symbols: string[] = ['AAPL']) {
  if (_warmed) return
  _warmed = true
  for (const sym of symbols) {
    setImmediate(async () => {
      try {
        await ensureVenv()
        const key = cacheKey('scripts/options.py', [sym])
        const entry = cache.get(key)
        if (entry && isFresh(entry)) return  // already warm
        const env = buildEnv({})
        const result = await spawnPython('scripts/options.py', [sym], env, 48_000)
        if (result.ok) {
          setCached(key, result.data, 'scripts/options.py')
          console.log(`[exec-python] cache warmed for ${sym} in ${result.latencyMs}ms`)
        }
      } catch (e) {
        console.error(`[exec-python] warm failed for ${sym}:`, e)
      }
    })
  }
}

// ── Cache management helpers ───────────────────────────────────────────────────

export function invalidateCache(scriptRelPath: string, args?: string[]) {
  if (args) {
    cache.delete(cacheKey(scriptRelPath, args))
  } else {
    for (const key of cache.keys()) {
      if (key.startsWith(scriptRelPath)) cache.delete(key)
    }
  }
}

export function getCacheStats() {
  const now = Date.now()
  const entries = Array.from(cache.entries()).map(([key, e]) => ({
    key,
    fresh: isFresh(e),
    usable: isUsable(e),
    ageMs: now - e.storedAt,
    ttlRemainingMs: Math.max(0, e.expiresAt - now),
  }))
  return { size: cache.size, entries }
}

export function getCircuitState() {
  const state: Record<string, { state: string; failures: number; openUntil?: number }> = {}
  for (const [script, f] of failures.entries()) {
    state[script] = { state: circuitState(script), failures: f.count, openUntil: f.openUntil }
  }
  return state
}
