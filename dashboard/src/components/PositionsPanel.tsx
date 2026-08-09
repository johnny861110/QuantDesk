/**
 * PositionsPanel — interactive positions editor
 *
 * GET /api/positions  → load current portfolio
 * PUT /api/positions  → save changes back to positions.yaml
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import { validatePosition } from '../lib/validatePosition'

// ─── Types ────────────────────────────────────────────────────────────────────

interface PortfolioNav {
  value: number
  currency: string
}

interface Position {
  symbol: string
  instrument_type: 'stock' | 'futures' | 'option'
  quantity: number
  currency: string
  multiplier: number
  entry_price?: number | null
  // option-only
  strike?: number | null
  expiry?: string | null
  option_type?: 'call' | 'put' | null
  style?: 'european' | 'american' | null
}

interface PortfolioData {
  portfolio_nav: PortfolioNav
  positions: Position[]
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

const EMPTY_POSITION: Position = {
  symbol: '',
  instrument_type: 'stock',
  quantity: 0,
  currency: 'TWD',
  multiplier: 1,
  entry_price: null,
}

const INSTRUMENT_ICONS: Record<string, string> = {
  stock: '📈',
  futures: '📊',
  option: '⚙️',
}

function PositionRow({
  pos,
  onEdit,
  onDelete,
}: {
  pos: Position
  onEdit: () => void
  onDelete: () => void
}) {
  const isOption = pos.instrument_type === 'option'
  const qty = pos.quantity
  const qtyColor = qty > 0 ? 'text-green-400' : 'text-red-400'

  return (
    <tr className="border-b border-gray-800 hover:bg-gray-800/30 transition-colors">
      <td className="px-3 py-2 text-xs">
        <div className="flex items-center gap-1.5">
          <span>{INSTRUMENT_ICONS[pos.instrument_type] ?? '❓'}</span>
          <span className="font-mono font-bold text-white">{pos.symbol}</span>
        </div>
        <div className="text-[10px] text-gray-600 capitalize">{pos.instrument_type}</div>
      </td>
      <td className={`px-3 py-2 text-xs font-mono font-bold ${qtyColor}`}>
        {qty > 0 ? `+${qty}` : qty}
      </td>
      <td className="px-3 py-2 text-xs text-gray-400 font-mono">
        {pos.entry_price != null ? pos.entry_price.toLocaleString() : '—'}
      </td>
      <td className="px-3 py-2 text-xs text-gray-500">{pos.currency}</td>
      {isOption && (
        <td className="px-3 py-2 text-xs text-gray-400">
          <div>{pos.strike?.toLocaleString()} {pos.option_type?.toUpperCase()}</div>
          <div className="text-[10px] text-gray-600">{pos.expiry}</div>
        </td>
      )}
      {!isOption && <td className="px-3 py-2" />}
      <td className="px-3 py-2">
        <div className="flex gap-1">
          <button
            onClick={onEdit}
            className="rounded px-2 py-0.5 text-[10px] text-blue-400 border border-blue-800/60 hover:bg-blue-900/30 transition-colors"
          >
            編輯
          </button>
          <button
            onClick={onDelete}
            className="rounded px-2 py-0.5 text-[10px] text-red-400 border border-red-800/60 hover:bg-red-900/30 transition-colors"
          >
            刪除
          </button>
        </div>
      </td>
    </tr>
  )
}

// ─── Position Form Modal ──────────────────────────────────────────────────────

function PositionModal({
  initial,
  onSave,
  onClose,
}: {
  initial: Position
  onSave: (pos: Position) => void
  onClose: () => void
}) {
  const [pos, setPos] = useState<Position>({ ...initial })
  // 是否已嘗試送出——未送出前不顯示錯誤，避免使用者剛打開就滿screen紅字
  const [attempted, setAttempted] = useState(false)

  const errors = validatePosition(pos)
  const showError = (key: string) => (attempted ? errors[key] : undefined)

  const field = (
    key: keyof Position,
    label: string,
    type: 'text' | 'number' | 'date' = 'text',
    required = true,
  ) => {
    const err = showError(key)
    return (
      <div>
        <label className="block text-[11px] text-gray-500 mb-0.5">{label}{required && ' *'}</label>
        <input
          type={type}
          aria-label={label}
          aria-invalid={err ? true : undefined}
          value={(pos[key] as string | number | null | undefined) ?? ''}
          onChange={e => setPos(p => ({
            ...p,
            [key]: type === 'number' ? (e.target.value === '' ? null : Number(e.target.value)) : e.target.value,
          }))}
          className={`w-full rounded border bg-gray-900 px-2 py-1.5 text-xs text-white outline-none ${
            err ? 'border-red-600 focus:border-red-500' : 'border-gray-700 focus:border-blue-500'
          }`}
        />
        {err && <p role="alert" className="mt-0.5 text-[10px] text-red-400">{err}</p>}
      </div>
    )
  }

  const select = <K extends keyof Position>(key: K, label: string, options: string[]) => (
    <div>
      <label className="block text-[11px] text-gray-500 mb-0.5">{label} *</label>
      <select
        aria-label={label}
        value={(pos[key] as string) ?? ''}
        onChange={e => setPos(p => ({ ...p, [key]: e.target.value as Position[K] }))}
        className="w-full rounded border border-gray-700 bg-gray-900 px-2 py-1.5 text-xs text-white outline-none focus:border-blue-500"
      >
        {options.map(o => <option key={o} value={o}>{o}</option>)}
      </select>
    </div>
  )

  const isOption = pos.instrument_type === 'option'

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-xl border border-gray-700 bg-gray-900 p-5 shadow-2xl mx-4">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-bold text-white">
            {initial.symbol ? '編輯部位' : '新增部位'}
          </h2>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300 text-lg leading-none">×</button>
        </div>

        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            {field('symbol', '代碼（如 2330.TW / TXFF）')}
            {select('instrument_type', '商品類型', ['stock', 'futures', 'option'])}
          </div>
          <div className="grid grid-cols-2 gap-3">
            {field('quantity', '口數（正=多，負=空）', 'number')}
            {field('multiplier', '乘數（stock=1, TXO=50, TXFF=200）', 'number')}
          </div>
          <div className="grid grid-cols-2 gap-3">
            {select('currency', '幣別', ['TWD', 'USD', 'EUR', 'JPY'])}
            {field('entry_price', '成本價（選填）', 'number', false)}
          </div>

          {isOption && (
            <>
              <div className="border-t border-gray-800 pt-3">
                <p className="text-[10px] text-gray-600 mb-2 uppercase tracking-wider">選擇權欄位</p>
                <div className="grid grid-cols-2 gap-3">
                  {field('strike', '履約價', 'number')}
                  {field('expiry', '到期日', 'date')}
                </div>
                <div className="grid grid-cols-2 gap-3 mt-3">
                  {select('option_type', '買賣權', ['call', 'put'])}
                  {select('style', '行使方式', ['european', 'american'])}
                </div>
              </div>
            </>
          )}
        </div>

        <div className="mt-5 flex gap-2 justify-end">
          <button
            onClick={onClose}
            className="rounded-lg border border-gray-700 px-4 py-2 text-xs text-gray-400 hover:bg-gray-800 transition-colors"
          >
            取消
          </button>
          <button
            onClick={() => {
              // 先標記已嘗試送出，讓錯誤顯示出來；有錯就不送。
              // 修復前這裡是無條件 onSave(pos)，於是 option 缺 strike 也能存進
              // positions.yaml，等到 risk agent 跑起來才在後端報錯——
              // 使用者要到分析失敗才知道填錯了（SESSION_HANDOFF §七 P2-#9）。
              setAttempted(true)
              if (Object.keys(errors).length === 0) onSave(pos)
            }}
            className="rounded-lg bg-blue-600 px-4 py-2 text-xs font-bold text-white hover:bg-blue-500 transition-colors"
          >
            儲存
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Main Panel ───────────────────────────────────────────────────────────────

interface Props {
  onClose: () => void
}

export function PositionsPanel({ onClose }: Props) {
  const [data, setData] = useState<PortfolioData | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState('')
  const [editIndex, setEditIndex] = useState<number | null>(null)  // -1 = new
  const [showModal, setShowModal] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const resp = await fetch('/api/positions')
      const json = await resp.json() as PortfolioData
      setData(json)
    } catch {
      setData({ portfolio_nav: { value: 1000000, currency: 'TWD' }, positions: [] })
    } finally {
      setLoading(false)
    }
  }, [])

  // 掛載時載入持倉——這是 effect 的正當用途（向外部系統取資料）。
  // react-hooks/set-state-in-effect 之所以報錯，是因為 load() 會同步呼叫
  // setLoading(true) 才進入 await。為了消除告警而把 loading 狀態延後，
  // 只會讓 UI 短暫顯示空面板，是為了取悅 linter 而犧牲使用者體驗。
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { load() }, [load])

  // Serializes all writes onto one promise chain so overlapping saves land on
  // the server in submission order — otherwise two in-flight PUTs (each a
  // full-document overwrite) can resolve out of order and the earlier one's
  // stale payload silently clobbers the later one's.
  const saveChain = useRef<Promise<void>>(Promise.resolve())

  const queueSave = useCallback((d: PortfolioData) => {
    setSaving(true)
    setSaveMsg('')
    saveChain.current = saveChain.current
      .catch(() => {})
      .then(async () => {
        const resp = await fetch('/api/positions', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(d),
        })
        if (!resp.ok) throw new Error(await resp.text())
        setSaveMsg('✓ 已儲存')
        setTimeout(() => setSaveMsg(''), 2000)
      })
      .catch(() => setSaveMsg('✗ 儲存失敗'))
      .finally(() => setSaving(false))
  }, [])

  const handleSavePosition = (pos: Position) => {
    setData(prev => {
      if (!prev) return prev
      const positions = [...prev.positions]
      if (editIndex === -1) {
        positions.push(pos)
      } else if (editIndex !== null) {
        positions[editIndex] = pos
      }
      const next = { ...prev, positions }
      queueSave(next)
      return next
    })
    setShowModal(false)
    setEditIndex(null)
  }

  const handleDelete = (i: number) => {
    setData(prev => {
      if (!prev) return prev
      const positions = prev.positions.filter((_, idx) => idx !== i)
      const next = { ...prev, positions }
      queueSave(next)
      return next
    })
  }

  // NAV is a free-typed number input — debounce so each keystroke doesn't
  // fire its own full-document PUT.
  const navDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const handleNavChange = (value: number) => {
    setData(prev => (prev ? { ...prev, portfolio_nav: { ...prev.portfolio_nav, value } } : prev))
    if (navDebounceRef.current) clearTimeout(navDebounceRef.current)
    navDebounceRef.current = setTimeout(() => {
      setData(prev => {
        if (prev) queueSave(prev)
        return prev
      })
    }, 500)
  }

  const handleNavCurrencyChange = (currency: string) => {
    setData(prev => {
      if (!prev) return prev
      const next = { ...prev, portfolio_nav: { ...prev.portfolio_nav, currency } }
      queueSave(next)
      return next
    })
  }

  return (
    <>
      <div className="fixed inset-0 z-40 flex items-start justify-end pt-16 pr-4">
        <div className="w-full max-w-2xl rounded-xl border border-gray-700 bg-gray-950 shadow-2xl overflow-hidden">
          {/* Header */}
          <div className="flex items-center justify-between border-b border-gray-800 px-4 py-3">
            <div className="flex items-center gap-2">
              <span className="text-lg">🛡️</span>
              <span className="text-sm font-bold text-white">持倉管理</span>
              {saveMsg && (
                <span className={`text-xs ${saveMsg.startsWith('✓') ? 'text-green-400' : 'text-red-400'}`}>
                  {saveMsg}
                </span>
              )}
            </div>
            <button onClick={onClose} className="text-gray-500 hover:text-gray-300 text-lg">×</button>
          </div>

          {/* Portfolio NAV */}
          {data && (
            <div className="flex items-center gap-3 border-b border-gray-800 px-4 py-2.5 bg-gray-900/40">
              <span className="text-xs text-gray-500">組合 NAV</span>
              <input
                type="number"
                value={data.portfolio_nav.value}
                onChange={e => handleNavChange(Number(e.target.value))}
                className="w-36 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-white font-mono outline-none focus:border-blue-500"
              />
              <span className="text-xs text-gray-600">{data.portfolio_nav.currency}</span>
              <select
                value={data.portfolio_nav.currency}
                onChange={e => handleNavCurrencyChange(e.target.value)}
                className="rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-white outline-none focus:border-blue-500"
              >
                {['TWD', 'USD'].map(c => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
          )}

          {/* Positions table */}
          <div className="overflow-y-auto max-h-96">
            {loading ? (
              <div className="flex items-center justify-center py-10 text-xs text-gray-600">載入中...</div>
            ) : !data?.positions?.length ? (
              <div className="flex items-center justify-center py-10 text-xs text-gray-600">尚無持倉</div>
            ) : (
              <table className="w-full">
                <thead>
                  <tr className="border-b border-gray-800 text-[10px] uppercase tracking-wider text-gray-600">
                    <th className="px-3 py-2 text-left">標的</th>
                    <th className="px-3 py-2 text-left">口數</th>
                    <th className="px-3 py-2 text-left">成本</th>
                    <th className="px-3 py-2 text-left">幣別</th>
                    <th className="px-3 py-2 text-left">選擇權</th>
                    <th className="px-3 py-2 text-left">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {data.positions.map((pos, i) => (
                    <PositionRow
                      key={i}
                      pos={pos}
                      onEdit={() => { setEditIndex(i); setShowModal(true) }}
                      onDelete={() => handleDelete(i)}
                    />
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between border-t border-gray-800 px-4 py-3">
            <button
              onClick={() => { setEditIndex(-1); setShowModal(true) }}
              className="rounded-lg border border-blue-700 bg-blue-900/30 px-4 py-2 text-xs font-bold text-blue-300 hover:bg-blue-800/40 transition-colors"
            >
              + 新增部位
            </button>
            <div className="flex items-center gap-2 text-[10px] text-gray-600">
              {saving && <span className="animate-pulse text-blue-400">儲存中...</span>}
              <span>自動儲存至 config/positions.yaml</span>
            </div>
          </div>
        </div>
      </div>

      {/* Backdrop */}
      <div className="fixed inset-0 z-30 bg-black/40" onClick={onClose} />

      {/* Modal */}
      {showModal && (
        <PositionModal
          initial={editIndex === -1 ? EMPTY_POSITION : (data?.positions[editIndex!] ?? EMPTY_POSITION)}
          onSave={handleSavePosition}
          onClose={() => { setShowModal(false); setEditIndex(null) }}
        />
      )}
    </>
  )
}
