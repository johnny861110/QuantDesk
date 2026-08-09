# QuantDesk — Session Handoff Document
> 給下一個 Claude Code session 的完整專案交接文件
> 最後更新：2026-08-09（Phase 19 完成，全部合併進 `main`）
> Git HEAD：`main`，無殘留分支
>
> ⚠️ **§二 是 2026-07-25 Phase 15 當下的工作紀錄**，屬歷史，不代表現況。
> §三（狀態）、§五（結構）、§六（環境變數）、§七（已知問題）已於 2026-08-09 對帳。
>
> §一 與 §四 的架構圖尚未涵蓋 Phase 16 新增的 `stock_investment` query_type
> （個股投資建議，**不含 risk agent**——見 §七 P0-1）。
> 完整 query_type 路由表以 `README.md` 為準。

---

## 一、專案一句話總結

QuantDesk 是一個**多智能體量化投研系統**：7 個 Domain Agent 並行分析（技術/籌碼/基本面/新聞/總經/跨市場/風控），由三層規則引擎 Supervisor 仲裁，支援 Bull/Bear/PM Debate，透過 FastAPI SSE 串流至 React + TypeScript Dashboard。

---

## 二、今日完成的工作（2026-07-25）

### 架構重構
1. **Query-Type Routing**（最重要）
   - `RouterOutput` 擴充：`query_type` / `agents[]` / `run_supervisor` / `run_debate`
   - 5 種查詢類型：`stock_analysis` / `investment_strategy` / `fundamental_review` / `macro_outlook` / `portfolio_risk`
   - `_stream_analysis` 依 `router_out.agents` 篩選，問「技術面」不再跑 risk agent
   - `run_supervisor=False` 時直接輸出各 agent 結果，不做仲裁（解決「技術面查詢被 portfolio risk override 強制降級」）

2. **Async 修復**
   - 所有 agent 從序列改為並行（asyncio.Queue + Task）
   - feedparser 加 requests 10s timeout（RSS 原本無 timeout，掛 267 秒）
   - 每個 agent 加 `asyncio.wait_for(timeout=45s)` 保護
   - `_ANALYSIS_SEMAPHORE(5)` 防止高併發 thread pool 耗盡

3. **新 API Endpoints**
   ```
   GET /api/analyze/stock?symbol=      # technical+chip+news
   GET /api/analyze/strategy?query=    # 全 agent+Debate+Supervisor
   GET /api/analyze/fundamental?symbol= # fundamental+chip
   GET /api/analyze/macro?market=      # macro+cross_market
   GET /api/analyze/risk               # risk only
   GET /api/agent/{name}?symbol=&market= # 單一 agent SSE
   GET /api/positions                  # 讀 positions.yaml
   PUT /api/positions                  # 寫回 positions.yaml
   ```

### 新聞面修復
- RSS 從全市場 feed → **個股專屬 Google News RSS**（搜「台積電 2330」）+ Yahoo Finance
- 新增 40 支台股代碼→中文名對照表（`TW_STOCK_NAMES`）
- `TAVILY_API_KEY` 有設定但**從來沒被注入** → 現在正確注入 `TavilyNewsAdapter(api_key=key)`
- 搜尋詞從 `["2330"]` 改為 `["台積電", "2330", "TSMC"]`

### Fundamental 修復
- `_current_year_quarter()` 回傳 2026Q2（DB 只有 2026Q1）→ 改為 DB 查最新 `insight_ready` 分期
- 空資料不再呼叫 LLM（避免幻覺分析）
- `crawl_if_missing()` 資料不存在時自動觸發 FinancialReports pipeline 爬取
- 爬取進度透過 `asyncio.Queue` 推送至 SSE（`fundamental_crawl` 事件）

### 輸出品質
- **Chip narrative prompt** 禁止複讀數字，改為解讀趨勢意涵
- **Macro narrative** 區分「真正無事件」vs「有事件但缺 consensus」
- `hard_constraint_details` 去重（原本 `net_delta_pct_nav` 顯示兩次）
- FinMind health check 實際呼叫 API 驗證 token 有效性
- TTL 快取（5 分鐘）：price_adapter / fx_adapter / cross_market_adapter

