"""
Multi-agent Debate — Bull / Bear / PM 三角辯論架構

架構
----
  DomainReport (x N)
      │
      ├──► BullAgent (async)   ─────────────────────────────┐
      │    只找看多論據                                       │
      │                                                       ├──► PMAgent (async)
      └──► BearAgent (async)   ─────────────────────────────┘
           只找看空論據             asyncio.gather()          做最終裁決

Bull + Bear 並行執行（asyncio.gather），PM 等兩者完成後執行。

設計原則
--------
1. 只能使用 DomainReport.key_findings 裡已有的數字，不可發明新數字
2. LLM 只產出白話論述（narrative），不參與規則引擎決策
3. 任何 LLM 失敗都 fallback 至確定性邏輯（signal 投票 + key_findings 摘取）
4. 所有 LLM 呼叫都用 @observe 裝飾（Langfuse 可觀測性）

使用方式
--------
    import asyncio
    from agents.debate_agents import run_debate

    debate = asyncio.run(run_debate(reports=domain_reports, symbol="2330"))
    print(debate.bull.thesis)
    print(debate.bear.thesis)
    print(debate.pm_verdict.thesis)
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from observability.langfuse_setup import observe, update_current_span
from schemas.agent_signal import Signal
from schemas.debate import DebateOutput, DebateParty
from schemas.domain_report import DomainReport


# ─── System Prompts ───────────────────────────────────────────────────────────

_BULL_SYSTEM_PROMPT = """你是 QuantDesk 的多方辯護人（Bull Analyst）。

你的工作：
1. 從以下 domain reports 中找出最有力的看多論據
2. 組織成有說服力的投資論述
3. 明確點出 2-4 個最強的多方論點

嚴格限制：
- 所有數字必須來自 domain reports 的 key_findings，不能發明或推算新數字
- 你的角色是找多方論據，不是全面評估（空方觀點由另一位分析師負責）
- confidence 反映你對這份多方論述的強度信心

輸出格式（JSON）：
```json
{
  "thesis": "多方完整論述（2-4句白話）",
  "key_points": ["論點1", "論點2", "論點3"],
  "confidence": 0.72
}
```
只輸出 JSON，不要其他文字。"""

_BEAR_SYSTEM_PROMPT = """你是 QuantDesk 的空方辯護人（Bear Analyst）。

你的工作：
1. 從以下 domain reports 中找出最重要的看空論據與風險
2. 組織成有說服力的風險論述
3. 明確點出 2-4 個最強的空方論點或風險警示

嚴格限制：
- 所有數字必須來自 domain reports 的 key_findings，不能發明或推算新數字
- 你的角色是找空方論據與風險，不是全面評估（多方觀點由另一位分析師負責）
- confidence 反映你對這份空方論述的強度信心

輸出格式（JSON）：
```json
{
  "thesis": "空方完整論述（2-4句白話）",
  "key_points": ["風險1", "風險2", "風險3"],
  "confidence": 0.65
}
```
只輸出 JSON，不要其他文字。"""

_PM_SYSTEM_PROMPT = """你是 QuantDesk 的投資組合經理（Portfolio Manager）。

你剛才聽完了多方分析師和空方分析師的論述。現在你需要做最終裁決。

你的工作：
1. 綜合評估多方與空方的論述強度
2. 給出最終方向建議（bullish / bearish / neutral）
3. 解釋你的裁決邏輯
4. 給出 2-3 個執行建議

評估標準：
- 哪方的論據更具體、有數據支撐？
- 哪方的風險更不對稱？
- 現在的市場環境對哪方更有利？

嚴格限制：
- 所有引用的數字必須來自已提供的論述，不能發明新數字
- 你的 confidence 反映你對這個裁決的把握程度

