# Phase 5 Supervisor 設計文件

> **狀態**：設計審查中，待 review 後才開始實作 supervisor/graph.py  
> **依據**：docs/spec.md §5（三層仲裁邏輯）、§5.3（cross_market 特殊地位）、CLAUDE.md §三條不可違反設計原則  

---

## 一、六個真實 AgentSignal 範例

以下全部來自實際 pipeline 執行（非假設資料）。
生成時間：2026-07-22。使用 mock adapter 注入受控輸入，確保可重現。

### 1. RISK（短期 / 風控）

```
signal       = BEARISH
confidence   = 0.80
time_horizon = SHORT
hard_constraints:
  [BREACHED] type="net_delta_pct_nav"  current=-2.331  limit=0.3
             → 組合淨 delta 空頭嚴重超限（-233% NAV），遠超±30%上限
  [OK]       type="gamma_limit"        current=0.01    limit=1_000_000
  [OK]       type="vega_limit"         current=154_994 limit=500_000
key_evidence:
  ("portfolio net delta (% of NAV)", source="risk_agent:aggregation")
  ("portfolio net gamma (TWD per spot² unit)", source="risk_agent:aggregation")
metrics: net_delta_pct_nav=-2.331, net_gamma_twd=0.01, net_vega_twd=154993,
         scenario_worst_pnl_twd=…, portfolio_nav=…
data_quality: completeness=1.00, errors=1
narrative: "組合淨 delta 空頭偏重，淨 gamma 偏空（凸性不利大行情），
           ⚠️ 風控限制已觸限：net_delta_pct_nav。請立即檢視並調整部位。"
```

### 2. TECHNICAL（短期 / 技術面）

```
signal       = BULLISH
confidence   = 0.38      ← 低信心（Bollinger Band 收窄，震盪訊號）
time_horizon = SHORT
hard_constraints: []
key_evidence:
  ("RSI", source="technical_agent:rsi")
  ("MACD 柱狀圖", source="technical_agent:macd")
metrics: rsi≈58, sma_trend="bullish_alignment", macd_hist>0,
         kd_cross="death_cross", bb_width_narrow=True
data_quality: completeness=1.00, errors=0
narrative: "均線多頭排列，RSI 處於強勢區，⚠️ 布林通道收窄，市場處於
           區間震盪，技術訊號可信度低。"
```

### 3. FUNDAMENTAL（長期 / 基本面）

```
signal       = BULLISH
confidence   = ~0.75     ← 由 quality_score=0.95 × 完整性加權得出
time_horizon = LONG
hard_constraints: []     ← EWS warning_level="none"，無財務預警
key_evidence:
  ("ROIC vs WACC 價值創造缺口", source="fundamental:2330:2024Q1")
  ("EWS 信號計數", source="fundamental:2330:2024Q1")
metrics: roic=8.70, wacc=6.50, value_creation_gap=+2.20,
         ews_warning_level="none", ews_signal_count=0
data_quality: completeness=1.00
narrative: "2330 ROIC(8.70%) > WACC(6.50%)，價值創造缺口+2.20pp，
           無 EWS 財務預警，長期基本面健康。"
```

### 4. NEWS（短期 / 新聞面）— 降級輸出範例

```
signal       = NEUTRAL
confidence   = 0.10      ← 降級硬底線（LLM analysis failed）
time_horizon = SHORT
hard_constraints: []
key_evidence:
  ("台積電法說：Q3 營收指引優於市場預期", source="news:t1:mops", tier=1)
  ("台積電 AI 晶片訂單暴增，法人看好下半年", source="news:t2:cnyes", tier=2)
metrics: raw_article_count=4, dedup_article_count=2,
         has_official_disclosure=True, weighted_sentiment_score=0.0,
         llm_analysis_failed=True, injection_warnings_count=0
data_quality: completeness=0.00    ← LLM 失敗 → completeness 強制歸零
errors: ["[降級] LLM 分析不可用：此 signal 為降級輸出，請勿與正常分析
         結果同等對待。"]
narrative: "本期包含公開資訊觀測站重大訊息公告。… LLM 分析失敗。"
```

### 5. MACRO（中期 / 總經面）

