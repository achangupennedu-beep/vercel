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
    INTRINIO_API_KEY:    process.env.INTRINIO_API_KEY    ?? 'OjdlMzdiN2IxNzFjMjU3MDhlY2EwM2U3MzhhNjFjY2I5',
    APCA_API_KEY_ID:     process.env.APCA_API_KEY_ID     ?? '',
    APCA_API_SECRET_KEY: process.env.APCA_API_SECRET_KEY ?? '',
    FINNHUB_API_KEY:     process.env.FINNHUB_API_KEY     ?? 'd8tbcp9r01qhcnk1ft60d8tbcp9r01qhcnk1ft6g',
    AV_KEY_1:  'FUKEKMUEN8GIC82A', AV_KEY_2:  'CYBWW8VF831209WH',
    AV_KEY_3:  'H58YGLP8WN0V8OXS', AV_KEY_4:  'U3XMEDPQGL1POIAH',
    AV_KEY_5:  'ELEXFQA94KKGL0OI', AV_KEY_6:  '9FRSHRAZCWHI7IHV',
    AV_KEY_7:  'UFOY6OS1TKTPN1K5', AV_KEY_8:  'L5Z0LJA84D07FB60',
    AV_KEY_9:  'NYD9SXABZ0D87JR3', AV_KEY_10: '2L7M89R071KQVT9N',
    MASSIVE_API_KEY:  process.env.MASSIVE_API_KEY  ?? 'Ns0BKHdMyS7tNaAQ_RREHtCpJ1x49FNi',
    TIINGO_API_KEY:   process.env.TIINGO_API_KEY   ?? '641295bf53a9841702e86b0bae7a15cd5bd6adf9',
    OPTIONDATA_KEY:   process.env.OPTIONDATA_KEY   ?? 'apikey_Y3VzX1VsQ2tRMWlicFRIdkk5fDE3ODIzMTU0MzgzODN8YjM5MWE0NWY1NWQ4OGE4MQ',
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
