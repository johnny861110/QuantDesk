/**
 * PositionsPanel —— 驗證接線的整合測試（Phase 19）
 *
 * validatePosition.test.ts 已驗證純函式邏輯正確。
 * 本檔驗證的是**它真的接上 UI** —— 純函式正確不代表使用者受到保護，
 * 「寫好驗證但忘了在送出時呼叫」是很常見的疏漏。
 *
 * 核心情境即 SESSION_HANDOFF §七 P2-#9：
 * option 沒填 strike 也能存進 positions.yaml，等 risk agent 跑起來才在後端失敗。
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { PositionsPanel } from './PositionsPanel'

const EMPTY_PORTFOLIO = {
  portfolio_nav: { value: 5_000_000, currency: 'TWD' },
  positions: [],
}

let putBodies: string[] = []

function mockApi() {
  putBodies = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === 'PUT') {
        putBodies.push(String(init.body))
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(EMPTY_PORTFOLIO),
      })
    }),
  )
}

beforeEach(mockApi)

/** 開啟「新增部位」表單並切換成選擇權。 */
async function openOptionForm() {
  const user = userEvent.setup()
  render(<PositionsPanel onClose={() => {}} />)

  await screen.findByText('+ 新增部位')
  await user.click(screen.getByText('+ 新增部位'))

  await user.selectOptions(screen.getByLabelText('商品類型'), 'option')
  return user
}

describe('選擇權欄位驗證接線（P2-#9）', () => {
  it('缺 strike 時按儲存不得送出 PUT', async () => {
    const user = await openOptionForm()

    await user.click(screen.getByText('儲存'))

    await waitFor(() => {
      expect(screen.getAllByRole('alert').length).toBeGreaterThan(0)
    })
    expect(putBodies).toHaveLength(0)
  })

  it('缺 strike 時顯示可讀的錯誤訊息，而非靜默失敗', async () => {
    const user = await openOptionForm()
    await user.click(screen.getByText('儲存'))

    const alerts = await screen.findAllByRole('alert')
    const texts = alerts.map(a => a.textContent).join(' | ')
    expect(texts).toContain('選擇權必填')
  })

  it('打開表單時不立刻顯示錯誤（避免一開啟就滿版紅字）', async () => {
    await openOptionForm()
    expect(screen.queryAllByRole('alert')).toHaveLength(0)
  })

  it('錯誤欄位標記 aria-invalid，輔助技術可辨識', async () => {
    const user = await openOptionForm()
    await user.click(screen.getByText('儲存'))

    await waitFor(() => {
      const strike = screen.getByLabelText('履約價')
      expect(strike).toHaveAttribute('aria-invalid', 'true')
    })
  })
})

describe('一般必填欄位', () => {
  it('空白代碼擋下送出', async () => {
    const user = userEvent.setup()
    render(<PositionsPanel onClose={() => {}} />)
    await screen.findByText('+ 新增部位')
    await user.click(screen.getByText('+ 新增部位'))

    // 預設 instrument_type=stock、symbol 為空、quantity 為 0
    await user.click(screen.getByText('儲存'))

    await waitFor(() => {
      expect(screen.getAllByRole('alert').length).toBeGreaterThan(0)
    })
    expect(putBodies).toHaveLength(0)
  })

  it('填妥必填欄位後可成功送出 PUT', async () => {
    const user = userEvent.setup()
    render(<PositionsPanel onClose={() => {}} />)
    await screen.findByText('+ 新增部位')
    await user.click(screen.getByText('+ 新增部位'))

    await user.type(screen.getByLabelText('代碼（如 2330.TW / TXFF）'), '2330.TW')
    const qty = screen.getByLabelText('口數（正=多，負=空）')
    await user.clear(qty)
    await user.type(qty, '1000')

    await user.click(screen.getByText('儲存'))

    await waitFor(() => expect(putBodies).toHaveLength(1))
    expect(putBodies[0]).toContain('2330.TW')
  })
})
