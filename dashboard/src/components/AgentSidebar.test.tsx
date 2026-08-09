/**
 * AgentSidebar —— Phase 19
 *
 * 兩個測試重點：
 *   1. SymbolSearch 的 debounce 搜尋（唯一會打 API 的展示元件）
 *   2. **本輪改動的 prop→state 同步**——原本用 useEffect，
 *      已改為 React 官方的「render 期間調整 state」模式，需驗證行為不變
 *
 * 另外守護一個產品規則：沒有選定標的時不得執行單一 agent
 * （Phase 15 移除了硬編碼預設 2330 的行為，不能退回去）。
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AgentSidebar } from './AgentSidebar'
import type { AgentPayload } from '../types'

function agent(over: Partial<AgentPayload> = {}): AgentPayload {
  return {
    agent: 'technical', signal: 'neutral', confidence: 0, time_horizon: '',
    data_completeness: 0, key_findings: {}, narrative_summary: '', errors: [],
    ...over,
  } as AgentPayload
}

const baseProps = {
  agents: {},
  activeAgents: [],
  symbol: null as string | null,
  onSymbolChange: () => {},
  onRunAgent: () => {},
  onRunAll: () => {},
  collapsed: false,
  onToggle: () => {},
}

function mockSearch(results: { symbol: string; name: string }[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve(results) })),
  )
}

beforeEach(() => mockSearch([]))

describe('標的搜尋（debounced）', () => {
  it('輸入後會查詢 /api/symbols/search 並帶上 encode 過的關鍵字', async () => {
    mockSearch([{ symbol: '2330', name: '台積電' }])
    const user = userEvent.setup()
    render(<AgentSidebar {...baseProps} />)

    await user.type(screen.getByPlaceholderText('搜尋標的（代碼／名稱）'), '台積')

    await waitFor(
      () => {
        expect(fetch).toHaveBeenCalled()
        const url = String((fetch as ReturnType<typeof vi.fn>).mock.calls[0][0])
        expect(url).toContain('/api/symbols/search?q=')
        expect(url).toContain(encodeURIComponent('台積'))
      },
      { timeout: 2000 },
    )
  })

  it('debounce 生效：連續輸入不會每個字元都打一次 API', async () => {
    mockSearch([{ symbol: '2330', name: '台積電' }])
    const user = userEvent.setup()
    render(<AgentSidebar {...baseProps} />)

    await user.type(screen.getByPlaceholderText('搜尋標的（代碼／名稱）'), '2330')
    await waitFor(() => expect(fetch).toHaveBeenCalled(), { timeout: 2000 })

    expect((fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBeLessThan(4)
  })

  it('顯示搜尋結果，點選後回傳代碼', async () => {
    mockSearch([{ symbol: '2330', name: '台積電' }])
    const onSymbolChange = vi.fn()
    const user = userEvent.setup()
    render(<AgentSidebar {...baseProps} onSymbolChange={onSymbolChange} />)

    await user.type(screen.getByPlaceholderText('搜尋標的（代碼／名稱）'), '2330')
    const hit = await screen.findByText('台積電', {}, { timeout: 2000 })
    await user.click(hit)

    expect(onSymbolChange).toHaveBeenCalledWith('2330')
  })

  it('清空輸入不打 API（避免空查詢）', async () => {
    const user = userEvent.setup()
    render(<AgentSidebar {...baseProps} />)
    const box = screen.getByPlaceholderText('搜尋標的（代碼／名稱）')

    await user.type(box, 'a')
    await user.clear(box)
    await new Promise(r => setTimeout(r, 400))

    const urls = (fetch as ReturnType<typeof vi.fn>).mock.calls.map(c => String(c[0]))
    expect(urls.every(u => !u.endsWith('q='))).toBe(true)
  })

  it('API 失敗時不崩潰、不顯示結果', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('network down'))))
    const user = userEvent.setup()
    render(<AgentSidebar {...baseProps} />)

    await user.type(screen.getByPlaceholderText('搜尋標的（代碼／名稱）'), '2330')
    await new Promise(r => setTimeout(r, 400))

    expect(screen.getByPlaceholderText('搜尋標的（代碼／名稱）')).toBeInTheDocument()
  })
})

describe('prop → state 同步（本輪從 useEffect 改為 render 期間調整）', () => {
  it('外部 symbol 變動時輸入框跟著更新', async () => {
    const { rerender } = render(<AgentSidebar {...baseProps} symbol="2330" />)
    expect(screen.getByPlaceholderText('搜尋標的（代碼／名稱）')).toHaveValue('2330')

    rerender(<AgentSidebar {...baseProps} symbol="2454" />)
    await waitFor(() =>
      expect(screen.getByPlaceholderText('搜尋標的（代碼／名稱）')).toHaveValue('2454'),
    )
  })

  it('symbol 變成 null 時清空輸入框，不得殘留舊值', async () => {
    const { rerender } = render(<AgentSidebar {...baseProps} symbol="2330" />)
    rerender(<AgentSidebar {...baseProps} symbol={null} />)
    await waitFor(() =>
      expect(screen.getByPlaceholderText('搜尋標的（代碼／名稱）')).toHaveValue(''),
    )
  })

  it('symbol 沒變時不覆蓋使用者正在輸入的內容', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<AgentSidebar {...baseProps} symbol="2330" />)
    const box = screen.getByPlaceholderText('搜尋標的（代碼／名稱）')

    await user.clear(box)
    await user.type(box, '聯發')
    // 同樣的 symbol 重新 render（例如父元件因其他 state 更新）
    rerender(<AgentSidebar {...baseProps} symbol="2330" />)

    expect(box).toHaveValue('聯發')
  })
})

describe('單一 agent 執行的前置條件（Phase 15 移除硬編碼 2330 的產品規則）', () => {
  it('沒有選定標的時，**全部**七個 agent 按鈕都停用', () => {
    render(<AgentSidebar {...baseProps} symbol={null} />)
    const btns = screen.getAllByTitle('請先搜尋並選擇標的')
    expect(btns).toHaveLength(7)
    for (const btn of btns) expect(btn).toBeDisabled()
  })

  it('選定標的後可執行，並帶上該標的', async () => {
    const onRunAgent = vi.fn()
    const user = userEvent.setup()
    render(<AgentSidebar {...baseProps} symbol="2330" onRunAgent={onRunAgent} />)

    await user.click(screen.getByText('技術面'))
    expect(onRunAgent).toHaveBeenCalledWith('technical', '2330')
  })
})

describe('agent 狀態呈現', () => {
  it('七個 domain agent 都列出', () => {
    render(<AgentSidebar {...baseProps} />)
    for (const label of ['技術面', '籌碼面', '基本面', '新聞面', '總經面', '跨市場', '風控']) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
  })

  it('收合時隱藏文字標籤但仍可切換', async () => {
    const onToggle = vi.fn()
    const user = userEvent.setup()
    render(<AgentSidebar {...baseProps} collapsed onToggle={onToggle} />)

    expect(screen.queryByText('技術面')).not.toBeInTheDocument()
    await user.click(screen.getByTitle('展開側欄'))
    expect(onToggle).toHaveBeenCalled()
  })

  it('收合時不渲染搜尋框', () => {
    render(<AgentSidebar {...baseProps} collapsed />)
    expect(screen.queryByPlaceholderText('搜尋標的（代碼／名稱）')).not.toBeInTheDocument()
  })

  it('失敗與完成的 agent 有可區分的狀態（不得看起來一樣）', () => {
    const { container } = render(
      <AgentSidebar
        {...baseProps}
        agents={{
          technical: agent({ loading: false, signal: 'bullish' }),
          news: agent({ agent: 'news', failed: true, loading: false }),
        }}
      />,
    )
    expect(container.querySelector('.bg-green-500')).toBeTruthy()  // 完成且看多
    expect(container.querySelector('.bg-red-500')).toBeTruthy()    // 失敗
  })
})
