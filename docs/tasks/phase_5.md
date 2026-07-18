# Phase 5 — Supervisor 匯總層做實（序列）

## 目標
把 supervisor/graph.py 從 stub 升級成三層仲裁。

## 三層仲裁
1. 硬約束優先（規則層）：任一 risk agent hard_constraint breached → 最終建議強制降級/加註強制警告。
   由規則引擎執行，不讓 LLM 裁量。
2. 時間框架分層：按 time_horizon 分層呈現（短期看空/長期看多），不強融成一句話。
3. 信心加權：同一時間框架內按 confidence × data_quality × 來源可靠度加權，非等權平均。
   先用規則式權重（財報 > 新聞的基礎可靠度）。

## Supervisor 的 LLM 只做
- 把結構化分層結論轉成帶引用的白話投研摘要
- 不算數字、不決定是否忽略硬約束、不隨意調權重

## 完成標準
- golden set（歷史情境 + 專家標註）評估匯總是否合理、硬約束是否正確觸發
- 衝突情境測試：技術面看多 + 風控觸限 → 最終必須降級
- uv run pytest 全過
