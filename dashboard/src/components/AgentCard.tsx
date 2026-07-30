import { useState } from 'react'
import type { AgentPayload, Signal } from '../types'
import { RiskGreeksChart } from './charts/RiskGreeksChart'
import { TechnicalRadar } from './charts/TechnicalRadar'
import { ChipFlowChart } from './charts/ChipFlowChart'

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

/**
 * Higher-precision variant for the "完整計算指標" detail view. Backend metrics
 * are often unrounded floats (e.g. a division result with 15+ digits) — the
 * old raw String(v) dump forced those into unreadable multi-line
 * character-wraps in a narrow column. Round to 6 decimals and strip trailing
 * zeros via the Number() round-trip, so it stays meaningfully more precise
 * than the 2-decimal card summary without becoming an unbounded digit string.
 */
function formatValFull(v: string | number | boolean | null): string {
  if (v === null) return '—'
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  if (typeof v === 'number') {
    if (Math.abs(v) >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 2 })
    if (Number.isInteger(v)) return String(v)
    return String(Number(v.toFixed(6)))
  }
  return String(v)
}

/**
 * Full-width metadata modal (overlay, not confined to the narrow card column).
 * The old inline panel squeezed the value column into ~55% of an already
 * narrow 3-column card and force-broke long numbers character-by-character
 * (`break-all`) inside a fixed max-h-48 scroll box — unreadable in practice.
 */
