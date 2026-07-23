# QuantDesk Refactor Plan — Agentic Multi-Domain Reasoning System

> 版本：v1.0 | 日期：2026-07-23 | 作者：主 session（claude-sonnet-4-6）
>
> 目標：把整個系統從「確定性管線包 LangGraph 殼」改造成「真正有推理能力的多智能體系統」。
> 每個 domain agent 用 LLM ReAct loop 綜合資料，Supervisor 用 LLM 做最終仲裁而非加權投票。

---

## 一、問題診斷：為什麼現在的系統沒有意義

### 1.1 現況

```
User → supervisor.invoke() → [6 個確定性 node] → 加權投票 → SupervisorOutput
```

每個 agent 的核心邏輯：
```python
# technical_agent.py _determine_signal_and_confidence()
if avg_score > 0.30:
    signal = Signal.BULLISH
elif avg_score < -0.30:
    signal = Signal.BEARISH
else:
    signal = Signal.NEUTRAL
```

```python
# _build_narrative()  — 所有文字都是 f-string 模板
if sma5 > sma20 > sma60:
    parts.append("均線多頭排列")
elif sma5 < sma20 < sma60:
    parts.append("均線空頭排列")
```

### 1.2 具體問題清單

| 問題 | 說明 |
|------|------|
| **LangGraph 只是包裝** | `fetch → compute → signal` 是線性確定性流程。LangGraph 提供零條件路由，直接叫函數效果一樣 |
| **LLM 只寫模板文字** | `fundamental_agent` 的 synthesize node 呼叫 GPT，但只用來把已知指標翻成白話。不做任何推理 |
| **資料沒有串起來** | Risk agent 用 `spot_price=500.0` 硬編碼，IV=20% 寫死。沒有真實市場資料 |
| **沒有意圖理解** | 使用者說「2330 現在怎樣」跟「我有 50 口 TXO」會走完全一樣的流程 |
| **缺少 Chip Analysis** | 台股最重要的籌碼分析（三大法人、融資融券、外資持股）完全缺失 |
| **Macro Agent 死了** | 依賴 TradingEconomics API key，沒有就 fallback 到空資料 |
| **無法跨標的推理** | 找機會不代表只看一檔，但系統沒有多標的路由能力 |

### 1.3 成功定義

使用者說「2330 現在適合買嗎」，系統能夠：
1. **Router LLM** 理解這是「單標的分析」意圖，路由到 Scenario 1
2. **7 個 ReAct Agent** 各自拉真實資料、推理、輸出結構化結論
3. **Synthesis LLM** 讀七份報告，整合成一個有觀點、有根據、有風險警示的投研摘要
4. 整個過程在 Langfuse 可觀測

---

## 二、目標架構

```
┌─────────────────────────────────────────────────────────────────────┐
│                         使用者輸入                                    │
│  "2330現在怎樣" │ "我有TXO組合" │ "掃描金融股機會"                    │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │   Router LLM    │  intent classification
                    │  (GPT-4o-mini)  │  → scenario + targets + depth
                    └────────┬────────┘
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
    ┌─────▼──────┐    ┌──────▼─────┐    ┌──────▼──────┐
    │ Scenario 1 │    │ Scenario 2 │    │ Scenario 3  │
    │ 單標的分析  │    │ 組合風控   │    │ 多標的篩選  │
    └─────┬──────┘    └──────┬─────┘    └──────┬──────┘
          │                  │                  │
          └──────────────────┼──────────────────┘
                             │
         ┌───────────────────┼───────────────────┐
         │        平行執行（LangGraph parallel）   │
    ┌────▼────┐ ┌────▼────┐ ┌────▼────┐ ┌────▼────┐
    │Technical│ │ Chip    │ │Fundament│ │  Risk   │
    │ReAct   │ │Analysis │ │al ReAct │ │  ReAct  │
    │Agent   │ │ReAct   │ │Agent   │ │  Agent  │
    └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘
         │           │           │            │
    ┌────▼────┐ ┌────▼────┐ ┌────▼──────┐
    │  News   │ │  Macro  │ │CrossMarket│
    │  ReAct  │ │  ReAct  │ │  ReAct   │
    │  Agent  │ │  Agent  │ │  Agent   │
    └────┬────┘ └────┬────┘ └────┬──────┘
         │           │           │
         └─────────────────────────
                      │
             ┌────────▼────────┐
             │  Synthesis LLM  │  真正的推理仲裁
             │   (GPT-4o)      │  讀 7 份 DomainReport
             └────────┬────────┘
                      │
             ┌────────▼────────┐
             │  SupervisorOutput│  (保留 HITL Gate / hard_constraint 邏輯)
             └─────────────────┘
```

