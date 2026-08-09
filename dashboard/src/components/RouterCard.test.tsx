/**
 * RouterCard —— Phase 19
 *
 * 這支測試的來由：撰寫過程中發現 QUERY_TYPE_LABEL **漏了
 * Phase 16-E 新增的 stock_investment**，導致 UI 顯示原始英文 key
 * 「stock_investment」而非中文標籤（其他類型都是中文）。
 *
 * 已改用 `satisfies Record<QueryType, string>` 讓 TypeScript 在編譯期擋下，
 * 這裡再補行為層的驗證。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { RouterCard } from './RouterCard'
import { QUERY_TYPES, type QueryType, type RouterPayload } from '../types'

function payload(over: Partial<RouterPayload> = {}): RouterPayload {
  return {
    scenario: 'single_stock',
    targets: ['2330'],
    market: 'TW',
    depth: 'standard',
    method: 'llm',
    query_type: 'stock_analysis',
    agents: ['technical', 'chip', 'news'],
    ...over,
  }
}

describe('基本渲染', () => {
  it('顯示標的與市場', () => {
    render(<RouterCard router={payload()} />)
    expect(screen.getByText(/2330/)).toBeInTheDocument()
  })

  it('LLM 路由與 regex fallback 有可區分的標示', () => {
    const { unmount } = render(<RouterCard router={payload({ method: 'llm' })} />)
    expect(screen.getByText(/GPT-4o-mini/)).toBeInTheDocument()
    unmount()

    render(<RouterCard router={payload({ method: 'regex' })} />)
    expect(screen.getByText(/regex fallback/)).toBeInTheDocument()
  })
})

describe('query_type 標籤（跨檔案一致性）', () => {
  it.each(QUERY_TYPES)('%s 顯示中文標籤，不得漏出原始 key', (qt) => {
    render(<RouterCard router={payload({ query_type: qt as QueryType })} />)
    // 若標籤表漏了這個 key，元件會 fallback 顯示原始英文 key
    expect(screen.queryByText(qt)).not.toBeInTheDocument()
  })

  it('stock_investment 顯示「個股投資建議」（Phase 16-E 漏改的那個）', () => {
    render(<RouterCard router={payload({ query_type: 'stock_investment' })} />)
    expect(screen.getByText('個股投資建議')).toBeInTheDocument()
  })

  it('investment_strategy 標籤反映 16-E 後收窄的組合層語意', () => {
    render(<RouterCard router={payload({ query_type: 'investment_strategy' })} />)
    expect(screen.getByText('組合策略')).toBeInTheDocument()
  })

  it('沒有 query_type 時退回顯示 scenario，不得空白', () => {
    render(<RouterCard router={payload({ query_type: undefined })} />)
    expect(screen.getByText('single_stock')).toBeInTheDocument()
  })
})

describe('未知值的容錯', () => {
  it('未知 scenario 不得崩潰', () => {
    expect(() =>
      render(<RouterCard router={payload({ scenario: 'brand_new_scenario' })} />),
    ).not.toThrow()
  })

  it('未知 depth 不得崩潰', () => {
    expect(() =>
      render(<RouterCard router={payload({ depth: 'ultra' })} />),
    ).not.toThrow()
  })
})
