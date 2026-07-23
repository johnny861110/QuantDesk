import type { DebatePartyPayload, DebatePMPayload, Signal } from '../types'

const PM_SIGNAL_STYLE: Record<Signal, { bg: string; text: string; border: string; label: string; bar: string }> = {
  bullish: { bg: 'bg-green-900/25', text: 'text-green-400', border: 'border-green-600', label: '看多 ↑', bar: 'bg-green-500' },
  bearish: { bg: 'bg-red-900/25',   text: 'text-red-400',   border: 'border-red-600',   label: '看空 ↓', bar: 'bg-red-500'   },
  neutral: { bg: 'bg-yellow-900/20',text: 'text-yellow-400',border: 'border-yellow-700',label: '中性 →', bar: 'bg-yellow-500'},
}

function ConfidenceBar({ value, color }: { value: number; color: string }) {
  const pct = Math.round(value * 100)
  return (
    <div className="mt-2">
      <div className="flex justify-between text-xs mb-1">
        <span className="text-gray-500">信心</span>
        <span className={`font-mono font-medium ${color}`}>{pct}%</span>
      </div>
      <div className="h-1.5 w-full rounded-full bg-gray-700">
        <div
          className={`h-1.5 rounded-full transition-all duration-700 ${
            color.includes('green') ? 'bg-green-500' : color.includes('red') ? 'bg-red-500' : 'bg-yellow-500'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

interface PartyBoxProps {
  emoji: string
  role: string
  roleEn: string
  color: string
  data: DebatePartyPayload
  loading?: boolean
}

function PartyBox({ emoji, role, roleEn, color, data, loading }: PartyBoxProps) {
  if (loading) {
    return (
      <div className="animate-pulse-slow flex-1 rounded-xl border border-gray-700 bg-gray-800/40 p-4">
        <div className="mb-3 flex items-center gap-2">
          <span className="text-2xl">{emoji}</span>
          <div>
            <div className="h-3.5 w-20 rounded bg-gray-700" />
            <div className="mt-1 h-2.5 w-14 rounded bg-gray-700/60" />
          </div>
        </div>
        <div className="space-y-2">
          {[1, 2, 3, 4].map(i => (
            <div key={i} className="h-3 rounded bg-gray-700" style={{ width: `${100 - i * 10}%` }} />
          ))}
        </div>
        <div className="mt-3 space-y-1.5">
          {[1, 2].map(i => (
            <div key={i} className="h-2.5 w-3/4 rounded bg-gray-700/60" />
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="animate-fade-in flex-1 rounded-xl border border-gray-700 bg-gray-800/40 p-4">
      <div className="mb-2 flex items-center gap-2">
        <span className="text-2xl">{emoji}</span>
        <div>
          <p className={`text-sm font-bold ${color}`}>{role}</p>
          <p className="text-xs text-gray-500">{roleEn}</p>
        </div>
      </div>

      <ConfidenceBar value={data.confidence} color={color} />

      <p className="mt-3 text-sm leading-relaxed text-gray-200">{data.thesis || '—'}</p>

      {data.key_points.length > 0 && (
        <div className="mt-3 space-y-1.5">
          <p className="text-xs font-semibold text-gray-500">主要論點</p>
          {data.key_points.map((pt, i) => (
            <div key={i} className="flex gap-2 text-xs text-gray-300">
              <span className={`mt-0.5 shrink-0 font-bold ${color}`}>{i + 1}.</span>
              <span className="leading-relaxed">{pt}</span>
            </div>
          ))}
        </div>
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
  const pmPct = pm ? Math.round(pm.confidence * 100) : 0

  return (
    <div className="animate-fade-in space-y-4">
      {/* Section title */}
      <div className="flex items-center gap-3">
        <div className="h-px flex-1 bg-gray-800" />
        <span className="flex items-center gap-2 text-xs font-semibold uppercase tracking-widest text-gray-400">
          <span>⚔</span> Multi-agent Debate
          <span className="rounded-full bg-gray-800 px-2 py-0.5 text-gray-500 text-xs font-normal normal-case tracking-normal">
            Bull + Bear 並行執行
          </span>
        </span>
        <div className="h-px flex-1 bg-gray-800" />
      </div>

      {/* Bull & Bear */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <PartyBox
          emoji="🐂"
          role="多方論述"
          roleEn="Bull Analyst"
          color="text-green-400"
          data={bull ?? { thesis: '', key_points: [], confidence: 0 }}
          loading={!bull}
        />
        <PartyBox
          emoji="🐻"
          role="空方論述"
          roleEn="Bear Analyst"
          color="text-red-400"
          data={bear ?? { thesis: '', key_points: [], confidence: 0 }}
          loading={!bear}
        />
      </div>

      {/* PM Verdict */}
      {pm && pmStyle ? (
        <div className={`animate-fade-in rounded-xl border-2 p-5 ${pmStyle.bg} ${pmStyle.border}`}>
          <div className="flex items-start justify-between mb-3">
            <div className="flex items-center gap-3">
              <span className="text-2xl">👔</span>
              <div>
                <p className="text-sm font-bold text-white">Portfolio Manager 最終裁決</p>
                <p className="text-xs text-gray-400">綜合 Bull + Bear 論述，做出最終投資建議</p>
              </div>
            </div>
            <div className={`rounded-xl border px-3 py-1.5 text-sm font-black ${pmStyle.text} border-current`}>
              {pmStyle.label}
            </div>
          </div>

          {/* PM confidence bar */}
          <div className="mb-3">
            <div className="flex justify-between text-xs mb-1">
              <span className="text-gray-400">PM 裁決信心</span>
              <span className={`font-mono font-bold ${pmStyle.text}`}>{pmPct}%</span>
            </div>
            <div className="h-2 w-full rounded-full bg-gray-700/60">
              <div
                className={`h-2 rounded-full transition-all duration-1000 ${pmStyle.bar}`}
                style={{ width: `${pmPct}%` }}
              />
            </div>
          </div>

          <p className="text-sm leading-relaxed text-gray-100">{pm.thesis}</p>

          {pm.key_points.length > 0 && (
            <div className="mt-3 space-y-1.5">
              <p className={`text-xs font-semibold ${pmStyle.text}`}>執行建議</p>
              {pm.key_points.map((pt, i) => (
                <div key={i} className="flex gap-2 text-sm text-gray-200">
                  <span className={`shrink-0 font-bold ${pmStyle.text}`}>→</span>
                  <span>{pt}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : (
        <div className="animate-pulse-slow rounded-xl border border-gray-700 bg-gray-800/40 p-5">
          <div className="flex items-center gap-3 mb-3">
            <span className="text-2xl">👔</span>
            <div>
              <div className="h-4 w-40 rounded bg-gray-700" />
              <div className="mt-1 h-3 w-56 rounded bg-gray-700/60" />
            </div>
          </div>
          <div className="space-y-2">
            {[1, 2, 3].map(i => (
              <div key={i} className="h-3 rounded bg-gray-700" style={{ width: `${95 - i * 8}%` }} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
