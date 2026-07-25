import { useState, useRef, useCallback, type KeyboardEvent } from 'react'
import { useAnalysis } from './hooks/useAnalysis'
import { useQueryHistory } from './hooks/useQueryHistory'
import { RouterCard } from './components/RouterCard'
import { AgentCard } from './components/AgentCard'
import { AgentSidebar } from './components/AgentSidebar'
import { DebatePanel } from './components/DebatePanel'
import { SupervisorCard } from './components/SupervisorCard'
import { PipelineProgress } from './components/PipelineProgress'
import { PositionsPanel } from './components/PositionsPanel'
import type { Signal } from './types'

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
  const [showHistory, setShowHistory] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [showPositions, setShowPositions] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const { state, analyze, analyzeAgent, reset, retry } = useAnalysis()
  const { history, addQuery, clearHistory } = useQueryHistory()

  const handleSubmit = () => {
    const q = query.trim()
    if (!q || state.status === 'streaming') return
    analyze(q)
  }

  // Record completed analysis in history
  const prevStatusRef = useRef(state.status)
  if (prevStatusRef.current === 'streaming' && state.status === 'done') {
    // For queries without Supervisor, fall back to the first non-neutral agent signal
    const sig: Signal | undefined = state.supervisor?.signal
      ?? (Object.values(state.agents).find(a => !a.loading && !a.failed && a.signal !== 'neutral')?.signal as Signal | undefined)
    addQuery(query, sig)
  }
  prevStatusRef.current = state.status

  const exportJson = useCallback(() => {
    const data = {
      query,
      timestamp: new Date().toISOString(),
      router: state.router,
      agents: state.agents,
      debate: state.debate,
      supervisor: state.supervisor,
    }
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    const target = state.router?.targets?.[0] ?? 'analysis'
    a.download = `quantdesk-${target}-${new Date().toISOString().slice(0, 10)}.json`
    a.click()
    URL.revokeObjectURL(url)
  }, [query, state])

  const handleKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') handleSubmit()
  }

  const hasContent = state.router || Object.keys(state.agents).length > 0
  const activeTarget = state.router?.targets?.[0]
  const activeAgents = state.router?.agents ?? []
  const currentSymbol = activeTarget ?? query.match(/\b(\d{4})\b/)?.[1] ?? '2330'

  const handleRunAgent = useCallback((agentId: string, sym: string) => {
    analyzeAgent(agentId, sym)
  }, [analyzeAgent])

  const handleRunAll = useCallback(() => {
    const q = query.trim() || `${currentSymbol} 完整分析`
    analyze(q)
  }, [query, currentSymbol, analyze])

  return (
    <div className="flex h-screen overflow-hidden bg-gray-950 text-gray-100">

      {/* ── Left Sidebar ─────────────────────────────── */}
      <AgentSidebar
        agents={state.agents}
        activeAgents={activeAgents}
        symbol={currentSymbol}
        onRunAgent={handleRunAgent}
        onRunAll={handleRunAll}
        collapsed={sidebarCollapsed}
        onToggle={() => setSidebarCollapsed(v => !v)}
      />

      {/* ── Main Content ─────────────────────────────── */}
      <div className="flex flex-1 flex-col overflow-hidden">

      {/* ── Header ───────────────────────────────────── */}
      <header className="shrink-0 border-b border-gray-800 bg-gray-950/90 backdrop-blur-sm">
        <div className="flex items-center justify-between px-4 py-3">
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

            <button
              onClick={() => setShowPositions(v => !v)}
              className="rounded-lg border border-gray-700 px-2.5 py-1 text-xs text-gray-400 transition-colors hover:border-blue-600 hover:text-blue-400"
              title="管理持倉"
            >
              🛡️ 持倉
            </button>
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

      <main className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-4xl space-y-5 px-4 py-6">

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

          {/* Example queries + history */}
          {state.status === 'idle' && (
            <div className="mt-3 space-y-2">
              <div className="flex flex-wrap gap-2">
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

              {/* Query history */}
              {history.length > 0 && (
                <div>
                  <button
                    onClick={() => setShowHistory(v => !v)}
                    className="text-xs text-gray-600 hover:text-gray-400 transition-colors"
                  >
                    {showHistory ? '▾' : '▸'} 最近查詢 ({history.length})
                  </button>
                  {showHistory && (
                    <div className="mt-1 flex flex-wrap gap-1.5">
                      {history.slice(0, 10).map((h, i) => (
                        <button
                          key={i}
                          onClick={() => { setQuery(h.query); setTimeout(() => inputRef.current?.focus(), 0) }}
                          className="flex items-center gap-1 rounded-full border border-gray-800 bg-gray-900/60 px-2.5 py-1 text-xs text-gray-400 hover:border-gray-600 hover:text-gray-200 transition-colors"
                        >
                          {h.signal && (
                            <span className={h.signal === 'bullish' ? 'text-green-500' : h.signal === 'bearish' ? 'text-red-500' : 'text-yellow-500'}>●</span>
                          )}
                          <span className="truncate max-w-[140px]">{h.query}</span>
                        </button>
                      ))}
                      <button
                        onClick={clearHistory}
                        className="text-[10px] text-gray-700 hover:text-gray-500 ml-1"
                      >
                        清除
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        {/* ── Pipeline Progress ─────────────────────────── */}
        <PipelineProgress state={state} />

        {/* ── Error ────────────────────────────────────── */}
        {state.status === 'error' && state.error && (
          <div className="animate-fade-in rounded-xl border border-red-700 bg-red-900/20 p-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-red-300">
                <span className="text-lg">✗</span>
                <span className="text-sm font-medium">{state.error}</span>
              </div>
              <button
                onClick={retry}
                className="rounded-lg border border-red-700 px-3 py-1.5 text-xs font-medium text-red-400 transition-colors hover:bg-red-900/40 hover:text-red-300"
              >
                重試
              </button>
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
        {state.supervisor ? (
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
        ) : state.status === 'done' && Object.keys(state.agents).length > 0 && (
          <div className="rounded-xl border border-gray-800 bg-gray-900/40 px-4 py-3 text-center text-xs text-gray-600">
            本次為 <span className="text-gray-400 font-medium">{state.router?.query_type ?? '個股分析'}</span> 模式，各 Agent 結果獨立輸出，不進行 Supervisor 仲裁整合。
            若需完整投資建議請詢問「{state.router?.targets?.[0] ?? ''} 值得買嗎」等策略性問題。
          </div>
        )}

        {/* ── Done footer ───────────────────────────────── */}
        {state.status === 'done' && (
          <div className="py-4 text-center space-y-2">
            <p className="text-xs text-gray-500 flex items-center justify-center gap-2">
              <span className="text-green-500">✓</span>
              分析完成
              {state.elapsedMs != null && (
                <span className="text-gray-600">· 耗時 {(state.elapsedMs / 1000).toFixed(1)}s</span>
              )}
            </p>
            <div className="flex items-center justify-center gap-3">
              <button
                onClick={retry}
                className="rounded-lg border border-gray-700 px-3 py-1.5 text-xs text-gray-400 transition-colors hover:border-blue-600 hover:text-blue-400"
              >
                重新分析
              </button>
              <button
                onClick={exportJson}
                className="rounded-lg border border-gray-700 px-3 py-1.5 text-xs text-gray-400 transition-colors hover:border-green-600 hover:text-green-400"
              >
                匯出 JSON
              </button>
            </div>
            <p className="text-xs text-gray-800">
              LangGraph + GPT-4o · 確定性規則引擎 + LLM 仲裁
            </p>
          </div>
        )}
      </div>
      </main>
      </div>

      {/* ── Positions Panel ───────────────────────────── */}
      {showPositions && <PositionsPanel onClose={() => setShowPositions(false)} />}
    </div>
  )
}
