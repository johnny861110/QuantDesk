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

## 三種場景

1. **single_stock**（單標的分析）
   觸發條件：提到單一股票代號或名稱，詢問現況、走勢、值不值得買
   例：「2330 現在怎樣」「台積電適合買嗎」「0050 的技術面」

2. **portfolio_risk**（組合風控）
   觸發條件：提到選擇權部位、多口期貨、持倉組合的風險
   例：「我有 10 口 TXO Call 850」「我的 delta 曝險多少」「我的選擇權組合要怎麼對沖」

3. **multi_stock_scan**（多標的篩選）
   觸發條件：掃描、找機會、比較多檔、某個產業或類股
   例：「幫我掃金融股找機會」「技術面最強的半導體股」「哪幾檔最近外資在買」

## 輸出格式（JSON）

```json
{
  "scenario": "single_stock",
  "targets": ["2330"],
  "market": "TW",
  "depth": "standard",
  "extra_context": {}
}
```

## 規則
- targets：台股用 4 碼數字（不加 .TW），美股用 ticker（大寫）
- market：TW / US / MIXED
- depth：quick（純看技術面即可）/ standard（多 domain 分析）/ deep（完整研究）
- portfolio_risk 場景：extra_context.positions 填使用者提到的部位描述
- multi_stock_scan 場景：extra_context.sector 填產業，extra_context.criteria 填篩選條件
- 不確定時 scenario 預設 single_stock，depth 預設 standard
- 只輸出 JSON，不要有其他文字
"""

# ─── LLM-based router ────────────────────────────────────────────────────────


@observe(name="router:classify_intent", as_type="generation")  # type: ignore[misc]
def _llm_classify(query: str) -> dict[str, Any]:
    """
    呼叫 GPT-4o-mini 做意圖分類，回傳 dict（JSON 解析後）。
    失敗時 raise，由 route() 捕捉並使用 fallback。
    """
    from openai import OpenAI

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


def _regex_fallback(query: str) -> RouterOutput:
    """
    無 LLM 的規則式 fallback 分類。
    準確率低於 LLM，但保證可用性。
    """
    tickers = _TW_TICKER_RE.findall(query)

    if _PORTFOLIO_KEYWORDS.search(query):
        scenario = "portfolio_risk"
        targets = tickers or ["PORTFOLIO"]
    elif _SCAN_KEYWORDS.search(query) or len(tickers) > 1:
        scenario = "multi_stock_scan"
        targets = tickers
    elif tickers:
        scenario = "single_stock"
        targets = [tickers[0]]
    else:
        scenario = "single_stock"
        targets = []

    return RouterOutput(
        scenario=scenario,  # type: ignore[arg-type]
        targets=targets,
        market="TW",
        depth="standard",
        original_query=query,
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
        output = RouterOutput(
            scenario=raw.get("scenario", "single_stock"),  # type: ignore[arg-type]
            targets=raw.get("targets", []),
            market=raw.get("market", "TW"),
            depth=raw.get("depth", "standard"),  # type: ignore[arg-type]
            original_query=query,
            extra_context=raw.get("extra_context", {}),
        )
        update_current_span(output={
            "scenario": output.scenario,
            "targets": output.targets,
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
