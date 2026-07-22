# QuantDesk — 多智能體量化研究桌系統
## 完整專案規格與系統設計 (System Design Spec) v1.0

> 一個橫跨基本面、技術面、風控（含 Greeks 量化）、財經新聞、總體經濟、跨市場連動的全方位 agentic 投研系統。六個 domain agent 可獨立使用，也可由 Supervisor 匯總成綜合投資評估。
>
> 本文為架構規格，非機密資料。所有機構級資料源（Reuters/Bloomberg）在設計上以 adapter pattern 抽象化，實作以可負擔的公開替代源模擬其角色。

---

## 目錄
0. 定位與設計哲學
1. 現有兩專案的角色重定位
2. 系統整體架構
3. 標準化 Agent 輸出 Schema（系統的骨架）
4. 六大 Domain Agent 詳細規格
5. Supervisor 匯總與仲裁層
6. 資料源 Adapter 設計
7. 資料層與儲存設計
8. 可觀測性與評估框架
9. Production 硬化與安全
10. 分階段實作路線圖
11. 面試敘事重點

---

## 0. 定位與設計哲學

### 0.1 這個系統要解決的真實痛點

不是「算不出財務指標」（那是 Bloomberg/CapIQ 早已商品化的東西），而是投資人真正稀缺的四件事：
- **時間**：把數小時的 due diligence 壓縮成分鐘級。
- **訊號 vs 雜訊**：跨越多個資料源後，自動判斷「這次到底哪裡不一樣、要不要警覺」。
- **量化 + 質化的綜合判讀**：數字漂亮但法說會語氣保守、EPS 成長但現金流品質惡化——這種矛盾訊號的綜合判斷才是分析師的核心價值。
- **可稽核性**：每個結論都能回溯到帶時間戳的原始來源。

### 0.2 貫穿全系統的三條設計原則

1. **確定性計算與 LLM 生成嚴格分離**：所有數字（財務指標、Greeks、技術指標、統計量）一律由確定性程式產出，LLM 只負責「路由、組織語言、下結論、寫白話說明」，永遠不負責產出數字。這是從 FinancialReports 的 Verifier 精神延伸到整個系統的統一哲學。
2. **風控是硬約束，不是投票的一票**：任何 domain agent 的 `hard_constraints` 一旦觸發，Supervisor 的最終建議必須被強制降級或加註警告，不能被其他 agent 的樂觀訊號蓋過。
3. **每個判斷都要帶來源與時間戳**：資料有時效，`asof` 是一等公民；系統要能區分「最新事實」與「已作廢的舊值」（財報重編、數據修正）。

---

## 1. 現有兩專案的角色重定位

在 QuantDesk 架構下，你現有的兩個 repo 不是被丟棄，而是各自找到明確位置——它們合起來正好構成「財報 Domain」這一個 specialist 的完整實作。

| 現有專案 | 目前定位 | 在 QuantDesk 中的新角色 |
|---|---|---|
| **FinancialReports** | 台股財報 ETL/RAG pipeline（XBRL→iXBRL→FinMind→PDF，四階段管道，七條驗證規則，四維品質分數，10 種 insight cards） | **財報 Domain 的資料層 + 敘述檢索層**。ETL 產出的 `financial_facts`/`financial_metrics` 成為財報 agent 的資料底座；`document_chunks` 補完 RAG 後成為質化檢索來源 |
| **Financial_Agent** | LangGraph 分析應用（ROIC/WACC、earnings quality、management score、factor exposure、EWS，但讀靜態 JSON、agent 只是意圖分類器） | **財報 Domain 的分析工具層 + 該 domain 的內部編排**。深度分析函數成為財報 agent 的工具集 |

**關鍵洞察**：你上一輪糾結的「Financial_Agent 定位模糊」問題，在這個更大架構下自然解開——它不需要自己是完整產品，只要把「財報」這一個 domain 做到最深，然後跟其他五個 domain 一起接受 Supervisor 統一調度。定位從「一個什麼都想做但都不深的產品」變成「一個財報領域的深度專家」。

### 1.1 兩專案目前最大的技術債（優化起點）