### 2.1 新舊對比

| 層次 | 現在 | 目標 |
|------|------|------|
| 入口 | 直接呼叫 `supervisor.invoke()` | Router LLM → Scenario Handler |
| Agent 推理 | if/else 確定性規則 | LLM ReAct loop over real tools |
| Agent 輸出 | AgentSignal（窄 schema）| DomainReport（寬 schema + reasoning trace） |
| Supervisor 仲裁 | 加權投票 + 確定性規則 | Synthesis LLM + 規則引擎 (risk override 保留) |
| 資料來源 | 硬編碼 / mock / yfinance | FinMind API / FRED / Tavily |
| 可觀測性 | Langfuse spans | Langfuse spans + reasoning steps |

---

## 三、資料源綁定地圖

### 3.1 FinMind 資料集對應（已有 FINMIND_KEY）

| Domain | 資料集 | 用途 |
|--------|--------|------|
| **Technical** | `TaiwanStockPrice` | OHLCV（現在已連，但走 yfinance fallback） |
| **Technical** | `TaiwanStockPrice` FinMind 優先 | 台股必走 FinMind（速度快、不需 `.TW` 後綴） |
| **Chip** | `TaiwanStockInstitutionalInvestorsBuySell` | 三大法人買賣超 |
| **Chip** | `TaiwanStockMarginPurchaseShortSale` | 融資融券餘額 |
| **Chip** | `TaiwanStockShareholding` | 外資持股比例 |
| **Chip** | `TaiwanFuturesInstitutionalInvestors` | 期貨三大法人（配合 Risk） |
| **Fundamental** | `TaiwanStockMonthRevenue` | 月營收（逐月更新，比季報快） |
| **Fundamental** | `TaiwanStockPER` | 本益比 / 股價淨值比（估值） |
| **Fundamental** | `TaiwanStockFinancialStatements` | 損益表 EPS（補 FinancialReports） |
| **Risk** | `TaiwanOptionDaily` | 選擇權日行情（IV 反推） |
| **Risk** | `TaiwanStockPrice` | 現貨價（取代硬編碼 500.0） |
| **Risk** | `TaiwanExchangeRate` | USD/TWD 匯率 |
| **Macro** | `TaiwanExchangeRate` | 台幣匯率趨勢 |
| **Macro** | `InterestRate` | 台灣央行利率 |
| **Macro** | `GovernmentBondsYield` | 美國 10Y 殖利率 |
| **CrossMarket** | `TaiwanStockPrice`（指數：`001`=加權） | 加權指數走勢 |

### 3.2 FRED API（免費，不需 key 取基本資料）

| 資料 | Series ID | 用途 |
|------|-----------|------|
| 美國非農就業 | `PAYEMS` | Macro 就業情況 |
| CPI 通膨 | `CPIAUCSL` | Macro 通膨壓力 |
| 失業率 | `UNRATE` | Macro 景氣判斷 |
| 美國 10Y 殖利率 | `DGS10` | Macro / Risk 折現率 |
| 聯邦基準利率 | `FEDFUNDS` | Macro 利率環境 |

FRED 免費端點：`https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}`（CSV，不需 API key）

### 3.3 其他資料源

| Domain | 資料源 | 用途 |
|--------|--------|------|
| **News** | Tavily（已有 key）| 即時新聞搜尋 |
| **News** | 公開資訊觀測站 RSS | 台灣重大訊息 |
| **Synthesis** | OpenAI GPT-4o（已有 key）| 最終報告生成 |

---

## 四、新增 Schema：DomainReport

