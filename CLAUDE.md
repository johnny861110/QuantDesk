# QuantDesk 開發守則（Claude Code Constitution）

> 這是本 repo 的憲法。每個 session、每個 subagent 都必須遵守。違反以下任一條，即為錯誤實作。

## 專案是什麼
QuantDesk 是一個多智能體量化投研系統：一個 Supervisor 匯總六個獨立的 domain agent
（risk / technical / fundamental / news / macro / cross_market），可個別使用，
也可匯總成綜合投資評估。完整規格見 `docs/spec.md`。

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
- 框架：**LangGraph**。每個 domain agent 是一個 node，Supervisor 是編排 graph。
- 所有 domain agent 都輸出 `AgentSignal`（見 schema），**絕不輸出自由文字給 Supervisor**。
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
- [ ] Phase 9：Multi-agent Debate（Bull/Bear/PM async）
- [ ] Phase 10：Streamlit Dashboard（Router → Debate → Supervisor 全流程視覺化）