1. **資料層斷裂**：Financial_Agent 讀的是 `convert_financial_report.py` 手動轉出的靜態 JSON，不是 FinancialReports 自動化管道的產出。兩個系統其實是孤島。
2. **RAG 敘述層是空的**：FinancialReports 的 `vector_store.py` 是 ChromaDB 佔位，`document_chunks` 切好了但沒 embedding、沒檢索。所以整套系統目前只能做數字計算，完全無法處理質化內容（管理階層討論、風險揭露）。
3. **Agent 不是真 agentic**：Financial_Agent 的 `workflow.py` 本質是「LLM 意圖分類 + 派工」，沒有 decomposition、沒有 verification、沒有 reflection loop。
4. **信心分數沒有貫穿**：FinancialReports 算了 confidence/quality_score，但沒有傳遞到 Financial_Agent 的下游分析，所以 ROIC/WACC 這些結果無法標註「這個數字有多可信」。
5. **無認證、觀測性不統一**：Financial_Agent 自己承認 API 沒有 authentication；兩邊的 Langfuse trace 沒有打通。

---

## 2. 系統整體架構

```
                          ┌──────────────────────────────┐
                          │      Portfolio Supervisor       │
                          │  匯總 / 仲裁 / 時間框架分層       │
                          │  (硬約束強制、信心加權)          │
                          └───────────────┬────────────────┘
                                          │ 標準化 Agent Signal Schema
      ┌──────────┬───────────┬────────────┼────────────┬────────────┬────────────┐
      ▼          ▼           ▼            ▼            ▼            ▼
 ┌─────────┐┌─────────┐┌──────────┐┌──────────┐┌──────────┐┌────────────┐
 │ 風控     ││ 技術面   ││ 財報     ││ 新聞     ││ 總經     ││ 跨市場      │
 │ Risk    ││ Technical││ Fundamen ││ News    ││ Macro   ││ CrossMarket│
 │ +Greeks ││          ││ -tal    ││         ││         ││            │
 └────┬────┘└────┬────┘└────┬─────┘└────┬────┘└────┬────┘└─────┬──────┘
      │          │          │            │          │           │
      ▼          ▼          ▼            ▼          ▼           ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │                     Tool / Data Access Layer                       │
 │  Greeks引擎 │ 技術指標 │ FinancialReports │ 新聞RAG │ 經濟日曆 │ 行情  │
 └──────────────────────────────────────────────────────────────────┘
      │          │          │            │          │           │
      ▼          ▼          ▼            ▼          ▼           ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │              Data Source Adapter Layer (可替換抽象)                 │
 │  券商API │ yfinance │ SQLite/pg │ NewsAPI/RSS │ TradingEconomics    │
 └──────────────────────────────────────────────────────────────────┘

橫切關注點 (貫穿所有層)：Observability (Langfuse) │ Auth │ Cache │ Eval
```

### 2.1 為什麼用 Supervisor + 獨立 Agent，而非單一大 agent loop

- **獨立可呼叫**：滿足「個別使用」需求——使用者可以只問風控、只問技術面，不必每次都跑完整六路匯總。
- **責任邊界清楚**：每個 domain 的資料節奏、工具、失敗模式都不同（技術面近即時、財報季度批次、新聞高雜訊），用獨立 agent 各自最佳化，比一個大 agent 硬吞所有情境更可控、更好除錯、成本更低。
- **可組合**：新增一個 domain（例如未來加「籌碼面/法人買賣超」）只要實作一個符合標準 schema 的 agent，不用動 Supervisor 核心。

---

## 3. 標準化 Agent 輸出 Schema（系統的骨架）

**這是整個系統最重要的設計決策**。所有 domain agent 都輸出這個結構化 schema，而非自由文字。這讓 Supervisor 能「程式化匯總」而不是「叫 LLM 讀六段文字自己腦補」。

```json
{
  "agent": "risk | technical | fundamental | news | macro | cross_market",
  "target": {"symbol": "2330", "market": "TW", "asof": "2025-11-14T13:30:00+08:00"},
  "signal": "bullish | bearish | neutral",
  "confidence": 0.0,
  "time_horizon": "intraday | short | medium | long",
  "key_evidence": [
    {"claim": "毛利率 QoQ 下滑 3.2%", "value": 0.512, "source": "financial_facts#2330_2025Q3", "asof": "..."}
  ],
  "hard_constraints": [],
  "metrics": {},
  "narrative": "LLM 生成的白話說明（僅組織語言，不含未經工具驗證的數字）",
  "data_quality": {"completeness": 0.95, "staleness_sec": 120, "confidence": 0.9},
  "errors": []
}
```

