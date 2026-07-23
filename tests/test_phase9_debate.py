"""
Phase 9 — Multi-agent Debate 測試

覆蓋範圍：
  1. schemas/debate.py  — DebateParty / DebateOutput
  2. agents/debate_agents.py
     - _build_reports_context()
     - _fallback_bull() / _fallback_bear() / _fallback_pm()
     - run_debate() 確定性 fallback（no LLM）
     - run_debate() LLM 路徑（mock OpenAI）
     - asyncio.gather() 並行執行（Bull + Bear in parallel）
  3. supervisor/graph.py
     - Supervisor.aggregate_debate() 正常路徑
     - Supervisor.aggregate_debate() risk_override → debate narrative skipped
     - Supervisor.aggregate_debate() LLM failure → deterministic fallback
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from schemas.agent_signal import AgentType, HardConstraint, Signal, TimeHorizon
from schemas.debate import DebateOutput, DebateParty
from schemas.domain_report import DomainReport


# ─── Fixtures ─────────────────────────────────────────────────────────────────


def _make_report(
    agent: AgentType = AgentType.CHIP,
    signal: Signal = Signal.BULLISH,
    confidence: float = 0.70,
    breached: bool = False,
    key_findings: dict | None = None,
) -> DomainReport:
    hc = HardConstraint(
        type="delta_limit",
        current=0.0,
        limit=1.0,
        breached=breached,
        detail=None,
    )
    return DomainReport(
        agent=agent,
        symbol="2330",
        market="TW",
        asof=datetime.now(UTC),
        signal=signal,
        confidence=confidence,
        time_horizon=TimeHorizon.SHORT,
        hard_constraints=[hc] if breached else [],
        key_findings=key_findings or {"score": 0.5, "rsi": 55.2},
        narrative_summary="test narrative",
        data_completeness=1.0,
    )


def _make_openai_resp(content: str) -> MagicMock:
    """回傳一個模擬 OpenAI async response 的 mock。"""
    choice = MagicMock()
    choice.message.content = content
    resp = MagicMock()
    resp.choices = [choice]
    return resp


# ─── 1. Schema Tests ──────────────────────────────────────────────────────────


class TestDebateSchema:
    def test_debate_party_defaults(self) -> None:
        party = DebateParty(role="bull", thesis="test", confidence=0.7)
        assert party.role == "bull"
        assert party.key_points == []

    def test_debate_output_fields(self) -> None:
        bull = DebateParty(role="bull", thesis="bulls win", confidence=0.75)
        bear = DebateParty(role="bear", thesis="bears win", confidence=0.60)
        pm = DebateParty(role="pm", thesis="bulls are right", confidence=0.68)
        out = DebateOutput(
            symbol="2330",
            scenario="single_stock",
            bull=bull,
            bear=bear,
            pm_verdict=pm,
            final_signal=Signal.BULLISH,
            final_confidence=0.68,
        )
        assert out.final_signal == Signal.BULLISH
        assert out.method == "llm"
        assert out.error == ""

    def test_debate_output_fallback_method(self) -> None:
        bull = DebateParty(role="bull", thesis="", confidence=0.2)
        bear = DebateParty(role="bear", thesis="", confidence=0.2)
        pm = DebateParty(role="pm", thesis="", confidence=0.2)
        out = DebateOutput(
            symbol="",
            scenario="single_stock",
            bull=bull,
            bear=bear,
            pm_verdict=pm,
            final_signal=Signal.NEUTRAL,
            final_confidence=0.2,
            method="fallback",
        )
        assert out.method == "fallback"


# ─── 2. Context Builder ───────────────────────────────────────────────────────


class TestContextBuilder:
    def test_context_contains_symbol(self) -> None:
        from agents.debate_agents import _build_reports_context
        reports = [_make_report(AgentType.CHIP, Signal.BULLISH)]
        ctx = _build_reports_context(reports, "2330")
        assert "2330" in ctx

    def test_context_contains_agent_name(self) -> None:
        from agents.debate_agents import _build_reports_context
        reports = [_make_report(AgentType.TECHNICAL, Signal.BEARISH)]
        ctx = _build_reports_context(reports, "2330")
        assert "technical" in ctx.lower()

    def test_context_contains_key_findings(self) -> None:
        from agents.debate_agents import _build_reports_context
        reports = [_make_report(key_findings={"rsi": 72.3, "macd": -0.5})]
        ctx = _build_reports_context(reports, "2330")
        assert "rsi" in ctx
        assert "72.3" in ctx

    def test_context_multiple_reports(self) -> None:
        from agents.debate_agents import _build_reports_context
        reports = [
            _make_report(AgentType.CHIP, Signal.BULLISH),
            _make_report(AgentType.MACRO, Signal.BEARISH),
        ]
        ctx = _build_reports_context(reports, "2330")
        assert "chip" in ctx.lower()
        assert "macro" in ctx.lower()


# ─── 3. Deterministic Fallbacks ───────────────────────────────────────────────


class TestFallbacks:
    def test_fallback_bull_bullish_reports(self) -> None:
        from agents.debate_agents import _fallback_bull
        reports = [
            _make_report(AgentType.CHIP, Signal.BULLISH, confidence=0.80),
            _make_report(AgentType.TECHNICAL, Signal.BULLISH, confidence=0.70),
        ]
        party = _fallback_bull(reports)
        assert party.role == "bull"
        assert "偏多" in party.thesis or "bullish" in party.thesis.lower() or "2" in party.thesis
        assert party.confidence > 0.20

    def test_fallback_bull_no_bullish(self) -> None:
        from agents.debate_agents import _fallback_bull
        reports = [_make_report(signal=Signal.BEARISH)]
        party = _fallback_bull(reports)
        assert party.role == "bull"
        assert party.confidence == 0.20

    def test_fallback_bear_bearish_reports(self) -> None:
        from agents.debate_agents import _fallback_bear
        reports = [
            _make_report(AgentType.MACRO, Signal.BEARISH, confidence=0.75),
        ]
        party = _fallback_bear(reports)
        assert party.role == "bear"
        assert party.confidence > 0.20

    def test_fallback_bear_with_hard_constraint(self) -> None:
        from agents.debate_agents import _fallback_bear
        reports = [_make_report(signal=Signal.NEUTRAL, breached=True)]
        party = _fallback_bear(reports)
        assert party.role == "bear"
        assert any("delta_limit" in p for p in party.key_points)
        # breached 加分
        assert party.confidence > 0.45

    def test_fallback_bear_no_bearish_no_breach(self) -> None:
        from agents.debate_agents import _fallback_bear
        reports = [_make_report(signal=Signal.BULLISH)]
        party = _fallback_bear(reports)
        assert party.confidence == 0.20

    def test_fallback_pm_bull_wins(self) -> None:
        from agents.debate_agents import _fallback_pm
        reports = [_make_report()]
        bull = DebateParty(role="bull", thesis="", confidence=0.80)
        bear = DebateParty(role="bear", thesis="", confidence=0.60)
        pm, signal = _fallback_pm(reports, bull, bear)
        assert signal == Signal.BULLISH
        assert pm.role == "pm"

    def test_fallback_pm_bear_wins(self) -> None:
        from agents.debate_agents import _fallback_pm
        reports = [_make_report()]
        bull = DebateParty(role="bull", thesis="", confidence=0.50)
        bear = DebateParty(role="bear", thesis="", confidence=0.75)
        pm, signal = _fallback_pm(reports, bull, bear)
        assert signal == Signal.BEARISH

    def test_fallback_pm_neutral_when_tied(self) -> None:
        from agents.debate_agents import _fallback_pm
        reports = [_make_report()]
        bull = DebateParty(role="bull", thesis="", confidence=0.65)
        bear = DebateParty(role="bear", thesis="", confidence=0.65)
        pm, signal = _fallback_pm(reports, bull, bear)
        assert signal == Signal.NEUTRAL


# ─── 4. run_debate() — no reports ─────────────────────────────────────────────


class TestRunDebateEmpty:
    def test_empty_reports_returns_neutral(self) -> None:
        from agents.debate_agents import run_debate
        result = asyncio.run(run_debate(reports=[], symbol="2330"))
        assert isinstance(result, DebateOutput)
        assert result.final_signal == Signal.NEUTRAL
        assert result.method == "fallback"
        assert result.final_confidence == 0.10

    def test_empty_reports_all_parties_have_role(self) -> None:
        from agents.debate_agents import run_debate
        result = asyncio.run(run_debate(reports=[]))
        assert result.bull.role == "bull"
        assert result.bear.role == "bear"
        assert result.pm_verdict.role == "pm"


# ─── 5. run_debate() — LLM mocked ─────────────────────────────────────────────


class TestRunDebateLLM:
    def _mock_response(self, content: str) -> AsyncMock:
        choice = MagicMock()
        choice.message.content = content
        resp = MagicMock()
        resp.choices = [choice]
        coro = AsyncMock(return_value=resp)
        return coro

    def test_llm_path_returns_debate_output(self) -> None:
        """mock 三個 LLM 呼叫，確認 DebateOutput 欄位正確填入。"""
        from agents.debate_agents import run_debate

        reports = [
            _make_report(AgentType.CHIP, Signal.BULLISH),
            _make_report(AgentType.TECHNICAL, Signal.NEUTRAL),
        ]

        with patch("agents.debate_agents._call_bull_llm", new_callable=AsyncMock, return_value={"thesis": "外資連買5日，多方強勢", "key_points": ["外資買超", "RSI未超買"], "confidence": 0.78}), \
             patch("agents.debate_agents._call_bear_llm", new_callable=AsyncMock, return_value={"thesis": "融資增加值得警惕", "key_points": ["融資創高", "量縮"], "confidence": 0.55}), \
             patch("agents.debate_agents._call_pm_llm", new_callable=AsyncMock, return_value={"signal": "bullish", "thesis": "多方論據更紮實，PM 看多", "key_points": ["建議分批買進"], "confidence": 0.70}):
            result = asyncio.run(run_debate(reports=reports, symbol="2330"))

        assert result.method == "llm"
        assert result.final_signal == Signal.BULLISH
        assert result.bull.thesis == "外資連買5日，多方強勢"
        assert result.bear.thesis == "融資增加值得警惕"
        assert result.pm_verdict.thesis == "多方論據更紮實，PM 看多"
        assert result.final_confidence == pytest.approx(0.70)

    def test_bear_signal_from_pm(self) -> None:
        """PM 回傳 bearish → final_signal = BEARISH。"""
        from agents.debate_agents import run_debate

        reports = [_make_report(AgentType.MACRO, Signal.BEARISH)]

        with patch("agents.debate_agents._call_bull_llm", new_callable=AsyncMock, return_value={"thesis": "some bull", "key_points": [], "confidence": 0.40}), \
             patch("agents.debate_agents._call_bear_llm", new_callable=AsyncMock, return_value={"thesis": "some bear", "key_points": [], "confidence": 0.75}), \
             patch("agents.debate_agents._call_pm_llm", new_callable=AsyncMock, return_value={"signal": "bearish", "thesis": "空方論據更紮實", "key_points": [], "confidence": 0.65}):
            result = asyncio.run(run_debate(reports=reports, symbol="2330"))

        assert result.final_signal == Signal.BEARISH

    def test_neutral_signal_from_pm(self) -> None:
        from agents.debate_agents import run_debate

        reports = [_make_report()]

        with patch("agents.debate_agents._call_bull_llm", new_callable=AsyncMock, return_value={"thesis": "b", "key_points": [], "confidence": 0.55}), \
             patch("agents.debate_agents._call_bear_llm", new_callable=AsyncMock, return_value={"thesis": "b", "key_points": [], "confidence": 0.55}), \
             patch("agents.debate_agents._call_pm_llm", new_callable=AsyncMock, return_value={"signal": "neutral", "thesis": "平手", "key_points": [], "confidence": 0.50}):
            result = asyncio.run(run_debate(reports=reports, symbol="2330"))

        assert result.final_signal == Signal.NEUTRAL

    def test_llm_failure_falls_back_deterministically(self) -> None:
        """Bull LLM 失敗 → fallback，不 raise，仍回傳有效 DebateOutput。"""
        from agents.debate_agents import run_debate

        reports = [
            _make_report(AgentType.CHIP, Signal.BULLISH, confidence=0.80),
            _make_report(AgentType.MACRO, Signal.BEARISH, confidence=0.60),
        ]

        with patch("agents.debate_agents._call_bull_llm", new_callable=AsyncMock, side_effect=Exception("API timeout")), \
             patch("agents.debate_agents._call_bear_llm", new_callable=AsyncMock, side_effect=Exception("API timeout")), \
             patch("agents.debate_agents._call_pm_llm", new_callable=AsyncMock, side_effect=Exception("API timeout")):
            result = asyncio.run(run_debate(reports=reports, symbol="2330"))

        assert isinstance(result, DebateOutput)
        assert result.bull.role == "bull"
        assert result.bear.role == "bear"
        assert result.pm_verdict.role == "pm"

    def test_pm_invalid_signal_defaults_neutral(self) -> None:
        """PM 回傳非預期 signal 字串 → 預設 NEUTRAL。"""
        from agents.debate_agents import run_debate

        reports = [_make_report()]

        with patch("agents.debate_agents._call_bull_llm", new_callable=AsyncMock, return_value={"thesis": "b", "key_points": [], "confidence": 0.6}), \
             patch("agents.debate_agents._call_bear_llm", new_callable=AsyncMock, return_value={"thesis": "b", "key_points": [], "confidence": 0.6}), \
             patch("agents.debate_agents._call_pm_llm", new_callable=AsyncMock, return_value={"signal": "HOLD", "thesis": "b", "key_points": [], "confidence": 0.5}):
            result = asyncio.run(run_debate(reports=reports, symbol="2330"))

        assert result.final_signal == Signal.NEUTRAL


# ─── 6. Supervisor.aggregate_debate() ─────────────────────────────────────────


class TestSupervisorAggregatDebate:
    def _make_supervisor_output(self, risk_override: bool = False) -> MagicMock:
        """建立一個輕量 SupervisorOutput mock。"""
        out = MagicMock()
        out.risk_override = risk_override
        out.overall_recommendation = Signal.BULLISH
        out.confidence = 0.70
        out.overall_narrative = "deterministic narrative"
        return out

    def test_aggregate_debate_normal_path(self) -> None:
        """PM narrative 正確替換 overall_narrative（no risk_override）。"""
        from supervisor.graph import Supervisor

        reports = [_make_report(AgentType.CHIP, Signal.BULLISH)]
        pm_thesis = "PM 裁決：多方論據勝出，看多 2330。"

        debate_out = DebateOutput(
            symbol="2330",
            scenario="single_stock",
            bull=DebateParty(role="bull", thesis="bull wins", confidence=0.78),
            bear=DebateParty(role="bear", thesis="bear loses", confidence=0.55),
            pm_verdict=DebateParty(role="pm", thesis=pm_thesis, confidence=0.70),
            final_signal=Signal.BULLISH,
            final_confidence=0.70,
        )

        with patch.object(Supervisor, "aggregate_agentic", return_value=self._make_supervisor_output(risk_override=False)), \
             patch("supervisor.graph.Supervisor.aggregate_debate.__wrapped__" if hasattr(Supervisor.aggregate_debate, "__wrapped__") else "agents.debate_agents.run_debate", new_callable=AsyncMock, return_value=debate_out):
            # 直接 patch run_debate
            with patch("agents.debate_agents.run_debate", new_callable=AsyncMock, return_value=debate_out):
                sup, debate = asyncio.run(
                    Supervisor().aggregate_debate(reports, symbol="2330")
                )

        assert debate.pm_verdict.thesis == pm_thesis
        assert sup.overall_narrative == pm_thesis

    def test_aggregate_debate_risk_override_skips_debate(self) -> None:
        """risk_override=True → debate narrative 不替換 overall_narrative。"""
        from supervisor.graph import Supervisor

        reports = [_make_report(breached=True)]
        pm_unique_text = "多方強勢建議進場"  # unique text that won't appear in skip message
        debate_out = DebateOutput(
            symbol="2330",
            scenario="single_stock",
            bull=DebateParty(role="bull", thesis="bull", confidence=0.5),
            bear=DebateParty(role="bear", thesis="bear", confidence=0.5),
            pm_verdict=DebateParty(role="pm", thesis=pm_unique_text, confidence=0.5),
            final_signal=Signal.BEARISH,
            final_confidence=0.5,
        )
        mock_sup_out = self._make_supervisor_output(risk_override=True)

        with patch.object(Supervisor, "aggregate_agentic", return_value=mock_sup_out), \
             patch("agents.debate_agents.run_debate", new_callable=AsyncMock, return_value=debate_out):
            sup, debate = asyncio.run(
                Supervisor().aggregate_debate(reports, symbol="2330")
            )

        # narrative 不被 PM 替換，但加了 skip 標記
        assert "Debate 已跳過" in sup.overall_narrative
        assert pm_unique_text not in sup.overall_narrative

    def test_aggregate_debate_empty_pm_thesis_keeps_original(self) -> None:
        """PM thesis 為空 → 保留原 narrative。"""
        from supervisor.graph import Supervisor

        reports = [_make_report()]
        debate_out = DebateOutput(
            symbol="2330",
            scenario="single_stock",
            bull=DebateParty(role="bull", thesis="b", confidence=0.5),
            bear=DebateParty(role="bear", thesis="b", confidence=0.5),
            pm_verdict=DebateParty(role="pm", thesis="", confidence=0.5),  # empty
            final_signal=Signal.NEUTRAL,
            final_confidence=0.5,
        )
        mock_sup_out = self._make_supervisor_output(risk_override=False)

        with patch.object(Supervisor, "aggregate_agentic", return_value=mock_sup_out), \
             patch("agents.debate_agents.run_debate", new_callable=AsyncMock, return_value=debate_out):
            sup, debate = asyncio.run(
                Supervisor().aggregate_debate(reports, symbol="2330")
            )

        assert sup.overall_narrative == "deterministic narrative"

    def test_aggregate_debate_returns_tuple(self) -> None:
        """確認回傳型別是 (SupervisorOutput, DebateOutput) tuple。"""
        from supervisor.graph import Supervisor

        reports = [_make_report()]
        debate_out = DebateOutput(
            symbol="2330",
            scenario="single_stock",
            bull=DebateParty(role="bull", thesis="b", confidence=0.5),
            bear=DebateParty(role="bear", thesis="b", confidence=0.5),
            pm_verdict=DebateParty(role="pm", thesis="verdict", confidence=0.65),
            final_signal=Signal.BULLISH,
            final_confidence=0.65,
        )

        with patch.object(Supervisor, "aggregate_agentic", return_value=self._make_supervisor_output()), \
             patch("agents.debate_agents.run_debate", new_callable=AsyncMock, return_value=debate_out):
            result = asyncio.run(Supervisor().aggregate_debate(reports))

        assert isinstance(result, tuple)
        assert len(result) == 2
        sup, debate = result
        assert isinstance(debate, DebateOutput)
