import type { AgentPayload, Signal } from '../types'

const SIGNAL_STYLE: Record<Signal, { badge: string; bar: string; glow: string; label: string }> = {
  bullish: { badge: 'bg-green-900/60 text-green-400 border-green-700', bar: 'bg-green-500', glow: 'border-green-800',  label: '偏多 ↑' },
  bearish: { badge: 'bg-red-900/60   text-red-400   border-red-700',   bar: 'bg-red-500',   glow: 'border-red-800',   label: '偏空 ↓' },
  neutral: { badge: 'bg-yellow-900/60 text-yellow-400 border-yellow-700', bar: 'bg-yellow-500', glow: 'border-gray-700', label: '中性 →' },
}

const AGENT_ICON: Record<string, string> = {
  technical:    '📉',
  chip:         '🏦',
  macro:        '🌐',
  fundamental:  '📋',
  news:         '📰',
  cross_market: '🔗',
  risk:         '🛡️',
}

const AGENT_NAME: Record<string, string> = {
  technical:    '技術面',
  chip:         '籌碼面',
  macro:        '總經面',
  fundamental:  '基本面',
  news:         '新聞面',
  cross_market: '跨市場',
  risk:         '風控',
}

// 把 snake_case key 轉成易讀中文標籤
const FINDING_LABEL: Record<string, string> = {
  rsi:                    'RSI',
  macd_hist:              'MACD 柱',
  macd:                   'MACD',
  macd_signal:            'MACD Signal',
  volume_ratio:           '量比',
  bb_width:               '布林帶寬',
  bb_position:            '布林帶位置',
  consecutive_days:       '外資連續(日)',
  foreign_ownership_ratio:'外資持股%',
  margin_balance:         '融資餘額',
  short_balance:          '融券餘額',
  foreign_net:            '外資淨買超',
  trust_net:              '投信淨買超',
  dealer_net:             '自營淨買超',
  event_count:            '總經事件',
  computable_count:       '可計算事件',
  nfp_surprise:           'NFP 驚喜',
  cpi_surprise:           'CPI 驚喜',
  fed_rate:               'Fed 利率',
  score:                  '綜合評分',
  data_points:            '資料筆數',
  degraded:               '降級模式',
}

function label(key: string): string {
  return FINDING_LABEL[key] ?? key.replace(/_/g, ' ')
}

function formatVal(v: string | number | boolean | null): string {
  if (v === null) return '—'
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (typeof v === 'number') {
    if (Math.abs(v) >= 1000) return v.toLocaleString()
    if (Number.isInteger(v)) return String(v)
    return v.toFixed(2)
  }
  return String(v)
}

interface Props {
  data: AgentPayload
}

export function AgentCard({ data }: Props) {
  if (data.loading) {
    return (
      <div className="animate-pulse-slow rounded-xl border border-gray-700 bg-gray-800/60 p-4">
        <div className="mb-3 flex items-center gap-2">
          <span className="text-lg">{AGENT_ICON[data.agent] ?? '🤖'}</span>
          <div>
            <div className="h-3.5 w-16 rounded bg-gray-700" />
            <div className="mt-1 h-2.5 w-10 rounded bg-gray-700/60" />
          </div>
          <div className="ml-auto h-5 w-14 rounded-full bg-gray-700" />
        </div>
        <div className="mt-2 h-1.5 w-full rounded-full bg-gray-700" />
        <div className="mt-4 space-y-2">
          {[1, 2, 3].map(i => (
            <div key={i} className="flex justify-between">
              <div className="h-3 w-24 rounded bg-gray-700/70" />
              <div className="h-3 w-12 rounded bg-gray-700/70" />
            </div>
          ))}
        </div>
        <div className="mt-4 h-12 rounded-lg bg-gray-700/40" />
      </div>
    )
  }

  const sig = data.signal as Signal
  const style = SIGNAL_STYLE[sig] ?? SIGNAL_STYLE.neutral
  const completeness = Math.round(data.data_completeness * 100)
  const pct = Math.round(data.confidence * 100)

  const findings = Object.entries(data.key_findings)
    .filter(([, v]) => v !== null && v !== '' && v !== false)
    .slice(0, 5)

  return (
    <div className={`animate-fade-in rounded-xl border bg-gray-800/60 p-4 transition-colors ${style.glow}`}>
      {/* Header */}
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xl">{AGENT_ICON[data.agent] ?? '🤖'}</span>
          <div>
            <p className="text-sm font-bold text-white">
              {AGENT_NAME[data.agent] ?? data.agent}
            </p>
            <p className="text-xs text-gray-500 capitalize">{data.agent} agent</p>
          </div>
        </div>
        <span className={`rounded-full border px-2.5 py-1 text-xs font-bold ${style.badge}`}>
          {style.label}
        </span>
      </div>

      {/* Confidence bar */}
      <div className="mt-3">
        <div className="mb-1 flex justify-between text-xs">
          <span className="text-gray-400">信心</span>
          <span className="font-mono font-semibold text-white">{pct}%</span>
        </div>
        <div className="h-2 w-full rounded-full bg-gray-700">
          <div
            className={`h-2 rounded-full transition-all duration-700 ${style.bar}`}
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>

      {/* Key findings */}
      {findings.length > 0 && (
        <div className="mt-3 rounded-lg bg-gray-900/50 p-2.5 space-y-1.5">
          {findings.map(([k, v]) => (
            <div key={k} className="flex items-center justify-between text-xs">
              <span className="text-gray-400">{label(k)}</span>
              <span className="font-mono font-medium text-gray-100">{formatVal(v)}</span>
            </div>
          ))}
        </div>
      )}

      {/* Narrative summary */}
      {data.narrative_summary && (
        <div className="mt-3 rounded-lg border-l-2 border-blue-600 bg-blue-950/30 px-3 py-2">
          <p className="text-xs font-semibold text-blue-400 mb-1">AI 分析摘要</p>
          <p className="text-xs leading-relaxed text-gray-300 italic line-clamp-4">
            {data.narrative_summary}
          </p>
        </div>
      )}

      {/* Footer */}
      <div className="mt-3 flex items-center justify-between text-xs">
        <span className="rounded bg-gray-700/60 px-1.5 py-0.5 text-gray-400">
          {data.time_horizon || 'short'}
        </span>
        <span className={`font-medium ${completeness >= 70 ? 'text-green-500' : completeness >= 40 ? 'text-yellow-500' : 'text-red-500'}`}>
          資料完整 {completeness}%
        </span>
      </div>

      {data.errors.length > 0 && (
        <p className="mt-2 text-xs text-yellow-500 leading-relaxed">
          ⚠ {data.errors[0]}
        </p>
      )}
    </div>
  )
}