### 3.1 各欄位的設計理由

- **`time_horizon`（時間框架）**：整個 schema 最關鍵的欄位之一。技術面只對短期有效、基本面對中長期有效——匯總層不該把它們硬融成一句話，而要能呈現「短期技術轉弱、中長期基本面支撐」的分層觀點。沒有這個欄位，Supervisor 就只能和稀泥。
- **`hard_constraints`（硬約束）**：任何 domain agent 均可發起，觸發後 Supervisor 不能覆蓋。目前已知會發起的 agent：risk agent（Greeks 曝險限制：gamma/vega/delta/集中度）、fundamental agent（EWS critical/high 觸發財務預警）。見第 4.1 與第 5 節。
- **`confidence` + `data_quality`**：Supervisor 做信心加權的輸入。財報可靠但落後、新聞即時但雜訊高，權重不該一樣。
- **`key_evidence[].source` + `asof`**：可稽核性的載體。每個結論都能點回帶時間戳的原始來源。
- **`errors`**：agent 部分失敗時（例如某個資料源掛了）不整個崩潰，而是回報「這部分沒資料」，讓 Supervisor 決定要不要降低該 agent 權重或標註不確定。

---

## 4. 六大 Domain Agent 詳細規格

### 4.1 風控 Agent（Risk + Greeks 量化引擎）

**核心問題**：這個部位／組合有什麼硬性風險（方向性、凸性、波動率、集中度）？

一般的「單一標的漲跌%停損」抓不到組合層級的凸性與波動率曝險。系統橫跨台指期、美股，遲早混進選擇權部位，用 Greeks 統一框架管理是唯一合理做法。

**Layer 1 — Position-level Greeks（統一框架處理現貨/期貨/選擇權）**

用同一套 Greeks schema 描述所有部位類型，混合組合可在同一引擎加總：

| 部位類型 | Delta | Gamma | Vega | Theta |
|---|---|---|---|---|
| 現貨/ETF | 1（空頭 −1） | 0 | 0 | 0 |
| 期貨（台指期） | 契約乘數 | 0 | 0 | 0 |
| 選擇權（TXO / 美股 options） | Black-Scholes 解析解 | BS 解析解 | BS 解析解 | BS 解析解 |

現貨/期貨視為「退化版選擇權」（線性 payoff），風控 agent 不必為不同資產類型寫不同邏輯，全部走同一套 Greeks 加總管線。

**Layer 2 — Portfolio Aggregation**

```
Portfolio Net Delta = Σ (qtyᵢ × multiplierᵢ × Δᵢ)
Portfolio Gamma     = Σ (qtyᵢ × multiplierᵢ × Γᵢ)
Portfolio Vega      = Σ (qtyᵢ × multiplierᵢ × νᵢ)
Portfolio Theta     = Σ (qtyᵢ × multiplierᵢ × Θᵢ)
```

**關鍵陷阱**：跨標的加總前，要先用 beta 或指數點值把不同標的的 delta 換算成統一基準（例如全部換算成「等價台指期口數」或「等價美股大盤 $ 曝險」）。直接把台積電 delta 跟台指期 delta 相加沒有意義——這步常被忽略，錯了整個風控就是假的。

**Layer 3 — Scenario Stress P&L（Greeks 風控真正的價值所在）**

用泰勒展開把情境直接轉成組合預估損益，不用重新對每個部位定價：

```
ΔP ≈ Δ×ΔS + ½×Γ×(ΔS)² + ν×ΔIV + Θ×Δt
```

對每次請求自動跑固定情境矩陣（大盤 ±1%/±3%/±5% × IV ±10%/±20%），輸出「這組合在各情境下大概賺賠多少」，比「目前虧損多少」更前瞻。這是機構風控桌標準做法（簡化版 scenario-based margin，概念類似 SPAN）。

**Layer 4 — Hard Constraints**

```json
{
  "agent": "risk",
  "hard_constraints": [
    {"type": "gamma_limit", "current": -850, "limit": -500, "breached": true},
    {"type": "vega_limit", "current": 12000, "limit": 20000, "breached": false},
    {"type": "net_delta_pct_nav", "current": 0.42, "limit": 0.35, "breached": true},
    {"type": "sector_concentration", "sector": "半導體", "current": 0.38, "limit": 0.30, "breached": true}
  ],
  "scenario_pnl": {"idx_-3pct_iv+20": -180000, "idx_+3pct_iv-10": 95000}
}
```

