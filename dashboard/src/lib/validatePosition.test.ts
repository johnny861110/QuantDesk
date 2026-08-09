/**
 * validatePosition —— Phase 19
 *
 * 這些測試的意義不只是「驗證函式正確」，更是**鎖住前後端契約一致**。
 * 每條規則都對應 agents/risk/position_loader.py::_parse_row() 的一項檢查；
 * 若後端契約改變而這裡沒跟上，測試會提醒（見 describe 內的對應註記）。
 */
import { describe, expect, it } from 'vitest'

import {
  isPositionValid,
  validatePosition,
  VALID_CURRENCIES,
  VALID_INSTRUMENT_TYPES,
  type PositionInput,
} from './validatePosition'

const NOW = new Date('2026-08-09T12:00:00')

const validStock: PositionInput = {
  symbol: '2330.TW',
  instrument_type: 'stock',
  quantity: 1000,
  currency: 'TWD',
  multiplier: 1,
}

const validOption: PositionInput = {
  symbol: 'TXO',
  instrument_type: 'option',
  quantity: -5,
  currency: 'TWD',
  multiplier: 50,
  strike: 22500,
  expiry: '2026-09-16',
  option_type: 'call',
  style: 'european',
}

describe('合法輸入', () => {
  it('完整的股票部位通過', () => {
    expect(validatePosition(validStock, NOW)).toEqual({})
    expect(isPositionValid(validStock, NOW)).toBe(true)
  })

  it('完整的選擇權部位通過', () => {
    expect(validatePosition(validOption, NOW)).toEqual({})
  })

  it('entry_price 為選填，缺少不算錯', () => {
    const { ...pos } = validStock
    delete (pos as PositionInput).entry_price
    expect(validatePosition(pos, NOW)).toEqual({})
  })

  it('負數量（空單）合法', () => {
    expect(validatePosition({ ...validStock, quantity: -500 }, NOW)).toEqual({})
  })
})

describe('通用必填欄位（對應後端 _require_str / _require_number）', () => {
  it('缺 symbol', () => {
    expect(validatePosition({ ...validStock, symbol: '' }, NOW)).toHaveProperty('symbol')
  })

  it('symbol 只有空白也算缺', () => {
    expect(validatePosition({ ...validStock, symbol: '   ' }, NOW)).toHaveProperty('symbol')
  })

  it('缺 quantity', () => {
    expect(validatePosition({ ...validStock, quantity: null }, NOW)).toHaveProperty('quantity')
  })

  it('quantity 為 0 應擋下（0 口部位對 Greeks 無貢獻，多半是填錯）', () => {
    expect(validatePosition({ ...validStock, quantity: 0 }, NOW)).toHaveProperty('quantity')
  })

  it('缺 instrument_type', () => {
    expect(validatePosition({ ...validStock, instrument_type: undefined }, NOW))
      .toHaveProperty('instrument_type')
  })

  it('instrument_type 非法值', () => {
    expect(validatePosition({ ...validStock, instrument_type: 'bond' }, NOW))
      .toHaveProperty('instrument_type')
  })

  it('currency 非法值', () => {
    expect(validatePosition({ ...validStock, currency: 'GBP' }, NOW))
      .toHaveProperty('currency')
  })

  it('multiplier 為 0 或負數應擋下', () => {
    expect(validatePosition({ ...validStock, multiplier: 0 }, NOW)).toHaveProperty('multiplier')
    expect(validatePosition({ ...validStock, multiplier: -1 }, NOW)).toHaveProperty('multiplier')
  })
})

describe('選擇權專屬欄位（P2-#9 的核心：這些缺了會讓 risk agent 失敗）', () => {
  it('option 缺 strike —— SESSION_HANDOFF P2-#9 明列的情境', () => {
    expect(validatePosition({ ...validOption, strike: null }, NOW))
      .toHaveProperty('strike')
  })

  it('option 缺 expiry', () => {
    expect(validatePosition({ ...validOption, expiry: null }, NOW))
      .toHaveProperty('expiry')
  })

  it('option 缺 option_type', () => {
    expect(validatePosition({ ...validOption, option_type: null }, NOW))
      .toHaveProperty('option_type')
  })

  it('strike 為 0 或負數應擋下', () => {
    expect(validatePosition({ ...validOption, strike: 0 }, NOW)).toHaveProperty('strike')
    expect(validatePosition({ ...validOption, strike: -100 }, NOW)).toHaveProperty('strike')
  })

  it('股票部位不要求 option 欄位', () => {
    const stockNoStrike = { ...validStock, strike: null, expiry: null, option_type: null }
    expect(validatePosition(stockNoStrike, NOW)).toEqual({})
  })
})

describe('expiry 日期防呆（對應後端 T ≤ 0 guard）', () => {
  it('過去日期擋下', () => {
    expect(validatePosition({ ...validOption, expiry: '2026-01-01' }, NOW))
      .toHaveProperty('expiry')
  })

  it('**今天**也要擋下 —— 後端註解明言當天到期 T=0 同樣是退化情況', () => {
    const errors = validatePosition({ ...validOption, expiry: '2026-08-09' }, NOW)
    expect(errors).toHaveProperty('expiry')
    expect(errors.expiry).toContain('晚於今天')
  })

  it('明天可以', () => {
    expect(validatePosition({ ...validOption, expiry: '2026-08-10' }, NOW)).toEqual({})
  })

  it('非 ISO 格式擋下', () => {
    for (const bad of ['2026/09/16', '09-16-2026', '2026-9-16', 'next month']) {
      expect(validatePosition({ ...validOption, expiry: bad }, NOW)).toHaveProperty('expiry')
    }
  })
})

describe('多重錯誤', () => {
  it('同時回報所有問題，不是只回第一個（使用者才不用逐個試）', () => {
    const errors = validatePosition(
      { symbol: '', instrument_type: 'option', quantity: null },
      NOW,
    )
    expect(Object.keys(errors).sort()).toEqual(
      ['expiry', 'option_type', 'quantity', 'strike', 'symbol'].sort(),
    )
  })
})

describe('前後端契約一致性', () => {
  // 這兩條若失敗，代表前端的合法值清單與 position_loader.py 漂移了
  it('instrument_type 清單與後端 VALID_INSTRUMENT_TYPES 相同', () => {
    expect([...VALID_INSTRUMENT_TYPES].sort()).toEqual(['futures', 'option', 'stock'])
  })

  it('currency 清單與後端 VALID_CURRENCIES 相同', () => {
    expect([...VALID_CURRENCIES].sort()).toEqual(['EUR', 'JPY', 'TWD', 'USD'])
  })
})