function MetadataModal({ data, onClose }: { data: AgentPayload; onClose: () => void }) {
  const allFindings = Object.entries(data.key_findings)
  const hcs = data.hard_constraints ?? []
  const errs = data.errors ?? []

  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 pt-12 sm:pt-20">
        <div className="w-full max-w-2xl rounded-xl border border-gray-700 bg-gray-950 shadow-2xl">
          {/* Header */}
          <div className="flex items-center justify-between border-b border-gray-800 px-4 py-3">
            <div className="flex items-center gap-2">
              <span className="text-lg">{AGENT_ICON[data.agent] ?? '🤖'}</span>
              <span className="text-sm font-bold text-white">
                {AGENT_NAME[data.agent] ?? data.agent} · 中繼資料
              </span>
            </div>
            <button onClick={onClose} className="text-gray-500 hover:text-gray-300 text-lg leading-none">×</button>
          </div>

          <div className="max-h-[75vh] space-y-4 overflow-y-auto p-4 text-xs">
            {/* Timestamp / symbol / market */}
            {(data.asof || data.symbol || data.market) && (
              <div className="flex flex-wrap items-center gap-x-6 gap-y-1 rounded-lg bg-gray-900/60 px-3 py-2">
                {data.asof && (
                  <div><span className="text-gray-500">資料時間戳 </span><span className="font-mono text-gray-300">{new Date(data.asof).toLocaleString('zh-TW')}</span></div>
                )}
                {data.symbol && <div><span className="text-gray-500">標的 </span><span className="font-mono text-gray-300">{data.symbol}</span></div>}
                {data.market && <div><span className="text-gray-500">市場 </span><span className="font-mono text-gray-300">{data.market}</span></div>}
              </div>
            )}

            {/* Full key_findings table */}
            {allFindings.length > 0 && (
              <div>
                <p className="mb-1.5 text-[10px] font-bold uppercase tracking-wider text-gray-600">
                  完整計算指標 ({allFindings.length})
                </p>
                <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1.5 rounded-lg bg-gray-900/60 px-3 py-2.5">
                  {allFindings.map(([k, v]) => (
                    <div key={k} className="contents">
                      <span className="truncate font-mono text-gray-500">{k}</span>
                      <span className="text-right font-mono text-gray-200">{formatValFull(v)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Hard constraints */}
            {hcs.length > 0 && (
              <div>
                <p className="mb-1.5 text-[10px] font-bold uppercase tracking-wider text-gray-600">
                  硬約束 ({hcs.length})
                </p>
                <div className="space-y-1.5">
                  {hcs.map((hc, i) => (
                    <div key={i} className={`rounded px-3 py-2 ${hc.breached ? 'bg-red-950/60 border border-red-800/40' : 'bg-gray-900/60'}`}>
                      <div className="flex items-center justify-between">
                        <span className={`font-mono font-bold ${hc.breached ? 'text-red-400' : 'text-gray-400'}`}>{hc.type}</span>
                        <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-bold ${hc.breached ? 'bg-red-900/60 text-red-300' : 'bg-gray-700 text-gray-400'}`}>
                          {hc.breached ? '⛔ BREACH' : '✓ OK'}
                        </span>
                      </div>
                      {(hc.current !== null || hc.limit !== null) && (
                        <div className="mt-1 flex gap-4 text-[10px] text-gray-500">
                          {hc.current !== null && <span>current: <span className="text-gray-300 font-mono">{typeof hc.current === 'number' ? hc.current.toFixed(4) : hc.current}</span></span>}
                          {hc.limit !== null && <span>limit: <span className="text-gray-300 font-mono">{hc.limit}</span></span>}
                        </div>
                      )}
                      {hc.detail && (
                        <p className="mt-1 text-[10px] leading-relaxed text-gray-600">{hc.detail}</p>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Errors / warnings */}
            {errs.length > 0 && (
              <div>
                <p className="mb-1.5 text-[10px] font-bold uppercase tracking-wider text-gray-600">
                  Pipeline 錯誤 / 警告 ({errs.length})
                </p>
                <div className="space-y-1 rounded-lg bg-gray-900/60 px-3 py-2.5">
                  {errs.map((e, i) => (
                    <p key={i} className="break-words leading-relaxed text-yellow-500/80">{e}</p>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Received time */}
          {data.receivedAt && (
            <div className="flex items-center justify-between border-t border-gray-800 px-4 py-2 text-[10px] text-gray-600">
              <span>接收時間</span>
              <span className="font-mono">{new Date(data.receivedAt).toLocaleTimeString()}</span>
            </div>
          )}
        </div>
      </div>
    </>
  )
}

interface Props {
  data: AgentPayload
  defaultExpanded?: boolean
}

export function AgentCard({ data, defaultExpanded = false }: Props) {
  const [showMeta, setShowMeta] = useState(defaultExpanded)
  if (data.failed) {
    return (
      <div className="rounded-xl border border-red-900/60 bg-red-950/30 p-4">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xl">{AGENT_ICON[data.agent] ?? '🤖'}</span>
          <div>
            <p className="text-sm font-bold text-red-400">
              {AGENT_NAME[data.agent] ?? data.agent}
            </p>
            <p className="text-xs text-gray-600 capitalize">{data.agent} agent</p>
          </div>
          <span className="ml-auto rounded-full border border-red-800 bg-red-900/40 px-2 py-0.5 text-xs text-red-400">
            失敗
          </span>
        </div>
        {data.errors[0] && (
          <p className="text-xs text-red-500/80 leading-relaxed mt-1 pl-7">
            {data.errors[0]}
          </p>
        )}
      </div>
    )
  }

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

      {/* Agent-specific chart */}
      {data.agent === 'risk' && <RiskGreeksChart findings={data.key_findings} />}
      {data.agent === 'technical' && <TechnicalRadar findings={data.key_findings} />}
      {data.agent === 'chip' && <ChipFlowChart findings={data.key_findings} />}

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
        <div className="flex items-center gap-2">
          <span className="rounded bg-gray-700/60 px-1.5 py-0.5 text-gray-400">
            {data.time_horizon || 'short'}
          </span>
          {data.receivedAt && (
            <span className="text-gray-600 font-mono text-[10px]">
              {new Date(data.receivedAt).toLocaleTimeString()}
            </span>
          )}
        </div>
        <span className={`font-medium ${completeness >= 70 ? 'text-green-500' : completeness >= 40 ? 'text-yellow-500' : 'text-red-500'}`}>
          資料完整 {completeness}%
        </span>
      </div>

      {/* Metadata toggle */}
      <button
        onClick={() => setShowMeta(v => !v)}
        className="mt-3 flex w-full items-center gap-1.5 rounded-lg border border-gray-700/50 bg-gray-900/40 px-2.5 py-1.5 text-left text-[11px] text-gray-500 transition-colors hover:border-gray-600 hover:text-gray-300"
      >
        <span>{showMeta ? '▾' : '▸'}</span>
        <span>中繼資料</span>
        <span className="ml-auto font-mono text-[10px] text-gray-700">
          {Object.keys(data.key_findings).length} 指標
          {(data.errors?.length ?? 0) > 0 && ` · ${data.errors.length} 警告`}
          {(data.hard_constraints?.length ?? 0) > 0 && ` · ${data.hard_constraints!.length} 約束`}
        </span>
      </button>

      {showMeta && <MetadataModal data={data} onClose={() => setShowMeta(false)} />}
    </div>
  )
}
