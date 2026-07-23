"""
Synthesis LLM — 多 Domain Report 整合仲裁

職責
----
讀取所有 DomainReport，用 GPT-4o 做跨 domain 推理仲裁，
輸出最終的投研結論（signal + confidence + narrative）。

重要邊界：LLM 仲裁 vs 規則引擎
-------------------------------
LLM（此模組）負責：
  - 找出各 domain 報告的共識與矛盾
  - 解讀矛盾背後的意涵（如：技術偏多 但籌碼外資賣超 → 如何看？）
  - 輸出最終建議的 signal 和 confidence
  - 生成白話投研摘要

規則引擎（supervisor/graph.py）負責（不可由 LLM 影響）：
  - hard_constraint breach → 強制 risk_override
  - confidence 低於門檻 → 觸發 HITL Gate
  - EWS critical → 強制 requires_human_review

Langfuse 可觀測性
-----------------
所有 LLM 呼叫都用 @observe 裝飾器包裝，
完整的 reasoning 過程在 Langfuse trace 中可見。

使用方式
--------
    from supervisor.synthesis import synthesize_reports, SynthesisOutput

    output = synthesize_reports(
        reports=domain_reports,     # list[DomainReport]
        symbol="2330",
        scenario="single_stock",
    )
    print(output.signal, output.confidence)
    print(output.narrative)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from observability.langfuse_setup import observe, update_current_span
from schemas.agent_signal import Signal
from schemas.domain_report import DomainReport


# ─── Synthesis Output ─────────────────────────────────────────────────────────


@dataclass
class SynthesisOutput:
    """
    Synthesis LLM 的輸出結構。

    由 supervisor/graph.py 接收後：
    1. signal + confidence 傳入現有 SupervisorOutput 邏輯
    2. narrative 替換 overall_narrative
    3. 規則引擎（risk_override / HITL）繼續跑在 confidence 上
    """
    signal: Signal
    confidence: float                # 0-1，LLM 對自身判斷的信心
    narrative: str                   # 跨 domain 整合白話摘要
    key_drivers: list[str]           # 最重要的 1-3 個根據
    key_risks: list[str]             # 最主要的風險
    domain_consensus: dict[str, str] # {domain_name: "bullish/bearish/neutral"}
    conflicts: list[str]             # 偵測到的矛盾訊號描述
    method: str = "llm"              # "llm" | "fallback"
    error: str = ""


# ─── System Prompt ────────────────────────────────────────────────────────────

_SYNTHESIS_SYSTEM_PROMPT = """你是 QuantDesk 的首席分析師。你已收到多個 domain 分析師的報告。

你的任務是：
1. 整合所有報告，找出共識與矛盾
2. 解釋矛盾的意涵（例：技術偏多但籌碼外資在賣，代表什麼？）
3. 給出最終方向建議和信心水準
4. 點出最重要的 1-3 個根據
5. 點出最主要的 1-2 個風險

嚴格限制：
- 所有數字都必須來自各報告提供的 key_findings，不能發明新數字
- 風控硬約束已由規則引擎處理，你不需要也不能覆蓋它
- confidence 應反映你對這份整合分析的信心，不是任何單一指標的信心

輸出格式（JSON）：
```json
{
  "signal": "bullish",
  "confidence": 0.72,
  "narrative": "...",
  "key_drivers": ["外資連5日買超", "費半強勢", "月營收年增18%"],
  "key_risks": ["RSI進入超買區（76）", "融資近期增加"],
  "domain_consensus": {
    "technical": "bullish",
    "chip": "bullish",
    "fundamental": "neutral",
    "news": "neutral",
    "macro": "neutral",
    "cross_market": "bullish"
  },
  "conflicts": ["技術偏多但融資追高值得警惕"]
}
```

