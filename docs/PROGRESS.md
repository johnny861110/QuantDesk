# 專案進度

> 最後更新：2026-08-05
> **本檔只記錄「當前狀態 + 下一步」。完整 Phase 清單見 `CLAUDE.md` 目前進度，
> 詳細交接見 `docs/SESSION_HANDOFF.md`，當前計畫見 `docs/tasks/phase_16.md`。**

---

## 當前狀態（2026-08-05）

- **已完成**：Phase 0-15（骨架 → 七個 domain agent → Supervisor 仲裁 → Debate →
  React SSE Dashboard → Query-Type Routing）
- **進行中**：Phase 16 — Truth & Correctness（branch: `phase-16-hardening`）
- **測試基準**：888 passed / 1 skipped、覆蓋率 85%、ruff & mypy 全綠、0 import 循環依賴

---

## 下一步

依 `docs/tasks/phase_16.md` 執行順序：

1. **16-A 文件真實性對帳**（進行中）— 修正文件宣稱與實作的落差 D1-D12
2. 16-B chip_agent 接上 Verifier（憲法違規修補）
3. 16-C 測試環境隔離（Langfuse 逾時）
4. 16-D SDK 統一 + Langfuse cost 追蹤（**須在 16-B 之後**）
5. 16-E `stock_investment` query_type 切分
6. Phase 17 Evaluation Framework（L4 依賴 16-B）

---

## 歷史教訓（跨 Phase 通用，勿刪）

- `pyproject.toml` / `uv.lock` 是全域檔案，平行開發前先統一把需要的套件一次裝好
- 三關驗收（ruff → mypy → pytest）要寫進每個 subagent 的任務描述
- 新 agent 的 narrative 檢查一律用 `agents/verifier.py` 共用模組，不要各寫一份
  （Phase 7 的 chip_agent 違反此條，Phase 16-B 修補中）
- 共用模組（`verifier.py` / `adapters/base.py` / `schemas/` / `pyproject.toml`）
  的改動要整包進同一顆 commit，commit 前先跑 `git status` 確認沒有遺漏
- **文件會腐化**：改動測試數、agent 數、架構描述時，同步檢查
  `CLAUDE.md` / `README.md` / `docs/spec.md` / `docs/SESSION_HANDOFF.md` 是否需要一起改