現有 `AgentSignal` 設計用於確定性管線，資訊太窄。重構後每個 Domain Agent 輸出 `DomainReport`，`AgentSignal` 保留向後相容（SupervisorOutput 仍使用）。

### 4.1 新增 `schemas/domain_report.py`

```python
"""
DomainReport — Agentic 架構的 domain agent 輸出格式。

每個 ReAct agent 完成推理後輸出這個結構。
Synthesis LLM 讀所有 DomainReport，產出最終報告。
Hard constraint 邏輯仍由規則引擎處理，不由 LLM 裁量。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from schemas.agent_signal import AgentType, Signal, TimeHorizon, HardConstraint


@dataclass
class ReasoningStep:
    """ReAct loop 中一個 think-act-observe 循環的紀錄。"""
    thought: str          # LLM 的推理
    action: str           # 呼叫了哪個工具
    action_input: dict    # 工具參數
    observation: str      # 工具回傳（摘要）


@dataclass
class DomainReport:
    """
    Domain agent 的完整輸出，包含推理過程。
    """
    agent: AgentType
    symbol: str
    market: str
    asof: datetime

    # 結論（機器可讀）
    signal: Signal
    confidence: float
    time_horizon: TimeHorizon
    hard_constraints: list[HardConstraint] = field(default_factory=list)

    # 推理過程（供 Synthesis LLM 閱讀）
    reasoning_steps: list[ReasoningStep] = field(default_factory=list)

    # 結構化發現（供規則引擎使用）
    key_findings: dict[str, Any] = field(default_factory=dict)
    # e.g. {"rsi": 72.3, "foreign_net_buy_3d": 12_000_000, "ews_level": "critical"}

    # 白話摘要（LLM 生成，供 Synthesis 讀取）
    narrative_summary: str = ""

    # 資料品質
    data_completeness: float = 1.0  # 0-1
    errors: list[str] = field(default_factory=list)
```

### 4.2 Schema 策略

- `AgentSignal` **保留不動**：test_phase0 ~ test_phase5 測試全部依賴它，不改
- `DomainReport` **新增**：新架構輸出到這裡
- Supervisor 仍輸出 `SupervisorOutput`（保留 HITL Gate / hard_constraint 邏輯）
- 轉接橋：`domain_report_to_agent_signal(report: DomainReport) -> AgentSignal` 供舊測試相容

---

## 五、Router Layer（新增）

### 5.1 `router/intent_router.py`

```python
"""
Router LLM — 意圖理解與場景路由。

輸入：自然語言字串
輸出：RouterOutput（場景 + 目標 + 分析深度）
"""
```

**RouterOutput schema：**

```python
@dataclass
class RouterOutput:
    scenario: Literal["single_stock", "portfolio_risk", "multi_stock_scan"]
    targets: list[str]          # ["2330"] / ["PORTFOLIO"] / ["2882", "2881", "2884"]
    market: str                 # "TW" / "US" / "MIXED"
    depth: Literal["quick", "standard", "deep"]
    original_query: str
    # 場景專屬資料（如 portfolio risk 需要 positions JSON）
    extra_context: dict
```

**Router Prompt 設計（繁中）：**

```
你是 QuantDesk 的智能路由員。

根據使用者輸入，判斷：
1. 場景：single_stock（單標的分析）/ portfolio_risk（組合風控）/ multi_stock_scan（多標的篩選）
2. 股票代號列表（台股用 4 碼，美股用 ticker）
3. 市場（TW / US / MIXED）
4. 分析深度（quick=30秒 / standard=2分鐘 / deep=5分鐘）

輸出 JSON，不要說廢話。
```

---

## 六、各 Domain Agent 重構細節

### 6.1 Technical Agent（重構幅度：中）

**現況問題：**
- `_determine_signal_and_confidence()` 用 7 個 if/else 規則，不考慮指標間的關聯性
- `_build_narrative()` 是模板字串，不是推理

**目標改動：**

```
現在：fetch → compute → signal（確定性）
目標：fetch → compute → [LLM ReAct: 解讀指標組合, 查詢歷史型態] → DomainReport
```