```
signal       = BULLISH
confidence   = 0.60
time_horizon = MEDIUM
hard_constraints: []
key_evidence:
  ("United States Non Farm Payrolls [大幅超越預期]",
   source="macro:non_farm_payrolls:united_states")
     → actual=256K vs consensus=185K, surprise_pct=+38.4% (relative %, 絕對數值型用 pct 路徑)
  ("United States CPI [小幅不如預期]",
   source="macro:cpi:united_states")
     → actual=2.9% vs consensus=3.1%, pp=-0.2pp → "miss" (pp path)
     → direction=+1（通膨弱於預期 → 降息空間擴大 → bullish）
metrics: macro_score=0.302, hot_data_warning=False,
         computable_count=2, no_recent_events=False
data_quality: completeness=0.67
narrative: "整體總經面偏多，近期數據公布結果優於市場預期。
           NFP 大幅超越預期，對市場偏多；CPI 小幅不如預期，對市場偏多。"
```

### 6. CROSS_MARKET（中期 / 跨市場）

```
signal       = NEUTRAL
confidence   = 0.70
time_horizon = MEDIUM
hard_constraints: []
key_evidence:
  ("台美 20 日滾動相關係數", source="cross_market:corr_20d")
  ("台美 60 日滾動相關係數", source="cross_market:corr_60d")
metrics:
  tw_us_corr_20d   = 0.618
  tw_us_corr_60d   = 0.651
  regime           = "strong_coupling"     ← 台美高度連動
  divergence_detected = False
  is_background_only  = True               ← 機器可讀旗標，排除方向性投票
  lead_lag_optimal = 0                    ← 同步無明顯領先落後
data_quality: completeness=1.00
narrative: "台美市場高度正向連動，台美股同步移動，此為市場背景指標，
           非獨立進出場訊號。"
```

---

## 二、三層仲裁邏輯設計

### Layer 1：硬約束規則引擎（不可被 LLM 覆蓋）

```python
# 輸入：list[AgentSignal]
# 輸出：HardConstraintSummary（所有 breached constraint 的聚合）

breached = [
    (sig.agent, hc)
    for sig in signals
    for hc in sig.hard_constraints
    if hc.breached
]
if breached:
    # 強制降級 + 強制警告，不進入 Layer 2/3 的加權計算
    final_recommendation = downgrade(layer2_result, breached)
    add_mandatory_warning(narrative, breached)
```

**規則**（spec §5.1①）：
- 任何 `hard_constraints[].breached == True` → 最終建議強制降級
- 可由任意 agent 觸發（risk、fundamental 均可）
- LLM 接收 breached 清單但**不得決定是否忽略**

### Layer 2：時間框架分層（不強融成單一結論）

```
時間框架分組：
  SHORT:  technical.signal, news.signal（若未降級）
  MEDIUM: macro.signal, cross_market.signal → cross_market 僅背景
  LONG:   fundamental.signal

各層獨立輸出方向，不跨層加權。
最終 narrative 按層呈現，讓使用者自行整合：
  【短期】xxx
  【中期】xxx
  【長期】xxx
  【風控】xxx（若有 breached）
```

### Layer 3：信心加權（同一時間框架內）

```python
# 同一 horizon 內有多個 agent 時
weight = confidence × data_quality.completeness × SOURCE_RELIABILITY[agent]

SOURCE_RELIABILITY = {
    AgentType.FUNDAMENTAL: 1.0,   # 財報數字最可靠
    AgentType.MACRO:       0.85,  # 總經有 TD-MACRO-01 暫定值
    AgentType.TECHNICAL:   0.80,
    AgentType.NEWS:        0.60,  # LLM 可能降級
    AgentType.CROSS_MARKET: 0.0,  # 排除方向性投票（§5.3）
    AgentType.RISK:        None,  # 風控不參與方向投票，走 Layer 1
}
# 低信心訊號（confidence < 0.20）自動排除出加權池
```

---

## 三、五個具體測試情境

---

### 情境 S1：硬約束優先 A — Risk Agent Delta 超限 + 其他 Agent 看多

**測試目的**：驗證 Layer 1 規則引擎在所有 agent 看多時仍強制觸發，LLM 不得繞過。

**輸入（6 個 AgentSignal）**：

| Agent | signal | confidence | hard_constraints |
|---|---|---|---|
| RISK | BEARISH | 0.80 | `net_delta_pct_nav` BREACHED (current=-2.33, limit=0.3) |
| TECHNICAL | BULLISH | 0.65 | [] |
| FUNDAMENTAL | BULLISH | 0.75 | [] |
| NEWS | BULLISH | 0.55 | [] |
| MACRO | BULLISH | 0.60 | [] |
| CROSS_MARKET | NEUTRAL | 0.70 | [] |

