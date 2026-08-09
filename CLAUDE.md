# QuantDesk 開發守則（Claude Code Constitution）

> 這是本 repo 的憲法。每個 session、每個 subagent 都必須遵守。違反以下任一條，即為錯誤實作。

## 專案是什麼
QuantDesk 是一個多智能體量化投研系統：一個 Supervisor 匯總七個獨立的 domain agent
（risk / technical / fundamental / news / macro / cross_market / chip），可個別使用，
也可匯總成綜合投資評估。完整規格見 `docs/spec.md`。

> ⚠️ `docs/spec.md` 為 v1.0 原始藍圖，寫於 chip agent（Phase 7 新增）之前，
> 全篇以「六大 domain agent」描述。閱讀時以本檔與 `README.md` 為準。

## 三條不可違反的設計原則
1. **確定性計算與 LLM 嚴格分離**：Greeks、財務指標、技術指標、統計量一律由純函數
   （deterministic Python）產出。LLM 只負責路由、組織語言、寫白話說明，
   **永遠不產出數字**。任何 `narrative` 欄位裡出現的數字，都必須來自 `metrics` /
   `key_evidence` 裡經工具算出的值。
2. **風控是硬約束，不是投票的一票**：任何 domain agent 的 `hard_constraints[].breached == true`
   一旦出現，Supervisor 的最終建議必須強制降級或加註強制警告。這由**規則引擎**執行，
   不得讓 LLM 自由裁量是否忽略。目前已知會發起 hard_constraints 的 agent：
   risk agent（Greeks 曝險限制）、fundamental agent（EWS critical/high 財務預警）。
3. **每個判斷都要帶來源與時間戳**：所有 agent 輸出必須符合 `schemas/agent_signal.py`
   的 `AgentSignal`，且 `key_evidence` 每一項都要有 `source` 與 `asof`。

## 架構鐵則
- 框架：**LangGraph**。每個 domain agent 內部是一張 `StateGraph`（fetch → compute → signal）。
- **Supervisor 刻意「不」使用 `StateGraph`**：`supervisor/graph.py` 是純 Python 三層規則引擎
  （檔名沿用歷史命名，內容無任何 `add_node`）。理由是設計原則②要求 hard_constraint
  強制降級由規則引擎執行、不得被 LLM 自由裁量——用純函式而非 graph node，
  可確保這條路徑不會被圖的條件路由或 LLM 悄悄繞過。**不要在 Supervisor 裡加 LangGraph node**。
- 所有 domain agent 都輸出 `AgentSignal`（見 schema），**絕不輸出自由文字給 Supervisor**。
- **雙 schema 邊界（2026-08-05 拍板凍結）**：`schemas/agent_signal.py` 的 `AgentSignal`
  是唯一的跨 agent 共同契約。`schemas/domain_report.py` 的 `DomainReport` 僅
  `chip_agent` 使用，經 `domain_report_to_agent_signal()` 橋接回 `AgentSignal` 才進 Supervisor。
  `docs/refactor_plan.md` 原規劃的「七個 agent 全面 ReAct 化 / 全面改用 DomainReport」
  （該文 Phase D）**已正式放棄**，不要再依該文件的目標架構動工。理由見 refactor_plan.md 開頭註記。
- 所有外部資料存取都走 `adapters/` 的抽象介面，**agent 內不得直接呼叫外部 API**
  （不得在 agent 裡直接 import yfinance / requests 打新聞站）。
- 新增一個 domain agent **不得修改 Supervisor 核心**——只能新增 node 並註冊。

## 開發規範
- **schema 先行**：任何 agent 先寫 schema 契約與測試，再寫實作。
- **不准 mock 掉 Verifier**：fundamental agent 的數字驗證是核心功能，不是裝飾。
- **每個 Phase 結束一定有可驗證產出**：三關驗收標準，缺一不可：
  ```
  uv run ruff check .   # lint — 零 error，零 warning
  uv run mypy .         # 型別檢查 — zero issues
  uv run pytest -q      # 所有測試全過
  ```
  **順序固定**：先 ruff → mypy → pytest。三關全過才算完成，不允許只跑 pytest。
- **一次一個 Phase**：除非明確被指示平行，否則不要跨 Phase 動工。
- **依賴管理一律用 uv**（Python 3.11+）：加依賴用 `uv add <pkg>` / `uv add --dev <pkg>`，
  **絕不要用 `pip install`，也不要手動編輯 requirements.txt**。依賴定義在 `pyproject.toml`，
  鎖定在 `uv.lock`（進版控）。所有指令透過 `uv run` 執行，不手動 activate venv。

## 平行開發規則（重要）
- **Phase 0（骨架）不可平行**，必須由主 session 序列完成並經人工 review。
  原因：schema 是六個 agent 的共同語言，未鎖定前平行會產生不相容的輸出格式。
- **Phase 1-4（六個 agent）可平行**，但每個 subagent 必須嚴格遵守已鎖定的
  `schemas/agent_signal.py`，不得自行擴充或修改共同 schema。
- 若某個 agent 需要 schema 沒有的欄位，**先停下來問人**，不要擅自改共同 schema。