**新增工具（Technical ReAct 可呼叫）：**
- `get_ohlcv(symbol, period)` → FinMind `TaiwanStockPrice`
- `compute_indicators(ohlcv)` → 現有確定性計算（保留！）
- `check_pattern(indicators)` → 判斷經典型態（頭肩、雙底等）
- `get_volume_profile(symbol)` → 成交量分佈

**LLM Prompt 設計重點：**
```
工具輸出的指標數字已經算好了（RSI=72.3、MACD柱狀圖=+1.2）。
你的任務是推理這些指標的組合意涵：
- 它們互相印證還是矛盾？
- 目前是盤整突破前還是高檔過熱？
- 成交量型態支撐還是背離？
輸出結構化判斷，不要發明新數字。
```

**確定性計算保留（CLAUDE.md 第1條）：**
- `compute_rsi`, `compute_macd`, `compute_bollinger` 等全部保留
- LLM **只做推理**，計算結果從工具來

---

### 6.2 Chip Analysis Agent（全新，第7個 agent）

**這是台股最關鍵的缺失。籌碼比技術面更早反映大資金的看法。**

#### 6.2.1 新增 `schemas/agent_signal.py` 中的 AgentType

```python
class AgentType(str, Enum):
    RISK = "risk"
    TECHNICAL = "technical"
    FUNDAMENTAL = "fundamental"
    NEWS = "news"
    MACRO = "macro"
    CROSS_MARKET = "cross_market"
    CHIP = "chip"          # 新增
```

#### 6.2.2 新增 `adapters/chip_adapter.py`

```python
"""
籌碼分析 Adapter — 對接 FinMind 三大法人、融資融券、外資持股資料。
"""

class ChipDataAdapter(DataSourceAdapter):
    """
    整合三個 FinMind 資料集的籌碼資料源。
    """
    def fetch_institutional(self, stock_id: str, days: int = 10) -> pd.DataFrame:
        """三大法人買賣超：TaiwanStockInstitutionalInvestorsBuySell"""

    def fetch_margin(self, stock_id: str, days: int = 10) -> pd.DataFrame:
        """融資融券：TaiwanStockMarginPurchaseShortSale"""

    def fetch_shareholding(self, stock_id: str) -> pd.DataFrame:
        """外資持股比例：TaiwanStockShareholding"""

    def fetch_futures_institutional(self, symbol: str = "TXF") -> pd.DataFrame:
        """期貨三大法人：TaiwanFuturesInstitutionalInvestors"""
```

#### 6.2.3 Chip Agent 工具設計

| 工具名 | FinMind 資料集 | 計算輸出 |
|--------|---------------|---------|
| `get_institutional_flow` | `TaiwanStockInstitutionalInvestorsBuySell` | 外資/投信/自營商各自 N 日累積買賣超（股數 + 金額） |
| `get_margin_trend` | `TaiwanStockMarginPurchaseShortSale` | 融資使用率、融資增減、融券張數、借券餘額 |
| `get_foreign_ownership` | `TaiwanStockShareholding` | 外資持股比例、近期增減幅 |
| `get_futures_positioning` | `TaiwanFuturesInstitutionalInvestors` | 法人期貨淨部位（多空方向） |

#### 6.2.4 Chip Agent 推理邏輯（LLM 做，規則不做）

- 三大法人是否同向？（外資買、投信也買 → 強訊號）
- 融資增加配合上漲是追高嗎？還是資金流入？
- 外資持股比例接近上限，還是有進一步加碼空間？
- 期貨部位方向與現貨籌碼是否一致？

#### 6.2.5 新增 `agents/chip_agent.py`

```python
"""
Chip Analysis Agent — 台股籌碼分析
工具：fetch_institutional / fetch_margin / fetch_shareholding / fetch_futures_institutional
推理：外資動向 × 融資槓桿 × 法人期貨部位 → 籌碼信號
"""
```

---

### 6.3 Risk Agent（重構幅度：大）

**現況最嚴重的問題：`spot_price=500.0` 硬編碼，IV=20% 寫死。這讓整個 Greeks 引擎的輸出沒有實用價值。**

**目標改動：**

#### 6.3.1 真實資料接入

