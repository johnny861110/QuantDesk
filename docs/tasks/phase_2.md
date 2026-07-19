# Phase 2 — Greeks 風控引擎

## 目標
實作 risk domain agent，核心是 Greeks 量化引擎與情境壓力測試。

## 資料源決策（已與使用者確認）
- IV 來源：FinMind 選擇權成交價（TaiwanOptionDaily / taiwan_options_snapshot），
  **不含 IV 欄位**，需自行反推。之後有券商 API 權限後替換，先留 tech-debt 標記。
- 部位來源：`config/positions.yaml`，人工填寫，schema 見下方。之後接券商 API 取代。
- 定價模型：European（style=european，如 TXO）用 Black-Scholes；
  American（style=american，多數美股個股選擇權）用 Binomial tree (CRR)。
- 未來擴充：國泰期貨已有獨立程式化 API（需另外簽署「程式應用API申請書」，
  非新樹精靈看盤軟體），大多原生支援 C#。屆時 OptionsAdapter 介面設計需
  預留跨語言呼叫空間（如 pythonnet 呼叫 C# dll，或另建中介服務落地 DB）。
  現在先不實作，只確保 OptionsAdapter 是乾淨的抽象介面，換實作不動上層。

## 子任務
1. `agents/risk/black_scholes.py` — BS delta/gamma/vega/theta 解析解 +
   `implied_volatility_from_price()`（Newton-Raphson 或 bisection 反推 IV）。
2. `agents/risk/binomial_tree.py` — CRR binomial tree 計算美式選擇權 Greeks
   （用有限差分法估 delta/gamma，因為美式沒有解析解）。
3. `agents/risk/pricing_router.py` — 依 `style` 欄位分派到 BS 或 Binomial，
   統一輸出同一個 Greeks dataclass，上層不用知道底下用哪個模型。
4. `adapters/options_adapter.py`（FinMind 實作）— 拉成交價，呼叫反推 IV，
   標記 `iv_source="finmind_backed_out"` 讓下游知道這是反推值不是真實報價 IV。
5. `config/positions.yaml` + `agents/risk/position_loader.py` — 解析 YAML 成
   Position dataclass 列表，含現貨/期貨/選擇權。現貨/期貨視為退化版選擇權
   （delta=1或0, gamma=vega=theta=0）。
6. `agents/risk/aggregation.py` — 組合層級 Greeks 加總。
   ⚠️ 跨標的加總前必須用 beta/指數點值換算成統一基準（等價台指期口數 或
   等價美股 $ 曝險）。
7. `agents/risk/scenario.py` — 情境壓力測試：
   ΔP ≈ Δ×ΔS + ½×Γ×(ΔS)² + ν×ΔIV + Θ×Δt
   自動跑固定情境矩陣（大盤 ±1%/±3%/±5% × IV ±10%/±20%）。

   ⚠️ **Δt 日計慣例強制要求（與 black_scholes.py 對齊）**：
   `bs_theta` 採日曆天 365 慣例（`theta_per_calendar_day = annual_theta / 365`）。
   scenario.py 的 Θ×Δt 項中，Δt **必須**同樣以日曆天表示：
   `Δt = calendar_days / 365`（例如持有 1 天 → Δt = 1/365 ≈ 0.00274）。
   **禁止**改用交易日 252（`calendar_days / 252`）——混用會對 theta P&L 項產生
   系統性 ~45 % 高估誤差，且難以被上層測試發現。
8. `agents/risk_agent.py` — 產出 AgentSignal，含 hard_constraints
   （gamma_limit / vega_limit / net_delta_pct_nav / sector_concentration）。

## 完成標準
- 給一組混合部位（來自 positions.yaml：現貨+期貨+選擇權），能算出組合 Greeks 與情境 P&L
- BS 反推 IV 的單元測試：已知 IV 算出價格，再從價格反推回去，誤差 < 1e-4
- Binomial tree Greeks 與 BS Greeks 在到期日趨近時應收斂（做一個收斂性測試）
- hard_constraints 觸限時 breached=True 正確設定
- IV 資料缺失時，保守回報「無法評估凸性風險」並降低 confidence，不當作無風險
- uv run pytest -q 全過（含已知 Greeks 數值的單元測試對答案）

## 技術債（Phase 3 補齊）

### scenario.py — 個股/美股情境聯動（beta 未估計）
情境壓力測試的 index_shock 目前**只精確套用在 `INDEX_DERIVATIVE_SYMBOLS` 涵蓋的部位**
（TXFF、TXO 等台指衍生品）。個股（2330.TW、AAPL）與個股選擇權（AAPL call）
在 `ScenarioResult.unmapped_symbols` 裡被標記為「beta 未估計，此情境下無法精確評估」，
不納入情境 P&L 計算。

**原因**：對個股直接套用 `index_shock × spot × delta` 等同隱性假設 beta=1，
與 `aggregation.py` 刻意將這些部位歸入 `unmapped_single_name_exposure`
（明確拒絕假設 beta）的設計矛盾。

**Phase 3 需補上**：
- 在 `agents/technical/` 或 `agents/cross_market/` 裡實作 rolling beta 估計
  （60-day window 對 TAIEX 的 OLS regression）
- `run_scenarios()` 新增可選的 `beta_map: dict[str, float]` 參數；
  有 beta 的個股用 `ΔS_individual = index_shock × beta × spot` 計入 P&L；
  仍缺 beta 的留在 `unmapped_symbols`

## ⚠️ 鐵則
- Greeks 計算是純數學，不准用 LLM
- 反推 IV 的無風險利率/股利率假設要寫成具名常數 + comment，不能是魔術數字
- positions.yaml 是暫時方案，adapter 介面要設計成之後換券商 API 不用動上層邏輯
