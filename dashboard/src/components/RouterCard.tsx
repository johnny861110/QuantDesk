import type { RouterPayload } from '../types'

const SCENARIO_ICON: Record<string, string> = {
  single_stock: '📈',
  portfolio_risk: '🛡️',
  multi_stock_scan: '🔍',
}

const DEPTH_COLOR: Record<string, string> = {
  quick: 'text-yellow-400',
  standard: 'text-blue-400',
  deep: 'text-purple-400',
}

const QUERY_TYPE_LABEL: Record<string, string> = {
  stock_analysis:     '個股分析',
  investment_strategy:'投資策略',
  fundamental_review: '基本面',
  macro_outlook:      '總經觀點',
  portfolio_risk:     '組合風控',
}

interface Props {
  router: RouterPayload
}

export function RouterCard({ router }: Props) {
  const icon = SCENARIO_ICON[router.scenario] ?? '❓'
  const depthColor = DEPTH_COLOR[router.depth] ?? 'text-gray-400'

  return (
    <div className="animate-fade-in rounded-xl border border-gray-700 bg-gray-800/60 p-4">
      <div className="mb-2 flex items-center gap-2">
        <span className="text-lg">{icon}</span>
        <span className="text-xs font-semibold uppercase tracking-widest text-gray-400">
          Router 意圖解析
        </span>
        <span className={`ml-auto text-xs font-mono ${router.method === 'llm' ? 'text-green-400' : 'text-yellow-400'}`}>
          {router.method === 'llm' ? 'GPT-4o-mini ✓' : 'regex fallback'}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-x-8 gap-y-1 text-sm sm:grid-cols-4">
        <div>
          <span className="text-gray-500">查詢類型</span>
          <p className="font-medium text-white">
            {router.query_type ? (QUERY_TYPE_LABEL[router.query_type] ?? router.query_type) : router.scenario}
          </p>
        </div>
        <div>
          <span className="text-gray-500">標的</span>
          <p className="font-medium text-white">
            {router.targets.length > 0 ? router.targets.join(', ') : '—'}
          </p>
        </div>
        <div>
          <span className="text-gray-500">市場</span>
          <p className="font-medium text-white">{router.market}</p>
        </div>
        <div>
          <span className="text-gray-500">深度</span>
          <p className={`font-medium ${depthColor}`}>{router.depth}</p>
        </div>
      </div>

      {/* Active agents badge list */}
      {router.agents && router.agents.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {router.agents.map(a => (
            <span
              key={a}
              className="rounded-full border border-gray-700 bg-gray-800 px-2 py-0.5 text-[10px] text-gray-400"
            >
              {a}
            </span>
          ))}
        </div>
      )}

      {router.error && (
        <p className="mt-2 text-xs text-yellow-400">⚠ {router.error}</p>
      )}
    </div>
  )
}