任一 `breached: true`，Supervisor 最終建議必須強制降級（自動加註「風控已觸發，建議先減碼」）。

**資料與計算現實**：
- Greeks 需要 implied volatility 輸入：TXO 從券商 API 或公開報價反推 IV；美股 options 用 yfinance options chain（免費但更新頻率有限）。
- Black-Scholes 假設歐式；TXO 是歐式沒問題，美股個股選擇權多為美式，嚴謹要用 binomial tree / finite-difference，但風控用途下 BS 近似通常足夠（面試可誠實講此簡化假設及影響）。
- Greeks 計算純數學（`py_vollib` 或自實作 BS），不需要 LLM；LLM 只把 Greeks 數字轉成白話風險說明——與財報 agent「LLM 不算數字」原則一致。

### 4.2 技術面 Agent（Technical Analysis + Tracing）

**核心問題**：價量趨勢/型態現在說什麼？

- **資料節奏**：近即時（日線/盤中），跟財報的季度批次完全不同，要獨立 pipeline 與快取策略。
- **確定性計算**：均線、MACD、RSI、KD、布林通道、量能、型態辨識（突破/跌破關鍵價位）——全部由 `pandas-ta` 或自實作產出，LLM 不算指標。
- **輸出**：`signal`（趨勢方向）+ `time_horizon: short/intraday` + `key_evidence`（具體哪個指標在什麼價位觸發）。
- **踩雷**：技術指標對「盤整盤」訊號雜訊比極差，agent 要能自我標註「目前為區間震盪，技術訊號可信度低」（反映在 `confidence`），而非硬給方向。

### 4.3 財報 Agent（Fundamental）— 由現有兩專案合併實作

**核心問題**：基本面數字健不健康？（量化）管理層與風險揭露怎麼說？（質化）

- **資料層**：FinancialReports 的 `financial_facts`/`financial_metrics`（帶 confidence/quality_score）。
- **量化工具**：Financial_Agent 的 ROIC/WACC、earnings quality、management score、factor exposure、EWS。
- **質化 RAG**：補完 `document_chunks` 的 embedding + hybrid retrieval（見前一份文件的 structure-aware chunking），才能回答「管理階層對下季展望怎麼說」這類目前完全答不了的問題。
- **內部 agentic 流程**：Router → Decomposer（比較型問題拆解）→ Tool Selection → **Verifier**（生成的每個數字比對 `financial_facts` 是否一致）→ Synthesizer。
- **輸出**：`time_horizon: medium/long`，數字全部帶 `source` 指回原始 filing。

### 4.4 財經新聞 Agent（News Tracing）

**核心問題**：最近有什麼事件/情緒變化？

- **資料節奏**：即時但雜訊極高——天然適合 agentic RAG（檢索 → 相關性過濾 → 去重 → 摘要 → 情緒判斷）。
- **資料源現實**：Reuters/Bloomberg 官方 API 是機構級付費，個人專案不可能存取真品。可行替代：公開財經 RSS（鉅亨網、工商時報、經濟日報）、NewsAPI/Alpha Vantage News（有免費額度）、台股個股用公開資訊觀測站重大訊息。面試誠實講「adapter pattern 抽象化資料源，用可負擔替代源模擬機構級 feed 角色」完全站得住腳。
- **關鍵設計**：新聞 agent 最大風險是「把單一標題當成強訊號」。要做事件去重（同一則事件多家轉載）+ 來源可信度加權 + 明確區分「已發生事實」與「市場傳聞」，反映在 `confidence` 與 `data_quality`。
- **踩雷**：情緒分析（sentiment）不能只看正負面詞頻，財經語境下「符合預期」可能是中性偏空（已 price in），要用具財經語境的判斷而非通用 sentiment 模型。

### 4.5 總經 Agent（Macro Data Tracing）

**核心問題**：重要經濟數據跟預期比，是超預期還是不如預期？

- **資料源**：Investing.com 五星經濟日曆無官方免費批量 API，需 scraping 或改用 **Trading Economics API**（有免費額度）——設計上走 adapter，實作選可負擔者。
- **關鍵設計**：總經數據的訊號不是「絕對值」，是「實際 vs 市場預期（consensus）的差距」（surprise）。CPI 3% 是好是壞，完全取決於市場預期 2.8% 還是 3.2%。agent 必須抓 consensus 值做比較，這是總經分析的核心。
- **資料節奏**：事件驅動（數據公布時觸發），平時靜默。
- **輸出**：`time_horizon: medium/long`，`signal` 反映 surprise 方向對風險資產的意涵（需注意「好數據對股市不一定是好事」——例如過熱數據可能升息預期升溫）。

