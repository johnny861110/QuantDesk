import type { AnalysisState } from '../types'

interface Stage {
  id: string
  label: string
  icon: string
}

const STAGES: Stage[] = [
  { id: 'router',      label: 'Router',    icon: '🔀' },
  { id: 'technical',   label: '技術面',    icon: '📉' },
  { id: 'chip',        label: '籌碼面',    icon: '🏦' },
  { id: 'macro',       label: '總經面',    icon: '🌐' },
  { id: 'news',        label: '新聞面',    icon: '📰' },
  { id: 'cross_market',label: '跨市場',   icon: '🔗' },
  { id: 'fundamental', label: '基本面',    icon: '📋' },
  { id: 'debate',      label: 'Debate',    icon: '⚔' },
  { id: 'final',       label: 'Final',     icon: '🏁' },
]

type StageStatus = 'idle' | 'active' | 'done' | 'error'

function getStageStatus(stageId: string, state: AnalysisState): StageStatus {
  switch (stageId) {
    case 'router':
      if (state.router) return 'done'
      if (state.status === 'streaming') return 'active'
      return 'idle'

    case 'technical':
    case 'chip':
    case 'macro':
    case 'news':
    case 'cross_market':
    case 'fundamental': {
      const agent = state.agents[stageId]
      if (!agent) return 'idle'
      return agent.loading ? 'active' : 'done'
    }

    case 'debate':
      if (state.debate.pm) return 'done'
      if (state.debate.started) return 'active'
      return 'idle'

    case 'final':
      if (state.supervisor) return 'done'
      if (state.debate.pm && state.status === 'streaming') return 'active'
      return 'idle'

    default:
      return 'idle'
  }
}

const STATUS_STYLES: Record<StageStatus, { dot: string; text: string; label: string }> = {
  idle:   { dot: 'bg-gray-700 border-gray-600',          text: 'text-gray-600', label: '' },
  active: { dot: 'bg-blue-500/30 border-blue-400 animate-pulse', text: 'text-blue-400', label: '' },
  done:   { dot: 'bg-green-500/20 border-green-500',     text: 'text-green-400', label: '' },
  error:  { dot: 'bg-red-500/20 border-red-500',         text: 'text-red-400',   label: '' },
}

interface Props {
  state: AnalysisState
}

export function PipelineProgress({ state }: Props) {
  if (state.status === 'idle') return null

  return (
    <div className="animate-fade-in rounded-xl border border-gray-800 bg-gray-900/60 px-4 py-3">
      <div className="flex items-center justify-between">
        {STAGES.map((stage, i) => {
          const status = getStageStatus(stage.id, state)
          const style = STATUS_STYLES[status]
          const isLast = i === STAGES.length - 1

          return (
            <div key={stage.id} className="flex flex-1 items-center">
              {/* Stage node */}
              <div className="flex flex-col items-center gap-1">
                <div
                  className={`flex h-8 w-8 items-center justify-center rounded-full border-2 text-sm transition-all duration-500 ${style.dot}`}
                >
                  {status === 'done' ? (
                    <span className="text-green-400 text-xs font-bold">✓</span>
                  ) : status === 'active' ? (
                    <span>{stage.icon}</span>
                  ) : (
                    <span className="text-gray-600 text-xs">{i + 1}</span>
                  )}
                </div>
                <span className={`text-xs whitespace-nowrap ${style.text}`}>
                  {stage.label}
                </span>
              </div>

              {/* Connector */}
              {!isLast && (
                <div
                  className={`mx-1 h-0.5 flex-1 transition-all duration-700 ${
                    status === 'done' ? 'bg-green-700' : 'bg-gray-800'
                  }`}
                />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
