# QuantDesk — 多智能體量化研究桌系統

> 七個獨立 domain agent（風控 / 技術面 / 財報 / 新聞 / 總經 / 跨市場 / 籌碼），由三層仲裁 Supervisor 匯總，支援 **Multi-agent Debate**（Bull/Bear/PM），產出帶 **硬約束強制** 與 **人工複核標記** 的綜合投資評估。附帶 React + TypeScript SSE 即時 Dashboard。

---

## 系統架構

```
                         ┌─────────────────────────────────────────┐
                         │        Router LLM（意圖解析）             │
                         │  query → scenario / targets / depth      │
                         └──────────────────┬──────────────────────┘
                                            │
                         ┌──────────────────▼──────────────────────┐
                         │        Supervisor（三層仲裁）              │
                         │  Layer 1: 硬約束掃描（breach → 強制降級）   │
                         │  Layer 2: Horizon Breakdown（時間框架分層） │
                         │  Layer 3: 信心加權投票（SOURCE_RELIABILITY） │
                         │  HITL Gate: 機器可讀人工複核標記            │
                         │  Debate: async Bull/Bear/PM 多觀點辯論     │
                         │  Synthesis LLM: 白話報告生成               │
                         └──────────────────┬──────────────────────┘
                                            │  AgentSignal / DomainReport
      ┌────────┬──────────┬─────────────────┼──────────────┬──────────┬──────────┐
      ▼        ▼          ▼                 ▼              ▼          ▼          ▼
   Risk     Technical  Fundamental       News           Macro    CrossMarket   Chip
  Greeks   RSI/MACD/   XBRL財報         MOPS/RSS/     NFP/CPI   TAIEX↔S&P   三大法人
  FinMind  KD/布林     EWS預警          LLM分析       GDP/失業率  滾動相關    外資持股
  真實IV   型態辨識    盈餘品質          Tavily         pp計算     beta/regime  買賣超
```

每個 agent 都接受 mock adapter 注入，可獨立使用，也可透過 `Supervisor.aggregate()` / `aggregate_debate()` / `aggregate_agentic()` 匯總。

---

## 功能概覽

### 七個 Domain Agent

| Agent | 核心分析 | 資料源 | 硬約束 |
|-------|----------|--------|--------|
| **Risk** | Black-Scholes Greeks、Portfolio Aggregation、Scenario P&L、**真實 IV**（FinMind 反推） | `positions.yaml` + YFinance FX + FinMind TXO | `net_delta_pct_nav`、`vega_usd` 等曝險上限 |
| **Technical** | Wilder RSI、Slow KD、SMA/MACD、布林通道 | OHLCV adapter | — |
| **Fundamental** | 財報比率（毛利率/ROE/流動比）、EWS critical/high、盈餘品質 | FinancialReports SQLiteStore | EWS critical → hard_constraint |
| **News** | MOPS 重訊、財經 RSS、Tavily 搜尋、LLM 情感分析（含降級） | 三層 adapter + OpenAI | — |
| **Macro** | NFP/CPI/GDP/失業率 surprise 計算（pp 修正低基期偏差） | TradingEconomics / FRED adapter | — |
| **CrossMarket** | TAIEX↔S&P 500 滾動相關係數、beta、市場 regime | YFinance 跨市場 adapter | —（背景指標，不參與方向投票） |
| **Chip** | 三大法人買賣超、外資持股比例、融資融券 | FinMind 籌碼 adapter | — |

### Supervisor 三層仲裁

1. **Layer 1 — 硬約束掃描**：任何 `hard_constraints[].breached == true` → `risk_override=True`，最終建議強制降級至 bearish，信心壓縮至 0.35。規則引擎執行，LLM 不得自由裁量。
2. **Layer 2 — Horizon Breakdown**：依時間框架（short/medium/long）分組，計算 `consensus_share`（方向一致度）與 `evidence_confidence`（底層信心 = Σ(conf×compl×rel)/Σ(rel)，completeness 在分子不在分母，避免自我抵銷）。
3. **Layer 3 — 信心加權投票**：依 `SOURCE_RELIABILITY`（fundamental=1.0、macro=0.85、technical=0.80、news=0.60）加權，cross_market 為背景指標不參與投票，risk 走 Layer 1 不參與方向投票。

### Multi-agent Debate

三個非同步角色辯論（Bull / Bear / PM），各自依據 domain signals 建構論點，Supervisor 彙整為多觀點分析報告。透過 `aggregate_debate()` 調用。