→ 若等權投票：5 BULLISH vs 1 BEARISH → 多頭  
→ Layer 1 偵測到 `net_delta_pct_nav.breached=True`

**期望輸出**：

```
最終建議：⚠️ 強制警告：Net Delta 空頭超限（current=-233% NAV，limit=±30%）
         在調整部位前不得加碼多頭頭寸。
層級分析：
  【短期】技術面偏多，新聞面偏多
  【長期】基本面偏多
  【風控】[MANDATORY] net_delta_pct_nav 已觸限。所有操作須先處理此風控項目。
整體信心：0.35（強制壓縮，因硬約束觸發）
```

**理由**：
- CLAUDE.md §二：「風控是硬約束，不是投票的一票」
- spec §5.1①：規則引擎層強制執行，不由 Supervisor LLM 自由裁量
- 五個看多訊號不能「抵銷」一個 breached constraint

**驗證點（測試斷言）**：
```python
assert "net_delta_pct_nav" in supervisor_output.mandatory_warnings
assert supervisor_output.overall_recommendation != Signal.BULLISH
assert supervisor_output.confidence <= 0.40
assert supervisor_output.risk_override == True
```

---

### 情境 S2：硬約束優先 B — Fundamental EWS Critical + 其他看多

**測試目的**：驗證 fundamental agent 的 EWS critical 也觸發 Layer 1（不只 risk agent）。

**輸入**：

| Agent | signal | confidence | hard_constraints |
|---|---|---|---|
| RISK | NEUTRAL | 0.70 | [] |
| TECHNICAL | BULLISH | 0.65 | [] |
| FUNDAMENTAL | BEARISH | 0.80 | `ews_receivables_spike` BREACHED (level=critical) |
| NEWS | BULLISH | 0.55 | [] |
| MACRO | BULLISH | 0.60 | [] |
| CROSS_MARKET | NEUTRAL | 0.70 | [] |

→ fundamental 的 HardConstraint:
```python
HardConstraint(
    type="ews_receivables_spike",
    current=0.85,   # receivables/revenue ratio spike 85%
    limit=0.50,
    breached=True,
    detail="EWS critical：應收帳款佔營收比率異常飆升，財務健康疑慮"
)
```

**期望輸出**：

```
最終建議：⚠️ 強制警告：財務預警（EWS Critical）已觸發
         應收帳款佔營收比率異常飆升，財務健康疑慮。
         即使短中期技術面/總經面偏多，長期財務風險需優先處理。
層級分析：
  【短期】技術面偏多，新聞面偏多
  【中期】總經面偏多
  【長期】[MANDATORY EWS CRITICAL] 基本面發出財務預警，強制蓋過長期方向
  【風控】無希臘字母風控問題
整體信心：0.30（強制壓縮）
```

**理由**：
- CLAUDE.md §二：「目前已知會發起 hard_constraints 的 agent：risk agent（Greeks 曝險限制）、fundamental agent（EWS critical/high 財務預警）」
- 這個情境驗證 Supervisor 對所有 agent 的 hard_constraints 統一處理，不是只檢查 risk
- EWS critical 比短期技術面訊號優先級更高（長期財務惡化 > 短期趨勢）

**驗證點**：
```python
# 所有 agent 都能發起 hard constraint
assert any(
    hc.breached
    for sig in signals
    if sig.agent == AgentType.FUNDAMENTAL
    for hc in sig.hard_constraints
)
assert "ews_receivables_spike" in supervisor_output.mandatory_warnings
assert supervisor_output.risk_override == True
```

---

### 情境 S3：時間框架分層 — 技術面（短期）看空 + 基本面（長期）看多

**測試目的**：驗證不同時間框架的訊號不被強融成單一方向，且 Supervisor 的 narrative 按層呈現。

**輸入**：

| Agent | signal | confidence | time_horizon | hard_constraints |
|---|---|---|---|---|
| RISK | NEUTRAL | 0.70 | SHORT | [] |
| TECHNICAL | BEARISH | 0.70 | SHORT | [] |
| FUNDAMENTAL | BULLISH | 0.80 | LONG | [] |
| NEWS | BEARISH | 0.55 | SHORT | [] |
| MACRO | BULLISH | 0.60 | MEDIUM | [] |
| CROSS_MARKET | NEUTRAL | 0.70 | MEDIUM | [] (is_background_only=True) |