```python
# 現在（沒用）
position = OptionPosition(symbol="2330", spot=500.0, ...)

# 目標（真實）
spot = finmind.fetch_latest_price("2330")          # TaiwanStockPrice latest
iv   = finmind.backout_iv_from_option("2330", ...)  # TaiwanOptionDaily
```

**`adapters/options_adapter.py` 新增：**
```python
def fetch_spot_price(self, stock_id: str) -> float:
    """從 TaiwanStockPrice 取最新收盤價"""

def backout_implied_vol(self, stock_id: str, as_of: date) -> float:
    """從 TaiwanOptionDaily ATM 期權反推 IV（Black-Scholes 數值解）"""
```

#### 6.3.2 Portfolio Risk 場景（Scenario 2）

使用者輸入包含 positions JSON 時，Risk Agent 進入完整 Greeks 計算模式：
```python
# 使用者提供
positions = [
    {"symbol": "2330", "instrument": "call", "strike": 850, "expiry": "2025-09-17", "qty": 10},
    {"symbol": "2330", "instrument": "put",  "strike": 800, "expiry": "2025-09-17", "qty": -5},
]
# Agent 自動拉 spot + IV，算出組合 Greeks + 情境壓力測試
```

#### 6.3.3 Risk Agent ReAct 工具

| 工具 | 說明 |
|------|------|
| `get_spot_price(symbol)` | FinMind 最新現貨價 |
| `get_option_chain(symbol, expiry)` | FinMind TaiwanOptionDaily |
| `backout_iv(option_data, spot)` | 確定性：B-S 數值解 |
| `compute_greeks(position, spot, iv, r, q)` | 確定性：現有 black_scholes.py |
| `run_scenario_stress(portfolio, scenarios)` | 確定性：現有 scenario.py |
| `check_hard_constraints(greeks)` | 確定性：現有 aggregation.py |

**Black-Scholes 計算保留（CLAUDE.md 第1條）**

---

### 6.4 Fundamental Agent（重構幅度：中）

**現況問題：**
- 只看單一季報，不看趨勢
- 沒有月營收（台股月營收是最快的基本面更新頻率）
- 估值（PER/PBR）缺失

**新增工具：**

| 工具 | 資料源 | 說明 |
|------|--------|------|
| `get_quarterly_financials` | FinancialReports SQLite（現有）| 財報三表 |
| `get_monthly_revenue` | FinMind `TaiwanStockMonthRevenue` | 月營收 YoY/MoM |
| `get_valuation_multiples` | FinMind `TaiwanStockPER` | PER / PBR 現值 vs 歷史分位 |
| `get_revenue_trend` | FinMind 月營收 計算 | 連續N月成長/衰退 |

**LLM 推理重點：**
- 月營收加速成長 + 毛利率擴張 → 基本面改善
- EWS critical 觸發原因是什麼？是短期還是結構性？
- 估值在歷史分位的哪個位置？

---

### 6.5 Macro Agent（重構幅度：大，替換資料源）

**現況問題：依賴 TradingEconomics key（不存在），整個 agent 實際上不工作。**

**替換方案：FRED + FinMind**

#### 6.5.1 新增 `adapters/fred_adapter.py`

```python
"""
FRED Adapter — 美聯儲免費資料（不需 API key）

端點：https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}
"""
FRED_SERIES = {
    "nfp":          "PAYEMS",      # 非農就業人數（千人）
    "cpi_yoy":      "CPIAUCSL",    # CPI
    "unemployment": "UNRATE",      # 失業率
    "yield_10y":    "DGS10",       # 10Y 美債殖利率
    "fed_funds":    "FEDFUNDS",    # 聯邦基準利率
}

class FREDAdapter(MacroAdapter):
    BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

    def fetch_series(self, series_id: str, periods: int = 12) -> pd.Series:
        """下載最近 periods 筆資料，無需 API key"""
```

#### 6.5.2 Macro Agent 工具

