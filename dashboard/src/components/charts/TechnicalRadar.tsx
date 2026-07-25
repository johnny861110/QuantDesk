import { RadarChart, Radar, PolarGrid, PolarAngleAxis, ResponsiveContainer } from 'recharts'

interface Props {
  findings: Record<string, string | number | boolean | null>
}

export function TechnicalRadar({ findings }: Props) {
  // Normalize each indicator to 0-100 scale
  const rsi = Number(findings.rsi ?? 50)
  const macd = Number(findings.macd_hist ?? findings.macd ?? 0)
  const bbPos = Number(findings.bb_position ?? 0.5)
  const volRatio = Number(findings.volume_ratio ?? 1)

  const data = [
    { metric: 'RSI',       value: Math.min(100, Math.max(0, rsi)) },
    { metric: 'MACD',      value: Math.min(100, Math.max(0, 50 + macd * 10)) },
    { metric: 'BB位置',    value: Math.min(100, Math.max(0, bbPos * 100)) },
    { metric: '量比',      value: Math.min(100, Math.max(0, volRatio * 50)) },
  ]

  // Skip if no meaningful data
  if (data.every(d => d.value === 50)) return null

  return (
    <div className="mt-3 rounded-lg bg-gray-900/50 p-2">
      <p className="text-[10px] text-gray-500 mb-1 px-1">Technical Profile</p>
      <ResponsiveContainer width="100%" height={120}>
        <RadarChart data={data}>
          <PolarGrid stroke="#374151" />
          <PolarAngleAxis dataKey="metric" tick={{ fontSize: 10, fill: '#9ca3af' }} />
          <Radar dataKey="value" stroke="#3b82f6" fill="#3b82f6" fillOpacity={0.2} />
        </RadarChart>
      </ResponsiveContainer>
    </div>
  )
}