## Git / 版控規則（重要）
- **絕不直接 push 到 `main`**。所有工作在對應 Phase 的 branch 上進行：
  `phase-0-bootstrap`、`phase-1-fundamental`、`phase-2-risk-greeks`、
  `phase-3-technical-crossmarket`、`phase-4-news-macro`、`phase-5-supervisor`、`phase-6-hardening`。
- 平行開發時每個 subagent 只在自己負責的 branch 上 commit，不跨 branch 動別人的檔案。
- Commit message 用 conventional commits 並標註對應 phase：
  `feat(risk): implement Black-Scholes greeks engine (phase_2)`
- 每個 Phase 完成、`uv run pytest` 全過後才開 PR，PR 描述附上完成標準逐項打勾。
- 合併前 CI（`.github/workflows/ci.yml`）必須全綠（lint + test）。
- **schema 鎖定的 commit 要打 tag**（例如 `v0.1-schema-locked`），之後 Phase 2-4 的平行
  branch 一律從這個 tag 分出去，確保六個 agent 開發期間共同合約不會漂移。
- **共用模組必須整包進同一顆 commit（hotfix 教訓）**：
  涉及共用檔案（`agents/verifier.py`、`adapters/base.py`、`schemas/`、
  `pyproject.toml`/`uv.lock`）的改動，commit 前務必先跑 `git status`，
  確認所有「順手改到但沒明確提到」的共用模組全部一起進了同一顆 commit，
  不要遺漏。合併 PR 後，建議額外在乾淨環境（`git pull` 後直接跑，
  不依賴 working tree 累積的狀態）驗證一次完整測試通過，而不是只信任
  CI 綠燈與 merge 按鈕——CI 拉到的是 merge commit，但它讀的 `.venv`
  若有殘留狀態可能掩蓋缺失的 `uv.lock` 更新。

## 常用指令（全部透過 uv）
- 環境同步：`uv sync`（建 .venv + 裝依賴 + 鎖定 uv.lock）
- 測試：`uv run pytest -q`
- 型別檢查：`uv run mypy .`
- Lint：`uv run ruff check .`
- 加依賴：`uv add <pkg>` / `uv add --dev <pkg>`

## 目前進度
- [x] Phase 0：骨架（schema + Supervisor 殼 + adapter 基類）
- [x] Phase 1：財報 domain（接 FinancialReports + Financial_Agent）
- [x] Phase 2：Greeks 風控引擎
- [x] Phase 3：技術面 + 跨市場（technical_agent + cross_market_agent，824 tests）
- [x] Phase 4：新聞 + 總經（news_agent + macro_agent + FRED 免費資料源）
- [x] Phase 5：Supervisor 仲裁（三層規則引擎：硬約束 + 時間框架 + 信心加權）
- [x] Phase 6：Production 硬化（HITL Gate + Langfuse + disclaimer）
- [x] Phase 7：Agentic 重構（Router LLM + Chip Agent + Synthesis LLM + DomainReport schema）
- [x] Phase 8：Supervisor × Synthesis 整合（aggregate_agentic + demo script 接線）
- [x] Phase 9：Multi-agent Debate（async Bull/Bear/PM + Supervisor.aggregate_debate()）
- [x] Phase 10：React + TypeScript + SSE Dashboard（FastAPI backend + 豐富 UI）
- [x] Phase 11：Agent 功能全面驗測與修復（全 6 agent 接線、debate method 修正、UI error card + elapsed time）
- [x] Phase 12：Core Accuracy（FinMind IV 接入 risk agent、GDP/失業率 pp-fix、fred_adapter logging）
- [x] Phase 13：Frontend Pro（recharts 圖表、hard constraints 明細、查詢歷史、JSON 匯出、重試、時間戳）
- [x] Phase 14：Engineering Quality（E2E SSE 測試、CI frontend build、pytest-cov 覆蓋率）
- [x] Phase 15：Query-Type Routing + Async 重構 + 新聞修復 + AgentSidebar / PositionsPanel
- [x] Phase 16：Truth & Correctness（文件真實性對帳 + chip Verifier + 測試隔離 +
      SDK 統一/Langfuse cost + query_type 個股/組合切分）— 計畫見 `docs/tasks/phase_16.md`
- [x] Phase 17：Evaluation Framework（prompt 快照 / router golden set /
      supervisor 資料驅動情境 / narrative faithfulness）— 兌現 `docs/spec.md §8.2`

- [x] Phase 19：前端品質（vitest + ESLint + code splitting，dashboard 146 tests / 80% 覆蓋）

> 測試基準：後端 **1123 passed / 1 skipped / 2 deselected**、
> 前端 **146 passed**（覆蓋率 80%）（2026-08-09 實測）。
> deselected 是 `llm_eval` 標記的真實 LLM 評估，預設排除（會產生 API 費用），
> 手動執行：`uv run pytest -m llm_eval -q -s`
> 修改此數字前請先實跑 `uv run pytest -q --cov`，不要沿用舊值。

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **QuantDesk** (4191 symbols, 8366 relationships, 226 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/QuantDesk/context` | Codebase overview, check index freshness |
| `gitnexus://repo/QuantDesk/clusters` | All functional areas |
| `gitnexus://repo/QuantDesk/processes` | All execution flows |
| `gitnexus://repo/QuantDesk/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