| 工具 | 資料源 | 說明 |
|------|--------|------|
| `get_us_macro_data` | FRED | NFP / CPI / UNRATE / DGS10 |
| `get_taiwan_rate` | FinMind `InterestRate` | 台灣央行利率 |
| `get_tw_bond_yield` | FinMind `GovernmentBondsYield` | 台債殖利率 |
| `get_exchange_rate` | FinMind `TaiwanExchangeRate` | USD/TWD |
| `search_macro_news` | Tavily | 最新央行動向、地緣政治 |

---

### 6.6 News Agent（重構幅度：小）

**現況：Tavily 搜尋已實作，架構基本正確。主要問題是搜尋策略太簡單。**

**改動：**
- 增加結構化搜尋（公司新聞 + 產業新聞 + 重大事件分層）
- 情緒分數從模板判斷改為 LLM 推理（「這篇新聞對 2330 是利多還是利空，為什麼」）

---

### 6.7 CrossMarket Agent（重構幅度：中）

**現況問題：只看靜態相關係數，不推理市場間的因果關係。**

**改動：**
- 增加 `get_us_market_status` 工具（S&P500、Nasdaq 昨日表現）
- 增加 `get_semiconductor_index`（費半 SOX 指數）
- LLM 推理：「今天美股大跌，台積電 ADR 也跌，這對明天台股開盤意涵是什麼」

---

## 七、Synthesis LLM（取代加權投票）

### 7.1 現況問題

```python
# supervisor/graph.py _compute_layer2()
# 加權投票：把 7 個 signal 值加權平均
# 這不叫仲裁，叫算術
```

### 7.2 目標：Synthesis LLM

```python
"""
Synthesis Node — 讀所有 DomainReport，產出最終投研判斷。

規則引擎（確定性，不由 LLM 裁量）：
  - hard_constraint breach → 強制 risk_override
  - EWS critical → 強制 requires_human_review
  - 信心低於門檻 → 觸發 HITL Gate

LLM 仲裁（LLM 有推理空間）：
  - 各 domain 報告間的矛盾如何解讀？
  - 哪些訊號最重要？
  - 最終建議的信心水準為何？
"""

SYNTHESIS_SYSTEM_PROMPT = """
你是 QuantDesk 的首席分析師。你會收到 7 個 domain 分析師的報告。

你的任務：
1. 整合所有報告，找出共識與矛盾
2. 解釋矛盾（例如：技術面偏多，但籌碼外資持續賣超，如何看？）
3. 給出最終建議（BULLISH/BEARISH/NEUTRAL）和信心水準（0-1）
4. 指出最重要的 1-3 個根據
5. 指出最大的風險

嚴格限制：
- 所有數字必須來自各報告的 key_findings
- 不得發明報告中沒有的數字
- 風控硬約束已由規則引擎處理，你不需要也不能覆蓋它
"""
```

### 7.3 HITL Gate 保留

`_compute_hitl_gate()` 邏輯**完全保留**，這是規則引擎，不由 LLM 裁量：
- `risk_override` → 強制 `requires_human_review`
- `ews_critical` → 強制 `requires_human_review`
- `confidence < 0.40` → `requires_human_review`

---

## 八、三個場景的實作細節

### 8.1 Scenario 1：單標的快速分析

```
輸入："2330 現在怎樣"
→ Router: scenario=single_stock, targets=["2330"], depth=standard
→ 平行呼叫：Technical + Chip + Fundamental + News + CrossMarket（5個）
→ Risk 跳過（沒有 positions）
→ Macro 輕量版（只取近期數據）
→ Synthesis LLM → 最終報告
```

**輸出格式（Scenario 1）：**
```
## 台積電（2330）即時分析

**總結**：偏多（信心：72%）
**核心根據**：外資連 5 日買超 + 費半指數強勢 + 月營收年增 18%
**主要風險**：RSI 進入超買區（76），短線有回調壓力
**建議**：持有 / 觀察回調點佈局

---
技術面：短期偏多（RSI 76, MACD 黃金交叉）
籌碼面：強勢（外資 3 日淨買超 8 億，融資低水位）
基本面：正面（月營收 YoY +18%，EWS 無警報）
新聞：中性（無重大事件）
跨市場：正面（費半昨漲 2.3%，台積 ADR +1.8%）

⚠️ 人工複核：信心水準 72%（低於 80% 門檻），建議人工確認後執行
```