輸出格式（JSON）：
```json
{
  "signal": "bullish",
  "thesis": "PM 裁決完整論述（3-5句白話）",
  "key_points": ["執行建議1", "執行建議2", "執行建議3"],
  "confidence": 0.68
}
```
signal 只能是 "bullish" / "bearish" / "neutral"。
只輸出 JSON，不要其他文字。"""


# ─── Context Builder ──────────────────────────────────────────────────────────


def _build_reports_context(reports: list[DomainReport], symbol: str) -> str:
    """把 DomainReport 列表轉為辯論 LLM 可讀的格式（只傳 key_findings，節省 tokens）。"""
    lines: list[str] = [
        f"## 分析標的：{symbol}",
        "",
        "## Domain Reports 摘要",
        "",
    ]
    for report in reports:
        signal_label = {
            Signal.BULLISH: "偏多 ↑",
            Signal.BEARISH: "偏空 ↓",
            Signal.NEUTRAL: "中性 →",
        }.get(report.signal, "未知")

        lines.append(f"### {report.agent.value} Agent  [{signal_label}  信心={report.confidence:.0%}]")
        if report.key_findings:
            for k, v in report.key_findings.items():
                lines.append(f"  {k}: {v}")
        if report.narrative_summary:
            lines.append(f"  摘要：{report.narrative_summary[:120]}")
        lines.append("")
    return "\n".join(lines)


def _build_pm_context(
    reports_context: str,
    bull: DebateParty,
    bear: DebateParty,
) -> str:
    """給 PM 的完整 context：原始資料 + 多方論述 + 空方論述。"""
    return (
        f"{reports_context}\n"
        f"---\n"
        f"## 多方論述（Bull Analyst）\n{bull.thesis}\n"
        f"### 多方主論點\n"
        + "\n".join(f"- {p}" for p in bull.key_points)
        + f"\n\n## 空方論述（Bear Analyst）\n{bear.thesis}\n"
        f"### 空方主論點\n"
        + "\n".join(f"- {p}" for p in bear.key_points)
    )


# ─── LLM Callers（async）─────────────────────────────────────────────────────


@observe(name="debate:bull_llm_call", as_type="generation")  # type: ignore[misc]
async def _call_bull_llm(context: str) -> dict[str, Any]:
    """非同步呼叫 GPT-4o 產出多方論述。"""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _BULL_SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
        response_format={"type": "json_object"},
        max_tokens=500,
        temperature=0.3,
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content)  # type: ignore[no-any-return]


@observe(name="debate:bear_llm_call", as_type="generation")  # type: ignore[misc]
async def _call_bear_llm(context: str) -> dict[str, Any]:
    """非同步呼叫 GPT-4o 產出空方論述。"""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _BEAR_SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
        response_format={"type": "json_object"},
        max_tokens=500,
        temperature=0.3,
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content)  # type: ignore[no-any-return]


@observe(name="debate:pm_llm_call", as_type="generation")  # type: ignore[misc]
async def _call_pm_llm(context: str) -> dict[str, Any]:
    """非同步呼叫 GPT-4o 做 PM 最終裁決。"""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _PM_SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
        response_format={"type": "json_object"},
        max_tokens=600,
        temperature=0.2,
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content)  # type: ignore[no-any-return]


# ─── Deterministic Fallbacks ──────────────────────────────────────────────────


def _fallback_bull(reports: list[DomainReport]) -> DebateParty:
    """確定性多方 fallback：摘取 bullish signal 的 key_findings。"""
    bull_reports = [r for r in reports if r.signal == Signal.BULLISH]
    if not bull_reports:
        return DebateParty(
            role="bull",
            thesis="無可用的看多 domain 訊號。",
            key_points=[],
            confidence=0.20,
        )
    points: list[str] = []
    for r in bull_reports[:3]:
        for k, v in list(r.key_findings.items())[:2]:
            points.append(f"{r.agent.value}: {k}={v}")
    thesis = (
        f"共 {len(bull_reports)} 個 domain 訊號偏多（"
        f"{', '.join(r.agent.value for r in bull_reports)}），"
        f"平均信心 {sum(r.confidence for r in bull_reports)/len(bull_reports):.0%}。"
    )
    return DebateParty(role="bull", thesis=thesis, key_points=points, confidence=0.45)


def _fallback_bear(reports: list[DomainReport]) -> DebateParty:
    """確定性空方 fallback：摘取 bearish signal 的 key_findings。"""
    bear_reports = [r for r in reports if r.signal == Signal.BEARISH]
    hc_breaches = [
        hc.type
        for r in reports
        for hc in r.hard_constraints
        if hc.breached
    ]
    if not bear_reports and not hc_breaches:
        return DebateParty(
            role="bear",
            thesis="無可用的看空 domain 訊號或硬約束觸限。",
            key_points=[],
            confidence=0.20,
        )
    points: list[str] = []
    for r in bear_reports[:3]:
        for k, v in list(r.key_findings.items())[:2]:
            points.append(f"{r.agent.value}: {k}={v}")
    for hc_type in hc_breaches[:2]:
        points.append(f"硬約束觸限: {hc_type}")
    thesis = (
        f"共 {len(bear_reports)} 個 domain 訊號偏空"
        + (f"，{len(hc_breaches)} 個硬約束觸限" if hc_breaches else "")
        + "。"
    )
    return DebateParty(
        role="bear",
        thesis=thesis,
        key_points=points,
        confidence=0.45 + 0.15 * bool(hc_breaches),
    )


def _fallback_pm(
    reports: list[DomainReport],
    bull: DebateParty,
    bear: DebateParty,
) -> tuple[DebateParty, Signal]:
    """確定性 PM fallback：比較 bull / bear confidence，多者勝。"""
    if bull.confidence > bear.confidence + 0.05:
        signal = Signal.BULLISH
        verdict_text = f"多方論述（信心 {bull.confidence:.0%}）強於空方（{bear.confidence:.0%}），PM 傾向看多。"
    elif bear.confidence > bull.confidence + 0.05:
        signal = Signal.BEARISH
        verdict_text = f"空方論述（信心 {bear.confidence:.0%}）強於多方（{bull.confidence:.0%}），PM 傾向看空。"
    else:
        signal = Signal.NEUTRAL
        verdict_text = f"多空論述信心相當（多方 {bull.confidence:.0%} vs 空方 {bear.confidence:.0%}），PM 維持中性。"

    pm_conf = max(bull.confidence, bear.confidence) * 0.85  # PM 比單方降 15%
    return (
        DebateParty(
            role="pm",
            thesis=verdict_text,
            key_points=["確定性 fallback：多空信心比較", f"多方信心={bull.confidence:.0%}", f"空方信心={bear.confidence:.0%}"],
            confidence=pm_conf,
        ),
        signal,
    )


# ─── Per-Agent Async Runners ──────────────────────────────────────────────────


@observe(name="debate:run_bull_agent")  # type: ignore[misc]
async def _run_bull_agent(reports: list[DomainReport], context: str) -> tuple[DebateParty, bool]:
    """Returns (party, used_llm)."""
    try:
        raw = await _call_bull_llm(context)
        return DebateParty(
            role="bull",
            thesis=str(raw.get("thesis", "")),
            key_points=list(raw.get("key_points", [])),
            confidence=float(raw.get("confidence", 0.5)),
        ), True
    except Exception:  # noqa: BLE001
        return _fallback_bull(reports), False


@observe(name="debate:run_bear_agent")  # type: ignore[misc]
async def _run_bear_agent(reports: list[DomainReport], context: str) -> tuple[DebateParty, bool]:
    """Returns (party, used_llm)."""
    try:
        raw = await _call_bear_llm(context)
        return DebateParty(
            role="bear",
            thesis=str(raw.get("thesis", "")),
            key_points=list(raw.get("key_points", [])),
            confidence=float(raw.get("confidence", 0.5)),
        ), True
    except Exception:  # noqa: BLE001
        return _fallback_bear(reports), False


@observe(name="debate:run_pm_agent")  # type: ignore[misc]
async def _run_pm_agent(
    reports: list[DomainReport],
    bull: DebateParty,
    bear: DebateParty,
    reports_context: str,
) -> tuple[DebateParty, Signal, bool]:
    """Returns (pm_party, signal, used_llm)."""
    try:
        pm_context = _build_pm_context(reports_context, bull, bear)
        raw = await _call_pm_llm(pm_context)
        signal_raw = str(raw.get("signal", "neutral")).lower()
        signal = Signal.BULLISH if signal_raw == "bullish" else (
            Signal.BEARISH if signal_raw == "bearish" else Signal.NEUTRAL
        )
        pm = DebateParty(
            role="pm",
            thesis=str(raw.get("thesis", "")),
            key_points=list(raw.get("key_points", [])),
            confidence=float(raw.get("confidence", 0.5)),
        )
        return pm, signal, True
    except Exception:  # noqa: BLE001
        pm, signal = _fallback_pm(reports, bull, bear)
        return pm, signal, False


# ─── Public API ───────────────────────────────────────────────────────────────


@observe(name="debate:run_debate")  # type: ignore[misc]
async def run_debate(
    reports: list[DomainReport],
    symbol: str = "",
    scenario: str = "single_stock",
) -> DebateOutput:
    """
    主入口：執行 Bull / Bear / PM 三角辯論。

    Bull + Bear 並行執行（asyncio.gather），PM 等兩者完成後執行。
    任何 LLM 失敗都 fallback 至確定性邏輯，保證輸出不為 None。

    Parameters
    ----------
    reports  : list[DomainReport]  — domain agents 的輸出
    symbol   : str                 — 分析標的（用於 context 組織）
    scenario : str                 — 場景類型

    Returns
    -------
    DebateOutput
    """
    update_current_span(input={
        "n_reports": len(reports),
        "symbol": symbol,
        "scenario": scenario,
    })

    if not reports:
        empty_bull = DebateParty(role="bull", thesis="無 domain reports 可供辯論。", confidence=0.10)
        empty_bear = DebateParty(role="bear", thesis="無 domain reports 可供辯論。", confidence=0.10)
        empty_pm = DebateParty(role="pm", thesis="無資料，無法裁決。", confidence=0.10)
        return DebateOutput(
            symbol=symbol,
            scenario=scenario,
            bull=empty_bull,
            bear=empty_bear,
            pm_verdict=empty_pm,
            final_signal=Signal.NEUTRAL,
            final_confidence=0.10,
            method="fallback",
        )

    context = _build_reports_context(reports, symbol)

    # Bull + Bear 並行執行
    (bull, bull_llm), (bear, bear_llm) = await asyncio.gather(
        _run_bull_agent(reports, context),
        _run_bear_agent(reports, context),
    )

    # PM 等兩者完成後執行
    pm, final_signal, pm_llm = await _run_pm_agent(reports, bull, bear, context)

    method = "llm" if any([bull_llm, bear_llm, pm_llm]) else "fallback"

    output = DebateOutput(
        symbol=symbol,
        scenario=scenario,
        bull=bull,
        bear=bear,
        pm_verdict=pm,
        final_signal=final_signal,
        final_confidence=pm.confidence,
        method=method,
    )

    update_current_span(output={
        "final_signal": final_signal.value,
        "pm_confidence": pm.confidence,
        "bull_confidence": bull.confidence,
        "bear_confidence": bear.confidence,
    })

    return output
