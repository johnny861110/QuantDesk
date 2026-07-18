# Phase 6 — Production 硬化 + Multi-agent Debate（進階）

## 硬化
- API 認證（Financial_Agent 目前無 auth，是明確缺口）：加 API key / OAuth
- 統一 observability：兩 repo 的 Langfuse trace 打通成同一套，端到端回放
- HITL Gate：帶假設的分析（CAPM beta/稅率）信心低於門檻標記人工複核
- 資料一致性：財報重編/數據修正的版本標記與 cache invalidation
- 明確免責：系統定位為研究輔助與風險提示，非自動下單

## Multi-agent Debate（roadmap 延伸）
- Bull agent / Bear agent 各用同一組工具產出對立論點
- PM agent 仲裁
- 因工具與資料層已在 Phase 1-5 打穩，這步主要是 prompt/graph 設計

## 完成標準
- 對外 API 需認證
- 端到端 trace 可回放
- 衝突 debate 能產出平衡結論 + PM 仲裁
