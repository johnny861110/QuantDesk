/**
 * Vitest 全域 setup —— Phase 19
 *
 * 兩個目的：
 *   1. 掛上 jest-dom 的 matcher（toBeInTheDocument 等）
 *   2. **結構性保證測試不打真實網路**
 *
 * 第 2 點的理由與後端 conftest.py 的 LANGFUSE_ENABLED 隔離相同：
 * 測試若能對外連線，行為會依環境而異，且會在 CI 上間歇性失敗。
 * 這裡把 fetch 與 EventSource 都預設替換成會拋錯的版本——
 * 需要它們的測試必須明確 mock，忘記 mock 會直接得到清楚的錯誤訊息，
 * 而不是一個 30 秒後的神秘 timeout。
 */
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => {
      throw new Error(
        '[test] 未 mock 的 fetch 呼叫。測試不得打真實網路——' +
          '請在該測試內 vi.stubGlobal("fetch", ...) 明確提供 mock。',
      )
    }),
  )
  vi.stubGlobal(
    'EventSource',
    class {
      constructor() {
        throw new Error(
          '[test] 未 mock 的 EventSource。請使用 src/test/mockEventSource.ts。',
        )
      }
    },
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})