### UI 重大改版
- **左側 Agent Sidebar**：7 個 agent 列表 + 狀態點 + 點擊執行單一 agent
- **中繼資料面板**：每張 AgentCard 加「▸ 中繼資料」展開（完整 key_findings + hard constraints + errors + asof）
- **持倉管理面板**：`🛡️ 持倉` 按鈕開啟互動編輯（新增/編輯/刪除，自動寫回 YAML）
- `PipelineProgress` 依 `router.agents` 動態顯示（不再硬編碼 7 個）

---

## 三、目前狀態

```
ruff:   All checks passed（零 error 零 warning）
mypy:   0 issues, 70 source files
pytest: 1123 passed, 1 skipped, 2 deselected（deselected = llm_eval 標記，預設排除）
vitest: 146 passed（前端，覆蓋率 80%）
build:  npm run build ✓
```

---

## 四、完整架構圖

```
使用者查詢
    │
    ▼
Router LLM（intent_router.py）
    │  RouterOutput { query_type, agents[], run_supervisor, run_debate }
    │
    │  6 種 query_type（Phase 16-E 起）：
    │    stock_analysis       個股技術/籌碼/新聞      3 agent，無仲裁
    │    stock_investment     個股投資建議「值得買嗎」 6 agent（**不含 risk**）+ 仲裁 + Debate
    │    investment_strategy  組合層策略調整          7 agent（含 risk）+ 仲裁 + Debate
    │    fundamental_review   財報/基本面             2 agent，無仲裁
    │    macro_outlook        總經/市場環境           2 agent，無仲裁
    │    portfolio_risk       純 Greeks 風控查詢       risk only，無仲裁
    │
    │  ⚠️ stock_investment 與 investment_strategy 的差異是本架構最易誤解處：
    │     risk agent 讀 positions.yaml 分析的是**整個組合**，與所查個股無關。
    │     問「台積電值得買嗎」若納入 risk，組合中不相干部位的 breach 會透過
    │     Supervisor Layer 1 強制降級整個個股建議（confidence 壓到 0.35）。
    │     故個股走 stock_investment（無 risk），組合策略才走 investment_strategy。
    │
    ▼
api/main.py — _stream_analysis_with_router()
    │
    ├── [並行 asyncio Tasks]
    │   ├── technical_agent.py  → AgentSignal → DomainReport
    │   ├── chip_agent.py       → DomainReport
    │   ├── macro_agent.py      → AgentSignal → DomainReport
    │   ├── news_agent.py       → AgentSignal → DomainReport
    │   ├── cross_market_agent.py → AgentSignal → DomainReport
    │   ├── fundamental_agent.py → AgentSignal → DomainReport
    │   └── risk_agent.py       → AgentSignal → DomainReport
    │
    ├── [if run_supervisor]
    │   └── Supervisor.aggregate_debate() or .aggregate()
    │       ├── Layer 1: 硬約束掃描（breached → risk_override）
    │       ├── Layer 2: Horizon Breakdown
    │       └── Layer 3: 信心加權投票
    │
    └── SSE 串流 → React Dashboard
```

---

## 五、檔案結構

