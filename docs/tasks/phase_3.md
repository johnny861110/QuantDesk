# Phase 3 — 技術面 + 跨市場 Agent（可平行）

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
