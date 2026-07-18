# Phase 1 — 打通財報 Domain（最高投報率，先做這個）

## 目標
把現有兩個 repo（FinancialReports + Financial_Agent）合併成一個符合 AgentSignal 的
財報 domain agent。這是後面所有 phase 的地基。

## 前置
Phase 0 完成、schema 鎖定。

## 子任務
### 1a — 資料層打通
- 把 Financial_Agent 的 `data_loader` 從讀靜態 JSON 改成讀 FinancialReports 的
  `financial_facts` / `financial_metrics`（SQLite 或包一層 FastAPI）。
- 把 confidence / quality_score 帶進下游分析的 Pydantic model。
- 實作 `adapters/fundamental_adapter.py`（繼承 FundamentalAdapter）。

### 1b — 補完 RAG 敘述層
- 把 FinancialReports 的 `vector_store.py`（目前 ChromaDB 佔位）做實：pgvector + embedding。
- 實作 structure-aware chunking（表格整塊 + 摘要索引；敘述用 recursive splitting）。
- 參考 docs/spec.md 第4.3節與另一份 RAG 文件。

### 1c — 包成 domain agent
- `agents/fundamental_agent.py`：內部 Router → Decomposer → Tool Selection →
  **Verifier**（生成的每個數字比對 financial_facts 是否一致）→ Synthesizer。
- 輸出符合 `schemas/agent_signal.py` 的 AgentSignal，time_horizon = medium/long。

## 完成標準
- 財報 agent 能獨立回答量化（ROIC/WACC）與質化（管理層展望）問題
- Verifier 測試：故意讓 LLM 生成錯誤數字，驗證會被打回
- 所有輸出數字都帶 source 指回原始 filing
- `uv run pytest -q` 全過

## ⚠️ 鐵則
- 不准 mock 掉 Verifier
- LLM 不得產出數字，只組織語言
