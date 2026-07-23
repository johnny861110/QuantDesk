import type { DebatePartyPayload, DebatePMPayload, Signal } from '../types'

const PM_SIGNAL_STYLE: Record<Signal, { bg: string; text: string; border: string; label: string }> = {
  bullish: { bg: 'bg-green-900/30', text: 'text-green-400', border: 'border-green-600', label: '看多 ↑' },
  bearish: { bg: 'bg-red-900/30',   text: 'text-red-400',   border: 'border-red-600',   label: '看空 ↓' },
  neutral: { bg: 'bg-yellow-900/30',text: 'text-yellow-400',border: 'border-yellow-600',label: '中性 →' },
}

function ConfidenceDot({ value }: { value: number }) {
  const pct = Math.round(value * 100)
  return (
    <span className="ml-2 text-xs text-gray-400">
      信心 <span className="font-mono text-gray-200">{pct}%</span>
    </span>
  )
}

interface PartyBoxProps {
  emoji: string
  role: string
  color: string
  data: DebatePartyPayload
  loading?: boolean
}

function PartyBox({ emoji, role, color, data, loading }: PartyBoxProps) {
  if (loading) {
    return (
      <div className="animate-pulse-slow flex-1 rounded-xl border border-gray-700 bg-gray-800/40 p-4">
        <div className="mb-2 flex items-center gap-2">
          <span className="text-xl">{emoji}</span>
          <span className={`text-sm font-bold ${color}`}>{role}</span>
        </div>
        <div className="space-y-2">
          <div className="h-3 w-full rounded bg-gray-700" />
          <div className="h-3 w-4/5 rounded bg-gray-700" />
          <div className="h-3 w-3/5 rounded bg-gray-700" />
        </div>
      </div>
    )
  }

  return (
    <div className="animate-fade-in flex-1 rounded-xl border border-gray-700 bg-gray-800/40 p-4">
      <div className="mb-2 flex items-center gap-1">
        <span className="text-xl">{emoji}</span>
        <span className={`text-sm font-bold ${color}`}>{role}</span>
        <ConfidenceDot value={data.confidence} />
      </div>
      <p className="text-sm leading-relaxed text-gray-300">{data.thesis || '—'}</p>
      {data.key_points.length > 0 && (
        <ul className="mt-2 space-y-1">
          {data.key_points.map((pt, i) => (
            <li key={i} className="flex gap-1.5 text-xs text-gray-400">
              <span className={`mt-0.5 shrink-0 ${color}`}>•</span>
              <span>{pt}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

interface Props {
  started: boolean
  bull: DebatePartyPayload | null
  bear: DebatePartyPayload | null
  pm: DebatePMPayload | null
}

export function DebatePanel({ started, bull, bear, pm }: Props) {
  if (!started) return null

  const pmStyle = pm ? PM_SIGNAL_STYLE[pm.signal] ?? PM_SIGNAL_STYLE.neutral : null

  return (
    <div className="animate-fade-in space-y-3">
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-widest text-gray-400">
          ⚔ Multi-agent Debate
        </span>
        <span className="text-xs text-gray-600">(Bull + Bear 並行)</span>
      </div>

      {/* Bull & Bear side by side */}
      <div className="flex gap-3">
        <PartyBox
          emoji="🐂"
          role="Bull Analyst"
          color="text-green-400"
          data={bull ?? { thesis: '', key_points: [], confidence: 0 }}
          loading={!bull}
        />
        <PartyBox
          emoji="🐻"
          role="Bear Analyst"
          color="text-red-400"
          data={bear ?? { thesis: '', key_points: [], confidence: 0 }}
          loading={!bear}
        />
      </div>

      {/* PM Verdict */}
      {pm && pmStyle ? (
        <div className={`animate-fade-in rounded-xl border p-4 ${pmStyle.bg} ${pmStyle.border}`}>
          <div className="mb-2 flex items-center gap-2">
            <span className="text-xl">👔</span>
            <span className="text-sm font-bold text-white">Portfolio Manager 裁決</span>
            <span className={`ml-1 rounded-full border px-2.5 py-0.5 text-xs font-bold ${pmStyle.text} border-current`}>
              {pmStyle.label}
            </span>
            <ConfidenceDot value={pm.confidence} />
          </div>
          <p className="text-sm leading-relaxed text-gray-200">{pm.thesis}</p>
          {pm.key_points.length > 0 && (
            <ul className="mt-2 space-y-1">
              {pm.key_points.map((pt, i) => (
                <li key={i} className={`flex gap-1.5 text-xs ${pmStyle.text}`}>
                  <span className="mt-0.5 shrink-0">→</span>
                  <span>{pt}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <div className="animate-pulse-slow rounded-xl border border-gray-700 bg-gray-800/40 p-4">
          <div className="flex items-center gap-2">
            <span className="text-xl">👔</span>
            <span className="text-sm text-gray-400">PM 裁決中...</span>
          </div>
          <div className="mt-2 space-y-2">
            <div className="h-3 w-full rounded bg-gray-700" />
            <div className="h-3 w-4/5 rounded bg-gray-700" />
          </div>
        </div>
      )}
    </div>
  )
}