signal 只能是 "bullish" / "bearish" / "neutral"。
confidence 範圍 0.0-1.0。
"""


# ─── Synthesis Function ───────────────────────────────────────────────────────


def _build_reports_context(reports: list[DomainReport], symbol: str, scenario: str) -> str:
    """
    把所有 DomainReport 整理成 LLM 可讀的文字格式。

    只傳遞結構化發現（key_findings + narrative_summary + signal），
    不傳原始資料 payload（節省 tokens）。
    """
    lines: list[str] = [
        f"## 分析標的：{symbol}",
        f"## 分析場景：{scenario}",
        "",
        "## 各 Domain 報告",
        "",
    ]

    for report in reports:
        agent_name = report.agent.value
        signal_label = {
            Signal.BULLISH: "偏多 ↑",
            Signal.BEARISH: "偏空 ↓",
            Signal.NEUTRAL: "中性 →",
        }.get(report.signal, "未知")

        lines.append(f"### {agent_name} Agent")
        lines.append(f"訊號：{signal_label}  信心：{report.confidence:.0%}  時間框架：{report.time_horizon.value}")

        if report.key_findings:
            lines.append("關鍵數據：")
            for k, v in report.key_findings.items():
                lines.append(f"  - {k}: {v}")

        if report.narrative_summary:
            lines.append(f"摘要：{report.narrative_summary}")

        if report.errors:
            lines.append(f"⚠️ 資料問題：{'; '.join(report.errors[:3])}")

        lines.append("")

    return "\n".join(lines)


@observe(name="synthesis:llm_call", as_type="llm")  # type: ignore[misc]
def _call_synthesis_llm(context: str) -> dict[str, Any]:
    """
    呼叫 GPT-4o 做跨 domain 仲裁推理。
    失敗時 raise，由 synthesize_reports() 捕捉並使用 fallback。
    """
    from openai import OpenAI

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _SYNTHESIS_SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
        response_format={"type": "json_object"},
        max_tokens=800,
        temperature=0.2,
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content)  # type: ignore[no-any-return]


def _deterministic_fallback(reports: list[DomainReport]) -> SynthesisOutput:
    """
    無 LLM 的確定性 fallback：加權投票。
    當 GPT-4o 呼叫失敗時使用，保證系統可用性。
    """
    if not reports:
        return SynthesisOutput(
            signal=Signal.NEUTRAL,
            confidence=0.10,
            narrative="無可用的 domain 報告，無法產出分析結論。",
            key_drivers=[],
            key_risks=["所有 domain 資料均不可用"],
            domain_consensus={},
            conflicts=[],
            method="fallback",
        )

    # 加權投票：signal 轉換為 +1/0/-1，再做加權平均
    bull_score = 0.0
    total_weight = 0.0

    for r in reports:
        w = r.confidence * r.data_completeness
        if r.signal == Signal.BULLISH:
            bull_score += w
        elif r.signal == Signal.BEARISH:
            bull_score -= w
        total_weight += w

    avg = (bull_score / total_weight) if total_weight > 0 else 0.0

    if avg > 0.15:
        signal = Signal.BULLISH
    elif avg < -0.15:
        signal = Signal.BEARISH
    else:
        signal = Signal.NEUTRAL

    confidence = max(0.20, min(0.80, abs(avg) * 0.8 + 0.20))

    domain_consensus = {r.agent.value: r.signal.value for r in reports}

    # 找矛盾訊號
    signals_set = {r.signal for r in reports if r.confidence > 0.3}
    conflicts: list[str] = []
    if Signal.BULLISH in signals_set and Signal.BEARISH in signals_set:
        bull_agents = [r.agent.value for r in reports if r.signal == Signal.BULLISH]
        bear_agents = [r.agent.value for r in reports if r.signal == Signal.BEARISH]
        conflicts.append(f"{', '.join(bull_agents)} 偏多 vs {', '.join(bear_agents)} 偏空，訊號分歧")

    narrative = (
        f"確定性加權投票結論：{signal.value}（信心 {confidence:.0%}）。"
        f"共有 {len(reports)} 個 domain 報告參與仲裁。"
        + (" 訊號存在分歧，建議謹慎。" if conflicts else "")
    )

    return SynthesisOutput(
        signal=signal,
        confidence=confidence,
        narrative=narrative,
        key_drivers=[],
        key_risks=conflicts or [],
        domain_consensus=domain_consensus,
        conflicts=conflicts,
        method="fallback",
    )


@observe(name="synthesis:synthesize_reports")  # type: ignore[misc]
def synthesize_reports(
    reports: list[DomainReport],
    symbol: str = "",
    scenario: str = "single_stock",
) -> SynthesisOutput:
    """
    主入口：整合所有 DomainReport，回傳 SynthesisOutput。

    Parameters
    ----------
    reports  : 所有 domain agent 的輸出（list[DomainReport]）
    symbol   : 分析標的（用於報告組織）
    scenario : 場景類型（"single_stock" / "portfolio_risk" / "multi_stock_scan"）

    Returns
    -------
    SynthesisOutput
    """
    update_current_span(input={
        "n_reports": len(reports),
        "symbol": symbol,
        "scenario": scenario,
    })

    if not reports:
        return _deterministic_fallback([])

    context = _build_reports_context(reports, symbol, scenario)

    try:
        raw = _call_synthesis_llm(context)

        # 解析 signal
        signal_raw = str(raw.get("signal", "neutral")).lower()
        signal = Signal.BULLISH if signal_raw == "bullish" else (
            Signal.BEARISH if signal_raw == "bearish" else Signal.NEUTRAL
        )

        confidence = float(raw.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        output = SynthesisOutput(
            signal=signal,
            confidence=confidence,
            narrative=str(raw.get("narrative", "")),
            key_drivers=list(raw.get("key_drivers", [])),
            key_risks=list(raw.get("key_risks", [])),
            domain_consensus={k: str(v) for k, v in raw.get("domain_consensus", {}).items()},
            conflicts=list(raw.get("conflicts", [])),
            method="llm",
        )

        update_current_span(output={
            "signal": output.signal.value,
            "confidence": output.confidence,
            "method": "llm",
            "n_conflicts": len(output.conflicts),
        })
        return output

    except Exception as exc:  # noqa: BLE001
        output = _deterministic_fallback(reports)
        output.error = str(exc)[:200]
        update_current_span(output={
            "signal": output.signal.value,
            "confidence": output.confidence,
            "method": "fallback",
            "error": output.error,
        })
        return output