### 4.6 跨市場 Agent（Cross-Market Tracing）

**核心問題**：台指期與美股大盤的連動/領先落後關係現在如何？

- **資料源**：美股指數 yfinance（免費）；台指期券商 API 或公開源。
- **確定性計算**：相關係數（滾動窗口）、領先落後分析（台股開盤前參考美股夜盤/期貨）、beta、價差/背離偵測。
- **關鍵設計**：連動關係會隨市場 regime 改變（風險偏好期 vs 避險期相關性不同），agent 要用滾動窗口而非固定歷史相關係數，並標註「當前 regime 下的相關性」。
- **輸出**：常作為其他 agent 的「市場環境背景」，而非獨立進出場訊號。

---

## 5. Supervisor 匯總與仲裁層（工程含金量最高處）

六個 agent 意見衝突時（技術面看多、基本面看空）怎麼辦，不能讓 LLM 隨便選邊或打太極，要有明確機制：

### 5.1 三層仲裁邏輯

**① 硬約束優先（規則層，不可被 LLM 覆蓋）**
```
if any agent.hard_constraints[].breached == true:
    最終建議強制降級 / 加註強制警告
    （LLM 不得自由裁量是否忽略）
```
這是規則引擎層，不是 Supervisor LLM 的自由裁量。延續「風控是硬約束不是投票的一票」原則。

硬約束可由任何 domain agent 發起，Supervisor 統一處理所有 breached=True 的項目：
- **risk agent**：Greeks 曝險限制（gamma_limit、vega_limit、net_delta_pct_nav、sector_concentration）
- **fundamental agent**：EWS critical/high 觸發財務預警（receivables_spike、margin_compression 等）
- 未來 agent 若有需要亦可直接在其 AgentSignal.hard_constraints 中發起，無需修改 Supervisor 核心邏輯

**② 時間框架分層（不強融成單一結論）**
不把六個 agent 硬融成一句話，而是按 `time_horizon` 分層呈現：
```
【短期 / intraday-short】技術面轉弱、新聞面中性 → 偏空
【中期 / medium】總經 surprise 偏正、跨市場背景中性 → 偏多
【長期 / long】基本面 ROIC>WACC、earnings quality 健康 → 偏多
【風控】Gamma 曝險已觸限 → 強制加註：任何加碼前需先調整選擇權部位
```
這才是貼近投資人思考方式的輸出，不是含糊的和稀泥。

**③ 信心加權（同一時間框架內的多 agent 融合）**
同一時間框架內若有多個 agent，按 `confidence × data_quality × 來源可靠度權重` 加權，而非等權平均。先用規則式權重（財報 > 新聞的基礎可靠度），進階版才考慮讓權重依歷史準確度動態調整——但先讓規則版本正確運作。

### 5.2 Supervisor 的 LLM 只做什麼

- **不做**：不算數字、不決定要不要忽略硬約束、不隨意調權重。
- **只做**：把上述結構化的分層結論轉成給人看的、帶引用的白話投研摘要。

### 5.3 cross_market agent 的特殊地位（規則引擎強制）

**cross_market agent 的 `signal` 欄位不參與 Supervisor 的方向性投票／仲裁，僅作為其他 agent 判斷時的背景脈絡。**

技術理由：
- cross_market 輸出的是市場 regime（台美連動結構），不是標的本身的方向性觀點。
- 它的 AgentSignal 中 `metrics["is_background_only"] == True`，此欄位是機器可讀的旗標。

**Supervisor 規則引擎必須實作以下強制行為**：
```python
# Phase 5 實作時必須遵守——不得讓 LLM 自由裁量
if signal.agent == AgentType.CROSS_MARKET:
    # 排除出方向性投票池
    skip_directional_vote = True
    # 但 regime 資訊仍傳入 LLM narrative context（作為背景說明）
    background_context.append(signal)
```

cross_market signal 的 BEARISH 只表示「台美連動出現背離（regime 不穩定）」，
**不代表標的看空**，若直接納入等權投票會錯誤壓低多頭共識。

