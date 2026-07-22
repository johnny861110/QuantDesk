# Phase 4 — 新聞 + 總經 Agent（可平行）

## ⚠️ 平行開發前置提醒（Phase 2 教訓）
- pyproject.toml / uv.lock 是全域共用檔案，如果 Phase 3、Phase 4 要平行開跑，
  兩邊需要的新套件（技術指標庫如 pandas-ta、新聞/總經 SDK）建議先在 main 上
  一次性 uv add 加好，避免兩條 branch 各自加套件互撞。
- 每次 commit 前依序：uv run ruff check . → uv run mypy . → uv run pytest -q，
  三關全過才能 push，不能只跑 pytest。
- narrative 數字檢查一律呼叫 agents/verifier.py 的 check_narrative()，不要各寫一份。

## 新聞 Agent (agents/news_agent.py)
- agentic RAG：檢索 → 事件去重（同事件多家轉載）→ 來源可信度加權 → 摘要 → 情緒判斷
- 明確區分「已發生事實」與「市場傳聞」，反映在 confidence / data_quality
- 情緒分析要用財經語境（「符合預期」可能中性偏空，已 price in），不用通用 sentiment 詞頻
- adapters/news_adapter.py（NewsAPI/RSS/公開資訊觀測站）
- ⚠️ 外部內容進 prompt 前要與系統指令結構化分隔（prompt injection 防護）

## 總經 Agent (agents/macro_agent.py)
- 核心：訊號是「實際 vs 市場預期(consensus)」的 surprise，不是絕對值
- 必須抓 consensus 值做比較（CPI 3% 是好是壞取決於預期）
- 事件驅動（數據公布時觸發），平時靜默
- 注意「好數據對股市不一定是好事」（過熱→升息預期）
- adapters/macro_adapter.py（Trading Economics API，有免費額度）

## 完成標準
- 新聞 agent 能去重並輸出帶來源的事件摘要
- 總經 agent 輸出 surprise 方向而非絕對值
- uv run pytest 全過