```
quantdesk-starter/
├── api/
│   └── main.py              ← FastAPI SSE + 所有 endpoint 定義
├── agents/
│   ├── chip_agent.py
│   ├── cross_market_agent.py
│   ├── debate_agents.py
│   ├── fundamental_agent.py
│   ├── macro_agent.py
│   ├── news_agent.py
│   ├── risk_agent.py
│   ├── technical_agent.py
│   ├── verifier.py
│   └── risk/
│       ├── aggregation.py   ← 組合 Greeks 加總
│       ├── binomial_tree.py ← American option 定價
│       ├── black_scholes.py ← European option 定價 + IV
│       ├── position_loader.py ← 讀 positions.yaml
│       ├── pricing_router.py  ← BS vs Binomial 路由
│       └── scenario.py       ← 壓力測試情境
├── adapters/
│   ├── base.py
│   ├── cache.py             ← TTL 快取裝飾器（新）
│   ├── chip_adapter.py      ← FinMind 籌碼
│   ├── cross_market_adapter.py
│   ├── fred_adapter.py
│   ├── fundamental_adapter.py ← 含 crawl_if_missing()
│   ├── fx_adapter.py
│   ├── macro_adapter.py
│   ├── news_adapter.py      ← Google News RSS + Tavily + MOPS
│   ├── options_adapter.py   ← FinMind IV 反推
│   ├── price_adapter.py
│   ├── stock_info_adapter.py  ← FinMind 全市場股票清單
│   └── ticker_registry.py     ← 離線代碼→名稱查表（Phase 16）
├── schemas/
│   ├── agent_signal.py      ← AgentSignal、HardConstraint 等
│   ├── debate.py
│   └── domain_report.py     ← DomainReport、RouterOutput
├── supervisor/
│   ├── graph.py             ← Supervisor 三層規則引擎 + Debate 協調
│   ├── signal.py
│   └── synthesis.py
├── router/
│   └── intent_router.py     ← LLM + regex fallback 意圖分類
├── observability/
│   └── langfuse_setup.py
├── config/
│   └── positions.yaml       ← 持倉設定（前端可互動修改）
├── data/
│   └── tickers.jsonl        ← 台股 3,133 + 美股 10,398 ticker 註冊表（進版控）
├── scripts/
│   └── refresh_ticker_registry.py  ← 重新產生上表（需 SEC_USER_AGENT）
├── tests/                   ← 1123 tests（含 Phase 17 評估框架 golden sets）
├── dashboard/
│   ├── eslint.config.js     ← ESLint flat config（Phase 19）
│   ├── vite.config.ts       ← 含 vitest 設定
│   └── src/
│       ├── App.tsx           ← 主頁（sidebar 佈局）
│       ├── types.ts
│       ├── lib/
│       │   └── validatePosition.ts  ← 持倉欄位驗證（鏡射後端契約，Phase 19）
│       ├── test/
│       │   ├── setup.ts             ← 結構性保證測試不打真實網路
│       │   └── mockEventSource.ts   ← 可控 SSE 替身
│       ├── hooks/
│       │   ├── useAnalysis.ts   ← analyze() + analyzeAgent() + retry()
│       │   └── useQueryHistory.ts
│       └── components/
│           ├── AgentCard.tsx         ← 含中繼資料展開面板
│           ├── AgentSidebar.tsx      ← 左側 Agent 選擇欄（新）
│           ├── DebatePanel.tsx
│           ├── PipelineProgress.tsx  ← 動態 agent nodes
│           ├── PositionsPanel.tsx    ← 持倉互動管理（新）
│           ├── RouterCard.tsx
│           ├── SupervisorCard.tsx
│           └── charts/
│               ├── ChipFlowChart.tsx
│               ├── RiskGreeksChart.tsx
│               └── TechnicalRadar.tsx
├── tests/                   ← 後端 1123 tests
│   ├── data/                ← golden sets（router / supervisor）
│   ├── test_eval_prompts.py       ← Phase 17 L1
│   ├── test_eval_router.py        ← Phase 17 L2
│   ├── test_eval_supervisor.py    ← Phase 17 L3
│   └── test_eval_faithfulness.py  ← Phase 17 L4
└── docs/
    ├── SESSION_HANDOFF.md   ← 本文件
    ├── spec.md
    └── tasks/phase_16.md    ← Phase 16-17 完整計畫與執行紀錄
```

---

## 六、環境變數（.env）

| 變數 | 用途 | 必要性 |
|------|------|--------|
| `OPENAI_API_KEY` | Router LLM / Synthesis / News LLM / Fundamental narrative | ⚠️ 沒有就降級 |
| `FINMIND_TOKEN` | FinMind IV 反推 / 籌碼資料 | ⚠️ 沒有 IV 用 placeholder 0.20 |
| `TAVILY_API_KEY` | 新聞第三層搜尋 | ✅ 選填 |
| `LANGFUSE_ENABLED` | Langfuse tracing | ✅ 選填 |
| `FINANCIAL_DB_PATH` | SQLite 財報資料庫 | ⚠️ 預設 `../FinancialReports/data/financial.db` |
| `LANGFUSE_TRACING_ENABLED` | Langfuse **SDK 官方**開關。`langfuse.openai` drop-in 只看這道，不看 `LANGFUSE_ENABLED` | ✅ 選填（測試中強制 false） |
| `SEC_USER_AGENT` | `scripts/refresh_ticker_registry.py` 抓 SEC 美股清單用。SEC 規定 User-Agent 須含聯絡方式，否則被擋 | ⚠️ 執行該 script 時需要 |

---

## 七、已知問題與 Tech Debt

> **狀態對帳日期：2026-08-09**（Phase 16/17/19 完成後全面複驗）
> ⬜ 未解 ／ ✅ 已解

