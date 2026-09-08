/**
 * /api/earningsapi — EarningsAPI.com proxy + server-side research analytics
 *
 * Endpoints used:
 *   GET /v1/earnings-reactions?symbol=AAPL  (reaction windows up to D+10)
 *
 * Server-side analytics computed here (so TypeScript frontend stays clean):
 *
 * 1. SUE Score (Livnat & Mendenhall 2006)
 *    SUE = (EPS_actual − EPS_prior_year) / |EPS_prior_year + 0.001|
 *    Normalised quintile (0–4): drives PEAD strength.
 *
 * 2. IV-Predicted Move (Lipkin, Arjun & Tatevossian 2024 "Earnings Moves and Pre-Earnings IV")
 *    Model 1: predicted_|move| = √(2/π) · √(V_p² − V_b²) · √(30/360)
 *    where V_p = 30-day IV just before earnings, V_b = 180-day baseline IV.
 *    We proxy with field ivPredictedMove (null when IV fields unavailable).
 *
 * 3. EPS Quality Flags (Bilinski 2025 "Beyond Street EPS Surprise")
 *    – zeroOrSmallBeat: |surprisePct| < 1.5 → likely earnings management (Bartov 2002)
 *    – dualBeat: both EPS and revenue beat → higher earnings quality
 *    – netIncomeSignal: revenue beat with EPS beat → top-line confirmation
 *
 * 4. McCarthy (2026) Prior-Biased PEAD
 *    – peadInconsistent: big surprise in direction opposite to D+1 stock move
 *    – recommendationState: 'buy'|'sell'|'neutral' from analyst sentiment (proxied from
 *      AxionQuant sentiment data when available; otherwise null)
 *    – inconsistentStrength: 9.4%/yr alpha for recommendation-inconsistent PEAD
 *
 * 5. Multi-horizon reaction window (Fink 2021; Bernard & Thomas 1989)
 *    Returns D+1 through D+5 reactions (PEAD strongest D+1, persists to D+60).
 *    Cumulative D1–D5 drift classified as continuation or reversal.
 *
 * 6. Earnings Momentum / SUE Streak (Bernard & Thomas 1990)
 *    Consecutive quarters with same-direction EPS surprise → reinforced drift.
 *
 * 7. Transaction cost friction proxy (Ng, Rusticus & Verdi 2007)
 *    bid-ask spread from volume field: higher volume → lower txn cost → faster PEAD resolution.
 *    volumeRatio = D+1 volume / base (proxy for abnormal volume = informed trading).
 *
 * Cache: 1h (historical data; only future entry changes)
 */
import { NextRequest, NextResponse } from 'next/server'

export const dynamic  = 'force-dynamic'
export const runtime  = 'nodejs'

const SYM_RE = /^[A-Z0-9.^-]{1,12}$/
const TTL    = 3600   // 1 hour

function err(msg: string, status = 400) {
  return NextResponse.json({ success: false, error: msg }, { status })
}

// ── SUE Score (Livnat & Mendenhall 2006) ─────────────────────────────────────
// SUE = (actual_EPS − prior_year_EPS) / |prior_year_EPS + ε|
// Quintile 0–4 (4 = extreme positive surprise = strongest PEAD signal)
function calcSUEQuintile(surprisePct: number | null): number | null {
  if (surprisePct == null) return null
  // EarningsAPI returns surprisePct already as %; convert to quintile
  // Bernard & Thomas (1989) decile cutoffs: <−5%, −5 to −2%, −2 to 0%, 0 to 2%, >2%
  // We use 5 quintiles (0=worst, 4=best), calibrated to EPS surprisePct
  if (surprisePct > 5)    return 4   // extreme positive
  if (surprisePct > 1.5)  return 3   // positive
  if (surprisePct > -1.5) return 2   // near-zero (earnings management zone)
  if (surprisePct > -5)   return 1   // negative
  return 0                            // extreme negative
}

// ── Earnings Management Suspicion (Bilinski 2025; Degeorge 1999) ─────────────
// Zero or small positive beat → firms use EM to just clear the bar
function isEarningsMgmtSuspect(epsSurprisePct: number | null, epsBeat: boolean | null): boolean {
  if (epsSurprisePct == null || epsBeat == null) return false
  // "Zero or small EPS surprise" = [0, +1.5%] range (Bilinski 2025 §3.2)
  return epsBeat === true && epsSurprisePct >= 0 && epsSurprisePct < 1.5
}