### Synthesis LLM

Agentic 模式下，Router LLM 解析使用者查詢意圖 → domain agents 平行執行 → Synthesis LLM 將結構化結果轉為白話 `DomainReport`。透過 `aggregate_agentic()` 調用。

### HITL Gate（人工複核標記）

機器可讀，不解析 narrative 文字（同 `verifiable=False` 設計哲學）：

```python
output.requires_human_review  # bool
output.review_reasons         # list[str]
# 範例：
# "low_confidence:0.35 (caused_by:risk_override)"  ← 根因是 breach，非獨立低信心
# "hard_constraint_breach:net_delta_pct_nav"
# "unverifiable_constraint:vega_usd"
# "ews_critical"
```

### React + TypeScript SSE Dashboard

即時串流分析儀表板（FastAPI SSE backend + React frontend）：

- **SSE 即時串流**：Router → Agent 進度 → Debate → Supervisor 結果，即時推送
- **Agent 專屬圖表**：Risk Greeks 長條圖、Technical 雷達圖、Chip 法人流量圖（recharts）
- **Hard Constraints 明細**：current/limit gauge、breach 紅色標記
- **查詢歷史**：localStorage 儲存最近 20 筆，含 signal 指示燈
- **JSON 匯出**：一鍵下載完整分析結果
- **重試 / 重新分析**：錯誤或完成後可快速重新執行

---

## 快速開始

### 前置需求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)（套件管理器，本專案全程使用 uv）
- Node.js 22+（Dashboard 前端）
- （選填）FinancialReports、Financial_Agent 兩個 sibling repo（財報 agent 依賴）

### 安裝

```bash
git clone <repo-url>
cd quantdesk-starter
uv sync   # 建 .venv + 裝所有依賴 + 鎖定 uv.lock
```

### 驗收三關

```bash
uv run ruff check .    # lint — 零 error 零 warning
uv run mypy .          # 型別檢查 — zero issues
uv run pytest -q       # 所有測試全過（868 tests）
```

### 啟動 Dashboard（推薦）

```bash
# Terminal 1 — 後端 API
uv run uvicorn api.main:app --reload --port 8000

# Terminal 2 — 前端
cd dashboard && npm ci && npm run dev
```

開啟瀏覽器訪問 `http://localhost:5173`，輸入查詢（如「分析台積電」），即可看到即時串流分析結果。

### 執行端對端 Demo（CLI）

```bash
uv run python scripts/demo_full_pipeline.py
```

七個 agent 依序執行（全部使用 mock adapter，無需真實市場 API），輸出完整仲裁報告：

```
════════════════════════════════════════════════════════════════════
  QuantDesk — Full Pipeline Demo（六 Agent + Supervisor）
  Symbol  : 2330.TW  台積電
════════════════════════════════════════════════════════════════════

Step 1 — Risk Agent（positions.yaml + MockFX USDTWD=31.50）
  agent      : risk
  signal     : bearish   confidence=1.00  horizon=short
  constraints: ⛔ BREACH: net_delta_pct_nav

...（六個 agent 依序輸出）...

Supervisor Layer 1 — 硬約束掃描
  ⛔ BREACH [risk] net_delta_pct_nav  current=-2.10  limit=0.3
  risk_override: True

Supervisor Layer 2 — Horizon Breakdown
  Horizon     Direction    consensus_share   evidence_conf
  short       bullish              100.00%            0.50
  medium      bullish              100.00%            0.70

HITL Gate
  requires_human_review : True
  review_reasons: ["low_confidence:0.35 (caused_by:risk_override)",
                   "hard_constraint_breach:net_delta_pct_nav"]

最終 SupervisorOutput
  overall_recommendation : bearish
  confidence             : 0.35
  disclaimer: 本系統輸出為研究輔助與風險提示，非自動下單...
```

---

## 專案結構

