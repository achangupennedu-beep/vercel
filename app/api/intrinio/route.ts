import { NextRequest, NextResponse } from 'next/server'
import { execPython } from '@/lib/exec-python'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const SYM_RE  = /^[A-Z0-9.^-]{1,12}$/
const MODE_RE = /^[a-z_]{2,20}$/
const VALID_MODES = new Set([
  'unusual', 'implied_move', 'stats', 'ivol_rank', 'ivol_term', 'greeks',
])

function err(msg: string, status = 400) {
  return NextResponse.json({ success: false, error: msg }, { status })
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)

  const symbol = searchParams.get('symbol')?.trim().toUpperCase()
  const mode   = searchParams.get('mode')?.trim().toLowerCase() ?? 'stats'

  if (!symbol || !SYM_RE.test(symbol)) return err('Invalid symbol')
  if (!MODE_RE.test(mode))             return err('Invalid mode')
  if (!VALID_MODES.has(mode))          return err(`Unknown mode — valid: ${[...VALID_MODES].join(', ')}`)

  const env: Record<string, string> = {
    INTRINIO_API_KEY:    process.env.INTRINIO_API_KEY    ?? '',
    APCA_API_KEY_ID:     process.env.APCA_API_KEY_ID     ?? '',
    APCA_API_SECRET_KEY: process.env.APCA_API_SECRET_KEY ?? '',
    FINNHUB_API_KEY:     process.env.FINNHUB_API_KEY     ?? '',
    AV_KEY_1:  process.env.AV_KEY_1 ?? '', AV_KEY_2:  process.env.AV_KEY_2 ?? '',
    AV_KEY_3:  process.env.AV_KEY_3 ?? '', AV_KEY_4:  process.env.AV_KEY_4 ?? '',
    AV_KEY_5:  process.env.AV_KEY_5 ?? '', AV_KEY_6:  process.env.AV_KEY_6 ?? '',
    AV_KEY_7:  process.env.AV_KEY_7 ?? '', AV_KEY_8:  process.env.AV_KEY_8 ?? '',
    AV_KEY_9:  process.env.AV_KEY_9 ?? '', AV_KEY_10: process.env.AV_KEY_10 ?? '',
    MASSIVE_API_KEY:  process.env.MASSIVE_API_KEY  ?? '',
    TIINGO_API_KEY:   process.env.TIINGO_API_KEY   ?? '',
    OPTIONDATA_KEY:   process.env.OPTIONDATA_KEY   ?? '',
  }

  // Cache TTLs by mode (seconds)
  const cacheTTL: Record<string, number> = {
    unusual:      20,
    implied_move: 30,
    stats:        25,
    ivol_rank:    60,
    ivol_term:    60,
    greeks:       15,
  }

  const result = await execPython('scripts/intrinio_fetch.py', [symbol, mode], env)
  if (!result.ok) {
    const isProd = process.env.NODE_ENV === 'production'
    return NextResponse.json(
      { success: false, error: isProd ? 'Data fetch failed' : result.stderr },
      { status: 502 }
    )
  }

  const ttl = cacheTTL[mode] ?? 30
  return NextResponse.json({ success: true, data: result.data }, {
    headers: {
      'Cache-Control': `s-maxage=${ttl}, stale-while-revalidate=${ttl * 2}`,
      'X-Source': 'intrinio',
      'X-Mode':   mode,
    },
  })
}
