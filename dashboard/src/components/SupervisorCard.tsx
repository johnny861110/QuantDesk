import type { SupervisorPayload, Signal } from '../types'

const SIGNAL_CONFIG: Record<Signal, {
  label: string; sublabel: string
  text: string; bg: string; border: string; ring: string; bar: string
}> = {
  bullish: {
    label: '看多', sublabel: 'BULLISH ↑',
    text: 'text-green-400', bg: 'bg-green-900/20', border: 'border-green-700',
    ring: 'ring-green-500', bar: 'from-green-600 to-green-400',
  },
  bearish: {
    label: '看空', sublabel: 'BEARISH ↓',
    text: 'text-red-400', bg: 'bg-red-900/20', border: 'border-red-700',
    ring: 'ring-red-500', bar: 'from-red-600 to-red-400',
  },
  neutral: {
    label: '中性', sublabel: 'NEUTRAL →',
    text: 'text-yellow-400', bg: 'bg-yellow-900/10', border: 'border-yellow-800',
    ring: 'ring-yellow-500', bar: 'from-yellow-600 to-yellow-400',
  },
}

const HORIZON_SIGNAL: Record<Signal, string> = {
  bullish: 'text-green-400',
  bearish: 'text-red-400',
  neutral: 'text-yellow-400',
}

interface Props {
  data: SupervisorPayload
}

export function SupervisorCard({ data }: Props) {
  const sig = data.signal as Signal
  const cfg = SIGNAL_CONFIG[sig] ?? SIGNAL_CONFIG.neutral
  const pct = Math.round(data.confidence * 100)
  const horizonEntries = Object.entries(data.horizon_breakdown)

  return (
    <div className={`animate-fade-in rounded-xl border ${cfg.border} ${cfg.bg} p-5 shadow-2xl`}>

      {/* ── 主標題區 ────────────────────────────────── */}
      <div className="flex items-center gap-4 mb-4">
        {/* Signal badge */}
        <div className={`flex flex-col items-center justify-center rounded-2xl border-2 ${cfg.ring.replace('ring', 'border')} p-3 min-w-[80px]`}>
          <span className={`text-3xl font-black leading-none ${cfg.text}`}>{cfg.label}</span>
          <span className={`mt-0.5 text-xs font-bold tracking-widest ${cfg.text} opacity-70`}>
            {cfg.sublabel}
          </span>
        </div>

        {/* Confidence + label */}
        <div className="flex-1">
          <p className="text-xs font-semibold uppercase tracking-widest text-gray-400 mb-1">
            Supervisor 最終仲裁
          </p>

          {/* Confidence gauge */}
          <div className="flex items-center gap-3">
            <div className="relative flex-1 h-4 rounded-full bg-gray-700 overflow-hidden">
              <div
                className={`absolute inset-y-0 left-0 rounded-full bg-gradient-to-r transition-all duration-1000 ${cfg.bar}`}
                style={{ width: `${pct}%` }}
              />
              <div className="absolute inset-0 flex items-center justify-center">
                <span className="text-xs font-bold text-white drop-shadow">{pct}%</span>
              </div>
            </div>
            <span className="text-xs text-gray-400 whitespace-nowrap">信心水準</span>
          </div>
        </div>
      </div>

      {/* ── 警告區 ───────────────────────────────────── */}
      {data.risk_override && (
        <div className="mb-3 rounded-lg border border-red-600 bg-red-900/40 px-4 py-3">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-lg">🚨</span>
            <span className="text-sm font-bold text-red-300">風控強制降級</span>
          </div>
          {data.mandatory_warnings.length > 0 && (
            <ul className="space-y-0.5 pl-7">
              {data.mandatory_warnings.map((w, i) => (
                <li key={i} className="text-xs text-red-400">• {w}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {data.requires_human_review && (
        <div className="mb-3 rounded-lg border border-orange-600 bg-orange-900/30 px-4 py-3">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-lg">👤</span>
            <span className="text-sm font-bold text-orange-300">需人工複核</span>
          </div>
          {data.review_reasons.length > 0 && (
            <p className="text-xs text-orange-400 pl-7">{data.review_reasons.join('；')}</p>
          )}
        </div>
      )}

      {/* ── 時間框架分層 ─────────────────────────────── */}
      {horizonEntries.length > 0 && (
        <div className="mb-4 rounded-lg bg-gray-900/50 p-3">
          <p className="text-xs font-semibold uppercase tracking-widest text-gray-400 mb-2.5">
            時間框架分層
          </p>
          <div className="space-y-2.5">
            {horizonEntries.map(([horizon, result]) => {
              const hSig = result.direction as Signal
              const hColor = HORIZON_SIGNAL[hSig] ?? 'text-gray-400'
              const hPct = Math.round(result.evidence_confidence * 100)
              const hBar = hSig === 'bullish' ? 'bg-green-500' : hSig === 'bearish' ? 'bg-red-500' : 'bg-yellow-500'
              return (
                <div key={horizon}>
                  <div className="flex items-center justify-between text-xs mb-1">
                    <div className="flex items-center gap-2">
                      <span className="w-14 text-gray-500 font-medium">{horizon}</span>
                      <span className={`font-semibold ${hColor}`}>{result.direction.toUpperCase()}</span>
                      <span className="text-gray-600">·</span>
                      <span className="text-gray-500 truncate">{result.agents.join(', ')}</span>
                    </div>
                    <span className={`font-mono text-xs ${hColor}`}>{hPct}%</span>
                  </div>
                  <div className="h-1 w-full rounded-full bg-gray-700">
                    <div
                      className={`h-1 rounded-full transition-all duration-700 ${hBar}`}
                      style={{ width: `${hPct}%` }}
                    />
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* ── 投研摘要 ─────────────────────────────────── */}
      {data.narrative && (
        <div className="rounded-lg border border-gray-700 bg-gray-900/70 p-4">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-base">📝</span>
            <p className="text-xs font-semibold uppercase tracking-widest text-gray-400">
              投研摘要
            </p>
          </div>
          <p className="text-sm leading-relaxed text-gray-200">{data.narrative}</p>
        </div>
      )}
    </div>
  )
}