```
quantdesk-starter/
├── schemas/
│   └── agent_signal.py         # 共同合約（AgentSignal、HardConstraint、DomainReport…）
├── adapters/
│   ├── base.py                 # DataSourceAdapter 抽象基類（SourcedData 統一回傳格式）
│   ├── fx_adapter.py           # USDTWD（YFinance）
│   ├── price_adapter.py        # OHLCV（YFinance）
│   ├── fundamental_adapter.py  # FinancialReports SQLiteStore → FinancialSnapshotWithMeta
│   ├── news_adapter.py         # MOPS / RSS / Tavily（三層，信心分級）
│   ├── macro_adapter.py        # TradingEconomics API
│   ├── fred_adapter.py         # FRED 免費總經資料（GDP/CPI/失業率）
│   ├── cross_market_adapter.py # 跨市場 OHLCV（多 symbol）
│   └── options_adapter.py      # FinMind 選擇權（IV 反推）
├── agents/
│   ├── risk/                   # Greeks 計算引擎（BS + Binomial + Aggregation + Scenario）
│   ├── risk_agent.py           # Risk domain agent（含 FinMind 真實 IV pipeline）
│   ├── technical_agent.py      # Technical domain agent
│   ├── fundamental_agent.py    # Fundamental domain agent
│   ├── news_agent.py           # News domain agent
│   ├── macro_agent.py          # Macro domain agent（GDP/失業率 pp 修正）
│   ├── cross_market_agent.py   # Cross-market domain agent
│   ├── chip_agent.py           # Chip domain agent（三大法人 + 外資持股）
│   └── verifier.py             # Narrative Verifier（數字洩漏 + prompt injection 防護）
├── router/
│   └── intent_router.py        # Router LLM — 使用者查詢意圖解析
├── supervisor/
│   ├── graph.py                # Supervisor 三層仲裁 + HITL Gate + Debate + Agentic
│   └── signal.py               # SupervisorOutput dataclass
├── api/
│   └── main.py                 # FastAPI SSE backend（串流分析端點）
├── dashboard/                  # React + TypeScript + Tailwind 前端
│   ├── src/
│   │   ├── App.tsx             # 主頁面（查詢、歷史、匯出、重試）
│   │   ├── components/
│   │   │   ├── AgentCard.tsx           # Agent 結果卡片（含專屬圖表）
│   │   │   ├── SupervisorCard.tsx      # Supervisor 結果（含 hard constraint 明細）
│   │   │   ├── DebatePanel.tsx         # Bull/Bear/PM 辯論面板
│   │   │   └── charts/                # recharts 圖表元件
│   │   │       ├── RiskGreeksChart.tsx
│   │   │       ├── TechnicalRadar.tsx
│   │   │       └── ChipFlowChart.tsx
│   │   └── hooks/
│   │       ├── useAnalysis.ts          # SSE 串流 + 重試邏輯
│   │       └── useQueryHistory.ts      # localStorage 查詢歷史
│   └── package.json
├── observability/
│   └── langfuse_setup.py       # Langfuse optional tracing
├── config/
│   └── positions.yaml          # 持倉設定（風控 agent 輸入）
├── scripts/
│   ├── demo_full_pipeline.py   # 端對端 Demo
│   └── demo_langfuse_smoke.py  # Langfuse trace 驗煙
├── tests/                      # 868 tests（pytest + pytest-cov）
│   ├── test_phase0.py          # Schema contract
│   ├── test_phase1c.py         # Fundamental agent + Verifier
│   ├── test_phase2_*.py        # Greeks + Risk agent（含 IV wiring）
│   ├── test_phase3_*.py        # Technical + CrossMarket
│   ├── test_phase4_*.py        # News + Macro（pp 修正）
│   ├── test_phase5_supervisor.py  # Supervisor 仲裁（91 tests）
│   ├── test_phase9_*.py        # Debate + Agentic synthesis
│   └── test_api_e2e.py         # SSE 串流 E2E 測試
├── .github/workflows/ci.yml   # CI: ruff + mypy + pytest-cov + frontend build
├── CLAUDE.md                   # 開發守則（三條不可違反的設計原則）
└── pyproject.toml              # uv 套件管理（Python 3.11+）
```

---

## 核心設計原則

### 1. 確定性計算與 LLM 嚴格分離

Greeks、財務指標、技術指標、統計量一律由確定性 Python 計算。LLM 只負責寫白話說明，**永遠不產出數字**。`agents/verifier.py` 的 Verifier 會掃描 narrative 中的數字洩漏。

### 2. 風控是硬約束，不是投票的一票

`hard_constraints[].breached == true` → Supervisor Layer 1 強制降級，不能被其他 agent 的樂觀訊號投票蓋過。規則引擎執行。

### 3. 每個判斷都要帶來源與時間戳

所有 `key_evidence` 都必須有 `source` 與 `asof`。`data_quality` 欄位記錄 `completeness`、`confidence`、`staleness_sec`，Supervisor 用這些計算 `evidence_confidence`。

---

## Observability（Langfuse Tracing）