→ 短期層：TECHNICAL(BEARISH, 0.70) + NEWS(BEARISH, 0.55) → 偏空  
→ 中期層：MACRO(BULLISH, 0.60) → 偏多；CROSS_MARKET 排除投票  
→ 長期層：FUNDAMENTAL(BULLISH, 0.80) → 偏多

**期望輸出**：

```
層級分析（分層呈現，不強融）：
  【短期 intraday-short】偏空
    技術面：RSI 超買區回落，MACD 死亡交叉（信心 0.70）
    新聞面：負面情緒偏高（信心 0.55）
  【中期 medium】偏多
    總經面：NFP、CPI 數據偏利多（信心 0.60）
    台美背景：strong_coupling regime，無明顯分化（僅背景資訊）
  【長期 long】偏多
    基本面：ROIC > WACC，EWS 無預警，財務健康（信心 0.80）
  【風控】無硬約束觸發
投資人使用建議（narrative）：
  "短線技術面偏空，不適合追高。若持有長期倉位，基本面支撐完整，
  可持倉觀察，但短期或需面對回調壓力。"
```

**理由**：
- spec §5.1②：「不把六個 agent 硬融成一句話，而是按 time_horizon 分層呈現」
- 「技術面(short)看空 + 財報(long)看多」是完全正常的分歧，強融成 NEUTRAL 會失去資訊
- Supervisor LLM 的工作是「把分層結論轉成給人看的白話說明」，不是「選邊站」

**驗證點**：
```python
# Supervisor output 必須有 per_horizon breakdown
assert "short" in supervisor_output.horizon_breakdown
assert "long" in supervisor_output.horizon_breakdown
# 不得有單一 overall_direction 蓋掉分層資訊
assert supervisor_output.horizon_breakdown["short"].direction == Signal.BEARISH
assert supervisor_output.horizon_breakdown["long"].direction == Signal.BULLISH
# 不允許兩個方向相反的層被平均成 NEUTRAL 然後消去資訊
```

---

### 情境 S4：信心加權 — 同一時間框架內高低信心衝突

**測試目的**：驗證 Layer 3 信心加權讓高品質訊號主導，不讓降級訊號等權稀釋。

**輸入（SHORT 時間框架內兩個衝突 agent）**：

| Agent | signal | confidence | completeness | hard_constraints |
|---|---|---|---|---|
| RISK | NEUTRAL | 0.70 | 1.00 | [] |
| TECHNICAL | BULLISH | 0.70 | 1.00 | [] |
| FUNDAMENTAL | BULLISH | 0.78 | 1.00 | [] |
| NEWS | NEUTRAL | 0.10 | 0.00 | [] |（← LLM 降級，confidence floor） |
| MACRO | BULLISH | 0.60 | 0.67 | [] |
| CROSS_MARKET | NEUTRAL | 0.70 | 1.00 | [] |（is_background_only=True，排除） |

SHORT 層：TECHNICAL(BULLISH, w=0.70×1.00×0.80=0.56) + NEWS(NEUTRAL, w=0.10×0.00×0.60=0.0)

→ NEWS 的 confidence=0.10 + completeness=0.00 → 有效權重=0  
→ SHORT 層完全由 TECHNICAL 主導 → BULLISH

**錯誤實作**（等權投票）：
```
SHORT: TECHNICAL(BULLISH) + NEWS(NEUTRAL) → 1 BULLISH + 1 NEUTRAL → 平均偏 BULLISH
但這樣 NEWS 降級訊號仍貢獻 50% 投票權，稀釋了技術面真實訊號
```

**正確實作**（信心加權）：
```
SHORT: 有效貢獻 = TECHNICAL(BULLISH, weight=0.56) + NEWS(weight≈0) → BULLISH
理由：NEWS 的 completeness=0.00 讓其有效權重歸零
```

**期望輸出**：

```
SHORT 層結果：BULLISH（信心 0.70，主導：TECHNICAL）
NEWS 降級說明：新聞面訊號因 LLM 分析失敗（confidence floor 0.10，
              completeness=0），本次分析中排除出加權池。
              下次分析仍包含新聞來源，建議補充手動確認。
```

**理由**：
- spec §5.1③：「按 confidence × data_quality × 來源可靠度權重加權，而非等權平均」
- NEWS agent 在 LLM 失敗時明確設計了 `completeness=0.0` 正是為了讓 Supervisor 識別
- confidence=0.10 是 `_LLM_FAILURE_CONFIDENCE` 硬底線，不是真實分析產出的 0.10