---

## 6. 資料源 Adapter 設計

**核心原則**：所有資料源走統一 adapter 介面，機構級來源與可負擔替代源可互換，不影響上層 agent 邏輯。

```
DataSourceAdapter (抽象介面)
  ├── PriceAdapter        → yfinance / 券商API / 公開行情
  ├── OptionsAdapter      → 券商API (TXO IV) / yfinance options chain
  ├── FundamentalAdapter  → FinancialReports SQLite/pg
  ├── NewsAdapter         → NewsAPI / RSS / 公開資訊觀測站
  ├── MacroAdapter        → Trading Economics API / (Investing 日曆)
  └── CrossMarketAdapter  → yfinance (美股指數) / 台指期源
```

每個 adapter 統一回傳帶 `asof` 時間戳與 `source` 標記的標準化資料，上層 agent 不知道也不關心底層是真機構 feed 還是替代源——這讓「未來若拿到 Bloomberg 存取權，只換 adapter 不動 agent」成為可能，也是面試時展現架構前瞻性的好點。

---

## 7. 資料層與儲存設計

| 資料類型 | 儲存 | 節奏 | 說明 |
|---|---|---|---|
| 財報結構化數字 | FinancialReports 現有 SQLite（可升 PostgreSQL） | 季度批次 | `financial_facts`/`financial_metrics` |
| 財報敘述向量 | pgvector（取代目前 ChromaDB 佔位） | 季度批次 | `document_chunks` + embedding |
| 行情/技術指標 | 時序儲存（可先 SQLite，量大再上時序DB） | 日線/盤中 | 讀多寫多 |
| 新聞 | 向量庫 + metadata（時間、來源、標的） | 即時串流 | 去重與時效管理 |
| 部位/風控狀態 | PostgreSQL（交易級一致性需求） | 事件驅動 | Greeks 計算輸入 |
| Agent trace | Langfuse | 每次呼叫 | 見第 8 節 |

**升級建議**：Phase 1 先讓 Financial_Agent 從讀靜態 JSON 改成讀 FinancialReports 的 DB（打通資料層）；資料庫從 SQLite 升 PostgreSQL 的時機是「多 agent 並發寫入 + 需要 pgvector」時，不用一開始就上。

---

## 8. 可觀測性與評估框架

### 8.1 Observability（Langfuse 統一貫穿）

- 每個 agent 的每次呼叫、每個工具呼叫、每次 LLM 生成都要有 trace（輸入輸出、耗時、token、資料源 asof）。
- Multi-agent 系統出錯時若只看最終輸出無法定位是哪個 agent／哪一步壞的——trace 是唯一的除錯手段。
- Financial_Agent 已有 Langfuse（目前 optional），FinancialReports 沒有；要統一打通成同一套 trace，才能做端到端回放。

### 8.2 Evaluation（分層評估）

- **單 agent 層**：財報 agent 用 numeric exact match（數字對不對）；技術面用訊號 vs 實際後續走勢的命中率；新聞用事件擷取的 precision/recall。
- **RAG 層**：Recall@k、MRR、faithfulness（見前一份文件第 5 節）。
- **Supervisor 層**：最難評估——建立 golden set（歷史情境 + 專家標註的「合理綜合判斷」），評估匯總結論是否合理、硬約束是否正確觸發、時間框架分層是否恰當。
- **迴歸測試**：golden set 跑進 CI/CD，每次改 prompt/權重/chunking 都比對 baseline，避免「改好 A 卻弄壞 B」。

---

## 9. Production 硬化與安全

1. **認證**：Financial_Agent 目前 API 無 authentication——這是明確缺口（面試會被問）。加 API key / OAuth 層再對外。
2. **Prompt Injection**：新聞 agent 檢索外部內容塞進 prompt，惡意來源可能藏「忽略先前指示」文字——資料與指令要結構化分隔，敏感操作（下單、調整部位）不得由檢索內容直接觸發。
3. **風控 fail-safe**：Greeks 計算所需資料源（IV）缺失時，風控 agent 應保守回報「無法評估凸性風險」並降低整體建議信心，絕不因缺資料就當作無風險。
4. **資料一致性**：財報重編、經濟數據修正時要有版本標記與 cache invalidation，避免引用作廢值。
5. **HITL Gate**：帶假設的分析（ROIC/WACC 的 CAPM beta、稅率）信心低於門檻時標記人工複核，尤其是要給實際投資決策參考的輸出。
6. **絕不變成投資建議機器人**：系統輸出定位為「研究輔助與風險提示」，不是自動下單或保證獲利；面對真實資金決策要有明確免責與人工把關設計。