### 8.2 Scenario 2：選擇權組合風控

```
輸入："我有 10 口 2330 Call 850 9月到期 + 5 口 Put 800 short"
→ Router: scenario=portfolio_risk, targets=["2330"], positions=[...]
→ Risk Agent（完整模式：拉 spot + IV + 算 Greeks + 壓力測試）
→ Technical + Chip（為 Risk 的市場方向判斷提供參考）
→ Synthesis：風控為主，方向為輔
```

**輸出格式（Scenario 2）：**
```
## 選擇權組合風控報告

**組合 Greeks**
- Net Delta: +0.42（多方偏斜）
- Net Gamma: +0.008
- Net Vega: +245（做多波動率）
- Net Theta: -180 TWD/天

**情境分析**
- 股價 +5%：+NTD 42,000
- 股價 -5%：-NTD 28,000
- IV +5pp：+NTD 12,250

**風控狀態**：✅ 所有限制未觸限
**建議**：注意 Theta 消耗，若未來 7 天 2330 無大方向，考慮縮減 Gamma 曝險
```

### 8.3 Scenario 3：多標的篩選

```
輸入："幫我掃描金融股找機會"
→ Router: scenario=multi_stock_scan, 從 TaiwanStockInfo 取金融股清單
→ 對每個標的並行呼叫 Technical + Chip（輕量）
→ Fundamental 只拉最新 PER/PBR 和月營收
→ Synthesis：產出排行榜
```

**輸出格式（Scenario 3）：**
```
## 金融股機會掃描（前5名）

| 排名 | 股票 | 技術 | 籌碼 | 估值 | 綜合 |
|------|------|------|------|------|------|
| 1 | 富邦金(2881) | ↑多 | 外資買 | PBR 0.9x | ★★★★ |
| 2 | 玉山金(2884) | ↑多 | 投信買 | PBR 1.1x | ★★★☆ |
...

**推薦理由**：富邦金技術突破季線 + 外資連 3 日買超 + PBR 低於歷史中位數
**注意事項**：整體金融股對利率敏感，近期 10Y 美債殖利率上揚需關注
```

---

## 九、實作路線圖

### Phase A：基礎設施（1-2 天）

**目標：把所有真實資料接進來，讓現有確定性計算用上真實數字**

1. `adapters/chip_adapter.py` — FinMind 三大法人/融資/外資
2. `adapters/fred_adapter.py` — FRED 免費 CSV 端點
3. `adapters/options_adapter.py` 升級 — 從 FinMind 拉 spot + option chain
4. `adapters/price_adapter.py` 升級 — 確保 FinMind 優先（現有 yfinance fallback 保留）
5. 驗收：`uv run pytest tests/test_adapters_live.py`（需真實 API key）

### Phase B：Schema 擴充（半天）

1. `schemas/domain_report.py` — DomainReport + ReasoningStep
2. `schemas/agent_signal.py` — AgentType 加 `CHIP`
3. `schemas/router_output.py` — RouterOutput
4. `agents/verifier.py` 升級 — 驗證 DomainReport.narrative_summary
5. 驗收：`uv run mypy .` 零 issues（不改現有 AgentSignal，向後相容）

### Phase C：Chip Analysis Agent（1-2 天）

1. `agents/chip_agent.py` — 完整 ReAct agent
2. `tests/test_chip_agent.py` — mock adapter 下的推理驗證
3. 驗收：agent 在 2330 上輸出有意義的籌碼判斷

### Phase D：各 Agent 升級（2-3 天，可並行）

**可平行執行的子任務：**

| 子任務 | 涉及檔案 | 估計 |
|--------|---------|------|
| Technical ReAct 化 | `agents/technical_agent.py` | 半天 |
| Risk 真實資料接入 | `agents/risk_agent.py` + `adapters/options_adapter.py` | 1 天 |
| Fundamental 月營收 + PER | `agents/fundamental_agent.py` + adapter | 半天 |
| Macro FRED 替換 | `agents/macro_agent.py` + `adapters/fred_adapter.py` | 半天 |
| CrossMarket 強化 | `agents/cross_market_agent.py` | 半天 |