### P0（影響結果正確性）
1. ✅ **Risk agent 是組合層級，非個股**：`positions.yaml` 分析的是整個投資組合，不是查詢的個股。`stock_analysis` 模式現在已排除 risk agent，但 `investment_strategy` 仍會跑 risk 並可能因組合 breach 強制降級整個策略建議。
   → **Phase 16-E 已修復**：新增 `stock_investment` query_type 做個股/組合語意切分。
2. ⬜ **FinMind IV 反推成功率未知**：`iv_source: "placeholder_0.20: 3/3"` 表示沒有取到真實 IV，需要確認 `FINMIND_TOKEN` 是否正確設定且帳號有 TXO 選擇權資料權限。
   → 需**人工確認 FinMind 帳號權限**，非程式問題，Phase 16 不處理。

### P1（輸出品質）
3. ⬜ **Fundamental narrative 偶爾空白**：`OPENAI_API_KEY` 正確但若網路超時仍會空白。
4. ⬜ **Macro degraded 模式**：FRED 資料沒有 consensus，`computable_count=0`，信心只有 0.1，實際上有 15 個事件卻顯示降級。
5. ✅ **新聞面 Tavily 搜尋詞**：原本 `TW_STOCK_NAMES` 只有手寫 30 檔，其餘台股沒有中文名、只能搜股票代碼。
   → **已修復**：改由 `adapters/ticker_registry.py` 離線查表（`data/tickers.jsonl`，台股 3,133 + 美股 10,398 = 13,531 筆），涵蓋率 30 → 13,531。

### P2（待優化）
6. ⬜ **持倉 YAML 需每月手動更新到期日**：選擇權 expiry 到期後 position_loader 會報 T≤0 錯誤。應加入自動跳過已到期部位的邏輯。
7. ✅ **Frontend bundle size 大**：642KB（含 recharts）。
   → **Phase 19 已修復**：三張圖表改用 React.lazy 動態載入，recharts 拆為按需 chunk。
     主 bundle 646.62 kB → **254.01 kB**（gzip 190.25 → 76.91 kB，降 60%），
     >500kB 警告消失。多數查詢不會同時跑到 risk/technical/chip，本來就不需載入 recharts。
8. ✅ **單 agent endpoint 無 symbol 輸入提示**：sidebar 點 agent 時用當前 Router 解析到的 symbol，若沒有則預設 2330。
   → **已修復**：Phase 15 移除硬編碼 2330 fallback，未選標的時 agent 按鈕一律 `disabled`
     並顯示「請先搜尋並選擇標的」；Phase 19 補上測試守護（七個按鈕全部停用）。
9. ✅ **PositionsPanel 未做欄位驗證**：例如 option 沒填 strike 可能導致 risk agent 失敗。
   → **Phase 19 已修復**：新增 `dashboard/src/lib/validatePosition.ts`，
     規則逐條鏡射後端 `position_loader.py::_parse_row()`（含 T≤0 到期防呆）。
     驗證未過不送出 PUT，錯誤就地顯示（含 aria-invalid / role=alert）。
     30 個測試涵蓋純函式邏輯 + UI 接線整合。

### P0'（2026-08-05 新發現，原清單未涵蓋）
10. ✅ **chip_agent 是唯一未接 Verifier 的 agent**：`_llm_synthesize_chip()` 僅靠 prompt 文字約束 LLM 不複讀數字，無程式化檢查。**違反設計原則①**。
    → **Phase 16-B 已修復**（處置比照其餘 5 agent：記錄錯誤、保留 narrative）。
11. ✅ **測試依賴本機 `.env`**：`.env` 的 `LANGFUSE_ENABLED=true` 讓 `pytest` 對 Langfuse 發真實網路請求並逾時。同一顆測試在不同機器行為不同。
    → **Phase 16-C 已修復**。注意需**兩道**閘門：`LANGFUSE_ENABLED`（專案自訂）
      + `LANGFUSE_TRACING_ENABLED`（SDK 官方，drop-in 只看這道）。
