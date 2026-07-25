export type Signal = 'bullish' | 'bearish' | 'neutral'
export type StreamStatus = 'idle' | 'streaming' | 'done' | 'error'

export interface RouterPayload {
  scenario: string
  targets: string[]
  market: string
  depth: string
  method: string
  query_type?: string   // e.g. "stock_analysis" | "investment_strategy" | ...
  agents?: string[]     // which agents will run, e.g. ["technical","chip","news"]
  error?: string
}

export interface AgentHardConstraint {
  type: string
  current: number | null
  limit: number | null
  breached: boolean
  verifiable: boolean
  detail: string | null
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
  hard_constraints?: AgentHardConstraint[]  // per-agent constraint details
  asof?: string        // data timestamp ISO string
  symbol?: string
  market?: string
  loading?: boolean
  failed?: boolean
  receivedAt?: number
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

export interface HardConstraintDetail {
  agent: string
  type: string
  current: number
  limit: number
  breached: boolean
  verifiable: boolean
  detail: string | null
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
  hard_constraint_details?: HardConstraintDetail[]
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
  elapsedMs: number | null   // set when status becomes 'done'
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
  elapsedMs: null,
}
