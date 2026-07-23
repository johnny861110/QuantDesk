import type { SupervisorPayload, Signal } from '../types'

const SIGNAL_CONFIG: Record<Signal, { label: string; text: string; ring: string; glow: string }> = {
  bullish: { label: '看多 ↑', text: 'text-green-400',  ring: 'ring-green-500',  glow: 'shadow-green-500/20' },
  bearish: { label: '看空 ↓', text: 'text-red-400',    ring: 'ring-red-500',    glow: 'shadow-red-500/20'   },
  neutral: { label: '中性 →', text: 'text-yellow-400', ring: 'ring-yellow-500', glow: 'shadow-yellow-500/20' },
}

function ConfidenceGauge({ value }: { value: number }) {
  const pct = Math.round(value * 100)
  const color = value >= 0.6 ? 'from-green-500 to-green-400' : value >= 0.4 ? 'from-yellow-500 to-yellow-400' : 'from-red-500 to-red-400'

  return (
    <div className="mt-3">
      <div className="mb-1.5 flex justify-between text-xs">
        <span className="text-gray-400">信心水準</span>
        <span className="font-mono font-bold text-white">{pct}%</span>
      </div>
      <div className="relative h-3 w-full overflow-hidden rounded-full bg-gray-700">
        <div
          className={`h-full rounded-full bg-gradient-to-r transition-all duration-1000 ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

interface Props {
  data: SupervisorPayload
}

export function SupervisorCard({ data }: Props) {
  const sig = data.signal as Signal
  const cfg = SIGNAL_CONFIG[sig] ?? SIGNAL_CONFIG.neutral

  const horizonEntries = Object.entries(data.horizon_breakdown)

  return (
    <div className={`animate-fade-in rounded-xl border border-gray-600 bg-gray-800/80 p-5 shadow-xl ${cfg.glow}`}>
      {/* Header */}
      <div className="mb-4 flex items-start justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-widest text-gray-400">
            Supervisor 最終仲裁
          </p>
          <p className={`mt-1 text-3xl font-black tracking-tight ${cfg.text}`}>
            {cfg.label}
          </p>
        </div>
        <div className={`ring-2 ${cfg.ring} rounded-full p-1.5`}>
          <div className={`h-10 w-10 rounded-full flex items-center justify-center text-xs font-black ${cfg.text}`}>
            {Math.round(data.confidence * 100)}%
          </div>
        </div>
      </div>

      {/* Warnings */}
      {data.risk_override && (
        <div className="mb-3 rounded-lg border border-red-700 bg-red-900/30 px-3 py-2 text-sm text-red-300">
          🚨 <strong>風控強制降級</strong> — 強制警告已觸發
          {data.mandatory_warnings.length > 0 && (
            <ul className="mt-1 list-disc list-inside text-xs text-red-400">
              {data.mandatory_warnings.map((w, i) => <li key={i}>{w}</li>)}
            </ul>
          )}
        </div>
      )}

      {data.requires_human_review && (
        <div className="mb-3 rounded-lg border border-orange-700 bg-orange-900/30 px-3 py-2 text-sm text-orange-300">
          👤 <strong>需人工複核</strong>
          {data.review_reasons.length > 0 && (
            <p className="mt-0.5 text-xs text-orange-400">{data.review_reasons.join('；')}</p>
          )}
        </div>
      )}

      {/* Confidence Gauge */}
      <ConfidenceGauge value={data.confidence} />

      {/* Horizon Breakdown */}
      {horizonEntries.length > 0 && (
        <div className="mt-4">
          <p className="mb-2 text-xs font-semibold text-gray-400">時間框架分層</p>
          <div className="space-y-1.5">
            {horizonEntries.map(([horizon, result]) => {
              const hSig = result.direction as Signal
              const hCfg = SIGNAL_CONFIG[hSig] ?? SIGNAL_CONFIG.neutral
              return (
                <div key={horizon} className="flex items-center gap-2 text-xs">
                  <span className="w-16 shrink-0 text-gray-500">{horizon}</span>
                  <span className={`font-medium ${hCfg.text}`}>{result.direction}</span>
                  <span className="text-gray-600">|</span>
                  <span className="text-gray-400">{Math.round(result.evidence_confidence * 100)}%</span>
                  <span className="text-gray-600 truncate">{result.agents.join(', ')}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Narrative */}
      {data.narrative && (
        <div className="mt-4 rounded-lg bg-gray-900/60 p-3">
          <p className="text-xs font-semibold text-gray-400 mb-1.5">投研摘要</p>
          <p className="text-sm leading-relaxed text-gray-200">{data.narrative}</p>
        </div>
      )}
    </div>
  )
}
