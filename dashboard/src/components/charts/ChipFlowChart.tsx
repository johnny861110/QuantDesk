import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'

interface Props {
  findings: Record<string, string | number | boolean | null>
}

export function ChipFlowChart({ findings }: Props) {
  const items = [
    { name: '外資', value: Number(findings.foreign_net ?? 0) },
    { name: '投信', value: Number(findings.trust_net ?? 0) },
    { name: '自營', value: Number(findings.dealer_net ?? 0) },
  ]

  // Skip if all zero
  if (items.every(d => d.value === 0)) return null

  return (
    <div className="mt-3 rounded-lg bg-gray-900/50 p-2">
      <p className="text-[10px] text-gray-500 mb-1 px-1">三大法人淨買超</p>
      <ResponsiveContainer width="100%" height={80}>
        <BarChart data={items} margin={{ left: 0, right: 4, top: 4, bottom: 0 }}>
          <XAxis dataKey="name" tick={{ fontSize: 10, fill: '#9ca3af' }} axisLine={false} tickLine={false} />
          <YAxis hide />
          <Tooltip
            contentStyle={{ background: '#1f2937', border: '1px solid #374151', fontSize: 11 }}
            formatter={(v) => [Number(v ?? 0).toLocaleString(), '淨買超']}
          />
          <Bar dataKey="value" radius={[3, 3, 0, 0]} maxBarSize={28}>
            {items.map((d, i) => (
              <Cell key={i} fill={d.value >= 0 ? '#22c55e' : '#ef4444'} fillOpacity={0.7} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
