import { useState, useRef, type KeyboardEvent } from 'react'
import { useAnalysis } from './hooks/useAnalysis'
import { RouterCard } from './components/RouterCard'
import { AgentCard } from './components/AgentCard'
import { DebatePanel } from './components/DebatePanel'
import { SupervisorCard } from './components/SupervisorCard'

const EXAMPLE_QUERIES = [
  '2330 現在怎樣',
  '台積電技術面分析',
  '2317 鴻海值得買嗎',
  '0050 目前總經環境如何',
]

function StatusBar({ status, currentEvent }: { status: string; currentEvent: string }) {
  if (status === 'idle') return null

  const colors: Record<string, string> = {
    streaming: 'text-blue-400',
    done: 'text-green-400',
    error: 'text-red-400',
  }

  return (
    <div className={`flex items-center gap-2 text-sm ${colors[status] ?? 'text-gray-400'}`}>
      {status === 'streaming' && (
        <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-blue-400" />
      )}
      {status === 'done' && <span>✓</span>}
      {status === 'error' && <span>✗</span>}
      <span>{currentEvent}</span>
    </div>
  )
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

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      {/* Header */}
      <header className="border-b border-gray-800 bg-gray-950/80 backdrop-blur-sm sticky top-0 z-10">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="text-2xl">📊</span>
            <div>
              <h1 className="text-lg font-black tracking-tight text-white">QuantDesk</h1>
              <p className="text-xs text-gray-500">AI 多智能體投研系統</p>
            </div>
          </div>
          {hasContent && (
            <button
              onClick={reset}
              className="text-xs text-gray-500 hover:text-gray-300 transition-colors"
            >
              清除
            </button>
          )}
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-4 py-6 space-y-6">

        {/* Query Input */}
        <div className="rounded-xl border border-gray-700 bg-gray-800/60 p-4">
          <div className="flex gap-2">
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={e => setQuery(e.target.value)}
              onKeyDown={handleKey}
              placeholder="輸入查詢，例如：2330 現在怎樣"
              disabled={state.status === 'streaming'}
              className="flex-1 rounded-lg border border-gray-600 bg-gray-900 px-3 py-2.5 text-sm text-white placeholder-gray-500 outline-none focus:border-blue-500 disabled:opacity-50"
            />
            <button
              onClick={handleSubmit}
              disabled={!query.trim() || state.status === 'streaming'}
              className="rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white transition-all hover:bg-blue-500 active:scale-95 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {state.status === 'streaming' ? '分析中...' : '分析'}
            </button>
          </div>

          {/* Example queries */}
          {state.status === 'idle' && (
            <div className="mt-3 flex flex-wrap gap-2">
              {EXAMPLE_QUERIES.map(q => (
                <button
                  key={q}
                  onClick={() => { setQuery(q); setTimeout(() => inputRef.current?.focus(), 0) }}
                  className="rounded-full border border-gray-600 bg-gray-700/50 px-3 py-1 text-xs text-gray-300 transition-colors hover:border-blue-500 hover:text-blue-300"
                >
                  {q}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Status Bar */}
        <StatusBar status={state.status} currentEvent={state.currentEvent} />

        {/* Error */}
        {state.status === 'error' && state.error && (
          <div className="rounded-xl border border-red-700 bg-red-900/20 p-4 text-sm text-red-300">
            ✗ {state.error}
          </div>
        )}

        {/* Router Card */}
        {state.router && <RouterCard router={state.router} />}

        {/* Agent Cards */}
        {state.agentOrder.length > 0 && (
          <div>
            <p className="mb-2 text-xs font-semibold uppercase tracking-widest text-gray-500">
              Domain Agents
            </p>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              {state.agentOrder.map(agent => (
                <AgentCard key={agent} data={state.agents[agent]} />
              ))}
            </div>
          </div>
        )}

        {/* Debate Panel */}
        {state.debate.started && (
          <DebatePanel
            started={state.debate.started}
            bull={state.debate.bull}
            bear={state.debate.bear}
            pm={state.debate.pm}
          />
        )}

        {/* Supervisor Final Verdict */}
        {state.supervisor && (
          <div>
            <SupervisorCard data={state.supervisor} />
          </div>
        )}

        {/* Done footer */}
        {state.status === 'done' && (
          <p className="text-center text-xs text-gray-600 pb-4">
            分析完成 · Router → Domain Agents → Debate → Supervisor
          </p>
        )}
      </main>
    </div>
  )
}
