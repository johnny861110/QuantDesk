"""
Intent Router — 使用者意圖分類與場景路由

Public API
----------
route(query)              → RouterOutput（主入口）
route_from_text(query)    → RouterOutput（同上，alias）

功能
----
1. 用 GPT-4o-mini 理解使用者輸入的自然語言意圖
2. 分類為三種場景：single_stock / portfolio_risk / multi_stock_scan
3. 解析股票代號、市場、分析深度
4. 回傳機器可讀的 RouterOutput

失敗處理
--------
LLM 呼叫失敗時，fallback 至 regex-based 規則分類（不影響可用性）。
Langfuse trace 記錄每次分類決策。

使用方式
--------
    from router.intent_router import route

    result = route("2330 現在怎樣")
    # RouterOutput(scenario='single_stock', targets=['2330'], market='TW', depth='standard')

    result = route("幫我掃金融股找機會")
    # RouterOutput(scenario='multi_stock_scan', targets=[], market='TW', ...)

    result = route("我有 10 口 TXO Call 850 9月到期")
    # RouterOutput(scenario='portfolio_risk', targets=['TXO'], market='TW', ...)
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from observability.langfuse_setup import observe, update_current_span
from schemas.domain_report import RouterOutput


# ─── Router System Prompt ─────────────────────────────────────────────────────

_ROUTER_SYSTEM_PROMPT = """你是 QuantDesk 的智能路由員，負責理解使用者的金融分析需求並分類。

## 六種查詢類型與對應 agents

1. **stock_analysis**（個股技術/籌碼/新聞分析）
   觸發：問技術面、K線、均線、籌碼、外資、新聞
   例：「2330 技術面分析」「台積電外資動向」「聯發科最近有什麼消息」
   agents: ["technical", "chip", "news"]
   run_supervisor: false, run_debate: false

2. **stock_investment**（**個股**投資建議 / 這檔值不值得買）
   觸發：針對「某一檔股票」問適不適合買、是不是買點、要不要進場、完整評估
   例：「台積電現在值得買嗎」「2330 完整分析」「幫我評估鴻海」「聯發科可以進場嗎」
   agents: ["technical", "chip", "news", "fundamental", "macro", "cross_market"]
   run_supervisor: true, run_debate: true
   ⚠️ 注意：**不含 risk**。risk agent 分析的是使用者「整個投資組合」的
      Greeks 曝險，與所查個股無關；納入會讓不相干部位的風控警訊
      錯誤地降級這檔股票的建議。

3. **investment_strategy**（**組合層**策略調整）
   觸發：針對「使用者手上的整個組合／部位」問該怎麼調整、如何配置、要不要換股
   例：「我的組合該怎麼調整」「現在該加碼還是減碼」「幫我看整體部位配置」
        「我的持股組合有什麼風險」
   agents: ["technical", "chip", "news", "fundamental", "macro", "cross_market", "risk"]
   run_supervisor: true, run_debate: true
   ⚠️ 與 stock_investment 的差別在於**問的是組合、不是單一標的**。
      只要問句聚焦在某一檔股票，一律選 stock_investment。

4. **fundamental_review**（財報/基本面分析）
   觸發：問財報、EPS、ROE、毛利率、盈餘品質
   例：「台積電的財報怎麼樣」「2330 這季 EPS 多少」「鴻海的毛利率趨勢」
   agents: ["fundamental", "chip"]
   run_supervisor: false, run_debate: false

5. **macro_outlook**（總經/市場環境）
   觸發：問利率、通膨、GDP、聯準會、美股、總體經濟、市場背景
   例：「現在總經環境怎樣」「聯準會降息對台股影響」「美股最近走勢」
   agents: ["macro", "cross_market"]
   run_supervisor: false, run_debate: false

6. **portfolio_risk**（組合風控/Greeks）
   觸發：提到選擇權部位、期貨口數、delta/gamma/vega、對沖
   例：「我有 10 口 TXO Call 850 九月到期」「我的 delta 曝險」「組合怎麼對沖」
   agents: ["risk"]
   run_supervisor: false, run_debate: false

## 輸出格式（JSON）