### Phase E：Router + Synthesis（1 天）

1. `router/intent_router.py` — GPT-4o-mini 意圖分類
2. `supervisor/synthesis.py` — GPT-4o 最終仲裁
3. `supervisor/graph.py` 重構 — 保留 HITL Gate，把 `_compute_layer2` 換成 Synthesis LLM
4. 驗收：三個情境的 end-to-end demo

### Phase F：場景輸出格式化（半天）

1. `scripts/demo_scenario1.py`
2. `scripts/demo_scenario2.py`
3. `scripts/demo_scenario3.py`
4. 驗收：三個 demo 腳本跑通，輸出格式符合第八節設計

### Phase G：測試補全（1 天）

1. 現有 696 個測試必須繼續全過（向後相容）
2. 新增 Chip Agent 測試（mock adapter）
3. 新增 Router 測試（mock LLM）
4. 新增 Synthesis 測試（mock DomainReport inputs）
5. 驗收：`uv run ruff check . && uv run mypy . && uv run pytest -q`

---

## 十、不能動的東西（後向相容邊界）

| 檔案/模組 | 原因 |
|-----------|------|
| `schemas/agent_signal.py` | 696 個測試依賴，不改欄位 |
| `agents/risk/black_scholes.py` | 確定性計算核心，CLAUDE.md 第1條 |
| `agents/risk/scenario.py` | 同上 |
| `agents/risk/aggregation.py` | hard_constraint 規則引擎 |
| `supervisor/signal.py` SupervisorOutput | Phase 5/6 測試依賴 |
| `supervisor/graph.py` _compute_hitl_gate | HITL 邏輯不由 LLM 裁量 |
| `agents/verifier.py` | 數字驗證是核心功能，CLAUDE.md |

---

## 十一、重要技術決策記錄

### 11.1 為何用 FRED 而非 TradingEconomics

- TradingEconomics 需付費 key，使用者沒有
- FRED 是美聯儲官方資料，免費，CSV 端點不需 API key
- 涵蓋所有需要的總經指標（NFP、CPI、UNRATE、DGS10）
- 缺點：台灣本地經濟指標需用 FinMind 補足

### 11.2 為何 LLM 推理但計算保留確定性

CLAUDE.md 第1條不可違反：
> 確定性計算與 LLM 嚴格分離：Greeks、財務指標、技術指標、統計量一律由純函數產出。LLM 只負責路由、組織語言、寫白話說明，**永遠不產出數字**。

LLM ReAct 的角色：**呼叫確定性計算工具，解讀結果的組合意涵**，不替代計算。

### 11.3 為何加 Chip Agent 而非讓 Technical Agent 兼做

- 籌碼分析（三大法人買賣超方向）與技術分析（價格型態）是獨立的分析維度
- 台股特殊：外資是最重要的資金，其動向應獨立追蹤
- 籌碼 agent 可以對 Risk Agent 的期貨部位判斷提供補充
- 讓每個 agent 的職責邊界清晰，符合 CLAUDE.md 架構鐵則

### 11.4 Synthesis LLM 與 HITL Gate 的介面

Synthesis LLM 輸出建議和信心值 → 規則引擎計算 HITL Gate → 規則結果注入 SupervisorOutput

規則引擎**不受** Synthesis LLM 影響：
```
if any(hc.breached for r in domain_reports for hc in r.hard_constraints):
    output.risk_override = True   # 規則引擎決定，不問 LLM
    output.confidence = 0.35      # 強制降級
```

---

## 十二、執行優先順序建議

如果要快速看到效果，建議以下優先順序：

1. **先做 Phase A（資料接通）** — 讓現有系統用上真實資料，Risk Agent 有意義
2. **再做 Phase C（Chip Agent）** — 填補最大缺口，增加台股分析能力
3. **再做 Phase E（Synthesis LLM）** — 讓最終輸出變得有推理能力
4. **最後 Phase D（各 Agent ReAct 化）** — 最費工，但有了前三步系統已可實用

---

*本計劃對應 `phase-3-technical-crossmarket` branch 後的下一個大版本。*
*實作時一律開新 branch：`phase-7-agentic-refactor`*
