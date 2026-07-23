export type Signal = 'bullish' | 'bearish' | 'neutral'
export type StreamStatus = 'idle' | 'streaming' | 'done' | 'error'

export interface RouterPayload {
  scenario: string
  targets: string[]
  market: string
  depth: string
  method: string
  error?: string
}

export interface AgentPayload {
  agent: string
  signal: Signal
  confidence: number
  time_horizon: string
  data_completeness: number
  key_findings: Record<string, string | number | boolean | null>
  narrative_summary: string
  errors: string[]
  loading?: boolean
}

export interface DebatePartyPayload {
  thesis: string
  key_points: string[]
  confidence: number
}

export interface DebatePMPayload extends DebatePartyPayload {
  signal: Signal
}

export interface HorizonInfo {
  direction: Signal
  evidence_confidence: number
  agents: string[]
}

export interface SupervisorPayload {
  signal: Signal
  confidence: number
  risk_override: boolean
  requires_human_review: boolean
  narrative: string
  mandatory_warnings: string[]
  review_reasons: string[]
  horizon_breakdown: Record<string, HorizonInfo>
}

export interface DebateState {
  bull: DebatePartyPayload | null
  bear: DebatePartyPayload | null
  pm: DebatePMPayload | null
  started: boolean
}

export interface AnalysisState {
  status: StreamStatus
  currentEvent: string
  router: RouterPayload | null
  agents: Record<string, AgentPayload>
  agentOrder: string[]
  debate: DebateState
  supervisor: SupervisorPayload | null
  error: string | null
}

export const INITIAL_STATE: AnalysisState = {
  status: 'idle',
  currentEvent: '',
  router: null,
  agents: {},
  agentOrder: [],
  debate: { bull: null, bear: null, pm: null, started: false },
  supervisor: null,
  error: null,
}