**驗證點**：
```python
# LLM failed news must not influence direction
news_sig = [s for s in signals if s.agent == AgentType.NEWS][0]
assert news_sig.confidence == pytest.approx(0.10)
assert news_sig.data_quality.completeness == 0.0
assert news_sig.metrics.get("llm_analysis_failed") == True
# Supervisor excludes it from weighted pool
excluded = supervisor_output.excluded_from_voting
assert AgentType.NEWS in excluded
assert "llm_analysis_failed" in supervisor_output.exclusion_reasons[AgentType.NEWS]
```

---

### 情境 S5：Cross_Market 背景資訊不參與方向投票

**測試目的**：驗證 spec §5.3 的強制行為——cross_market signal 從不進入方向性投票池，
但 regime 資訊仍傳入 LLM narrative context。

**輸入（特別建立台美背離場景）**：

| Agent | signal | confidence | metrics |
|---|---|---|---|
| RISK | NEUTRAL | 0.70 | [] |
| TECHNICAL | BULLISH | 0.65 | sma_bullish=True |
| FUNDAMENTAL | BULLISH | 0.75 | ROIC>WACC |
| NEWS | BULLISH | 0.55 | has_official=True |（正常分析，非降級） |
| MACRO | NEUTRAL | 0.55 | macro_score=0.05 |
| CROSS_MARKET | **BEARISH** | 0.65 | regime="short_term_counter", corr_20d=-0.35, is_background_only=**True** |

→ CROSS_MARKET.signal = BEARISH 代表「台美近期背離（20日相關係數=-0.35）」  
→ 這**不代表**台積電股票看空，只代表台美連動結構暫時破裂

**錯誤實作**（等權投票）：
```
BULLISH: 3票（tech, fundamental, news）
BEARISH: 1票（cross_market）
NEUTRAL: 2票（risk, macro）
→ 3 BULLISH vs 1 BEARISH → 算法給出「弱多頭」
→ 但這 1 票 BEARISH 根本不代表看空標的！意義完全不同
```

**正確實作**（spec §5.3 強制規則）：
```python
if sig.agent == AgentType.CROSS_MARKET:
    skip_directional_vote = True        # 從投票池排除
    background_context.append(sig)     # 進入 narrative context 作背景說明
```

**期望輸出**：

```
方向性投票（5 agents，排除 CROSS_MARKET）：
  BULLISH: TECHNICAL(0.65), FUNDAMENTAL(0.75), NEWS(0.55) → 3票，加權多頭
  NEUTRAL: RISK(0.70), MACRO(0.55) → 2票
  方向：BULLISH（短期信心偏低，長期健康）

背景資訊（CROSS_MARKET，不參與投票）：
  regime=short_term_counter（台美 20 日相關係數=-0.35，近期背離）
  解讀建議：台股近期與美股短暫脫鉤，若美股大跌，台股相關保護
          效果下降。建議關注後續 60 日相關係數是否回歸。

narrative 明確說明：
  "CROSS_MARKET 訊號（BEARISH）代表台美連動結構異常，非個股方向。
  已排除出投票，作為風險背景參考。"
```

**理由**：
- spec §5.3 強制規則：`is_background_only=True` → 排除方向性投票
- cross_market BEARISH ≠ 標的看空；混入投票會系統性低估多頭共識
- 但 regime 資訊仍有價值，應出現在 narrative 的「背景脈絡」區塊

**驗證點**：
```python
cm_sig = [s for s in signals if s.agent == AgentType.CROSS_MARKET][0]
assert cm_sig.metrics["is_background_only"] == True
assert cm_sig.signal == Signal.BEARISH  # 它確實是 BEARISH

# Supervisor must NOT count it as a bearish vote
directional_pool = supervisor_output.directional_vote_pool
assert AgentType.CROSS_MARKET not in directional_pool

# But it SHOULD appear in background context
assert cm_sig in supervisor_output.background_context
# And overall should NOT be NEUTRAL/BEARISH because of cross_market
assert supervisor_output.short_horizon_direction == Signal.BULLISH
```

---

## 四、實作注意事項（供 review 後的實作階段參考）

### 輸出 Schema 建議（supervisor/signal.py）