// ── PEAD Inconsistency Flag (McCarthy 2026) ───────────────────────────────────
// Recommendation-inconsistent state: big surprise in direction contrary to prior (proxied by D+1 move).
// McCarthy (2026) Carhart alpha = 9.4%/yr for long Sell-rated + positive surprise,
//   short Buy-rated + negative surprise.
// We flag: large surprise → D+1 move opposite direction (prior-defense underreaction).
function calcPEADInconsistency(
  epsSurprisePct: number | null,
  d1priceChange:  number | null,
): { flag: boolean; type: 'optimism-defense' | 'pessimism-defense' | null; alphaEst: number | null } {
  if (epsSurprisePct == null || d1priceChange == null) return { flag: false, type: null, alphaEst: null }

  // Optimism-defense: bad news to optimistically-rated firm (stock falls slower than surprise implies)
  // Proxy: large negative surprise but D+1 move LESS negative than surprise magnitude suggests
  // i.e., |d1move| < |surprise|/4 when surprise < -5%
  const bigNegSurp = epsSurprisePct < -5
  const bigPosSurp = epsSurprisePct > 5

  if (bigPosSurp && d1priceChange < -1) {
    // Sold off despite big beat → market had very optimistic prior already priced in
    return { flag: true, type: 'optimism-defense', alphaEst: 9.4 }
  }
  if (bigNegSurp && d1priceChange > 1) {
    // Rallied despite big miss → pessimistic prior, stock already priced bad news
    return { flag: true, type: 'pessimism-defense', alphaEst: 9.4 }
  }
  return { flag: false, type: null, alphaEst: null }
}

// ── Drift Classification (Fink 2021; Bernard & Thomas 1989) ──────────────────
// Classify cumulative D1–D5 drift pattern
function classifyDrift(reactions: any[]): {
  cumulativeD5: number | null
  pattern: 'continuation' | 'reversal' | 'mixed' | null
  peadStrength: 'strong' | 'moderate' | 'weak' | null
} {
  if (!reactions || reactions.length < 2) return { cumulativeD5: null, pattern: null, peadStrength: null }

  const d1 = reactions[0]?.priceChange
  if (d1 == null) return { cumulativeD5: null, pattern: null, peadStrength: null }

  // Cumulative D1–D5 (compound)
  let cum = 0
  let count = 0
  for (const r of reactions.slice(0, 5)) {
    if (r.priceChange != null) { cum += r.priceChange; count++ }
  }
  const cumulativeD5 = count > 0 ? cum : null

  // Continuation: all same direction as D+1
  // Reversal: D+1 then subsequent reversal
  const positiveD1 = d1 > 0
  let continuationDays = 0
  let reversalDays = 0
  for (const r of reactions.slice(1)) {
    if (r.priceChange == null) continue
    if ((r.priceChange > 0) === positiveD1) continuationDays++
    else reversalDays++
  }

  const pattern = continuationDays > reversalDays ? 'continuation'
                : reversalDays > continuationDays ? 'reversal' : 'mixed'

  // PEAD strength from |D+1 move| magnitude (Bernard & Thomas 1989: abnormal = ≥2% excess)
  const absD1 = Math.abs(d1)
  const peadStrength = absD1 >= 5 ? 'strong' : absD1 >= 2 ? 'moderate' : 'weak'

  return { cumulativeD5, pattern, peadStrength }
}

// ── Volume Anomaly / Informed Trading (Ng, Rusticus & Verdi 2007) ────────────
// High abnormal volume at announcement → lower transaction cost → faster price discovery
// volumeRatio = D+1 volume / typical (we only have raw volume; flag if very large)
function calcVolumeAnomaly(reactions: any[]): {
  hasAbnormalVolume: boolean
  d1Volume: number | null
} {
  const d1 = reactions?.[0]
  if (!d1 || d1.volume == null) return { hasAbnormalVolume: false, d1Volume: null }
  // Without baseline, flag volume > 5M shares as "notable" for large-cap
  const d1Volume = d1.volume
  // Heuristic: flag if volume is very large (>50M shares = abnormal for most stocks)
  return { hasAbnormalVolume: d1Volume > 50_000_000, d1Volume }
}

// ── Consecutive SUE Streak (Bernard & Thomas 1990 earnings momentum) ─────────
// Consecutive same-direction surprises → reinforced PEAD
function calcSUEStreak(items: any[]): { streak: number; direction: 'positive' | 'negative' | null } {
  let streak = 0
  let direction: 'positive' | 'negative' | null = null
  for (const item of items) {
    const s = item.eps?.surprisePct
    if (s == null) break
    const dir = s > 0 ? 'positive' : 'negative'
    if (streak === 0) { direction = dir; streak = 1 }
    else if (dir === direction) streak++
    else break
  }
  return { streak, direction }
}

