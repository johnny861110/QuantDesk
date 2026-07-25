import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'

interface Props {
  findings: Record<string, string | number | boolean | null>
}

export function RiskGreeksChart({ findings }: Props) {
  const items = [
    { name: 'Delta%', value: Number(findings.net_delta_pct_nav ?? 0) * 100, unit: '%NAV' },
    { name: 'Gamma',  value: Number(findings.net_gamma_twd ?? 0),            unit: 'TWD' },
    { name: 'Vega',   value: Number(findings.net_vega_twd ?? 0),             unit: 'TWD' },
    { name: 'Theta',  value: Number(findings.net_theta_twd ?? 0),            unit: 'TWD' },
  ]

  // Skip chart if all zero
  if (items.every(d => d.value === 0)) return null

  return (
    <div className="mt-3 rounded-lg bg-gray-900/50 p-2">
      <p className="text-[10px] text-gray-500 mb-1 px-1">Greeks Profile</p>
      <ResponsiveContainer width="100%" height={90}>
        <BarChart data={items} layout="vertical" margin={{ left: 0, right: 8, top: 0, bottom: 0 }}>
          <XAxis type="number" hide />
          <YAxis type="category" dataKey="name" width={50} tick={{ fontSize: 10, fill: '#9ca3af' }} />
          <Tooltip
            contentStyle={{ background: '#1f2937', border: '1px solid #374151', fontSize: 11 }}
            labelStyle={{ color: '#d1d5db' }}
            formatter={(v, _, entry) => {
              const num = Number(v ?? 0)
              const unit = (entry?.payload as Record<string, string>)?.unit ?? ''
              return [`${num.toFixed(2)} ${unit}`, '']
            }}
          />
          <Bar dataKey="value" radius={[0, 3, 3, 0]} maxBarSize={14}>
            {items.map((d, i) => (
              <Cell key={i} fill={d.value >= 0 ? '#22c55e' : '#ef4444'} fillOpacity={0.7} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
