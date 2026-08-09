/**
 * useAnalysis —— SSE 狀態機測試（Phase 19）
 *
 * 為什麼優先測這支：dashboard 2,474 行 TSX 中，這裡是唯一的狀態機，
 * 所有 agent 卡片、進度條、Supervisor 面板的資料都由它產出。
 * 它壞掉 = 整個畫面壞掉，而先前完全沒有測試守護。
 *
 * 全部透過 mock EventSource 驅動真實的 hook，不打任何網路。
 */
import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'

import { useAnalysis } from './useAnalysis'
import { installMockEventSource } from '../test/mockEventSource'

let sse: ReturnType<typeof installMockEventSource>

beforeEach(() => {
  sse = installMockEventSource()
})

function startAnalysis() {
  const hook = renderHook(() => useAnalysis())
  act(() => hook.result.current.analyze('2330 技術面'))
  return hook
}

describe('初始狀態與啟動', () => {
  it('初始為 idle，無 agent', () => {
    const { result } = renderHook(() => useAnalysis())
    expect(result.current.state.status).toBe('idle')
    expect(result.current.state.agentOrder).toEqual([])
  })

  it('analyze() 進入 streaming 並開啟 SSE', () => {
    const { result } = startAnalysis()
    expect(result.current.state.status).toBe('streaming')
    expect(sse.instances).toHaveLength(1)
  })

  it('query 有做 URL encode（中文與空白不得壞掉）', () => {
    const { result } = renderHook(() => useAnalysis())
    act(() => result.current.analyze('台積電 值得買嗎?'))
    const url = sse.latest().url
    expect(url).toContain('/api/analyze/stream?query=')
    expect(url).not.toContain(' ')
    expect(url).toContain(encodeURIComponent('台積電 值得買嗎?'))
  })

  it('analyzeAgent() 打單一 agent endpoint 並帶 market', () => {
    const { result } = renderHook(() => useAnalysis())
    act(() => result.current.analyzeAgent('technical', '2330'))
    expect(sse.latest().url).toBe('/api/agent/technical?symbol=2330&market=TW')
  })

  it('重新 analyze 會關閉前一條連線（避免事件交錯）', () => {
    const { result } = startAnalysis()
    const first = sse.latest()
    act(() => result.current.analyze('2454 技術面'))
    expect(first.closed).toBe(true)
    expect(sse.instances).toHaveLength(2)
  })
})

describe('agent 生命週期', () => {
  it('agent_start 建立 loading 佔位並記錄順序', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('agent_start', { agent: 'technical' }))

    expect(result.current.state.agentOrder).toEqual(['technical'])
    expect(result.current.state.agents.technical.loading).toBe(true)
  })

  it('agent_done 覆寫佔位並清除 loading', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('agent_start', { agent: 'technical' }))
    act(() =>
      sse.emit('agent_done', {
        agent: 'technical',
        signal: 'bullish',
        confidence: 0.8,
        key_findings: { rsi: 62 },
      }),
    )

    const a = result.current.state.agents.technical
    expect(a.loading).toBe(false)
    expect(a.signal).toBe('bullish')
    expect(a.key_findings).toEqual({ rsi: 62 })
    expect(a.receivedAt).toBeTypeOf('number')
  })

  it('agent_error 標記 failed 並保留錯誤訊息', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('agent_error', { agent: 'news', error: 'RSS timeout' }))

    const a = result.current.state.agents.news
    expect(a.failed).toBe(true)
    expect(a.loading).toBe(false)
    expect(a.errors).toEqual(['RSS timeout'])
  })

  it('agent_error 缺 error 欄位時有預設訊息，不得顯示 undefined', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('agent_error', { agent: 'news' }))
    expect(result.current.state.agents.news.errors).toEqual(['未知錯誤'])
  })

  it('agentOrder 不重複，且保留首次出現順序', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('agent_start', { agent: 'technical' }))
    act(() => sse.emit('agent_start', { agent: 'chip' }))
    act(() => sse.emit('agent_done', { agent: 'technical', signal: 'bullish' }))
    act(() => sse.emit('agent_start', { agent: 'technical' }))

    expect(result.current.state.agentOrder).toEqual(['technical', 'chip'])
  })

  it('agent_error 也會把未見過的 agent 加進順序（否則卡片不會顯示）', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('agent_error', { agent: 'macro', error: 'boom' }))
    expect(result.current.state.agentOrder).toEqual(['macro'])
  })
})