---

## 10. 分階段實作路線圖

依「投報率 × 地基依賴」排序，每階段都是可獨立展示的里程碑：

**Phase 0 — 骨架先行（1 個里程碑）**
定稿標準化 Agent Signal Schema（第 3 節）+ 建一個空的 Supervisor 殼 + DataSourceAdapter 抽象介面。先有骨架，後面每個 agent 才有共同語言。

**Phase 1 — 打通財報 Domain（最高投報率）**
- 1a：Financial_Agent 的 `data_loader` 從讀靜態 JSON 改讀 FinancialReports DB，把 confidence/quality_score 帶進下游。
- 1b：補完 FinancialReports 的 pgvector RAG（做實 ChromaDB 佔位）。
- 1c：把財報 agent 包成符合標準 schema 的第一個 domain agent（含內部 Verifier）。
成果：兩個孤島合併成一個有量化+質化能力的財報專家。

**Phase 2 — Greeks 風控引擎**
Black-Scholes Greeks 計算 + 組合加總（含跨標的基準換算）+ 情境壓力測試 + hard_constraints 輸出。這是量化含金量最高的 agent，且與 Supervisor 的硬約束機制直接相關，建議緊接 Phase 1。

**Phase 3 — 技術面 + 跨市場 Agent**
兩者都是確定性計算為主、資料源相對好取得（yfinance），可一起做。建立近即時 pipeline 與盤中快取。

**Phase 4 — 新聞 + 總經 Agent**
兩者都依賴外部資料源 adapter，且雜訊處理/surprise 計算是重點。放在後面因為資料源最不穩定、最需要前面骨架成熟。

**Phase 5 — Supervisor 匯總層做實**
三層仲裁邏輯（硬約束 → 時間框架分層 → 信心加權）+ golden set 評估。前面 agent 都能獨立運作後，這一步把它們編織成綜合判斷。

**Phase 6 — Production 硬化 + multi-agent debate（進階）**
認證、統一 observability、HITL；roadmap 的 bull/bear/PM debate 作為 Supervisor 的自然延伸（各 agent 用同一組工具產出對立論點，PM agent 仲裁）。

---

## 11. 面試敘事重點

被問到這個系統時，建議的敘事順序（強調判斷力而非工具清單）：

1. **從痛點出發**：先講「投資人真正稀缺的不是計算，是時間、訊號雜訊分離、量化質化綜合、可稽核性」（第 0.1 節），展現你理解 domain 而非只會串框架。
2. **標準化 Schema 是骨架**：講「為什麼所有 agent 輸出結構化 schema 而非自由文字」——這讓 Supervisor 能程式化匯總。這是 multi-agent 系統能不能 scale 的分水嶺。
3. **三條設計哲學貫穿全系統**：確定性計算與 LLM 分離、風控是硬約束、每個判斷帶來源時間戳。能講出「這三條原則怎麼在六個不同 agent 一致落地」，展現系統性思維。
4. **Greeks 風控是量化深度的證明**：講 Layer 3 情境壓力測試（泰勒展開）與跨標的基準換算陷阱，展現你有真正的量化背景（結合你的固定收益/GARCH-MIDAS 底子），不是只會串 LLM。
5. **Supervisor 仲裁是工程深度的證明**：講「六個 agent 衝突時怎麼辦」的三層仲裁，尤其是「硬約束用規則引擎強制、不讓 LLM 自由裁量」——這是資深工程師與一般人的分水嶺。
6. **誠實面對資料源限制**：主動講「Bloomberg/Reuters 是機構級付費，我用 adapter pattern 抽象化、以可負擔替代源模擬其角色」——展現務實與架構前瞻性，比假裝有機構存取權可信得多。
7. **從兩個現有 repo 演進而來**：講「Financial_Agent 原本定位模糊，我重新定位成系統裡的財報專家」——展現你有能力反思並重構自己的專案，而非堆功能。

---

*附註：本規格為架構藍圖。建議下一步從 Phase 0（Schema 定稿）+ Phase 1（財報 domain 打通）落地成可執行程式碼，以最小改動證明整套匯總機制可運作。*
