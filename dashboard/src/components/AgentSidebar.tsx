import type { AgentPayload, Signal } from '../types'

interface AgentDef {
  id: string
  label: string
  icon: string
  desc: string
}

const AGENTS: AgentDef[] = [
  { id: 'technical',    label: '技術面',  icon: '📉', desc: 'RSI / MACD / KD / 布林' },
  { id: 'chip',         label: '籌碼面',  icon: '🏦', desc: '三大法人 / 外資持股' },
  { id: 'fundamental',  label: '基本面',  icon: '📋', desc: 'ROIC / EWS / 盈餘品質' },
  { id: 'news',         label: '新聞面',  icon: '📰', desc: 'MOPS / RSS / Tavily' },
  { id: 'macro',        label: '總經面',  icon: '🌐', desc: 'NFP / CPI / GDP' },
  { id: 'cross_market', label: '跨市場',  icon: '🔗', desc: 'TAIEX ↔ S&P 500' },
  { id: 'risk',         label: '風控',    icon: '🛡️', desc: 'Greeks / Scenario' },
]

const SIGNAL_DOT: Record<Signal, string> = {
  bullish: 'bg-green-500',
  bearish: 'bg-red-500',
  neutral: 'bg-yellow-500',
}

interface Props {
  agents: Record<string, AgentPayload>
  activeAgents: string[]           // currently selected agents from router
  symbol: string                   // current symbol to analyze
  onRunAgent: (agentId: string, symbol: string) => void
  onRunAll: () => void
  collapsed: boolean
  onToggle: () => void
}

export function AgentSidebar({
  agents, activeAgents, symbol, onRunAgent, onRunAll, collapsed, onToggle
}: Props) {
  return (
    <aside
      className={`
        flex flex-col h-full border-r border-gray-800 bg-gray-950 transition-all duration-300 shrink-0
        ${collapsed ? 'w-14' : 'w-52'}
      `}
    >
      {/* Sidebar header */}
      <div className={`flex items-center border-b border-gray-800 px-3 py-3 ${collapsed ? 'justify-center' : 'justify-between'}`}>
        {!collapsed && (
          <span className="text-[11px] font-bold uppercase tracking-widest text-gray-500">
            Agents
          </span>
        )}
        <button
          onClick={onToggle}
          className="rounded p-1 text-gray-600 hover:text-gray-300 transition-colors"
          title={collapsed ? '展開側欄' : '收起側欄'}
        >
          {collapsed ? '›' : '‹'}
        </button>
      </div>

      {/* Agent list */}
      <nav className="flex flex-col gap-0.5 p-2 flex-1 overflow-y-auto">
        {AGENTS.map(def => {
          const agentData = agents[def.id]
          const isActive = activeAgents.includes(def.id)
          const isDone = agentData && !agentData.loading && !agentData.failed
          const isLoading = agentData?.loading
          const isFailed = agentData?.failed
          const sig = agentData?.signal as Signal | undefined

          return (
            <button
              key={def.id}
              onClick={() => onRunAgent(def.id, symbol || '2330')}
              title={collapsed ? `${def.label} — ${def.desc}` : def.desc}
              className={`
                group flex items-center gap-2.5 rounded-lg px-2 py-2 text-left transition-all
                ${isActive
                  ? 'bg-blue-900/30 border border-blue-800/60'
                  : 'hover:bg-gray-800/60 border border-transparent'}
              `}
            >
              {/* Status dot */}
              <div className="relative shrink-0">
                <span className="text-base leading-none">{def.icon}</span>
                {isDone && sig && (
                  <span className={`absolute -bottom-0.5 -right-0.5 h-2 w-2 rounded-full border border-gray-950 ${SIGNAL_DOT[sig]}`} />
                )}
                {isLoading && (
                  <span className="absolute -bottom-0.5 -right-0.5 h-2 w-2 rounded-full border border-gray-950 bg-blue-400 animate-pulse" />
                )}
                {isFailed && (
                  <span className="absolute -bottom-0.5 -right-0.5 h-2 w-2 rounded-full border border-gray-950 bg-red-500" />
                )}
              </div>

              {/* Label + desc */}
              {!collapsed && (
                <div className="min-w-0 flex-1">
                  <p className={`text-xs font-medium truncate ${isActive ? 'text-blue-300' : 'text-gray-300 group-hover:text-white'}`}>
                    {def.label}
                  </p>
                  <p className="text-[10px] text-gray-600 truncate">{def.desc}</p>
                </div>
              )}

              {/* Confidence badge */}
              {!collapsed && isDone && agentData && (
                <span className={`text-[10px] font-mono shrink-0 ${
                  sig === 'bullish' ? 'text-green-500' :
                  sig === 'bearish' ? 'text-red-500' : 'text-yellow-500'
                }`}>
                  {Math.round(agentData.confidence * 100)}%
                </span>
              )}
            </button>
          )
        })}
      </nav>

      {/* Run All button */}
      <div className="border-t border-gray-800 p-2">
        <button
          onClick={onRunAll}
          title="執行完整分析（全部 agent）"
          className={`
            w-full rounded-lg bg-blue-900/40 border border-blue-800/60 py-2 text-xs font-bold text-blue-300
            hover:bg-blue-800/50 hover:text-blue-200 transition-colors
            ${collapsed ? 'px-1' : 'px-2'}
          `}
        >
          {collapsed ? '▶' : '▶ 完整分析'}
        </button>
      </div>
    </aside>
  )
}