```python
@dataclass
class HorizonResult:
    direction: Signal
    weighted_confidence: float
    contributing_agents: list[tuple[AgentType, Signal, float]]  # (agent, signal, weight)
    excluded_agents: list[AgentType]   # 排除原因見 exclusion_reasons

@dataclass  
class SupervisorOutput:
    target: Target
    asof: datetime
    # Layer 1
    hard_constraint_breaches: list[tuple[AgentType, HardConstraint]]
    risk_override: bool
    mandatory_warnings: list[str]
    # Layer 2
    horizon_breakdown: dict[str, HorizonResult]  # "short"|"medium"|"long"
    # Layer 3
    excluded_from_voting: list[AgentType]
    exclusion_reasons: dict[AgentType, str]
    background_context: list[AgentSignal]   # cross_market + other background
    # Final
    overall_narrative: str   # LLM 產出，引用 horizon_breakdown 的結構化結論
    raw_agent_signals: list[AgentSignal]
```

### cross_market 排除檢查（強制，不由 LLM 決定）

```python
def _should_exclude_from_directional_vote(sig: AgentSignal) -> tuple[bool, str]:
    """規則引擎層，非 LLM 自由裁量。"""
    if sig.agent == AgentType.CROSS_MARKET:
        return True, "cross_market is background-only (spec §5.3)"
    if sig.agent == AgentType.RISK:
        return True, "risk agent routes to Layer 1, not directional vote"
    if sig.data_quality.completeness == 0.0:
        # 明確旗標優於隱性閾值：
        # completeness==0.0 已完整覆蓋兩種已知降級情境：
        #   (a) news_agent: LLM failure → completeness 強制歸零
        #   (b) macro_agent: no_recent_events → completeness 強制歸零
        # 不使用 confidence <= threshold 猜測，因為無法區分
        # 「正常分析結果剛好低信心」與「降級輸出的信心底線」。
        # 若未來有 agent 需要表達「低信心但非 degraded」，
        # 應在 metrics["degraded"]=False 明確標註，而非靠 Supervisor 猜測。
        return True, "completeness=0.0 indicates degraded output (LLM failure or no data)"
    return False, ""
```

### 信心加權公式（Layer 3）

```python
SOURCE_RELIABILITY: dict[AgentType, float] = {
    AgentType.FUNDAMENTAL: 1.0,    # 財報數字確定性最高
    AgentType.MACRO:       0.85,   # ⚠️ 暫定值；有 TD-MACRO-01 未解決項目
    AgentType.TECHNICAL:   0.80,   # ⚠️ 暫定值
    AgentType.NEWS:        0.60,   # ⚠️ 暫定值；LLM 依賴度高
}

def effective_weight(sig: AgentSignal) -> float:
    rel = SOURCE_RELIABILITY.get(sig.agent, 0.0)
    return sig.confidence * sig.data_quality.completeness * rel
```

---

## 五、待 Review 的開放問題

1. **SupervisorOutput schema**：上述 dataclass 設計是否符合 Phase 5 的 AgentSignal schema
   延伸方向？還是 Supervisor 應該也輸出一個 AgentSignal（agent=SUPERVISOR）？

2. **overall_narrative 的 LLM 溫度**：Supervisor LLM 的 system prompt 要明確禁止哪些行為？
   （不能忽略 hard constraint、不能自行決定 cross_market 是否參與投票⋯⋯）

3. **horizon_breakdown 空層的處理**：若 MEDIUM 層只有 CROSS_MARKET（排除後為空），
   該層顯示「無方向性訊號」還是繼承 SHORT 層的結論？

4. **Source reliability 暫定值**：0.80/0.85/0.60 完全是主觀設定，
   Phase 6 是否建立歷史回測機制讓 reliability 動態調整？

5. ~~**信心門檻 0.15**~~：**已拍板**。`_should_exclude_from_directional_vote()` 移除
   `confidence <= 0.15` 條件，只保留 `completeness == 0.0` 這個明確旗標。
   理由：明確旗標優於隱性閾值——confidence 數值無法區分「正常低信心」與
   「降級輸出的底線值」；completeness==0.0 已完整覆蓋 news LLM failure 和
   macro no_recent_events 兩種已知降級情境。未來若有 agent 需要表達
   「confidence 很低但不是 degraded」，用 `metrics["degraded"]=False` 明確標註，
   不靠 Supervisor 猜測數值代表什麼。

---

*停止點：以上為設計文件，未修改任何 .py 程式碼。待 review 確認後再開始 supervisor/graph.py 實作。*