```json
{
  "scenario": "single_stock",
  "targets": ["2330"],
  "market": "TW",
  "depth": "standard",
  "query_type": "stock_analysis",
  "agents": ["technical", "chip", "news"],
  "run_supervisor": false,
  "run_debate": false,
  "extra_context": {}
}
```

## 規則
- targets：台股用 4 碼數字（不加 .TW），美股用 ticker（大寫）
- market：TW / US / MIXED
- depth：quick / standard / deep（不確定用 standard）
- 不確定 query_type 時預設 stock_analysis
- 個股 vs 組合是最容易搞混的一組：問句主詞是「某一檔股票」→ stock_investment；
  是「我的組合／我的部位／整體配置」→ investment_strategy
- portfolio_risk 場景：extra_context.positions 填使用者描述的部位
- 只輸出 JSON，不要有其他文字
"""

# ─── LLM-based router ────────────────────────────────────────────────────────


@observe(name="router:classify_intent")  # type: ignore[misc]
def _llm_classify(query: str) -> dict[str, Any]:
    """
    呼叫 GPT-4o-mini 做意圖分類，回傳 dict（JSON 解析後）。
    失敗時 raise，由 route() 捕捉並使用 fallback。
    """
    from langfuse.openai import OpenAI

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
        response_format={"type": "json_object"},
        max_tokens=300,
        temperature=0.0,    # 路由分類需要確定性
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content)  # type: ignore[no-any-return]


# ─── Regex fallback ────────────────────────────────────────────────────────────

# 台股四碼數字代號正則
_TW_TICKER_RE = re.compile(r"\b([0-9]{4})\b")

# 常見 portfolio/options 關鍵字
_PORTFOLIO_KEYWORDS = re.compile(
    r"(口|TXO|PUT|CALL|call|put|選擇權|期貨|組合|delta|gamma|vega|theta|對沖|避險)",
    re.IGNORECASE,
)

# 掃描類關鍵字
_SCAN_KEYWORDS = re.compile(
    r"(掃|篩|找機會|找標的|哪幾|哪些|金融股|半導體|電子股|傳產|類股|產業|板塊)",
    re.IGNORECASE,
)


_QUERY_TYPE_AGENTS: dict[str, list[str]] = {
    "stock_analysis":     ["technical", "chip", "news"],
    # 個股投資建議：**刻意不含 risk**。risk agent 讀 config/positions.yaml
    # 分析的是「整個投資組合」的 Greeks，與所查個股無關。若納入，
    # 組合中不相干部位的 breach 會透過 Supervisor Layer 1 強制降級
    # 整個個股建議（confidence 壓到 _RISK_OVERRIDE_CONFIDENCE=0.35）。
    "stock_investment":   ["technical", "chip", "news", "fundamental", "macro", "cross_market"],
    # 組合層策略：含 risk，組合 breach 強制降級在此情境下是正確行為。
    "investment_strategy": ["technical", "chip", "news", "fundamental", "macro", "cross_market", "risk"],
    "fundamental_review": ["fundamental", "chip"],
    "macro_outlook":      ["macro", "cross_market"],
    "portfolio_risk":     ["risk"],
}

# 需要 Supervisor 仲裁 + Debate 的查詢類型。
# 用集合成員判斷取代舊有的 `query_type == "investment_strategy"` 硬編碼比較，
# 避免新增類型時漏改（原本散落 6 處）。
_SUPERVISOR_QUERY_TYPES: frozenset[str] = frozenset({
    "stock_investment", "investment_strategy",
})

_FUNDAMENTAL_KEYWORDS = re.compile(
    r"(財報|EPS|ROE|毛利|盈餘|本益比|配息|股利|資產負債|現金流)",
    re.IGNORECASE,
)
_MACRO_KEYWORDS = re.compile(
    r"(總經|利率|通膨|CPI|GDP|聯準會|Fed|美股|降息|升息|殖利率|市場環境)",
    re.IGNORECASE,
)
_STRATEGY_KEYWORDS = re.compile(
    r"(值得買|適合買|買點|賣點|進場|出場|投資建議|完整分析|評估|看法)",
    re.IGNORECASE,
)


def _regex_fallback(query: str) -> RouterOutput:
    """
    無 LLM 的規則式 fallback 分類。
    準確率低於 LLM，但保證可用性。
    """
    tickers = _TW_TICKER_RE.findall(query)

    if _PORTFOLIO_KEYWORDS.search(query):
        scenario = "portfolio_risk"
        query_type = "portfolio_risk"
        targets = tickers or ["PORTFOLIO"]
    elif _SCAN_KEYWORDS.search(query) or len(tickers) > 1:
        scenario = "multi_stock_scan"
        query_type = "stock_analysis"
        targets = tickers
    elif _MACRO_KEYWORDS.search(query):
        scenario = "single_stock"
        query_type = "macro_outlook"
        targets = tickers
    elif _FUNDAMENTAL_KEYWORDS.search(query):
        scenario = "single_stock"
        query_type = "fundamental_review"
        targets = [tickers[0]] if tickers else []
    elif _STRATEGY_KEYWORDS.search(query):
        scenario = "single_stock"
        # keyword fallback 走到這裡必為個股情境——組合類查詢已被前面的
        # _PORTFOLIO_KEYWORDS 分支攔截。組合層 investment_strategy 只由
        # LLM router 產生（regex 無法可靠區分個股 vs 組合策略）。
        query_type = "stock_investment"
        targets = [tickers[0]] if tickers else []
    elif tickers:
        scenario = "single_stock"
        query_type = "stock_analysis"
        targets = [tickers[0]]
    else:
        scenario = "single_stock"
        query_type = "stock_analysis"
        targets = []

    agents = _QUERY_TYPE_AGENTS[query_type]
    return RouterOutput(
        scenario=scenario,  # type: ignore[arg-type]
        targets=targets,
        market="TW",
        depth="standard",
        original_query=query,
        query_type=query_type,  # type: ignore[arg-type]
        agents=agents,
        run_supervisor=query_type in _SUPERVISOR_QUERY_TYPES,
        run_debate=query_type in _SUPERVISOR_QUERY_TYPES,
    )


# ─── Public API ───────────────────────────────────────────────────────────────


@observe(name="router:route")  # type: ignore[misc]
def route(query: str) -> RouterOutput:
    """
    主入口：分類使用者意圖，回傳 RouterOutput。

    優先使用 GPT-4o-mini；失敗時 fallback 至正則規則。

    Parameters
    ----------
    query : 使用者的自然語言輸入

    Returns
    -------
    RouterOutput
    """
    update_current_span(input={"query": query})

    try:
        raw = _llm_classify(query)
        query_type = raw.get("query_type", "stock_analysis")
        # Fallback agents if LLM didn't specify or gave unknown query_type
        agents = raw.get("agents") or _QUERY_TYPE_AGENTS.get(query_type, _QUERY_TYPE_AGENTS["stock_analysis"])
        output = RouterOutput(
            scenario=raw.get("scenario", "single_stock"),  # type: ignore[arg-type]
            targets=raw.get("targets", []),
            market=raw.get("market", "TW"),
            depth=raw.get("depth", "standard"),  # type: ignore[arg-type]
            original_query=query,
            extra_context=raw.get("extra_context", {}),
            query_type=query_type,  # type: ignore[arg-type]
            agents=agents,
            run_supervisor=bool(raw.get("run_supervisor", query_type in _SUPERVISOR_QUERY_TYPES)),
            run_debate=bool(raw.get("run_debate", query_type in _SUPERVISOR_QUERY_TYPES)),
        )
        update_current_span(output={
            "scenario": output.scenario,
            "targets": output.targets,
            "query_type": output.query_type,
            "agents": output.agents,
            "method": "llm",
        })
        return output

    except Exception as exc:  # noqa: BLE001
        # LLM 失敗 → regex fallback
        output = _regex_fallback(query)
        update_current_span(output={
            "scenario": output.scenario,
            "targets": output.targets,
            "method": "regex_fallback",
            "error": str(exc)[:100],
        })
        return output


# route_from_text 是 route 的 alias，語意更明確
route_from_text = route
