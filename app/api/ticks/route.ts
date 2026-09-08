/**
 * /api/ticks — Dukascopy tick data endpoint
 *
 * Fetches historical tick data (bid/ask/volume) for a given ticker via dukascopy-node.
 * All 610 US equity instruments are supported via the DUKASCOPY_INSTRUMENTS map.
 *
 * Query params:
 *   symbol  (required) — e.g. "AAPL"
 *   from    (optional) — ISO date string, default = yesterday
 *   to      (optional) — ISO date string, default = today
 *   clean   (optional) — "1" to filter zero-volume ticks (default "1")
 *
 * Response shape:
 * {
 *   success: true,
 *   symbol:  "AAPL",
 *   instrument: "aaplususd",
 *   count:   1234,
 *   ticks: [
 *     { timestamp: 1585526400104, askPrice: 151.23, bidPrice: 151.20,
 *       askVolume: 0.75, bidVolume: 0.75, mid: 151.215, spread: 0.03 }
 *   ],
 *   stats: { min: 148.2, max: 155.1, vwap: 151.8, totalAskVol: 120.5, totalBidVol: 118.3 }
 * }
 */
import { NextRequest, NextResponse } from 'next/server'
import { getDukascopyId } from '@/lib/dukascopy-instruments'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const SYM_RE = /^[A-Z0-9.^-]{1,12}$/

function errorResp(msg: string, status = 400) {
  return NextResponse.json({ success: false, error: msg }, { status })
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)

  const rawSym = searchParams.get('symbol')?.trim().toUpperCase()
  if (!rawSym)              return errorResp('symbol is required')
  if (!SYM_RE.test(rawSym)) return errorResp('symbol contains invalid characters')

  const instrumentId = getDukascopyId(rawSym)
  if (!instrumentId) {
    return errorResp(
      `Dukascopy data not available for ${rawSym}. Supported tickers include AAPL, TSLA, MSFT, etc.`,
      404
    )
  }

  // Date range — default: yesterday 00:00 UTC → today 00:00 UTC
  const now  = new Date()
  const defaultTo   = new Date(now.getFullYear(), now.getMonth(), now.getDate())          // today midnight
  const defaultFrom = new Date(defaultTo.getTime() - 86_400_000)                         // yesterday midnight

  const fromStr = searchParams.get('from')
  const toStr   = searchParams.get('to')
  const cleanTicks = searchParams.get('clean') !== '0'

  let fromDate: Date
  let toDate: Date
  try {
    fromDate = fromStr ? new Date(fromStr) : defaultFrom
    toDate   = toStr   ? new Date(toStr)   : defaultTo
    if (isNaN(fromDate.getTime()) || isNaN(toDate.getTime())) {
      return errorResp('from/to must be valid ISO date strings')
    }
    // Cap range to 7 days to avoid runaway downloads
    const diffMs = toDate.getTime() - fromDate.getTime()
    if (diffMs > 7 * 86_400_000) {
      toDate = new Date(fromDate.getTime() + 7 * 86_400_000)
    }
    if (fromDate >= toDate) {
      return errorResp('from must be before to')
    }
  } catch {
    return errorResp('Invalid date format')
  }

  try {
    // Dynamic import — dukascopy-node is a large ESM-only package
    const { getHistoricalRates } = await import('dukascopy-node') as any

    const raw: Array<{
      timestamp: number
      askPrice: number
      bidPrice: number
      askVolume: number
      bidVolume: number
    }> = await getHistoricalRates({
      instrument: instrumentId,
      dates:      { from: fromDate, to: toDate },
      timeframe:  'tick',
    })

    if (!raw || !Array.isArray(raw)) {
      return NextResponse.json({
        success: true,
        symbol: rawSym,
        instrument: instrumentId,
        count: 0,
        ticks: [],
        stats: null,
      })
    }

    // Clean and structure ticks
    let ticks = raw
      .filter(t => {
        if (!cleanTicks) return true
        // Filter likely bad ticks: zero prices, negative spreads
        return t.askPrice > 0 && t.bidPrice > 0 && t.askPrice >= t.bidPrice
      })
      .map(t => ({
        timestamp:  t.timestamp,
        askPrice:   +t.askPrice.toFixed(4),
        bidPrice:   +t.bidPrice.toFixed(4),
        askVolume:  +t.askVolume.toFixed(4),
        bidVolume:  +t.bidVolume.toFixed(4),
        mid:        +((t.askPrice + t.bidPrice) / 2).toFixed(4),
        spread:     +(t.askPrice - t.bidPrice).toFixed(4),
      }))

    // Compute summary stats
    let stats: Record<string, number> | null = null
    if (ticks.length > 0) {
      const mids = ticks.map(t => t.mid)
      const min = Math.min(...mids)
      const max = Math.max(...mids)
      const totalAskVol = ticks.reduce((s, t) => s + t.askVolume, 0)
      const totalBidVol = ticks.reduce((s, t) => s + t.bidVolume, 0)
      const totalVol = totalAskVol + totalBidVol
      const vwap = totalVol > 0
        ? ticks.reduce((s, t) => s + t.mid * (t.askVolume + t.bidVolume), 0) / totalVol
        : mids.reduce((a, b) => a + b, 0) / mids.length
      stats = {
        min:          +min.toFixed(4),
        max:          +max.toFixed(4),
        vwap:         +vwap.toFixed(4),
        totalAskVol:  +totalAskVol.toFixed(2),
        totalBidVol:  +totalBidVol.toFixed(2),
        avgSpread:    +(ticks.reduce((s, t) => s + t.spread, 0) / ticks.length).toFixed(4),
        firstTs:      ticks[0].timestamp,
        lastTs:       ticks[ticks.length - 1].timestamp,
      }
    }

    return NextResponse.json({
      success:    true,
      symbol:     rawSym,
      instrument: instrumentId,
      from:       fromDate.toISOString(),
      to:         toDate.toISOString(),
      count:      ticks.length,
      ticks,
      stats,
    }, {
      headers: {
        'Cache-Control': 's-maxage=3600, stale-while-revalidate=7200',
      }
    })
  } catch (err: any) {
    const msg = err?.message ?? String(err)
    return NextResponse.json(
      { success: false, error: `Dukascopy fetch failed: ${msg}` },
      { status: 502 }
    )
  }
}
