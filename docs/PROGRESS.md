# 專案進度

> 最後更新：2026-08-05
> **本檔只記錄「當前狀態 + 下一步」。完整 Phase 清單見 `CLAUDE.md` 目前進度，
> 詳細交接見 `docs/SESSION_HANDOFF.md`，當前計畫見 `docs/tasks/phase_16.md`。**

---

## 當前狀態（2026-08-09）

- **已完成**：Phase 0-17、19
  - 0-15：骨架 → 七個 domain agent → Supervisor 仲裁 → Debate → React SSE Dashboard → Query-Type Routing
  - 16：Truth & Correctness（文件真實性對帳 + chip Verifier + verifier 4 位數修復 + SDK 統一 + query_type 切分）
  - 17：Evaluation Framework（prompt 快照 / router golden set / supervisor 情境 / faithfulness）
  - 19：前端品質（vitest + ESLint + code splitting）
- **測試基準**：後端 1123 passed / 1 skipped / 2 deselected、前端 146 passed（覆蓋率 80%）
- **品質關卡**：ruff / mypy / eslint 全綠、0 import 循環依賴
- 全部已合併進 `main`（PR #34 #35 #36 #37），無殘留分支

> 註：無 Phase 18。原規劃的編號在 Phase 16 重評估時併入 16/17，未另立。

---

## 下一步

**沒有明確待辦。** `docs/SESSION_HANDOFF.md` §七 剩餘未解項目多屬下列兩類，
不是寫程式能解決的：

| 項目 | 性質 |
|---|---|
| FinMind IV 反推成功率未知 | 需**人工確認** FinMind 帳號是否有 TXO 選擇權資料權限 |
| Macro degraded 模式 | FRED 免費資料源本身沒有 consensus 值 |
| Tavily 搜尋詞品質 | 外部搜尋服務的結果品質，非本專案邏輯 |
| 持倉到期日需手動更新 | 待接券商 API 才有意義（見 `position_loader.py` tech-debt 註記） |

若要繼續投入，價值較高的方向：
1. `api/main.py` 覆蓋率仍偏低（64%），SSE 串流與錯誤路徑大片未測
2. `adapters/cross_market_adapter.py` 44%、`fx_adapter.py` 57%——
   未測的程式碼與自承 tech-debt 的程式碼高度重疊
3. `multi_stock_scan` scenario 已定義但未實作（`SESSION_HANDOFF` §八）

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
- **文件的『下一步』區塊最容易腐化**：改狀態時只更新上半部、忘了下方的待辦清單，
  會讓同一份文件自相矛盾（2026-08-09 實際發生，Phase 19 收尾時只改了『當前狀態』）。
  改任何進度描述時，整份檔案讀過一遍再存。
- **文件會腐化**：改動測試數、agent 數、架構描述時，同步檢查
  `CLAUDE.md` / `README.md` / `docs/spec.md` / `docs/SESSION_HANDOFF.md` 是否需要一起改
