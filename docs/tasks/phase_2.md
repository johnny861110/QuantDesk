# Phase 2 — Greeks 風控引擎

## 目標
實作 risk domain agent，核心是 Greeks 量化引擎與情境壓力測試。

## 子任務
1. `agents/risk/greeks.py` — Black-Scholes delta/gamma/vega/theta（用 py_vollib 或自實作）。
   現貨/期貨視為退化版選擇權（線性 payoff）。
2. `agents/risk/aggregation.py` — 組合層級 Greeks 加總。
   ⚠️ 跨標的加總前必須用 beta/指數點值換算成統一基準（等價台指期口數 或 等價美股 $ 曝險）。
   直接把台積電 delta 跟台指期 delta 相加是錯的。
3. `agents/risk/scenario.py` — 情境壓力測試：
   ΔP ≈ Δ×ΔS + ½×Γ×(ΔS)² + ν×ΔIV + Θ×Δt
   自動跑固定情境矩陣（大盤 ±1%/±3%/±5% × IV ±10%/±20%）。
4. `agents/risk_agent.py` — 產出 AgentSignal，含 hard_constraints
   （gamma_limit / vega_limit / net_delta_pct_nav / sector_concentration）。
5. `adapters/options_adapter.py` — TXO IV（券商API）/ 美股 options chain（yfinance）。

## 完成標準
- 給一組混合部位（現貨+期貨+選擇權），能算出組合 Greeks 與情境 P&L
- hard_constraints 觸限時 breached=True 正確設定
- 資料缺失（IV 拿不到）時，保守回報「無法評估凸性風險」並降低 confidence，不當作無風險
- uv run pytest 全過（含已知 Greeks 數值的單元測試對答案）

## ⚠️ 鐵則
- Greeks 計算是純數學，不准用 LLM
- Black-Scholes 假設歐式；美股個股選擇權為美式，程式註解要標明此簡化假設
