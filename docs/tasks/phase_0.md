# Phase 0 — 骨架先行（序列，不可平行）

## 目標
建立系統骨架，不含任何 domain 邏輯。這是六個 agent 的共同語言，必須先鎖定並經人工 review。

## 為什麼不可平行
schema 是六個 agent 的共同合約。若未鎖定就讓多個 subagent 各寫各的 agent，
它們會發明不相容的輸出格式，最後 Supervisor 接不起來。先序列立骨架，再平行長肌肉。

## 產出（起手包已附骨架，此 Phase 主要是補完 + 驗證）
1. `schemas/agent_signal.py` — AgentSignal（已附，review 是否符合 docs/spec.md 第3節）
2. `adapters/base.py` — DataSourceAdapter 抽象基類（已附）
3. `supervisor/graph.py` — Supervisor 骨架，能接收 signal list（已附 stub）
4. `tests/test_phase0.py` — 骨架驗證（已附）
5. 依賴用 uv 管理：`pyproject.toml` 已定義 dependencies（pydantic, langgraph）與
   dev group（pytest, ruff, mypy）。執行 `uv sync` 產生 `.venv` 與 `uv.lock`。
   需要新套件用 `uv add <pkg>`，**不要用 pip、不要手改 requirements.txt**。

## 完成標準
- `uv run pytest -q` 全過
- 能用假 signal 跑通 `Supervisor().aggregate(signals)`
- 人工 review 確認 schema 涵蓋 spec 第3節所有欄位

## 完成後
停下，讓人 review。確認 schema 鎖定後，才進入 Phase 1。