六個 agent 全部支援 Langfuse trace。每個 agent 的 `run()` 為一個頂層 span，adapter fetch / LLM call / verifier 為子 span。

```bash
# .env
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-...
LANGFUSE_SECRET_KEY=sk-...
LANGFUSE_HOST=https://cloud.langfuse.com

uv run python scripts/demo_langfuse_smoke.py   # 驗證六個 agent trace 可見
```

OTel context 透過直接 node 呼叫（非 `graph.invoke()`）傳遞，確保子 span 正確掛在父 span 下。

---

## 持倉設定（Risk Agent 輸入）

編輯 `config/positions.yaml`，支援現貨、期貨（台指期 TXFF）、選擇權（TXO / 美股個股 options）：

```yaml
portfolio_nav:
  value: 5000000.0
  currency: TWD

positions:
  - symbol: 2330.TW
    instrument_type: stock
    quantity: -1000        # 負 = 空頭
    currency: TWD
    multiplier: 1

  - symbol: TXO
    instrument_type: option
    quantity: -5
    currency: TWD
    multiplier: 50
    strike: 22000
    expiry: 2026-09-17     # 第三週三結算
    option_type: call
    style: european
```

---

## 環境變數

| 變數 | 用途 | 必填 |
|------|------|------|
| `OPENAI_API_KEY` | Router LLM / Synthesis LLM / News agent LLM | 否（缺席時走降級路徑） |
| `FINMIND_TOKEN` | FinMind API（Risk IV / Chip 籌碼） | 否（缺席時 fallback placeholder IV） |
| `TE_API_KEY` | TradingEconomics（Macro agent） | 否（缺席時走降級路徑） |
| `TAVILY_API_KEY` | Tavily 搜尋（News agent） | 否（缺席時略過） |
| `LANGFUSE_ENABLED` | `true` 啟用 Langfuse tracing | 否 |
| `LANGFUSE_PUBLIC_KEY` | Langfuse public key | Langfuse 啟用時 |
| `LANGFUSE_SECRET_KEY` | Langfuse secret key | Langfuse 啟用時 |
| `LANGFUSE_HOST` | Langfuse 端點 | Langfuse 啟用時 |

---

## 測試結構

```bash
uv run pytest -q                          # 全部 868 tests
uv run pytest -q --cov --cov-report=term-missing  # 含覆蓋率報告
uv run pytest tests/test_phase5_supervisor.py -q   # Supervisor（91 tests）
uv run pytest -m integration              # 整合測試（需 FinancialReports editable install）
cd dashboard && npm run build             # 前端型別檢查 + build
```

| 測試檔案 | 覆蓋範圍 | Tests |
|----------|----------|-------|
| `test_phase0.py` | Schema contract、SupervisorOutput backward-compat | ~20 |
| `test_phase1c.py` | Fundamental agent、Verifier（數字洩漏、injection） | ~80 |
| `test_phase2_*.py` | Greeks（BS/Binomial）、Aggregation、Scenario、Risk agent（含 IV wiring） | ~185 |
| `test_phase3_*.py` | Technical indicators、CrossMarket correlation/beta/regime | ~120 |
| `test_phase4_*.py` | News（三層 adapter + LLM 降級）、Macro（surprise + pp 修正） | ~185 |
| `test_phase5_supervisor.py` | 三層仲裁 + HITL Gate（條件獨立性 + 多條件並列） | 91 |
| `test_phase9_*.py` | Debate（Bull/Bear/PM）、Agentic synthesis | ~30 |
| `test_api_e2e.py` | SSE 串流 E2E（health、事件 schema、全 agent 失敗） | 5 |

---

## 實作路線圖