12. ✅ **Langfuse 完全沒有 token / cost 追蹤**：8 個 LLM 呼叫點皆未帶 `usage_details`，dashboard 上成本為空。
    → **Phase 16-D 已修復**（8/8 改用 `langfuse.openai` drop-in）。
      **2026-08-09 已實測驗證**：真實 gpt-4o-mini 呼叫後查 Langfuse public API，
      generation 節點帶 `model=gpt-4o-mini-2024-07-18`、
      `usage={input:288, output:96, total:384}`、`cost=$0.0001008`。
      對照組：修復前的 `debate:*_llm_call` 歷史紀錄為 `model=None, usage=0, cost=None`。
13. ✅ **LangChain 孤例 + 隱性跨 repo 依賴**：`fundamental_agent.py:555` 是全專案唯一用 `langchain_openai` 的地方，且 langchain 未在本專案 `pyproject.toml` 宣告，靠 `financial-agent` 順帶帶入。
    → **Phase 16-D 已修復**，agents/ router/ supervisor/ api/ schemas/ 已無 langchain。
14. ✅ **改任何 prompt，測試全部照過**：無任何測試斷言 prompt 內容，LLM 一律 mock。
    → **Phase 17 L1 已修復**：`tests/test_eval_prompts.py`（31 項）——
      6 個 prompt 的 SHA256 快照 + 不變量（設計原則①②、OpenAI json_object
      硬性要求、`_QUERY_TYPE_AGENTS ↔ _ROUTER_SYSTEM_PROMPT` 跨檔案一致性、
      prompt-injection 防線）。已用突變測試驗證 4 種真實 bug 都會被抓到。
    → **Phase 17 Evaluation Framework 處理**。
15. ✅ **agents/verifier.py 對 4 位數以上數字全盲**（Phase 16-F 新發現）：
    `_NUM_RE` 只匹配 ≤3 位數或帶逗號格式，`1234` / `987654` / `-80773006`
    完全偵測不到——正是 LLM 在財經語境最常幻覺的型態。影響全部 6 個 agent。
    → **Phase 16-F 已修復**（regex + `symbol_allowlist()` 處理台股 4 碼誤判）。

---

## 八、下一步建議方向

### 優先（功能缺失）
1. **個股分析問投資建議但 risk 套組合**：考慮新增一個「個股投資建議」模式，risk agent 改成分析個股 beta 曝險而非整組合 Greeks
2. **Macro agent 真實 consensus 資料**：目前 TradingEconomics 沒有 consensus → surprise 都算不出來。考慮改用 Trading Economics Premium 或自建 consensus DB
3. **News Tavily 質量提升**：加入 domain 限定（`include_domains=["cnyes.com", "moneydj.com", "money.udn.com"]`）讓台股新聞更精準

### 工程優化
4. **Frontend code splitting**：recharts 獨立 chunk，主 bundle 壓縮到 <300KB
5. **Positions YAML validation**：前端 PUT 前做 Zod 驗證
6. **到期部位自動跳過**：`position_loader.py` 加 `skip_expired=True` 選項

### 新功能
7. **多標的掃描模式**：`multi_stock_scan` query_type 目前路由存在但沒有實作
8. **歷史回測**：technical agent 加回測模式（歷史訊號準確率）
9. **Alert 系統**：當 hard constraint breach 或重大新聞時推播通知

---

## 九、啟動指令

```bash
# 後端
cd /mnt/c/Users/johnn/GITHUB_REPO/quantdesk-starter
uv run uvicorn api.main:app --reload --port 8000

# 前端（另一個 terminal）
cd dashboard
npm run dev

# 瀏覽器
open http://localhost:5173
```

---

## 十、三關驗收

```bash
uv run ruff check .   # lint — 零 error
uv run mypy .         # 型別檢查 — zero issues
uv run pytest -q      # 1123 passed
cd dashboard && npm run build  # frontend build
```

---

## 十一、重要設計決策（不要破壞）

1. **LLM 永遠不產出數字**：narrative 裡的數字必須來自 metrics，`verifier.py` 會掃描違規
2. **硬約束是強制規則**：`hard_constraints[].breached=True` → Supervisor 必須降級，不能被 LLM 覆蓋
3. **每個 key_evidence 必須有 source + asof**
4. **新增 agent 不動 Supervisor 核心**：只加 node 並在 `_stream_analysis_with_router` 的 `_ALL_BUILDERS` dict 裡註冊
5. **依賴管理用 uv**：`uv add <pkg>`，不用 pip。本專案**沒有** requirements.txt（刻意移除，見下）
6. **不直接 push main**：所有工作在 feature branch，PR 合併前 CI 必須全綠
