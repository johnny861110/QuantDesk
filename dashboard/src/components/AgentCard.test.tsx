/**
 * AgentCard —— Phase 19
 *
 * 全專案最大的元件（382 行）。測試聚焦三種狀態的正確區分
 * （載入中 / 失敗 / 完成）與中繼資料面板，
 * 因為 Phase 11 就是在修「錯誤 agent 沒有可見回饋」的問題，
 * 不能讓它悄悄退回去。
 *
 * 圖表已改 lazy load，測試中以 Suspense fallback 呈現，
 * 這裡驗證的是卡片本身不因此崩潰。
 */
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { AgentCard } from './AgentCard'
import type { AgentPayload } from '../types'

function agent(over: Partial<AgentPayload> = {}): AgentPayload {
  return {
    agent: 'technical',
    signal: 'bullish',
    confidence: 0.82,
    time_horizon: 'short',
    data_completeness: 1,
    key_findings: { rsi: 62.5, macd: 1.2345678 },
    narrative_summary: '均線多頭排列，動能延續。',
    errors: [],
    ...over,
  } as AgentPayload
}

describe('三種狀態必須可區分（Phase 11 修過的問題，不得退回）', () => {
  it('載入中顯示進行中的樣態，不顯示結論', () => {
    render(<AgentCard data={agent({ loading: true })} />)
    expect(screen.queryByText('均線多頭排列，動能延續。')).not.toBeInTheDocument()
  })

  it('失敗時顯示錯誤內容，而非靜默空白卡片', () => {
    render(
      <AgentCard data={agent({ failed: true, loading: false, errors: ['RSS timeout after 45s'] })} />,
    )
    expect(screen.getByText(/RSS timeout after 45s/)).toBeInTheDocument()
  })

  it('失敗但沒帶錯誤訊息時仍要渲染，不得崩潰', () => {
    expect(() =>
      render(<AgentCard data={agent({ failed: true, loading: false, errors: [] })} />),
    ).not.toThrow()
  })

  it('完成時顯示 narrative', () => {
    render(<AgentCard data={agent({ loading: false })} />)
    expect(screen.getByText('均線多頭排列，動能延續。')).toBeInTheDocument()
  })
})

describe('中繼資料面板', () => {
  it('預設收合，點擊後展開顯示 key_findings', async () => {
    const user = userEvent.setup()
    render(<AgentCard data={agent({ loading: false })} />)

    expect(screen.queryByText(/rsi/)).not.toBeInTheDocument()
    await user.click(screen.getByText('中繼資料'))
    expect(screen.getByText(/rsi/)).toBeInTheDocument()
  })

  it('展開後顯示硬約束數量提示', () => {
    render(
      <AgentCard
        data={agent({
          loading: false,
          hard_constraints: [
            { type: 'gamma_limit', current: -850, limit: -500, breached: true, verifiable: true, detail: null },
          ],
        })}
      />,
    )
    expect(screen.getByText(/1 約束/)).toBeInTheDocument()
  })

  it('有警告時在收合狀態就提示數量（使用者不必展開才知道有問題）', () => {
    render(<AgentCard data={agent({ loading: false, errors: ['資料部分缺失', '來源延遲'] })} />)
    expect(screen.getByText(/2 警告/)).toBeInTheDocument()
  })
})

describe('數值格式化', () => {
  it('長小數被截斷，不得整串印出來', async () => {
    const user = userEvent.setup()
    render(<AgentCard data={agent({ loading: false, key_findings: { macd: 1.23456789012 } })} />)
    await user.click(screen.getByText('中繼資料'))
    expect(screen.queryByText(/1\.23456789012/)).not.toBeInTheDocument()
  })

  it('null 值顯示為破折號，不得印出 "null" 字樣', async () => {
    // 只測 null——key_findings 型別是 Record<string, string|number|boolean|null>，
    // 且 payload 來自 JSON.parse，永遠不會產生 undefined。
    // （初版曾用 `as never` 硬塞 undefined 進去測，那是不可能發生的狀態，已移除。）
    const user = userEvent.setup()
    const { container } = render(
      <AgentCard data={agent({ loading: false, key_findings: { beta: null } })} />,
    )
    await user.click(screen.getByText('中繼資料'))
    expect(container.textContent).not.toContain('null')
    expect(container.textContent).toContain('—')
  })

  it('空的 key_findings 不崩潰', () => {
    expect(() =>
      render(<AgentCard data={agent({ loading: false, key_findings: {} })} />),
    ).not.toThrow()
  })
})

describe('lazy 圖表', () => {
  it('risk agent 卡片渲染時不因 lazy chart 崩潰', () => {
    expect(() =>
      render(<AgentCard data={agent({ agent: 'risk', loading: false })} />),
    ).not.toThrow()
  })

  it('沒有對應圖表的 agent 正常渲染', () => {
    render(<AgentCard data={agent({ agent: 'news', loading: false })} />)
    expect(screen.getByText('均線多頭排列，動能延續。')).toBeInTheDocument()
  })
})