describe('router / debate / supervisor', () => {
  it('router 事件寫入 state', () => {
    const { result } = startAnalysis()
    act(() =>
      sse.emit('router', {
        scenario: 'single_stock',
        targets: ['2330'],
        query_type: 'stock_analysis',
        agents: ['technical', 'chip', 'news'],
      }),
    )
    expect(result.current.state.router?.query_type).toBe('stock_analysis')
    expect(result.current.state.currentEvent).toContain('2330')
  })

  it('router targets 為空時不顯示 undefined', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('router', { scenario: 'single_stock', targets: [] }))
    expect(result.current.state.currentEvent).not.toContain('undefined')
  })

  it('debate 三方各自寫入且互不覆蓋', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('debate_start', {}))
    act(() => sse.emit('debate_bull', { thesis: '多方' }))
    act(() => sse.emit('debate_bear', { thesis: '空方' }))
    act(() => sse.emit('debate_pm', { signal: 'bullish', thesis: 'PM' }))

    const d = result.current.state.debate
    expect(d.started).toBe(true)
    expect(d.bull?.thesis).toBe('多方')
    expect(d.bear?.thesis).toBe('空方')
    expect(d.pm?.thesis).toBe('PM')
  })

  it('supervisor 事件寫入並格式化信心百分比', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('supervisor', { signal: 'bullish', confidence: 0.725 }))
    expect(result.current.state.supervisor?.signal).toBe('bullish')
    expect(result.current.state.currentEvent).toContain('73%')
  })

  it('fundamental_crawl 進度不覆蓋既有 agent 資料', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('agent_done', { agent: 'chip', signal: 'bullish' }))
    act(() => sse.emit('fundamental_crawl', { stage: '下載', status: '進行中' }))

    expect(result.current.state.agents.chip.signal).toBe('bullish')
    expect(result.current.state.currentEvent).toContain('下載')
  })
})

describe('終止與錯誤處理', () => {
  it('done 事件結束串流並記錄耗時', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('done', {}))

    expect(result.current.state.status).toBe('done')
    expect(result.current.state.elapsedMs).toBeTypeOf('number')
    expect(sse.instances[0].closed).toBe(true)
  })

  it('error 事件寫入訊息並關閉連線', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('error', { message: '後端爆炸' }))

    expect(result.current.state.status).toBe('error')
    expect(result.current.state.error).toBe('後端爆炸')
    expect(sse.instances[0].closed).toBe(true)
  })

  it('連線中斷（onerror）顯示可理解的訊息而非空白', () => {
    const { result } = startAnalysis()
    act(() => sse.fail())

    expect(result.current.state.status).toBe('error')
    expect(result.current.state.error).toContain('連線中斷')
  })

  it('格式錯誤的 SSE 資料被忽略，不得讓整個 hook 崩潰', () => {
    const { result } = startAnalysis()
    act(() => sse.emitRaw('{ 這不是 json'))

    expect(result.current.state.status).toBe('streaming')
    expect(result.current.state.error).toBeFalsy()
  })

  it('未知事件類型被安全忽略（後端新增事件時前端不該爆）', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('agent_start', { agent: 'technical' }))
    act(() => sse.emit('some_future_event', { whatever: 1 }))

    expect(result.current.state.status).toBe('streaming')
    expect(result.current.state.agentOrder).toEqual(['technical'])
  })

  it('done 之後的事件不再改動狀態（連線已關閉）', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('done', {}))
    const after = result.current.state
    act(() => sse.emit('agent_start', { agent: 'chip' }))

    expect(result.current.state.status).toBe('done')
    expect(result.current.state.agentOrder).toEqual(after.agentOrder)
  })
})

describe('reset / retry', () => {
  it('reset 清空狀態並關閉連線', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('agent_done', { agent: 'technical', signal: 'bullish' }))
    act(() => result.current.reset())

    expect(result.current.state.status).toBe('idle')
    expect(result.current.state.agentOrder).toEqual([])
    expect(sse.instances[0].closed).toBe(true)
  })

  it('retry 用同一個 URL 重開連線', () => {
    const { result } = renderHook(() => useAnalysis())
    act(() => result.current.analyze('2330 技術面'))
    const url = sse.latest().url
    act(() => sse.fail())
    act(() => result.current.retry())

    expect(sse.instances).toHaveLength(2)
    expect(sse.latest().url).toBe(url)
    expect(result.current.state.status).toBe('streaming')
  })

  it('retry 會沿用 analyzeAgent 的 URL，不會退回一般查詢', () => {
    const { result } = renderHook(() => useAnalysis())
    act(() => result.current.analyzeAgent('risk', '2330'))
    act(() => result.current.retry())

    expect(sse.latest().url).toContain('/api/agent/risk')
  })

  it('沒跑過任何查詢時 retry 不得開連線', () => {
    const { result } = renderHook(() => useAnalysis())
    act(() => result.current.retry())
    expect(sse.instances).toHaveLength(0)
  })

  it('retry 會清掉前一次的 agent 結果（避免新舊混雜）', () => {
    const { result } = startAnalysis()
    act(() => sse.emit('agent_done', { agent: 'technical', signal: 'bullish' }))
    act(() => result.current.retry())

    expect(result.current.state.agentOrder).toEqual([])
    expect(result.current.state.status).toBe('streaming')
  })
})