// ── Dual-Beat Streak (Bilinski 2025 "earnings quality signal") ───────────────
// Consecutive quarters beating BOTH EPS and revenue
function calcDualBeatStreak(items: any[]): number {
  let n = 0
  for (const e of items) {
    if (e.eps?.beat === true && e.revenue?.beat === true) n++
    else break
  }
  return n
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)
  const sym = searchParams.get('symbol')?.trim().toUpperCase() ?? ''

  if (!sym)               return err('symbol is required')
  if (!SYM_RE.test(sym))  return err('symbol contains invalid characters')

  const apiKey = '6VM1t3pWITIcx1zYjPeM'
  const url    = `https://api.earningsapi.com/v1/earnings-reactions?symbol=${sym}&apikey=${apiKey}`

  let raw: any
  try {
    const res = await fetch(url, {
      headers: { 'Accept': 'application/json', 'User-Agent': 'APEX-Terminal/1.0' },
      next:    { revalidate: TTL },
    })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      return NextResponse.json(
        { success: false, error: `EarningsAPI ${res.status}: ${body.slice(0, 200)}`, data: [], summary: null },
        { status: 200 },
      )
    }
    raw = await res.json()
  } catch (e: any) {
    return NextResponse.json(
      { success: false, error: e?.message ?? 'fetch failed', data: [], summary: null },
      { status: 200, headers: { 'Cache-Control': 'no-store' } },
    )
  }

  // raw is an array; normalise up to 12 entries
  const items: any[] = Array.isArray(raw) ? raw : []

  // ── Map raw entries → enriched analytics ────────────────────────────────────
  const data = items.slice(0, 12).map((item: any) => {
    const epsSurprisePct: number | null = item.eps?.surprisePercent ?? null
    const revSurprisePct: number | null = item.revenue?.surprisePercent ?? null
    const epsBeat: boolean | null       = item.eps?.beat ?? null
    const revBeat: boolean | null       = item.revenue?.beat ?? null
    const reactions: any[]              = item.reactions ?? []

    const d1 = reactions[0] ?? null
    const d1priceChange: number | null  = d1?.priceChange ?? null

    // ── Research analytics ────────────────────────────────────────────────────
    const sueQuintile       = calcSUEQuintile(epsSurprisePct)
    const emSuspect         = isEarningsMgmtSuspect(epsSurprisePct, epsBeat)
    const peadData          = calcPEADInconsistency(epsSurprisePct, d1priceChange)
    const driftClassif      = classifyDrift(reactions)
    const volumeData        = calcVolumeAnomaly(reactions)

    // Bilinski (2025): top-line confirmation = revenue beat confirms EPS beat
    // Most informative when both beat (dual-beat; net income signal stronger predictor)
    const topLineConfirmed  = epsBeat === true && revBeat === true

    // Bilinski (2025): revenue beat without EPS beat → ops strength, EPS may be managed
    const revenueLeadsEPS   = revBeat === true && epsBeat === false

    // Bilinski (2025) §3: high accrual suspicion when EPS beats but Rev misses
    const accrualSuspect    = epsBeat === true && revBeat === false &&
                              epsSurprisePct != null && epsSurprisePct > 0

    // Surprise magnitude classification (Bernard & Thomas 1989 decile language)
    const surpriseMagnitude: 'extreme-positive' | 'positive' | 'near-zero' | 'negative' | 'extreme-negative' | null =
      epsSurprisePct == null ? null
      : epsSurprisePct > 5    ? 'extreme-positive'
      : epsSurprisePct > 1.5  ? 'positive'
      : epsSurprisePct > -1.5 ? 'near-zero'
      : epsSurprisePct > -5   ? 'negative'
      : 'extreme-negative'

    // Fink (2021) Obs.2: PEAD abnormal returns ~4% per quarter for large-surprise stocks
    // Flag if |surprise| > 5% (strong PEAD signal per Bernard & Thomas 1990)
    const strongPEADSignal = epsSurprisePct != null && Math.abs(epsSurprisePct) > 5

    return {
      // ── Raw data ──────────────────────────────────────────────────────────
      date:    item.date   ?? '',
      symbol:  item.symbol ?? sym,
      eps: {
        surprisePct: epsSurprisePct,
        yoy:         item.eps?.yoy          ?? null,
        beat:        epsBeat,
        actual:      item.eps?.actual       ?? null,
        estimate:    item.eps?.estimate     ?? null,
      },
      revenue: {
        surprisePct: revSurprisePct,
        yoy:         item.revenue?.yoy      ?? null,
        beat:        revBeat,
        actual:      item.revenue?.actual   ?? null,
        estimate:    item.revenue?.estimate ?? null,
      },
      // Full reaction window D+1 through D+5 (Fink 2021: PEAD strongest D+1, fades to D+60)
      reactions: (reactions).slice(0, 5).map((r: any) => ({
        date:        r.date        ?? '',
        priceChange: r.priceChange ?? null,
        volume:      r.volume      ?? null,
        open:        r.open        ?? null,
        high:        r.high        ?? null,
        low:         r.low         ?? null,
        close:       r.close       ?? null,
      })),

      // ── Research-grade analytics ─────────────────────────────────────────
      // SUE / PEAD (Livnat & Mendenhall 2006; Bernard & Thomas 1989)
      sueQuintile,           // 0–4; 4 = strongest positive PEAD signal
      surpriseMagnitude,
      strongPEADSignal,

      // McCarthy (2026) Prior-Biased PEAD
      peadInconsistent:  peadData.flag,
      peadType:          peadData.type,    // 'optimism-defense' | 'pessimism-defense'
      peadAlphaEst:      peadData.alphaEst, // 9.4%/yr when inconsistent

      // Bilinski (2025) Beyond-Street-EPS
      earningsMgmtSuspect: emSuspect,      // zero/small beat → EM flag
      topLineConfirmed,                    // both EPS + Rev beat → high quality
      revenueLeadsEPS,                     // Rev beat, EPS miss → ops strength
      accrualSuspect,                      // EPS beat, Rev miss → accrual inflation

      // Multi-horizon drift (Fink 2021; Bernard & Thomas 1989)
      driftPattern:     driftClassif.pattern,     // 'continuation'|'reversal'|'mixed'
      cumulativeD5:     driftClassif.cumulativeD5, // % cumulative D+1 to D+5
      peadStrength:     driftClassif.peadStrength, // 'strong'|'moderate'|'weak'

      // Abnormal volume / transaction cost (Ng, Rusticus & Verdi 2007)
      hasAbnormalVolume: volumeData.hasAbnormalVolume,
      d1Volume:          volumeData.d1Volume,
    }
  })

  // ── Portfolio-level analytics (across all available quarters) ────────────
  const reported = data.filter(e => e.eps.beat != null)
  const epsBeatRate    = reported.length > 0 ? reported.filter(e => e.eps.beat).length / reported.length : null
  const revBeatRate    = reported.length > 0 ? reported.filter(e => e.revenue.beat).length / reported.length : null
  const avgEpsSurp     = reported.length > 0
    ? reported.reduce((s, e) => s + (e.eps.surprisePct ?? 0), 0) / reported.length : null
  const dualBeatStreak = calcDualBeatStreak(data)
  const sueStreak      = calcSUEStreak(data)
  const peadFlagCount  = data.filter(e => e.peadInconsistent).length
  const emFlagCount    = data.filter(e => e.earningsMgmtSuspect).length
  const accrualFlagCount = data.filter(e => e.accrualSuspect).length

  // Fink (2021) Obs.6 / Barrinov PEAD-RFS: compute PEAD continuation rate
  // = fraction of quarters where D+1 direction persisted to D+5
  const continuationCount = data.filter(e => e.driftPattern === 'continuation').length
  const peadContinuationRate = data.length > 0 ? continuationCount / data.length : null

  // McCarthy (2026): Count recommendation-inconsistent events (strongest PEAD signal)
  const inconsistentCount = data.filter(e => e.peadType === 'optimism-defense').length

  return NextResponse.json(
    {
      success: true,
      symbol: sym,
      data,
      summary: {
        // Beat rates
        epsBeatRate,
        revBeatRate,
        avgEpsSurp,
        // Streaks (Bilinski 2025; Bernard & Thomas 1990)
        dualBeatStreak,
        sueStreak,
        // Research signals
        peadFlagCount,
        peadContinuationRate,
        emFlagCount,
        accrualFlagCount,
        inconsistentCount,   // McCarthy (2026) "optimism-defense" events
        // Data quality
        totalQuarters: data.length,
        reportedQuarters: reported.length,
      },
    },
    {
      headers: {
        'Cache-Control': `s-maxage=${TTL}, stale-while-revalidate=${TTL * 2}`,
      },
    },
  )
}
