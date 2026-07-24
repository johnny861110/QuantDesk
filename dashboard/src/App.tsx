import { useState, useRef, type KeyboardEvent } from 'react'
import { useAnalysis } from './hooks/useAnalysis'
import { RouterCard } from './components/RouterCard'
import { AgentCard } from './components/AgentCard'
import { DebatePanel } from './components/DebatePanel'
import { SupervisorCard } from './components/SupervisorCard'
import { PipelineProgress } from './components/PipelineProgress'

const EXAMPLE_QUERIES = [
  { text: '2330 現在怎樣', hint: '單標的綜合分析' },
  { text: '台積電技術面分析', hint: '技術指標深度' },
  { text: '2317 鴻海值得買嗎', hint: '多面向評估' },
  { text: '0050 目前總經環境如何', hint: '總經環境掃描' },
]

const STATUS_COLOR: Record<string, string> = {
  streaming: 'text-blue-400',
  done: 'text-green-400',
  error: 'text-red-400',
}

export default function App() {
  const [query, setQuery] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const { state, analyze, reset } = useAnalysis()

  const handleSubmit = () => {
    const q = query.trim()
    if (!q || state.status === 'streaming') return
    analyze(q)
  }

  const handleKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') handleSubmit()
  }

  const hasContent = state.router || Object.keys(state.agents).length > 0
  const activeTarget = state.router?.targets?.[0]

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">

      {/* ── Header ───────────────────────────────────── */}
      <header className="sticky top-0 z-10 border-b border-gray-800 bg-gray-950/90 backdrop-blur-sm">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <div className="flex items-center gap-3">
            <span className="text-2xl">📊</span>
            <div>
              <h1 className="text-lg font-black tracking-tight text-white">QuantDesk</h1>
              <p className="text-xs text-gray-500">AI 多智能體投研系統</p>
            </div>
          </div>

          <div className="flex items-center gap-3">
            {/* Active target badge */}
            {activeTarget && (
              <span className="rounded-full border border-blue-700 bg-blue-900/40 px-3 py-1 text-xs font-bold text-blue-300">
                {activeTarget}
              </span>
            )}

            {/* Status indicator */}
            {state.status !== 'idle' && (
              <div className={`flex items-center gap-1.5 text-xs ${STATUS_COLOR[state.status] ?? 'text-gray-400'}`}>
                {state.status === 'streaming' && (
                  <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-blue-400" />
                )}
                <span className="hidden sm:inline max-w-[200px] truncate">{state.currentEvent}</span>
              </div>
            )}

            {hasContent && (
              <button
                onClick={reset}
                className="rounded-lg border border-gray-700 px-2.5 py-1 text-xs text-gray-500 transition-colors hover:border-gray-500 hover:text-gray-300"
              >
                清除
              </button>
            )}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-5xl space-y-5 px-4 py-6">

        {/* ── Query Input ───────────────────────────────── */}
        <div className="rounded-xl border border-gray-700 bg-gray-800/50 p-4">
          <div className="flex gap-2">
            <div className="relative flex-1">
              <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500">🔍</span>
              <input
                ref={inputRef}
                type="text"
                value={query}
                onChange={e => setQuery(e.target.value)}
                onKeyDown={handleKey}
                placeholder="輸入查詢，例如：2330 現在怎樣"
                disabled={state.status === 'streaming'}
                className="w-full rounded-lg border border-gray-600 bg-gray-900 py-2.5 pl-9 pr-3 text-sm text-white placeholder-gray-500 outline-none focus:border-blue-500 disabled:opacity-50 transition-colors"
              />
            </div>
            <button
              onClick={handleSubmit}
              disabled={!query.trim() || state.status === 'streaming'}
              className="rounded-lg bg-blue-600 px-5 py-2.5 text-sm font-bold text-white transition-all hover:bg-blue-500 active:scale-95 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {state.status === 'streaming' ? (
                <span className="flex items-center gap-1.5">
                  <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-white border-t-transparent" />
                  分析中
                </span>
              ) : '分析'}
            </button>
          </div>

          {/* Example queries */}
          {state.status === 'idle' && (
            <div className="mt-3 flex flex-wrap gap-2">
              {EXAMPLE_QUERIES.map(({ text: q, hint }) => (
                <button
                  key={q}
                  onClick={() => { setQuery(q); setTimeout(() => inputRef.current?.focus(), 0) }}
                  className="group flex items-center gap-1.5 rounded-full border border-gray-700 bg-gray-800/60 px-3 py-1.5 text-xs text-gray-300 transition-all hover:border-blue-600 hover:bg-blue-900/20 hover:text-blue-300"
                >
                  <span>{q}</span>
                  <span className="text-gray-600 group-hover:text-blue-500/60">· {hint}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* ── Pipeline Progress ─────────────────────────── */}
        <PipelineProgress state={state} />

        {/* ── Error ────────────────────────────────────── */}
        {state.status === 'error' && state.error && (
          <div className="animate-fade-in rounded-xl border border-red-700 bg-red-900/20 p-4">
            <div className="flex items-center gap-2 text-red-300">
              <span className="text-lg">✗</span>
              <span className="text-sm font-medium">{state.error}</span>
            </div>
          </div>
        )}

        {/* ── Router Card ───────────────────────────────── */}
        {state.router && <RouterCard router={state.router} />}

        {/* ── Domain Agent Cards ────────────────────────── */}
        {state.agentOrder.length > 0 && (
          <div>
            <div className="mb-3 flex items-center gap-2">
              <span className="text-xs font-semibold uppercase tracking-widest text-gray-500">
                Domain Agents
              </span>
              <span className="text-xs text-gray-700">
                {Object.values(state.agents).filter(a => !a.loading && !a.failed).length} 完成
              {Object.values(state.agents).filter(a => a.failed).length > 0 && (
                <span className="text-red-500 ml-1">
                  · {Object.values(state.agents).filter(a => a.failed).length} 失敗
                </span>
              )}
              </span>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {state.agentOrder.map(agent => (
                <AgentCard key={agent} data={state.agents[agent]} />
              ))}
            </div>
          </div>
        )}

        {/* ── Debate Panel ──────────────────────────────── */}
        {state.debate.started && (
          <DebatePanel
            started={state.debate.started}
            bull={state.debate.bull}
            bear={state.debate.bear}
            pm={state.debate.pm}
          />
        )}

        {/* ── Supervisor Final Verdict ──────────────────── */}
        {state.supervisor && (
          <div>
            <div className="mb-3 flex items-center gap-2">
              <div className="h-px flex-1 bg-gray-800" />
              <span className="text-xs font-semibold uppercase tracking-widest text-gray-500">
                最終仲裁結果
              </span>
              <div className="h-px flex-1 bg-gray-800" />
            </div>
            <SupervisorCard data={state.supervisor} />
          </div>
        )}

        {/* ── Done footer ───────────────────────────────── */}
        {state.status === 'done' && (
          <div className="py-4 text-center space-y-1">
            <p className="text-xs text-gray-500 flex items-center justify-center gap-2">
              <span className="text-green-500">✓</span>
              分析完成
              {state.elapsedMs != null && (
                <span className="text-gray-600">· 耗時 {(state.elapsedMs / 1000).toFixed(1)}s</span>
              )}
            </p>
            <p className="text-xs text-gray-700">
              Router → Domain Agents → Multi-agent Debate → Supervisor
            </p>
            <p className="text-xs text-gray-800">
              LangGraph + GPT-4o · 確定性規則引擎 + LLM 仲裁
            </p>
          </div>
        )}
      </main>
    </div>
  )
}
