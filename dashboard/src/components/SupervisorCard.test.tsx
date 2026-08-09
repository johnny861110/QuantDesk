/**
 * SupervisorCard —— Phase 19
 *
 * 為什麼這支比其他展示元件重要：它是**風險警告的呈現介面**。
 * 後端 Supervisor 花了三層規則引擎算出「風控強制降級」「需人工複核」，
 * 若前端沒把它顯示出來，那整條防線在使用者眼裡等於不存在
 * （CLAUDE.md 設計原則②的最後一哩）。
 *
 * 測試針對語意而非樣式：警告有沒有出現、數值有沒有正確呈現、
 * 未驗證約束有沒有被標示。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { SupervisorCard } from './SupervisorCard'
import type { HardConstraintDetail, SupervisorPayload } from '../types'

function hc(over: Partial<HardConstraintDetail> = {}): HardConstraintDetail {
  return {
    agent: 'risk',
    type: 'net_delta_pct_nav',
    current: 0.42,
    limit: 0.35,
    breached: true,
    verifiable: true,
    detail: null,
    ...over,
  }
}

function payload(over: Partial<SupervisorPayload> = {}): SupervisorPayload {
  return {
    signal: 'neutral',
    confidence: 0.6,
    risk_override: false,
    requires_human_review: false,
    narrative: '綜合判斷為中性。',
    mandatory_warnings: [],
    review_reasons: [],
    horizon_breakdown: {},
    ...over,
  }
}

describe('風控強制降級（設計原則②的呈現層）', () => {
  it('risk_override=true 時必須顯示醒目警告', () => {
    render(<SupervisorCard data={payload({ risk_override: true })} />)
    expect(screen.getByText('風控強制降級')).toBeInTheDocument()
  })

  it('risk_override=false 時不得顯示該警告（避免狼來了）', () => {
    render(<SupervisorCard data={payload({ risk_override: false })} />)
    expect(screen.queryByText('風控強制降級')).not.toBeInTheDocument()
  })

  it('逐條列出 mandatory_warnings，不得只顯示第一條', () => {
    render(
      <SupervisorCard
        data={payload({
          risk_override: true,
          mandatory_warnings: ['net_delta_pct_nav', 'gamma_limit', 'unverifiable:vega_limit'],
        })}
      />,
    )
    expect(screen.getByText(/net_delta_pct_nav/)).toBeInTheDocument()
    expect(screen.getByText(/gamma_limit/)).toBeInTheDocument()
    expect(screen.getByText(/unverifiable:vega_limit/)).toBeInTheDocument()
  })
})

describe('人工複核（HITL Gate）', () => {
  it('requires_human_review=true 時顯示提示與原因', () => {
    render(
      <SupervisorCard
        data={payload({
          requires_human_review: true,
          review_reasons: ['hard_constraint_breach:gamma_limit', 'low_confidence:0.35'],
        })}
      />,
    )
    expect(screen.getByText('需人工複核')).toBeInTheDocument()
    expect(screen.getByText(/gamma_limit/)).toBeInTheDocument()
    expect(screen.getByText(/low_confidence/)).toBeInTheDocument()
  })

  it('不需複核時不顯示', () => {
    render(<SupervisorCard data={payload()} />)
    expect(screen.queryByText('需人工複核')).not.toBeInTheDocument()
  })
})

describe('風控約束明細', () => {
  it('顯示約束類型與 current / limit 數值', () => {
    render(<SupervisorCard data={payload({ hard_constraint_details: [hc()] })} />)
    expect(screen.getByText('net_delta_pct_nav')).toBeInTheDocument()
    expect(screen.getByText(/0\.4200 \/ 0\.3500/)).toBeInTheDocument()
  })

  it('verifiable=false 必須標示「未驗證」——這是保守處置的依據，不能被當成安全', () => {
    render(
      <SupervisorCard
        data={payload({
          hard_constraint_details: [hc({ breached: false, verifiable: false, type: 'gamma_limit' })],
        })}
      />,
    )
    expect(screen.getByText(/未驗證/)).toBeInTheDocument()
  })

  it('verifiable=true 時不加註未驗證', () => {
    render(<SupervisorCard data={payload({ hard_constraint_details: [hc()] })} />)
    expect(screen.queryByText(/未驗證/)).not.toBeInTheDocument()
  })

  it('limit=0 不得產生 NaN 或 Infinity（除零防護）', () => {
    const { container } = render(
      <SupervisorCard data={payload({ hard_constraint_details: [hc({ limit: 0, current: 5 })] })} />,
    )
    expect(container.textContent).not.toContain('NaN')
    expect(container.textContent).not.toContain('Infinity')
  })

  it('沒有明細時不渲染該區塊', () => {
    render(<SupervisorCard data={payload({ hard_constraint_details: [] })} />)
    expect(screen.queryByText('風控約束明細')).not.toBeInTheDocument()
  })
})

describe('容錯', () => {
  it('空 narrative 不得崩潰', () => {
    expect(() => render(<SupervisorCard data={payload({ narrative: '' })} />)).not.toThrow()
  })

  it('horizon_breakdown 為空不得崩潰', () => {
    expect(() => render(<SupervisorCard data={payload({ horizon_breakdown: {} })} />)).not.toThrow()
  })

  it('多層 horizon 皆呈現，不得只顯示其中一層（分層資訊不可被吃掉）', () => {
    render(
      <SupervisorCard
        data={payload({
          horizon_breakdown: {
            short: { direction: 'bearish', evidence_confidence: 0.7, agents: ['technical'] },
            long: { direction: 'bullish', evidence_confidence: 0.8, agents: ['fundamental'] },
          },
        })}
      />,
    )
    expect(screen.getByText(/short/i)).toBeInTheDocument()
    expect(screen.getByText(/long/i)).toBeInTheDocument()
  })
})
