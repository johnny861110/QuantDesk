/**
 * 可控的 EventSource 替身 —— 讓測試能逐一送出 SSE 事件並斷言狀態機轉換。
 *
 * useAnalysis 的整個狀態機都由 SSE 事件驅動，而 jsdom 沒有 EventSource，
 * 所以這是測試該 hook 的必要基礎設施。
 */
import { vi } from 'vitest'

export interface MockES {
  url: string
  onmessage: ((e: { data: string }) => void) | null
  onerror: ((e?: unknown) => void) | null
  close: () => void
  closed: boolean
}

/** 目前存活的 mock EventSource 實例（依建立順序）。 */
export const instances: MockES[] = []

/**
 * 安裝 mock EventSource。回傳操作用的 helper。
 * 需在測試內呼叫（setup.ts 預設安裝的是會拋錯的版本）。
 */
export function installMockEventSource() {
  instances.length = 0

  class FakeEventSource implements MockES {
    url: string
    onmessage: ((e: { data: string }) => void) | null = null
    onerror: ((e?: unknown) => void) | null = null
    closed = false

    constructor(url: string) {
      this.url = url
      instances.push(this)
    }

    close() {
      this.closed = true
    }
  }

  vi.stubGlobal('EventSource', FakeEventSource)

  return {
    /** 最新建立的實例（startSSE 每次呼叫都會建一個新的）。 */
    latest(): MockES {
      const es = instances[instances.length - 1]
      if (!es) throw new Error('尚未建立任何 EventSource')
      return es
    },
    /**
     * 送出一個 SSE 訊息事件。
     *
     * 已關閉的連線不派送——真實 EventSource 在 close() 之後不會再收到事件，
     * mock 若比真實情況寬鬆，會讓「忘記關連線」這類 bug 藏起來。
     */
    emit(type: string, payload: unknown) {
      const es = this.latest()
      if (es.closed) return
      es.onmessage?.({ data: JSON.stringify({ type, payload }) })
    },
    /** 送出格式錯誤的原始資料（測試 parse 錯誤處理）。 */
    emitRaw(data: string) {
      const es = this.latest()
      if (es.closed) return
      es.onmessage?.({ data })
    },
    /** 觸發連線錯誤。 */
    fail() {
      const es = this.latest()
      if (es.closed) return
      es.onerror?.()
    },
    instances,
  }
}
