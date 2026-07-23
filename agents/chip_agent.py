"""
Chip Analysis Agent — 台股籌碼面分析（Phase 7 新增）

職責
----
分析台股的籌碼面訊號：
  - 三大法人（外資/投信/自營商）買賣超方向與力道
  - 融資融券餘額趨勢（槓桿多空比例）
  - 外資持股比例變化（長線資金動向）
  - 期貨三大法人淨部位（方向性對賭）

Public API
----------
run_chip_agent(symbol, market, adapter, asof)  → DomainReport
create_chip_graph()                             → LangGraph CompiledGraph

架構模式：ReAct + 確定性計算
-----------------------------
確定性計算層（純函數，不含 LLM）：
  _compute_institutional_score()  計算法人流向分數
  _compute_margin_pressure()      計算融資槓桿壓力
  _compute_chip_concentration()   計算籌碼集中度

LLM 推理層（OpenAI，僅組織語言 + 解讀數值組合意涵）：
  _llm_synthesize_chip()          讀取確定性分數，生成白話摘要

CLAUDE.md 三條原則：
  ① LLM 不產出數字，所有數值來自確定性計算
  ② 無 hard_constraints（籌碼面不設硬約束）
  ③ key_findings 每個值都來自計算工具
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph

from adapters.chip_adapter import (
    ChipDataAdapter,
    FuturesInstResult,
    InstitutionalResult,
    MarginResult,
    ShareholdingResult,
)
from observability.langfuse_setup import observe, update_current_span
from schemas.agent_signal import AgentType, Signal, TimeHorizon
from schemas.domain_report import DomainReport, ReasoningStep


# ─── LangGraph State ──────────────────────────────────────────────────────────


class ChipAgentState(TypedDict):
    symbol: str
    market: str
    adapter: Optional[ChipDataAdapter]
    asof: datetime
    # 各資料集結果
    institutional: Optional[InstitutionalResult]
    margin: Optional[MarginResult]
    shareholding: Optional[ShareholdingResult]
    futures_inst: Optional[FuturesInstResult]
    # 確定性計算結果
    scores: dict[str, float]           # 各維度分數
    key_findings: dict[str, Any]       # 結構化數值
    reasoning_steps: list[ReasoningStep]
    # 最終輸出
    pipeline_errors: list[str]
    report: Optional[DomainReport]


# ─── 確定性計算層（純函數）───────────────────────────────────────────────────


def _compute_institutional_score(result: InstitutionalResult) -> dict[str, float]:
    """
    從三大法人資料計算方向性分數。

    Returns dict 含：
      institutional_score  : [-1, 1] 綜合分數
      foreign_score        : [-1, 1] 外資分數
      trust_score          : [-1, 1] 投信分數
      foreign_net_shares   : 外資 N 日累積買賣超（股數）
      consecutive_days     : 外資連續買超天數（正=買，負=賣）
    """
    if not result.records:
        return {
            "institutional_score": 0.0,
            "foreign_score": 0.0,
            "trust_score": 0.0,
            "foreign_net_shares": 0.0,
            "consecutive_days": 0.0,
        }

    # 外資分數：用連續買超天數 + 累積淨買超量做判斷
    foreign_shares = result.foreign_net_shares
    consecutive = result.consecutive_foreign_buy

    # 連續買超 ≥ 3 天 → 正向訊號；連續賣超 ≥ 3 天 → 負向訊號
    if consecutive >= 3:
        foreign_score = min(1.0, consecutive / 10.0 + 0.3)
    elif consecutive <= -3:
        foreign_score = max(-1.0, consecutive / 10.0 - 0.3)
    elif consecutive > 0:
        foreign_score = 0.2
    elif consecutive < 0:
        foreign_score = -0.2
    else:
        foreign_score = 0.0

    # 投信分數（較弱的先行指標）
    trust_shares = result.trust_net_shares
    if trust_shares > 0:
        trust_score = 0.3
    elif trust_shares < 0:
        trust_score = -0.3
    else:
        trust_score = 0.0

    # 加權：外資 70%，投信 30%
    institutional_score = 0.70 * foreign_score + 0.30 * trust_score

    return {
        "institutional_score": round(institutional_score, 4),
        "foreign_score": round(foreign_score, 4),
        "trust_score": round(trust_score, 4),
        "foreign_net_shares": round(foreign_shares, 0),
        "consecutive_days": float(consecutive),
    }


def _compute_margin_pressure(result: MarginResult) -> dict[str, float]:
    """
    從融資融券資料計算籌碼壓力分數。

    融資增加 + 股價上漲 = 追高槓桿（中性偏負）
    融資減少 + 股價下跌 = 去槓桿（中性偏正，洗清浮額）
    融券高位 = 可能的軋空燃料
    融券低位 = 無軋空支撐

    Returns dict 含：
      margin_pressure_score : [-1, 1]（負=籌碼承壓，正=籌碼輕盈）
      margin_balance        : 融資餘額（千股）
      short_balance         : 融券餘額（千股）
      margin_change_5d      : 5 日融資變化
    """
    if not result.records:
        return {
            "margin_pressure_score": 0.0,
            "margin_balance": 0.0,
            "short_balance": 0.0,
            "margin_change_5d": 0.0,
        }

    margin_change = result.margin_change_5d
    short_balance = result.short_balance

    # 融資 5 日大幅增加 → 槓桿追高，籌碼承壓（→ 偏負）
    # 融資 5 日減少 → 去槓桿，籌碼較輕（→ 偏正）
    if margin_change > 0:
        # 增加越多壓力越大（用相對比例，最大扣 0.4 分）
        if result.margin_balance > 0:
            change_ratio = abs(margin_change) / max(result.margin_balance, 1)
            margin_score = -min(0.4, change_ratio * 2)
        else:
            margin_score = -0.2
    elif margin_change < 0:
        margin_score = 0.2   # 融資減少 = 正面
    else:
        margin_score = 0.0

    # 融券高位 → 有軋空潛力（偏正）
    short_score = 0.0
    if result.margin_balance > 0 and short_balance > 0:
        short_ratio = short_balance / result.margin_balance
        if short_ratio > 0.1:    # 融券 > 10% 融資
            short_score = 0.15   # 軋空潛力

    margin_pressure_score = margin_score + short_score

    return {
        "margin_pressure_score": round(max(-1.0, min(1.0, margin_pressure_score)), 4),
        "margin_balance": round(result.margin_balance, 1),
        "short_balance": round(short_balance, 1),
        "margin_change_5d": round(margin_change, 1),
    }


def _compute_shareholding_signal(result: ShareholdingResult) -> dict[str, float]:
    """
    從外資持股比例計算信號。

    持股比例上升 → 長線資金流入（偏正）
    持股比例下降 → 長線資金流出（偏負）
    接近上限（通常 49%）→ 加碼空間受限
    """
    if not result.records:
        return {
            "shareholding_score": 0.0,
            "foreign_ownership_ratio": 0.0,
            "ownership_change_30d": 0.0,
        }

    change = result.change_30d
    ratio = result.latest_ratio

    # 30 日持股比例變化
    if change > 0.5:    # +0.5 ppt 以上
        shareholding_score = 0.4
    elif change > 0:
        shareholding_score = 0.2
    elif change < -0.5:
        shareholding_score = -0.4
    elif change < 0:
        shareholding_score = -0.2
    else:
        shareholding_score = 0.0

    # 若持股已超過 45%，加碼空間受限 → 稍微減分
    if ratio > 45.0:
        shareholding_score = max(shareholding_score - 0.1, -1.0)

    return {
        "shareholding_score": round(shareholding_score, 4),
        "foreign_ownership_ratio": round(ratio, 2),
        "ownership_change_30d": round(change, 3),
    }


def _compute_futures_signal(result: FuturesInstResult) -> dict[str, float]:
    """
    從期貨三大法人部位計算方向信號。

    外資期貨淨多 → 多方方向（偏正）
    外資期貨淨空 → 空方方向（偏負）
    外資現貨籌碼與期貨方向一致 → 訊號更強
    """
    if not result.records:
        return {
            "futures_signal_score": 0.0,
            "foreign_net_position": 0.0,
        }

    foreign_net = result.foreign_net_position

    # 淨部位 > 5000 口 → 強多方；< -5000 → 強空方
    if foreign_net > 5000:
        score = min(0.5, foreign_net / 20000)
    elif foreign_net > 0:
        score = 0.2
    elif foreign_net < -5000:
        score = max(-0.5, foreign_net / 20000)
    elif foreign_net < 0:
        score = -0.2
    else:
        score = 0.0

    return {
        "futures_signal_score": round(score, 4),
        "foreign_net_position": round(foreign_net, 0),
    }


def _determine_chip_signal(
    scores: dict[str, float],
) -> tuple[Signal, float]:
    """
    整合四個維度的分數，決定最終籌碼訊號。

    加權方式：
      外資現貨流向  (institutional_score)  40%
      外資持股趨勢  (shareholding_score)   25%
      外資期貨部位  (futures_signal_score) 20%
      融資壓力     (margin_pressure_score) 15%

    Signal 閾值：
      加權分 > 0.20 → BULLISH
      加權分 < -0.20 → BEARISH
      else → NEUTRAL
    """
    inst = scores.get("institutional_score", 0.0)
    share = scores.get("shareholding_score", 0.0)
    fut = scores.get("futures_signal_score", 0.0)
    margin = scores.get("margin_pressure_score", 0.0)

    weighted = 0.40 * inst + 0.25 * share + 0.20 * fut + 0.15 * margin

    if weighted > 0.20:
        signal = Signal.BULLISH
    elif weighted < -0.20:
        signal = Signal.BEARISH
    else:
        signal = Signal.NEUTRAL

    # 信心：與分數絕對值正相關，加上資料覆蓋率影響
    confidence = max(0.15, min(0.90, 0.35 + abs(weighted) * 0.8))

    return signal, round(confidence, 4)


# ─── LLM 摘要層 ──────────────────────────────────────────────────────────────


@observe(name="chip_agent:llm_synthesize", as_type="generation")  # type: ignore[misc]
def _llm_synthesize_chip(
    symbol: str,
    signal: Signal,
    confidence: float,
    key_findings: dict[str, Any],
) -> str:
    """
    呼叫 OpenAI GPT-4o-mini 生成白話籌碼分析摘要。

    LLM 只負責：
      - 組織已計算好的數值成白話說明
      - 解讀各籌碼指標間的組合意涵（如：外資買但融資也增，如何看？）

    LLM 不可以：
      - 發明新數字
      - 改變 signal 結論
      - 忽略 key_findings 中的任何關鍵數值

    失敗時 fallback 至確定性模板（不影響 signal 輸出）。
    """
    try:
        from openai import OpenAI

        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

        # 組織 key_findings 為可讀格式
        findings_text = "\n".join(
            f"  - {k}: {v}" for k, v in key_findings.items()
            if isinstance(v, (int, float))
        )

        signal_label = {
            Signal.BULLISH: "偏多（看漲）",
            Signal.BEARISH: "偏空（看跌）",
            Signal.NEUTRAL: "中性",
        }[signal]

        prompt = f"""你是台股籌碼分析師。以下是 {symbol} 的籌碼面數據（已由確定性工具計算完成）：

