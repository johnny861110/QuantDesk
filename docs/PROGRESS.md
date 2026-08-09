# 專案進度

> 最後更新：2026-08-05
> **本檔只記錄「當前狀態 + 下一步」。完整 Phase 清單見 `CLAUDE.md` 目前進度，
> 詳細交接見 `docs/SESSION_HANDOFF.md`，當前計畫見 `docs/tasks/phase_16.md`。**

---

## 當前狀態（2026-08-05）

- **已完成**：Phase 0-15（骨架 → 七個 domain agent → Supervisor 仲裁 → Debate →
  React SSE Dashboard → Query-Type Routing）
- **已完成**：Phase 16 — Truth & Correctness（branch `phase-16-hardening`，7 顆 commit）
- **已完成**：Phase 17 — Evaluation Framework（branch `phase-17-evaluation`，L1-L4 全數）
- **已完成**：Phase 19 — 前端品質（branch `phase-19-frontend`）
- **下一步**：無明確待辦；SESSION_HANDOFF §七 剩餘項目多為需人工確認或外部資料源限制
- **測試基準**：後端 1123 passed、前端 146 passed（覆蓋率 80%）、
  ruff & mypy & eslint 全綠、0 import 循環依賴

---

## 下一步

Phase 16 已全數完成（16-A/B/C/D/E + 計畫外的 16-F verifier 修復）。

**下一步：Phase 17 Evaluation Framework**（見 `docs/tasks/phase_16.md` §四）
1. L1 prompt 快照測試
2. L2 Router golden set（≥30 組，純確定性可進 CI）
3. L3 Supervisor S1-S5 情境改資料驅動 fixture
4. L4 narrative faithfulness 評分器（復用 `verifier.py::check_narrative`）

**待人工驗證**（pytest 測不出來）：在 `LANGFUSE_ENABLED=true` 環境跑一次
demo script，確認 dashboard 上各 agent generation 節點有 token 用量與成本。

---

## 歷史教訓（跨 Phase 通用，勿刪）

- `pyproject.toml` / `uv.lock` 是全域檔案，平行開發前先統一把需要的套件一次裝好
- 三關驗收（ruff → mypy → pytest）要寫進每個 subagent 的任務描述
- 新 agent 的 narrative 檢查一律用 `agents/verifier.py` 共用模組，不要各寫一份
  （Phase 7 的 chip_agent 違反此條，Phase 16-B 已修補）
- 共用模組（`verifier.py` / `adapters/base.py` / `schemas/` / `pyproject.toml`）
  的改動要整包進同一顆 commit，commit 前先跑 `git status` 確認沒有遺漏
- **共用防線也要測**：agents/verifier.py 的 4 位數偵測缺陷存在已久，三關全綠、
  888 個測試都沒測出來，是補一個 0 覆蓋函式的測試時才發現的（Phase 16-F）。
  共用模組的核心邏輯要有直接的邊界測試，不能只靠 caller 的間接覆蓋
- **文件會腐化**：改動測試數、agent 數、架構描述時，同步檢查
  `CLAUDE.md` / `README.md` / `docs/spec.md` / `docs/SESSION_HANDOFF.md` 是否需要一起改
