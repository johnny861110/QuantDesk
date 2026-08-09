# QuantDesk — 多智能體量化研究桌系統

> 7 個 Domain Agent 並行分析（技術/籌碼/基本面/新聞/總經/跨市場/風控），由三層規則引擎 Supervisor 仲裁，支援 Bull/Bear/PM Debate，透過 FastAPI SSE 串流至 React + TypeScript Dashboard。

---

## 系統架構

```
使用者查詢（自然語言）
    │
    ▼
Router LLM — 意圖分類 → query_type + agents[] + run_supervisor
    │
    ▼
7 個 Domain Agent（asyncio 並行執行）
    ├── 📉 Technical   RSI/MACD/KD/布林/均線
    ├── 🏦 Chip        三大法人/外資持股/融資券
    ├── 📋 Fundamental ROIC/EWS/盈餘品質（自動爬取）
    ├── 📰 News        MOPS + Google News + Tavily
    ├── 🌐 Macro       NFP/CPI/GDP（pp 修正低基期偏差）
    ├── 🔗 CrossMarket TAIEX↔S&P 滾動相關/beta/regime
    └── 🛡️ Risk        Black-Scholes Greeks + 情境壓力測試
    │
    ▼
Supervisor（三層規則引擎）
    ├── Layer 1: 硬約束掃描（breach → 強制降級）
    ├── Layer 2: Horizon Breakdown（時間框架分層）
    └── Layer 3: 信心加權投票（SOURCE_RELIABILITY）
    │
    ▼  [if run_debate]
Debate（async Bull / Bear / PM）
    │
    ▼
React + TypeScript SSE Dashboard（即時串流）
```

---

## 查詢類型路由

| query_type | 觸發範例 | Agent 組合 | Supervisor |
|-----------|---------|-----------|-----------|
| `stock_analysis` | 「2330 技術面」「台積電外資動向」 | technical + chip + news | ✗ |
| `stock_investment` | 「台積電值得買嗎」「2330 完整分析」 | 6 個（**不含 risk**） | ✓ + Debate |
| `investment_strategy` | 「我的組合該怎麼調整」「整體部位配置」 | 全部 7 個（含 risk） | ✓ + Debate |
| `fundamental_review` | 「台積電財報」「2330 ROE」 | fundamental + chip | ✗ |
| `macro_outlook` | 「總經環境」「聯準會降息影響」 | macro + cross_market | ✗ |
| `portfolio_risk` | 「我的 delta 曝險」「組合風控」 | risk | ✗ |

> **個股 vs 組合為什麼要分開**：risk agent 讀 `config/positions.yaml` 分析的是
> **整個投資組合**的 Greeks 曝險。若問「台積電值得買嗎」時納入 risk，組合中
> 不相干部位一旦觸發硬約束，會透過 Supervisor Layer 1 強制降級整個個股建議
> （confidence 壓到 0.35）。故個股建議走 `stock_investment`（無 risk），
> 組合策略才走 `investment_strategy`（含 risk，該情境下強制降級是正確行為）。
>
> ⚠️ `stock_investment` 模式**不評估組合風控** —— 若你持有已 breach 的部位，
> 問個股時不會收到警告，需另外查詢組合策略或 `portfolio_risk`。

---

## 快速開始