{findings_text}

系統判定的籌碼訊號：{signal_label}（信心：{confidence:.0%}）

請用 3-5 句繁體中文摘要：
1. 目前籌碼面的主要特徵
2. 各指標（法人流向、融資槓桿、持股比例）之間是否互相印證或矛盾
3. 這個籌碼態勢對後市的含義

嚴格限制：
- 只能使用上方提供的數字，不能發明新數字
- 不要重複說「系統判定為偏多/偏空」
- 直接進入分析，不要廢話
"""

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.2,
        )
        content = resp.choices[0].message.content or ""
        return content.strip()

    except Exception as exc:  # noqa: BLE001
        # LLM 失敗 → 確定性 fallback，不影響 signal 輸出
        return _fallback_narrative(signal, key_findings, str(exc))


def _fallback_narrative(
    signal: Signal,
    key_findings: dict[str, Any],
    error: str = "",
) -> str:
    """確定性 fallback 摘要，當 LLM 呼叫失敗時使用。"""
    parts: list[str] = []

    consecutive = key_findings.get("consecutive_days", 0)
    if consecutive >= 3:
        parts.append(f"外資連續買超 {int(consecutive)} 日")
    elif consecutive <= -3:
        parts.append(f"外資連續賣超 {int(abs(consecutive))} 日")

    change = key_findings.get("ownership_change_30d", 0.0)
    if abs(change) > 0.1:
        direction = "上升" if change > 0 else "下降"
        parts.append(f"外資持股比例近月{direction}")

    margin_change = key_findings.get("margin_change_5d", 0.0)
    if margin_change > 0:
        parts.append("融資近 5 日增加，槓桿偏高")
    elif margin_change < 0:
        parts.append("融資近 5 日減少，籌碼較輕")

    signal_label = {
        Signal.BULLISH: "籌碼面偏多",
        Signal.BEARISH: "籌碼面偏空",
        Signal.NEUTRAL: "籌碼面中性",
    }[signal]
    parts.append(signal_label)

    base = "，".join(parts) + "。"
    if error:
        base += f" [LLM 摘要降級，原因：{error[:80]}]"
    return base


# ─── LangGraph Node Functions ─────────────────────────────────────────────────


@observe(name="chip_agent:node_fetch_institutional")  # type: ignore[misc]
def _node_fetch_institutional(state: ChipAgentState) -> ChipAgentState:
    """拉三大法人買賣超資料。"""
    errors = list(state["pipeline_errors"])
    adapter = state["adapter"] or ChipDataAdapter()

    try:
        sourced = adapter.fetch_institutional(state["symbol"], days=10)
        inst: InstitutionalResult = sourced.payload
        update_current_span(output={
            "records": len(inst.records),
            "foreign_net": inst.foreign_net_shares,
            "consecutive": inst.consecutive_foreign_buy,
        })
        return {**state, "institutional": inst}
    except Exception as exc:
        errors.append(f"institutional fetch failed: {exc}")
        update_current_span(output={"error": str(exc)})
        return {**state, "institutional": None, "pipeline_errors": errors}


@observe(name="chip_agent:node_fetch_margin")  # type: ignore[misc]
def _node_fetch_margin(state: ChipAgentState) -> ChipAgentState:
    """拉融資融券資料。"""
    errors = list(state["pipeline_errors"])
    adapter = state["adapter"] or ChipDataAdapter()

    try:
        sourced = adapter.fetch_margin(state["symbol"], days=10)
        margin: MarginResult = sourced.payload
        update_current_span(output={
            "records": len(margin.records),
            "margin_balance": margin.margin_balance,
        })
        return {**state, "margin": margin}
    except Exception as exc:
        errors.append(f"margin fetch failed: {exc}")
        return {**state, "margin": None, "pipeline_errors": errors}


@observe(name="chip_agent:node_fetch_shareholding")  # type: ignore[misc]
def _node_fetch_shareholding(state: ChipAgentState) -> ChipAgentState:
    """拉外資持股比例。"""
    errors = list(state["pipeline_errors"])
    adapter = state["adapter"] or ChipDataAdapter()

    try:
        sourced = adapter.fetch_shareholding(state["symbol"], days=60)
        shareholding: ShareholdingResult = sourced.payload
        update_current_span(output={"latest_ratio": shareholding.latest_ratio})
        return {**state, "shareholding": shareholding}
    except Exception as exc:
        errors.append(f"shareholding fetch failed: {exc}")
        return {**state, "shareholding": None, "pipeline_errors": errors}


@observe(name="chip_agent:node_fetch_futures")  # type: ignore[misc]
def _node_fetch_futures(state: ChipAgentState) -> ChipAgentState:
    """拉期貨三大法人部位。"""
    errors = list(state["pipeline_errors"])
    adapter = state["adapter"] or ChipDataAdapter()

    try:
        sourced = adapter.fetch_futures_inst("TXF")
        futures: FuturesInstResult = sourced.payload
        update_current_span(output={"foreign_net": futures.foreign_net_position})
        return {**state, "futures_inst": futures}
    except Exception as exc:
        errors.append(f"futures institutional fetch failed: {exc}")
        return {**state, "futures_inst": None, "pipeline_errors": errors}


@observe(name="chip_agent:node_compute")  # type: ignore[misc]
def _node_compute(state: ChipAgentState) -> ChipAgentState:
    """確定性計算：將各資料集轉換為標準化分數。"""
    scores: dict[str, float] = {}
    key_findings: dict[str, Any] = {}
    steps = list(state["reasoning_steps"])

    # 三大法人
    if state["institutional"] is not None:
        inst_scores = _compute_institutional_score(state["institutional"])
        scores.update(inst_scores)
        key_findings.update(inst_scores)
        steps.append(ReasoningStep(
            thought="計算三大法人方向性分數，外資動向是最重要的先行指標。",
            action="_compute_institutional_score",
            action_input={"consecutive_days": state["institutional"].consecutive_foreign_buy},
            observation=f"institutional_score={inst_scores['institutional_score']:.3f}, "
                        f"連續天數={inst_scores['consecutive_days']:.0f}",
        ))

    # 融資融券
    if state["margin"] is not None:
        margin_scores = _compute_margin_pressure(state["margin"])
        scores.update(margin_scores)
        key_findings.update(margin_scores)
        steps.append(ReasoningStep(
            thought="計算融資融券壓力：融資增加代表追高槓桿，需注意後續清洗壓力。",
            action="_compute_margin_pressure",
            action_input={"margin_change_5d": state["margin"].margin_change_5d},
            observation=f"margin_pressure_score={margin_scores['margin_pressure_score']:.3f}",
        ))

    # 外資持股
    if state["shareholding"] is not None:
        share_scores = _compute_shareholding_signal(state["shareholding"])
        scores.update(share_scores)
        key_findings.update(share_scores)
        steps.append(ReasoningStep(
            thought="外資持股比例變化反映長線資金流向，比短線買賣超更穩定。",
            action="_compute_shareholding_signal",
            action_input={"change_30d": state["shareholding"].change_30d},
            observation=f"shareholding_score={share_scores['shareholding_score']:.3f}, "
                        f"ratio={share_scores['foreign_ownership_ratio']:.2f}%",
        ))

    # 期貨部位
    if state["futures_inst"] is not None:
        fut_scores = _compute_futures_signal(state["futures_inst"])
        scores.update(fut_scores)
        key_findings.update(fut_scores)
        steps.append(ReasoningStep(
            thought="期貨法人部位反映大資金對後市方向的對賭，淨多/空方向具有前瞻性。",
            action="_compute_futures_signal",
            action_input={"foreign_net_position": state["futures_inst"].foreign_net_position},
            observation=f"futures_signal_score={fut_scores['futures_signal_score']:.3f}",
        ))

    update_current_span(output={"scores": scores, "n_dimensions": len(scores)})
    return {**state, "scores": scores, "key_findings": key_findings, "reasoning_steps": steps}


@observe(name="chip_agent:node_signal")  # type: ignore[misc]
def _node_signal(state: ChipAgentState) -> ChipAgentState:
    """整合分數 → 最終 DomainReport。"""
    scores = state["scores"]
    key_findings = state["key_findings"]
    errors = list(state["pipeline_errors"])
    steps = list(state["reasoning_steps"])
    asof = state["asof"]
    symbol = state["symbol"]
    market = state["market"]

    if not scores:
        # 完全無資料 → fallback
        report = _error_report(symbol, market, asof, errors + ["no data from any chip source"])
        return {**state, "report": report}

    signal, confidence = _determine_chip_signal(scores)

    # 資料完整度（有幾個維度就幾分）
    n_dims = len([k for k in ["institutional_score", "shareholding_score",
                               "futures_signal_score", "margin_pressure_score"]
                  if k in scores])
    data_completeness = n_dims / 4.0

    # LLM 摘要
    narrative = _llm_synthesize_chip(symbol, signal, confidence, key_findings)

    steps.append(ReasoningStep(
        thought="整合所有籌碼維度的分數，做出最終方向性判斷。",
        action="_determine_chip_signal",
        action_input={"scores": scores},
        observation=f"signal={signal.value}, confidence={confidence:.2f}",
    ))

    report = DomainReport(
        agent=AgentType.CHIP,
        symbol=symbol,
        market=market,
        asof=asof,
        signal=signal,
        confidence=confidence,
        time_horizon=TimeHorizon.SHORT,   # 籌碼面屬短中期訊號
        hard_constraints=[],
        reasoning_steps=steps,
        key_findings=key_findings,
        narrative_summary=narrative,
        data_completeness=data_completeness,
        errors=errors,
    )

    update_current_span(output={"signal": signal.value, "confidence": confidence})
    return {**state, "report": report}


# ─── Error fallback ───────────────────────────────────────────────────────────


def _error_report(
    symbol: str,
    market: str,
    asof: datetime,
    errors: list[str],
) -> DomainReport:
    """完全失敗時的 fallback DomainReport（confidence=0）。"""
    return DomainReport(
        agent=AgentType.CHIP,
        symbol=symbol,
        market=market,
        asof=asof,
        signal=Signal.NEUTRAL,
        confidence=0.0,
        time_horizon=TimeHorizon.SHORT,
        narrative_summary="籌碼資料擷取失敗，無法產出籌碼面分析。",
        data_completeness=0.0,
        errors=errors,
    )


# ─── Full pipeline（convenience wrapper）──────────────────────────────────────


@observe(name="chip_agent:run")  # type: ignore[misc]
def run_chip_agent(
    *,
    symbol: str,
    market: str = "TW",
    adapter: Optional[ChipDataAdapter] = None,
    asof: Optional[datetime] = None,
) -> DomainReport:
    """
    執行完整的籌碼面分析管線，回傳 DomainReport。

    Parameters
    ----------
    symbol  : 台股代號（e.g. "2330"，無需 .TW 後綴）
    market  : 市場代碼（目前只支援 "TW"）
    adapter : ChipDataAdapter 實例，None = 自動建立（讀 FINMIND_KEY 環境變數）
    asof    : 時間戳，None = UTC 現在
    """
    eff_asof = asof if asof is not None else datetime.now(tz=UTC)
    update_current_span(input={"symbol": symbol, "market": market})

    state: ChipAgentState = {
        "symbol": symbol,
        "market": market,
        "adapter": adapter,
        "asof": eff_asof,
        "institutional": None,
        "margin": None,
        "shareholding": None,
        "futures_inst": None,
        "scores": {},
        "key_findings": {},
        "reasoning_steps": [],
        "pipeline_errors": [],
        "report": None,
    }

    # 逐步執行 node（順序：先拉資料，再計算，再輸出）
    state = _node_fetch_institutional(state)
    state = _node_fetch_margin(state)
    state = _node_fetch_shareholding(state)
    state = _node_fetch_futures(state)
    state = _node_compute(state)
    state = _node_signal(state)

    result = state["report"] or _error_report(
        symbol=symbol,
        market=market,
        asof=eff_asof,
        errors=state["pipeline_errors"],
    )
    update_current_span(output={"signal": result.signal.value, "confidence": result.confidence})
    return result


# ─── LangGraph graph ──────────────────────────────────────────────────────────


def create_chip_graph() -> Any:
    """
    Build and compile the chip agent as a LangGraph CompiledGraph.

    Usage in Supervisor
    -------------------
        from agents.chip_agent import create_chip_graph
        chip_graph = create_chip_graph()
        result = chip_graph.invoke(initial_state)
        report: DomainReport = result["report"]
    """
    builder: StateGraph = StateGraph(ChipAgentState)

    builder.add_node("fetch_institutional", _node_fetch_institutional)
    builder.add_node("fetch_margin",        _node_fetch_margin)
    builder.add_node("fetch_shareholding",  _node_fetch_shareholding)
    builder.add_node("fetch_futures",       _node_fetch_futures)
    builder.add_node("compute",             _node_compute)
    builder.add_node("signal",              _node_signal)

    builder.set_entry_point("fetch_institutional")
    builder.add_edge("fetch_institutional", "fetch_margin")
    builder.add_edge("fetch_margin",        "fetch_shareholding")
    builder.add_edge("fetch_shareholding",  "fetch_futures")
    builder.add_edge("fetch_futures",       "compute")
    builder.add_edge("compute",             "signal")
    builder.add_edge("signal",              END)

    return builder.compile()
