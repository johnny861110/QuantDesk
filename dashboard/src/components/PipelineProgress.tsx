import type { AnalysisState } from '../types'

interface Stage {
  id: string
  label: string
  icon: string
}

// Domain agent stages (row 1)
const AGENT_STAGES: Stage[] = [
  { id: 'router',      label: 'Router', icon: '🔀' },
  { id: 'technical',   label: '技術',   icon: '📉' },
  { id: 'chip',        label: '籌碼',   icon: '🏦' },
  { id: 'macro',       label: '總經',   icon: '🌐' },
  { id: 'news',        label: '新聞',   icon: '📰' },
  { id: 'cross_market',label: '跨市場', icon: '🔗' },
  { id: 'fundamental', label: '基本面', icon: '📋' },
]

// Synthesis stages (row 2)
const SYNTH_STAGES: Stage[] = [
  { id: 'debate', label: 'Debate',     icon: '⚔' },
  { id: 'final',  label: 'Supervisor', icon: '🏁' },
]

type StageStatus = 'idle' | 'active' | 'done' | 'error' | 'failed'

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
      if (agent.failed) return 'failed'
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

const STATUS_STYLES: Record<StageStatus, { dot: string; text: string }> = {
  idle:   { dot: 'bg-gray-800 border-gray-700',                   text: 'text-gray-600' },
  active: { dot: 'bg-blue-500/30 border-blue-400 animate-pulse',  text: 'text-blue-400' },
  done:   { dot: 'bg-green-500/20 border-green-500',              text: 'text-green-400' },
  error:  { dot: 'bg-red-500/20 border-red-500',                  text: 'text-red-400'  },
  failed: { dot: 'bg-red-900/40 border-red-700',                  text: 'text-red-500'  },
}

function StageNode({ stage, status, index }: { stage: Stage; status: StageStatus; index: number }) {
  const style = STATUS_STYLES[status]
  return (
    <div className="flex flex-col items-center gap-1 min-w-0">
      <div
        className={`flex h-7 w-7 items-center justify-center rounded-full border-2 text-xs transition-all duration-500 ${style.dot}`}
      >
        {status === 'done' ? (
          <span className="text-green-400 font-bold text-[10px]">✓</span>
        ) : status === 'failed' ? (
          <span className="text-red-500 font-bold text-[10px]">✗</span>
        ) : status === 'active' ? (
          <span>{stage.icon}</span>
        ) : (
          <span className="text-gray-700 text-[10px]">{index + 1}</span>
        )}
      </div>
      <span className={`text-[10px] whitespace-nowrap leading-tight ${style.text}`}>
        {stage.label}
      </span>
    </div>
  )
}

function Connector({ done }: { done: boolean }) {
  return (
    <div
      className={`mx-0.5 mb-4 h-0.5 flex-1 min-w-[4px] transition-all duration-700 ${
        done ? 'bg-green-700' : 'bg-gray-800'
      }`}
    />
  )
}

interface Props {
  state: AnalysisState
}

export function PipelineProgress({ state }: Props) {
  if (state.status === 'idle') return null

  return (
    <div className="animate-fade-in rounded-xl border border-gray-800 bg-gray-900/60 px-4 py-3 space-y-2">
      {/* Row 1: Router + Domain Agents */}
      <div className="flex items-center">
        {AGENT_STAGES.map((stage, i) => {
          const status = getStageStatus(stage.id, state)
          const isLast = i === AGENT_STAGES.length - 1
          return (
            <div key={stage.id} className="flex flex-1 items-center">
              <StageNode stage={stage} status={status} index={i} />
              {!isLast && <Connector done={status === 'done'} />}
            </div>
          )
        })}
      </div>

      {/* Divider + Row 2: Debate → Supervisor */}
      <div className="flex items-center gap-2">
        <div className="h-px flex-1 bg-gray-800" />
        <span className="text-[10px] text-gray-700 uppercase tracking-wider">仲裁</span>
        <div className="h-px flex-1 bg-gray-800" />
      </div>
      <div className="flex items-center justify-center gap-4">
        {SYNTH_STAGES.map((stage, i) => {
          const status = getStageStatus(stage.id, state)
          const isLast = i === SYNTH_STAGES.length - 1
          return (
            <div key={stage.id} className="flex items-center gap-4">
              <StageNode stage={stage} status={status} index={i} />
              {!isLast && (
                <div className={`h-0.5 w-16 mb-4 transition-all duration-700 ${status === 'done' ? 'bg-green-700' : 'bg-gray-800'}`} />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
