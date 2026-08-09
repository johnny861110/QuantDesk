/**
 * App —— 整合測試（Phase 19）
 *
 * 串起 useAnalysis（SSE）、useQueryHistory（localStorage）與各卡片元件。
 * 重點守護兩件事：
 *   1. 查詢送出的前置條件（空查詢、串流中不得重複送出）
 *   2. **本輪修掉的 render 副作用**——查詢完成後寫入歷史的邏輯
 *      原本在 render body（會 setState + 寫 localStorage），已移進 useEffect。
 *      main.tsx 啟用 StrictMode，這裡也用 StrictMode 包起來測，
 *      確保雙重 render 下歷史不會出現重複項目。
 */
import { StrictMode } from 'react'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import { installMockEventSource } from './test/mockEventSource'

let sse: ReturnType<typeof installMockEventSource>

beforeEach(() => {
  localStorage.clear()
  sse = installMockEventSource()
  // AgentSidebar 的標的搜尋與 PositionsPanel 會用到
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      // /api/positions 回傳 portfolio 形狀；/api/symbols/search 回傳陣列。
      // 初版一律回 [] 導致 PositionsPanel 讀 portfolio_nav.value 時 TypeError。
      const body = String(url).includes('/api/positions')
        ? { portfolio_nav: { value: 5_000_000, currency: 'TWD' }, positions: [] }
        : []
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) })
    }),
  )
})

function renderApp(strict = false) {
  return strict
    ? render(<StrictMode><App /></StrictMode>)
    : render(<App />)
}

const INPUT = '輸入查詢，例如：2330 現在怎樣'

describe('查詢送出', () => {
  it('輸入後按 Enter 啟動分析', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.type(screen.getByPlaceholderText(INPUT), '2330 技術面{Enter}')
    expect(sse.instances).toHaveLength(1)
    expect(sse.latest().url).toContain(encodeURIComponent('2330 技術面'))
  })

  it('空查詢不得送出', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.type(screen.getByPlaceholderText(INPUT), '{Enter}')
    expect(sse.instances).toHaveLength(0)
  })

  it('只有空白的查詢不得送出', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.type(screen.getByPlaceholderText(INPUT), '   {Enter}')
    expect(sse.instances).toHaveLength(0)
  })

  it('串流進行中不得重複送出（避免事件交錯）', async () => {
    const user = userEvent.setup()
    renderApp()
    const box = screen.getByPlaceholderText(INPUT)

    await user.type(box, '2330{Enter}')
    expect(sse.instances).toHaveLength(1)

    await user.type(box, '{Enter}')
    expect(sse.instances).toHaveLength(1)
  })
})

describe('查詢歷史（守護本輪修掉的 render 副作用）', () => {
  async function runOneQuery(strict: boolean) {
    const user = userEvent.setup()
    renderApp(strict)
    await user.type(screen.getByPlaceholderText(INPUT), '2330 技術面{Enter}')
    await user.click(document.body) // blur，避免輸入框吃掉後續事件

    act(() => sse.emit('agent_done', { agent: 'technical', signal: 'bullish', confidence: 0.8 }))
    act(() => sse.emit('done', {}))
    return user
  }

  it('分析完成後寫入歷史', async () => {
    await runOneQuery(false)
    await waitFor(() => {
      const raw = localStorage.getItem('quantdesk_history')
      expect(raw).toBeTruthy()
      expect(JSON.parse(raw!)).toHaveLength(1)
    })
  })

  it('StrictMode 雙重 render 下歷史不得出現重複項目', async () => {
    await runOneQuery(true)
    await waitFor(() => {
      const raw = localStorage.getItem('quantdesk_history')
      expect(raw).toBeTruthy()
      const entries = JSON.parse(raw!) as { query: string }[]
      expect(entries.filter(e => e.query === '2330 技術面')).toHaveLength(1)
    })
  })

  it('分析尚未完成時不得寫入歷史', async () => {
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByPlaceholderText(INPUT), '2330{Enter}')
    act(() => sse.emit('agent_done', { agent: 'technical', signal: 'bullish' }))

    // 沒有 done 事件 → 不該記錄
    expect(localStorage.getItem('quantdesk_history')).toBeFalsy()
  })
})

describe('串流狀態呈現', () => {
  it('agent 完成後顯示對應卡片', async () => {
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByPlaceholderText(INPUT), '2330{Enter}')

    act(() => sse.emit('agent_done', {
      agent: 'technical', signal: 'bullish', confidence: 0.8,
      key_findings: {}, narrative_summary: '均線多頭排列。', errors: [],
    }))

    expect(await screen.findByText('均線多頭排列。')).toBeInTheDocument()
  })

  it('agent 失敗時顯示錯誤而非靜默消失', async () => {
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByPlaceholderText(INPUT), '2330{Enter}')

    act(() => sse.emit('agent_error', { agent: 'news', error: 'RSS 逾時' }))
    // 錯誤同時出現在頂部狀態列與該 agent 的卡片內——兩處都該有，
    // 故用 findAllByText（初版用 findByText 要求唯一，反而是錯的斷言）。
    const hits = await screen.findAllByText(/RSS 逾時/)
    expect(hits.length).toBeGreaterThanOrEqual(2)
  })

  it('連線中斷顯示可理解的訊息', async () => {
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByPlaceholderText(INPUT), '2330{Enter}')

    act(() => sse.fail())
    expect(await screen.findByText(/連線中斷/)).toBeInTheDocument()
  })
})

describe('清除', () => {
  it('清除後移除已顯示的結果', async () => {
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByPlaceholderText(INPUT), '2330{Enter}')
    act(() => sse.emit('agent_done', {
      agent: 'technical', signal: 'bullish', confidence: 0.8,
      key_findings: {}, narrative_summary: '均線多頭排列。', errors: [],
    }))
    await screen.findByText('均線多頭排列。')

    await user.click(screen.getByText('清除'))
    await waitFor(() =>
      expect(screen.queryByText('均線多頭排列。')).not.toBeInTheDocument(),
    )
  })
})

describe('範例查詢', () => {
  it('點擊範例可直接帶入輸入框', async () => {
    const user = userEvent.setup()
    renderApp()

    const example = screen.getByText('2330 現在怎樣')
    await user.click(example)

    await waitFor(() =>
      expect(screen.getByPlaceholderText(INPUT)).toHaveValue('2330 現在怎樣'),
    )
  })
})

describe('持倉面板', () => {
  it('可開啟持倉管理', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.click(screen.getByTitle('管理持倉'))
    await waitFor(() => {
      expect(within(document.body).getByText(/新增部位/)).toBeInTheDocument()
    })
  })
})
