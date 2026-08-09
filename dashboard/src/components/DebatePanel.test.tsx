/**
 * DebatePanel —— Phase 19
 *
 * Bull / Bear / PM 三方論述的呈現。重點在於「部分到達」的中間狀態
 * 不能崩潰——三方是 async 並行執行，SSE 會分別送達，
 * 使用者一定會看到只有 bull 到、bear 還沒到的畫面。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { DebatePanel } from './DebatePanel'
import type { DebatePMPayload, DebatePartyPayload } from '../types'

const bull: DebatePartyPayload = {
  thesis: '外資連續買超，基本面支撐強勁。',
  key_points: ['外資連 5 日買超', 'ROIC 高於 WACC'],
  confidence: 0.72,
}

const bear: DebatePartyPayload = {
  thesis: '評價偏高，融資餘額同步攀升。',
  key_points: ['本益比高於同業'],
  confidence: 0.58,
}

// 不使用 `as DebatePMPayload` —— 初版曾用 cast 略過 key_points，
// 型別系統本來會擋下，卻被 cast 關掉，結果測試跑起來才以 TypeError 爆出。
// 後端 schemas/debate.py 的 key_points 有 default_factory=list，永遠存在。
const pm: DebatePMPayload = {
  signal: 'bullish',
  confidence: 0.65,
  thesis: '多方論據較強，但需注意評價風險。',
  key_points: ['分批建立部位', '設定停損於季線'],
}

describe('顯示條件', () => {
  it('未啟動時不渲染', () => {
    const { container } = render(
      <DebatePanel started={false} bull={null} bear={null} pm={null} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('啟動後即渲染，即使三方都還沒到（使用者要看到「進行中」）', () => {
    const { container } = render(
      <DebatePanel started bull={null} bear={null} pm={null} />,
    )
    expect(container).not.toBeEmptyDOMElement()
  })
})

describe('部分到達的中間狀態（三方 async 並行，必然會出現）', () => {
  it('只有 bull 到達時不崩潰且顯示其論述', () => {
    render(<DebatePanel started bull={bull} bear={null} pm={null} />)
    expect(screen.getByText('外資連續買超，基本面支撐強勁。')).toBeInTheDocument()
  })

  it('只有 bear 到達時不崩潰', () => {
    expect(() =>
      render(<DebatePanel started bull={null} bear={bear} pm={null} />),
    ).not.toThrow()
  })

  it('bull/bear 都到但 PM 未到時不崩潰', () => {
    expect(() =>
      render(<DebatePanel started bull={bull} bear={bear} pm={null} />),
    ).not.toThrow()
  })
})

describe('完整內容', () => {
  it('三方論述都呈現', () => {
    render(<DebatePanel started bull={bull} bear={bear} pm={pm} />)
    expect(screen.getByText('外資連續買超，基本面支撐強勁。')).toBeInTheDocument()
    expect(screen.getByText('評價偏高，融資餘額同步攀升。')).toBeInTheDocument()
    expect(screen.getByText('多方論據較強，但需注意評價風險。')).toBeInTheDocument()
  })

  it('逐條列出 key_points，不得只顯示第一條', () => {
    render(<DebatePanel started bull={bull} bear={bear} pm={pm} />)
    expect(screen.getByText('外資連 5 日買超')).toBeInTheDocument()
    expect(screen.getByText('ROIC 高於 WACC')).toBeInTheDocument()
  })

  it('PM 裁決方向有可見標示', () => {
    render(<DebatePanel started bull={bull} bear={bear} pm={pm} />)
    expect(screen.getByText(/看多/)).toBeInTheDocument()
  })

  it('空 thesis 顯示破折號而非空白', () => {
    render(
      <DebatePanel
        started
        bull={{ thesis: '', key_points: [], confidence: 0 }}
        bear={null}
        pm={null}
      />,
    )
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })
})

describe('容錯', () => {
  it('未知 PM signal 不崩潰（後端新增訊號類型時前端不該爆）', () => {
    expect(() =>
      render(
        <DebatePanel
          started
          bull={bull}
          bear={bear}
          pm={{ ...pm, signal: 'very_bullish' as DebatePMPayload['signal'] }}
        />,
      ),
    ).not.toThrow()
  })

  it('key_points 為空陣列不崩潰', () => {
    expect(() =>
      render(
        <DebatePanel
          started
          bull={{ thesis: '有論述但無條列', key_points: [], confidence: 0.5 }}
          bear={null}
          pm={null}
        />,
      ),
    ).not.toThrow()
  })
})