| Phase | 內容 | 狀態 |
|-------|------|------|
| 0 | Schema 鎖定 + Supervisor 骨架 + Adapter 基類 | ✅ 完成 |
| 1 | 財報 domain（FinancialReports + EWS + Verifier） | ✅ 完成 |
| 2 | Greeks 風控引擎（BS + Binomial + Aggregation + Scenario） | ✅ 完成 |
| 3 | 技術面 + 跨市場 agent | ✅ 完成 |
| 4 | 新聞 + 總經 agent | ✅ 完成 |
| 5 | Supervisor 三層仲裁 | ✅ 完成 |
| 6 | Production 硬化（免責聲明 / IV fail-safe / Langfuse / HITL Gate） | ✅ 完成 |
| 7 | Agentic 重構（Router LLM + Chip Agent + Synthesis LLM + DomainReport） | ✅ 完成 |
| 8 | Supervisor × Synthesis 整合（aggregate_agentic + demo 接線） | ✅ 完成 |
| 9 | Multi-agent Debate（async Bull/Bear/PM + aggregate_debate） | ✅ 完成 |
| 10 | React + TypeScript + SSE Dashboard（FastAPI backend + 豐富 UI） | ✅ 完成 |
| 11 | Agent 功能全面驗測與修復（全 agent 接線 + debate + UI error card） | ✅ 完成 |
| 12 | Core Accuracy（FinMind 真實 IV 接入 risk agent、GDP/失業率 pp 修正） | ✅ 完成 |
| 13 | Frontend Pro（recharts 圖表、hard constraints 明細、查詢歷史、JSON 匯出） | ✅ 完成 |
| 14 | Engineering Quality（E2E SSE 測試、CI frontend build、pytest-cov 覆蓋率） | ✅ 完成 |

---

## 設計補充說明

### AgentSignal Schema（七個 agent 的共同合約）

```python
@dataclass
class AgentSignal:
    agent: AgentType          # RISK / TECHNICAL / FUNDAMENTAL / NEWS / MACRO / CROSS_MARKET / CHIP
    target: Target            # symbol + market + asof
    signal: Signal            # BULLISH / BEARISH / NEUTRAL
    confidence: float         # [0, 1] — per-field source confidence（非 quality_score）
    time_horizon: TimeHorizon # SHORT / MEDIUM / LONG
    key_evidence: list[Evidence]     # 每項必須有 source + asof
    hard_constraints: list[HardConstraint]
    metrics: dict[str, Any]   # 確定性計算結果（Greeks、比率、統計量）
    narrative: str            # LLM 生成白話說明（不含數字，經 Verifier 掃描）
    data_quality: DataQuality # completeness / confidence / staleness_sec
```

### evidence_confidence 公式

```
eff_weight_i = confidence_i × completeness_i × SOURCE_RELIABILITY_i
evidence_confidence = Σ(eff_weight_i) / Σ(SOURCE_RELIABILITY_i)
```

`completeness`（= `quality_score` for fundamental）在分子，不在分母。若放分母，compl 會與分子的 `eff_weight/rel` 互消，導致低品質資料被當作高品質，這是 Phase 5 設計時明確排除的 bug。

### news agent 降級路徑

`OPENAI_API_KEY` 缺失或 API 失敗 → `analysis.llm_failed=True` → `data_quality.completeness=0.0` → Supervisor Layer 3 自動排除投票（completeness=0 是明確降級旗標）。整條 pipeline 不 crash，news signal 以 `confidence=0.10` 的降級輸出保留在 `raw_agent_signals` 供追溯。

---

## 依賴關係

```
quantdesk-starter/
├── financial-reports    (editable, ../FinancialReports)  ← 財報 ETL + SQLiteStore
├── financial-agent      (editable, ../Financial_Agent)   ← 財報分析工具函數
├── langgraph            ← 所有 agent 的 graph 編排框架
├── pydantic             ← Schema 驗證（AgentSignal、DataQuality 等）
├── fastapi + uvicorn    ← SSE 串流 API 後端
├── langfuse             ← 可觀測性 tracing（optional）
├── openai               ← Router / Synthesis / News LLM（optional）
├── yfinance             ← FX / OHLCV / 跨市場 live adapter
├── scipy / numpy        ← Black-Scholes、滾動相關係數等數值計算
├── feedparser           ← News agent RSS 抓取
├── pandas               ← 資料處理（FinMind 回傳解析等）
└── recharts (npm)       ← 前端圖表（React）
```

---

## 常用指令

```bash
# 環境
uv sync                              # 同步 Python 依賴
cd dashboard && npm ci                # 同步前端依賴

# 開發三關（順序固定）
uv run ruff check .                  # 1. lint
uv run mypy .                        # 2. 型別檢查
uv run pytest -q                     # 3. 測試（868 tests）

# 覆蓋率
uv run pytest -q --cov --cov-report=term-missing

# 前端
cd dashboard && npm run dev           # 開發伺服器（Vite）
cd dashboard && npm run build         # 型別檢查 + production build

# 後端 API
uv run uvicorn api.main:app --reload  # FastAPI SSE 伺服器

# Demo
uv run python scripts/demo_full_pipeline.py  # 七 agent + Supervisor

# 加依賴
uv add <pkg>
uv add --dev <pkg>
```
