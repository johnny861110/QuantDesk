/**
 * 持倉欄位驗證 —— Phase 19（修 SESSION_HANDOFF §七 P2-#9）
 *
 * 問題
 * ----
 * PositionsPanel 的「儲存」按鈕原本無條件呼叫 onSave()，完全沒有驗證。
 * 於是 option 沒填 strike / expiry 也能存進 positions.yaml，
 * 等到 risk agent 跑起來才在後端 position_loader 報錯——
 * 使用者要到分析失敗才知道自己填錯了，而且錯誤訊息在後端。
 *
 * 設計原則：**鏡射後端契約，不自己發明規則**
 * ------------------------------------------
 * 下列規則逐條對應 agents/risk/position_loader.py::_parse_row()：
 *   · 通用必填    symbol / instrument_type / quantity
 *   · 列舉值      instrument_type、currency、option_type、style
 *   · option 必填 strike / expiry / option_type
 *   · expiry 防呆 必須晚於今天（後端 T ≤ 0 guard：當天到期 T=0 也是退化情況）
 *
 * 前端驗證是**體驗改善**而非安全邊界——後端仍會獨立驗證一次。
 * 若兩邊規則漂移，以後端為準並回頭修正此檔。
 */

export const VALID_INSTRUMENT_TYPES = ['stock', 'futures', 'option'] as const
export const VALID_CURRENCIES = ['TWD', 'USD', 'EUR', 'JPY'] as const
export const VALID_OPTION_TYPES = ['call', 'put'] as const
export const VALID_STYLES = ['european', 'american'] as const

export interface PositionInput {
  symbol?: string
  instrument_type?: string
  quantity?: number | null
  currency?: string
  multiplier?: number | null
  entry_price?: number | null
  strike?: number | null
  expiry?: string | null
  option_type?: string | null
  style?: string | null
}

/** 欄位名 → 錯誤訊息。空物件代表通過。 */
export type ValidationErrors = Record<string, string>

/** 今天（本地時區）的 ISO 日期字串，供 expiry 比較用。 */
function todayISO(now: Date = new Date()): string {
  const y = now.getFullYear()
  const m = String(now.getMonth() + 1).padStart(2, '0')
  const d = String(now.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

export function validatePosition(
  pos: PositionInput,
  now: Date = new Date(),
): ValidationErrors {
  const errors: ValidationErrors = {}

  // ── 通用必填 ──────────────────────────────────────────────────────────
  if (!pos.symbol || !pos.symbol.trim()) {
    errors.symbol = '必填'
  }

  if (!pos.instrument_type) {
    errors.instrument_type = '必填'
  } else if (!(VALID_INSTRUMENT_TYPES as readonly string[]).includes(pos.instrument_type)) {
    errors.instrument_type = `必須是 ${VALID_INSTRUMENT_TYPES.join(' / ')}`
  }

  if (pos.quantity === null || pos.quantity === undefined || Number.isNaN(pos.quantity)) {
    errors.quantity = '必填'
  } else if (pos.quantity === 0) {
    // 後端不擋 0，但 0 口的部位對 Greeks 無貢獻，多半是使用者填錯
    errors.quantity = '不可為 0（正=多，負=空）'
  }

  if (pos.currency && !(VALID_CURRENCIES as readonly string[]).includes(pos.currency)) {
    errors.currency = `必須是 ${VALID_CURRENCIES.join(' / ')}`
  }

  if (
    pos.multiplier !== null &&
    pos.multiplier !== undefined &&
    !Number.isNaN(pos.multiplier) &&
    pos.multiplier <= 0
  ) {
    errors.multiplier = '必須大於 0'
  }

  // ── option 專屬 ───────────────────────────────────────────────────────
  if (pos.instrument_type === 'option') {
    if (pos.strike === null || pos.strike === undefined || Number.isNaN(pos.strike)) {
      errors.strike = '選擇權必填'
    } else if (pos.strike <= 0) {
      errors.strike = '必須大於 0'
    }

    if (!pos.expiry) {
      errors.expiry = '選擇權必填'
    } else if (!ISO_DATE.test(pos.expiry)) {
      errors.expiry = '格式須為 YYYY-MM-DD'
    } else if (pos.expiry <= todayISO(now)) {
      // 對應後端 T ≤ 0 guard：當天到期 T=0 也是退化情況
      errors.expiry = '必須晚於今天（已到期部位無法計算 Greeks）'
    }

    if (!pos.option_type) {
      errors.option_type = '選擇權必填'
    } else if (!(VALID_OPTION_TYPES as readonly string[]).includes(pos.option_type)) {
      errors.option_type = `必須是 ${VALID_OPTION_TYPES.join(' / ')}`
    }

    if (pos.style && !(VALID_STYLES as readonly string[]).includes(pos.style)) {
      errors.style = `必須是 ${VALID_STYLES.join(' / ')}`
    }
  }

  return errors
}

/** 便利函式：是否可送出。 */
export function isPositionValid(pos: PositionInput, now: Date = new Date()): boolean {
  return Object.keys(validatePosition(pos, now)).length === 0
}