### 前置需求
- Python 3.11+、[uv](https://docs.astral.sh/uv/)、Node.js 22+
- 可選：`FinancialReports`、`Financial_Agent` sibling repos（財報 agent 依賴）

### 安裝
```bash
git clone <repo-url>
cd quantdesk-starter
uv sync
cd dashboard && npm ci
```

### 啟動
```bash
# Terminal 1 — 後端 API（port 8000）
uv run uvicorn api.main:app --reload --port 8000

# Terminal 2 — 前端（port 5173）
cd dashboard && npm run dev

# 開啟 http://localhost:5173
```

### 驗收三關（順序固定）
```bash
uv run ruff check .          # lint — 零 error 零 warning
uv run mypy .                # 型別 — zero issues
uv run pytest -q             # 1125 passed / 1 skipped / 2 deselected
cd dashboard && npm run lint  # 前端 lint — 0 problems
cd dashboard && npm run test  # 前端 146 tests
cd dashboard && npm run build # frontend build
```

---

## API Endpoints

### 主要分析（SSE 串流）
```
GET /api/analyze/stream?query=...     # 自然語言 → Router 決定 agent 組合
GET /api/analyze/stock?symbol=2330    # 個股分析（technical+chip+news）
GET /api/analyze/strategy?query=...   # 完整策略（全 agent + Debate + Supervisor）
GET /api/analyze/fundamental?symbol=  # 財報/基本面（fundamental+chip）
GET /api/analyze/macro?market=TW      # 總經快照（macro+cross_market）
GET /api/analyze/risk                 # 組合風控（risk only）
GET /api/agent/{agent_name}?symbol=&market= # 單一 agent
```

### 標的搜尋
```
GET /api/symbols/search?q=台積&limit=20
# → [{"symbol": "2330", "name": "台積電"}, ...]
# 依代碼前綴或名稱子字串搜尋（FinMind 全市場清單）。
# 支撐 sidebar 的搜尋框——單一 agent 執行前必須有明確選定的標的，
# 不再靜默 fallback 到硬編碼預設值。查無或失敗時回傳 []。
```

### 持倉管理
```
GET /api/positions    # 讀取 config/positions.yaml
PUT /api/positions    # 寫回 config/positions.yaml
```

### 健康檢查
```
GET /health
# → {"status": "ok", "finmind": "valid|invalid|unset|set_but_unreachable", ...}
# finmind 欄位會實際發一次請求驗證 token 有效性，不只檢查環境變數是否存在。
```

### SSE 事件格式
```json
{"type": "router",            "payload": RouterPayload}
{"type": "agent_start",       "payload": {"agent": str}}
{"type": "agent_done",        "payload": AgentPayload}
{"type": "agent_error",       "payload": {"agent": str, "error": str}}
{"type": "debate_start",      "payload": {}}
{"type": "debate_bull",       "payload": DebatePartyPayload}
{"type": "debate_bear",       "payload": DebatePartyPayload}
{"type": "debate_pm",         "payload": DebatePMPayload}
{"type": "supervisor",        "payload": SupervisorPayload}
{"type": "fundamental_crawl", "payload": {"stage": str, "status": str}}
{"type": "done",              "payload": {}}
{"type": "error",             "payload": {"message": str}}
```

> `done` 與 `error` 是**終止事件**——收到任一個就該關閉 EventSource。
> `debate_start` 只在 `run_debate=true` 的 query_type 出現（見上方路由表）。

---

## 環境變數

| 變數 | 用途 | 必填 |
|------|------|------|
| `OPENAI_API_KEY` | Router / Synthesis / News / Fundamental LLM | 降級 |
| `FINMIND_TOKEN` | 選擇權 IV 反推 / 籌碼資料 | 降級（placeholder IV） |
| `TAVILY_API_KEY` | 新聞第三層搜尋 | 選填 |
| `FINANCIAL_DB_PATH` | SQLite 財報資料庫路徑 | 預設 `../FinancialReports/data/financial.db` |
| `LANGFUSE_ENABLED` | `true` 啟用 Langfuse tracing | 選填 |
| `LANGFUSE_PUBLIC_KEY` / `SECRET_KEY` / `HOST` | Langfuse 設定 | Langfuse 啟用時 |
| `LANGFUSE_TRACING_ENABLED` | Langfuse **SDK 官方**開關 —— `langfuse.openai` drop-in 只看這道 | 選填（測試中強制 false） |
| `SEC_USER_AGENT` | 執行 `scripts/refresh_ticker_registry.py` 時抓 SEC 美股清單用。SEC 規定須含聯絡方式 | 僅該 script 需要 |

---

## 持倉設定

編輯 `config/positions.yaml`，或在 Dashboard 右上角點 **「🛡️ 持倉」** 互動管理（新增/編輯/刪除，即時儲存）：

```yaml
portfolio_nav:
  value: 5000000.0
  currency: TWD

positions:
  - symbol: "2330.TW"
    instrument_type: stock    # stock | futures | option
    quantity: 1000            # 正=多 負=空
    currency: TWD
    multiplier: 1.0

  - symbol: "TXO"
    instrument_type: option
    quantity: -5
    strike: 22500.0
    expiry: "2026-09-16"      # 每月第三週三更新
    option_type: call         # call | put
    style: european           # european | american
    currency: TWD
    multiplier: 50.0
```

---

## Dashboard 功能

| 功能 | 說明 |
|------|------|
| **左側 Agent Sidebar** | 列出 7 個 agent，點擊執行單一 agent；狀態點顯示多/空/中性/載入/失敗 |
| **中繼資料面板** | 每張 AgentCard 點「▸ 中繼資料」展開完整指標 + 硬約束 + 錯誤 |
| **持倉管理** | 右上角「🛡️ 持倉」開啟互動編輯面板，即時 PUT 儲存 |
| **動態進度條** | PipelineProgress 依 `router.agents` 動態顯示，不硬編碼 7 個 |
| **查詢歷史** | localStorage 最近 20 筆，含信號指示燈 |
| **匯出 JSON** | 一鍵下載完整分析結果 |
| **重試** | 錯誤或完成後一鍵重新執行 |
| **recharts 圖表** | Risk Greeks 長條圖 / Technical 雷達圖 / Chip 法人流量圖 |

---

## 專案結構

```
quantdesk-starter/
├── api/main.py              # FastAPI SSE 後端（全部 endpoint）
├── agents/                  # 7 個 domain agent
│   ├── chip_agent.py
│   ├── cross_market_agent.py
│   ├── debate_agents.py
│   ├── fundamental_agent.py
│   ├── macro_agent.py
│   ├── news_agent.py
│   ├── risk_agent.py
│   ├── technical_agent.py
│   ├── verifier.py
│   └── risk/                # Greeks 計算引擎
│       ├── aggregation.py
│       ├── binomial_tree.py
│       ├── black_scholes.py
│       ├── position_loader.py
│       ├── pricing_router.py
│       └── scenario.py
├── adapters/                # 資料源抽象介面
│   ├── cache.py             # TTL 快取裝飾器（5 分鐘）
│   ├── chip_adapter.py
│   ├── cross_market_adapter.py
│   ├── fred_adapter.py
│   ├── fundamental_adapter.py  # 含 crawl_if_missing()
│   ├── fx_adapter.py
│   ├── macro_adapter.py
│   ├── news_adapter.py      # Google News RSS + Tavily + MOPS
│   ├── ticker_registry.py   # 離線代碼→名稱查表（無執行期網路）
│   ├── options_adapter.py   # FinMind IV 反推
│   └── price_adapter.py
├── schemas/
│   ├── agent_signal.py      # AgentSignal / HardConstraint
│   ├── debate.py
│   └── domain_report.py     # DomainReport / RouterOutput
├── supervisor/
│   ├── graph.py             # 三層規則引擎 + Debate 協調
│   ├── signal.py
│   └── synthesis.py
├── router/
│   └── intent_router.py     # LLM + regex fallback 意圖分類
├── config/positions.yaml    # 持倉設定（前端可互動修改）
├── data/tickers.jsonl       # 台股+美股 ticker 註冊表（13,531 筆，進版控）
├── scripts/refresh_ticker_registry.py  # 重新產生上表
├── tests/                   # 1125 tests（pytest + pytest-cov）
│   └── data/                # golden sets（router / supervisor）
├── dashboard/               # 146 tests（vitest），覆蓋率 80%
│   ├── eslint.config.js     # ESLint flat config
│   ├── vite.config.ts       # 含 vitest 設定
│   └── src/
│       ├── App.tsx          # 主頁（sidebar 佈局）
│       ├── types.ts         # 含 QueryType union（與後端 Literal 對齊）
│       ├── lib/
│       │   └── validatePosition.ts  # 持倉驗證（鏡射後端 position_loader）
│       ├── test/
│       │   ├── setup.ts             # 結構性保證測試不打真實網路
│       │   └── mockEventSource.ts   # 可控 SSE 替身
│       ├── hooks/
│       │   ├── useAnalysis.ts       # analyze() + analyzeAgent() + retry()
│       │   └── useQueryHistory.ts
│       └── components/
│           ├── AgentCard.tsx        # 含中繼資料面板；圖表 lazy load
│           ├── AgentSidebar.tsx     # 左側 agent 選擇欄
│           ├── DebatePanel.tsx
│           ├── PipelineProgress.tsx # 動態 agent nodes
│           ├── PositionsPanel.tsx   # 持倉互動管理 + 欄位驗證
│           ├── RouterCard.tsx
│           ├── SupervisorCard.tsx
│           └── charts/              # recharts（動態 chunk，不進主 bundle）
├── .github/workflows/ci.yml    # CI: ruff + mypy + pytest / eslint + vitest + build
├── CLAUDE.md                    # 開發守則（三條不可違反）
└── docs/SESSION_HANDOFF.md      # 完整交接文件（下一個 session 必讀）
```

---

## 核心設計原則

### 1. 確定性計算與 LLM 嚴格分離
Greeks、財務指標、技術指標、統計量一律由純 Python 計算。LLM 只負責寫白話說明，`verifier.py` 掃描 narrative 中的數字洩漏。

### 2. 風控是硬約束，不是投票的一票
`hard_constraints[].breached=True` → Supervisor Layer 1 強制降級至 bearish，信心壓縮至 0.35。規則引擎執行，LLM 不得覆蓋。

### 3. 每個判斷都要帶來源與時間戳
所有 `key_evidence` 必須有 `source` + `asof`，`data_quality` 記錄 `completeness`、`confidence`、`staleness_sec`。

---

## 實作路線圖

| Phase | 內容 | 狀態 |
|-------|------|------|
| 0–6 | 骨架 + 六 agent + Supervisor + Production 硬化 | ✅ |
| 7–9 | Agentic 重構 + Chip + Debate + Synthesis | ✅ |
| 10–11 | React Dashboard + 全 agent 接線修復 | ✅ |
| 12 | Core Accuracy（FinMind IV + GDP pp-fix + FRED logging）| ✅ |
| 13 | Frontend Pro（recharts + constraints + 歷史 + 匯出）| ✅ |
| 14 | Engineering Quality（E2E + CI frontend + coverage）| ✅ |
| 15 | Query-Type Routing + Async 重構 + 新聞修復 + UI 重大改版 | ✅ |
| 16 | Truth & Correctness（文件對帳 + chip Verifier + verifier 修復 + SDK 統一 + query_type 切分）| ✅ |
| 17 | Evaluation Framework（prompt 快照 + golden set + faithfulness）| ✅ |
| 19 | 前端品質（vitest + ESLint + code splitting）| ✅ |

> 無 Phase 18——原規劃編號在 Phase 16 重評估時併入 16/17，未另立。
> Phase 16/17 計畫見 `docs/tasks/phase_16.md`

---

## 常用指令

```bash
# 環境同步
uv sync && cd dashboard && npm ci

# 開發三關
uv run ruff check .
uv run mypy .
uv run pytest -q

# 含覆蓋率
uv run pytest -q --cov --cov-report=term-missing

# 啟動服務
uv run uvicorn api.main:app --reload --port 8000
cd dashboard && npm run dev

# 加依賴
uv add <pkg>
uv add --dev <pkg>
```

---

> 詳細交接文件（含所有 class/function 簽名、已知問題、下一步建議）請見 `docs/SESSION_HANDOFF.md`
