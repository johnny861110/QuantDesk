import type { AgentPayload, Signal } from '../types'

const SIGNAL_STYLE: Record<Signal, { badge: string; bar: string; label: string }> = {
  bullish: { badge: 'bg-green-900/60 text-green-400 border-green-700',  bar: 'bg-green-500',  label: '偏多 ↑' },
  bearish: { badge: 'bg-red-900/60   text-red-400   border-red-700',    bar: 'bg-red-500',    label: '偏空 ↓' },
  neutral: { badge: 'bg-yellow-900/60 text-yellow-400 border-yellow-700', bar: 'bg-yellow-500', label: '中性 →' },
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

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(value * 100)
  const signal: Signal = value >= 0.6 ? 'bullish' : value >= 0.4 ? 'neutral' : 'bearish'
  const bar = SIGNAL_STYLE[signal].bar
  return (
    <div className="mt-2">
      <div className="mb-1 flex justify-between text-xs text-gray-400">
        <span>信心</span>
        <span>{pct}%</span>
      </div>
      <div className="h-1.5 w-full rounded-full bg-gray-700">
        <div
          className={`h-1.5 rounded-full transition-all duration-700 ${bar}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
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
          <span className="text-sm font-semibold capitalize text-white">{data.agent}</span>
        </div>
        <div className="h-4 w-20 rounded bg-gray-700" />
        <div className="mt-3 h-1.5 w-full rounded-full bg-gray-700" />
        <div className="mt-3 space-y-1.5">
          <div className="h-3 w-full rounded bg-gray-700/60" />
          <div className="h-3 w-3/4 rounded bg-gray-700/60" />
        </div>
      </div>
    )
  }

  const sig = data.signal as Signal
  const style = SIGNAL_STYLE[sig] ?? SIGNAL_STYLE.neutral
  const completeness = Math.round(data.data_completeness * 100)

  // Top-3 key findings (skip internal/long ones)
  const findings = Object.entries(data.key_findings)
    .filter(([, v]) => v !== null && v !== '' && v !== false)
    .slice(0, 4)

  return (
    <div className="animate-fade-in rounded-xl border border-gray-700 bg-gray-800/60 p-4">
      <div className="mb-3 flex items-center gap-2">
        <span className="text-lg">{AGENT_ICON[data.agent] ?? '🤖'}</span>
        <span className="text-sm font-semibold capitalize text-white">{data.agent}</span>
        <span className={`ml-auto rounded-full border px-2 py-0.5 text-xs font-semibold ${style.badge}`}>
          {style.label}
        </span>
      </div>

      <ConfidenceBar value={data.confidence} />

      {findings.length > 0 && (
        <div className="mt-3 space-y-1">
          {findings.map(([k, v]) => (
            <div key={k} className="flex justify-between text-xs">
              <span className="truncate text-gray-400 mr-2">{k}</span>
              <span className="shrink-0 font-mono text-gray-200">
                {typeof v === 'number' ? v.toFixed(typeof v === 'number' && Math.abs(v) < 10 ? 2 : 0) : String(v)}
              </span>
            </div>
          ))}
        </div>
      )}

      <div className="mt-2 flex items-center justify-between text-xs text-gray-500">
        <span>{data.time_horizon}</span>
        <span>完整度 {completeness}%</span>
      </div>

      {data.errors.length > 0 && (
        <p className="mt-1.5 truncate text-xs text-yellow-500">⚠ {data.errors[0]}</p>
      )}
    </div>
  )
}
