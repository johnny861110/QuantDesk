import { useCallback, useRef, useState } from 'react'
import {
  type AnalysisState,
  type AgentPayload,
  INITIAL_STATE,
} from '../types'

function handleEvent(
  state: AnalysisState,
  type: string,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  payload: any,
  startedAt: number,
): AnalysisState {
  switch (type) {
    case 'router':
      return { ...state, router: payload, currentEvent: `Router: ${payload.scenario} → ${payload.targets?.join(', ') || '?'}` }

    case 'agent_start': {
      const stub: AgentPayload = {
        agent: payload.agent,
        signal: 'neutral',
        confidence: 0,
        time_horizon: '',
        data_completeness: 0,
        key_findings: {},
        narrative_summary: '',
        errors: [],
        loading: true,
      }
      const order = state.agentOrder.includes(payload.agent)
        ? state.agentOrder
        : [...state.agentOrder, payload.agent]
      return {
        ...state,
        agents: { ...state.agents, [payload.agent]: stub },
        agentOrder: order,
        currentEvent: `${payload.agent} 分析中...`,
      }
    }

    case 'agent_done':
      return {
        ...state,
        agents: { ...state.agents, [payload.agent]: { ...payload, loading: false } },
        currentEvent: `${payload.agent} 完成 (${payload.signal})`,
      }

    case 'agent_error': {
      // Mark the agent as failed so the card can render an error state
      const failedStub: AgentPayload = {
        agent: payload.agent,
        signal: 'neutral',
        confidence: 0,
        time_horizon: '',
        data_completeness: 0,
        key_findings: {},
        narrative_summary: '',
        errors: [payload.error ?? '未知錯誤'],
        loading: false,
        failed: true,
      }
      const order = state.agentOrder.includes(payload.agent)
        ? state.agentOrder
        : [...state.agentOrder, payload.agent]
      return {
        ...state,
        agents: { ...state.agents, [payload.agent]: failedStub },
        agentOrder: order,
        currentEvent: `${payload.agent} 失敗: ${payload.error}`,
      }
    }

    case 'debate_start':
      return {
        ...state,
        debate: { ...state.debate, started: true },
        currentEvent: 'Debate 啟動 (Bull + Bear 並行執行...)',
      }

    case 'debate_bull':
      return {
        ...state,
        debate: { ...state.debate, bull: payload },
        currentEvent: '多方論述完成',
      }

    case 'debate_bear':
      return {
        ...state,
        debate: { ...state.debate, bear: payload },
        currentEvent: '空方論述完成',
      }

    case 'debate_pm':
      return {
        ...state,
        debate: { ...state.debate, pm: payload },
        currentEvent: `PM 裁決: ${payload.signal?.toUpperCase()}`,
      }

    case 'supervisor':
      return {
        ...state,
        supervisor: payload,
        currentEvent: `Supervisor 仲裁完成: ${payload.signal?.toUpperCase()} (${(payload.confidence * 100).toFixed(0)}%)`,
      }

    case 'done':
      return { ...state, status: 'done', currentEvent: '分析完成 ✓', elapsedMs: Date.now() - startedAt }

    case 'error':
      return { ...state, status: 'error', error: payload.message, currentEvent: '錯誤' }

    default:
      return state
  }
}

export function useAnalysis() {
  const [state, setState] = useState<AnalysisState>(INITIAL_STATE)
  const esRef = useRef<EventSource | null>(null)
  const startedAtRef = useRef<number>(0)

  const analyze = useCallback((query: string) => {
    // Close any existing connection
    if (esRef.current) {
      esRef.current.close()
      esRef.current = null
    }

    // Reset state and record start time
    startedAtRef.current = Date.now()
    setState({ ...INITIAL_STATE, status: 'streaming' })

    const url = `/api/analyze/stream?query=${encodeURIComponent(query)}`
    const es = new EventSource(url)
    esRef.current = es

    es.onmessage = (e: MessageEvent) => {
      try {
        const { type, payload } = JSON.parse(e.data as string)
        setState(prev => handleEvent(prev, type as string, payload, startedAtRef.current))
        if (type === 'done' || type === 'error') {
          es.close()
          esRef.current = null
        }
      } catch {
        // ignore parse errors
      }
    }

    es.onerror = () => {
      setState(prev => ({
        ...prev,
        status: 'error',
        error: '連線中斷，請重試。',
        currentEvent: '連線錯誤',
      }))
      es.close()
      esRef.current = null
    }
  }, [])

  const reset = useCallback(() => {
    if (esRef.current) {
      esRef.current.close()
      esRef.current = null
    }
    setState(INITIAL_STATE)
  }, [])

  return { state, analyze, reset }
}
