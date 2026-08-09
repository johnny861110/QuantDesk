/**
 * PipelineProgress —— Phase 19
 *
 * 展示元件中唯一有真實邏輯的：依 router.agents 動態組出階段列表，
 * 並由 state 推導每個階段的狀態。測試針對**行為與推導邏輯**，
 * 不斷言 class name（那會讓改樣式就紅燈）。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { PipelineProgress } from './PipelineProgress'
import { INITIAL_STATE, type AgentPayload, type AnalysisState } from '../types'

function agent(over: Partial<AgentPayload> = {}): AgentPayload {
  return {
    agent: 'technical',
    signal: 'neutral',
    confidence: 0,
    time_horizon: '',
    data_completeness: 0,
    key_findings: {},
    narrative_summary: '',
    errors: [],
    ...over,
  } as AgentPayload
}

function state(over: Partial<AnalysisState> = {}): AnalysisState {
  return { ...INITIAL_STATE, status: 'streaming', ...over }
}

describe('顯示條件', () => {
  it('idle 時完全不渲染（避免空進度條佔版面）', () => {
    const { container } = render(<PipelineProgress state={INITIAL_STATE} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('streaming 時渲染 Router 階段', () => {
    render(<PipelineProgress state={state()} />)
    expect(screen.getByText('Router')).toBeInTheDocument()
  })
})

describe('動態階段列表（Phase 15 的核心改動：不再硬編碼 7 個 agent）', () => {
  it('只顯示 router.agents 指定的 agent', () => {
    render(
      <PipelineProgress
        state={state({
          router: {
            scenario: 'single_stock', targets: ['2330'], market: 'TW',
            depth: 'standard', method: 'llm',
            query_type: 'stock_analysis',
            agents: ['technical', 'chip', 'news'],
          },
        })}
      />,
    )
    expect(screen.getByText('技術')).toBeInTheDocument()
    expect(screen.getByText('籌碼')).toBeInTheDocument()
    expect(screen.getByText('新聞')).toBeInTheDocument()
    // 未被路由到的 agent 不該出現
    expect(screen.queryByText('風控')).not.toBeInTheDocument()
    expect(screen.queryByText('基本面')).not.toBeInTheDocument()
  })

  it('router 尚未回來時，退而顯示已出現過的 agent', () => {
    render(<PipelineProgress state={state({ agents: { macro: agent({ agent: 'macro' }) } })} />)
    expect(screen.getByText('總經')).toBeInTheDocument()
  })

  it('router.agents 含未知 id 時安全略過，不得崩潰', () => {
    render(
      <PipelineProgress
        state={state({
          router: {
            scenario: 'single_stock', targets: [], market: 'TW',
            depth: 'standard', method: 'llm',
            agents: ['technical', 'not_a_real_agent'],
          },
        })}
      />,
    )
    expect(screen.getByText('技術')).toBeInTheDocument()
  })
})

describe('階段狀態推導', () => {
  it('agent 尚未開始 → 顯示序號而非勾號', () => {
    render(<PipelineProgress state={state()} />)
    // Router 之外沒有任何 agent，Router 本身在 streaming 且無 router payload → active
    expect(screen.queryByText('✓')).not.toBeInTheDocument()
  })

  it('agent 完成 → 出現勾號', () => {
    render(
      <PipelineProgress
        state={state({ agents: { technical: agent({ loading: false }) } })}
      />,
    )
    expect(screen.getAllByText('✓').length).toBeGreaterThan(0)
  })

  it('agent 失敗 → 出現叉號（不得與完成混淆）', () => {
    render(
      <PipelineProgress
        state={state({ agents: { technical: agent({ failed: true, loading: false }) } })}
      />,
    )
    expect(screen.getByText('✗')).toBeInTheDocument()
  })

  it('router payload 到達後 Router 階段標記完成', () => {
    render(
      <PipelineProgress
        state={state({
          router: {
            scenario: 'single_stock', targets: ['2330'], market: 'TW',
            depth: 'standard', method: 'llm', agents: [],
          },
        })}
      />,
    )
    expect(screen.getAllByText('✓').length).toBeGreaterThan(0)
  })
})

describe('仲裁列（Debate → Supervisor）', () => {
  it('debate 啟動時顯示仲裁列', () => {
    render(
      <PipelineProgress
        state={state({ debate: { ...INITIAL_STATE.debate, started: true } })}
      />,
    )
    expect(screen.getByText('仲裁')).toBeInTheDocument()
    expect(screen.getByText('Debate')).toBeInTheDocument()
    expect(screen.getByText('Supervisor')).toBeInTheDocument()
  })

  it('router.agents 不含 risk 且無 debate/supervisor → 不顯示仲裁列', () => {
    render(
      <PipelineProgress
        state={state({
          router: {
            scenario: 'single_stock', targets: ['2330'], market: 'TW',
            depth: 'standard', method: 'llm',
            query_type: 'stock_analysis',
            agents: ['technical', 'chip', 'news'],
          },
        })}
      />,
    )
    expect(screen.queryByText('仲裁')).not.toBeInTheDocument()
  })

  it('組合策略（含 risk）→ 顯示仲裁列', () => {
    render(
      <PipelineProgress
        state={state({
          router: {
            scenario: 'portfolio_risk', targets: ['PORTFOLIO'], market: 'TW',
            depth: 'standard', method: 'llm',
            query_type: 'investment_strategy',
            agents: ['technical', 'risk'],
          },
        })}
      />,
    )
    expect(screen.getByText('仲裁')).toBeInTheDocument()
  })
})
