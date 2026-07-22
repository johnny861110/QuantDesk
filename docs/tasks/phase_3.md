# Phase 3 — 技術面 + 跨市場 Agent（可平行）

## ⚠️ 平行開發前置提醒（Phase 2 教訓）
- pyproject.toml / uv.lock 是全域共用檔案，如果 Phase 3、Phase 4 要平行開跑，
  兩邊需要的新套件（技術指標庫如 pandas-ta、新聞/總經 SDK）建議先在 main 上
  一次性 uv add 加好，避免兩條 branch 各自加套件互撞。
- 每次 commit 前依序：uv run ruff check . → uv run mypy . → uv run pytest -q，
  三關全過才能 push，不能只跑 pytest。
- narrative 數字檢查一律呼叫 agents/verifier.py 的 check_narrative()，不要各寫一份。

## 技術面 Agent (agents/technical_agent.py)
- 確定性計算：均線/MACD/RSI/KD/布林/量能/型態辨識（pandas-ta 或自實作），LLM 不算指標
- 資料節奏近即時（日線/盤中），需獨立 pipeline 與快取
- 盤整盤時自我標註「區間震盪，技術訊號可信度低」（反映在 confidence），不硬給方向
- 輸出 time_horizon = short/intraday
- adapters/price_adapter.py（yfinance/券商/公開行情）

## 跨市場 Agent (agents/cross_market_agent.py)
- 確定性計算：滾動窗口相關係數、領先落後分析、beta、背離偵測
- 用滾動窗口而非固定歷史相關係數（連動關係隨 regime 改變），標註當前 regime 相關性
- 常作為其他 agent 的市場背景，而非獨立進出場訊號
- adapters/cross_market_adapter.py（yfinance 美股指數 / 台指期源）

## 完成標準
- 兩個 agent 各自獨立產出 AgentSignal
- 指標計算有單元測試對答案
- uv run pytest 全過
