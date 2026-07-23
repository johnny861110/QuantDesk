"""
Phase 9 Tests — aggregate_agentic() integration

Tests for:
  1. CHIP in SOURCE_RELIABILITY
  2. aggregate_agentic() with various DomainReport inputs
  3. LLM synthesis narrative replacement
  4. risk_override skips synthesis
  5. synthesis failure falls back to deterministic narrative
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from schemas.agent_signal import AgentType, HardConstraint, Signal, TimeHorizon
from schemas.domain_report import DomainReport
from supervisor.graph import SOURCE_RELIABILITY, Supervisor
from supervisor.signal import SupervisorOutput


# ─── Fixtures ────────────────────────────────────────────────────────────────


def _make_domain_report(
    agent: AgentType = AgentType.CHIP,
    signal: Signal = Signal.BULLISH,
    breached: bool = False,
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
        confidence=0.70,
        time_horizon=TimeHorizon.SHORT,
        hard_constraints=[hc] if breached else [],
        key_findings={"score": 0.5},
    )


# ─── Test 1: CHIP in SOURCE_RELIABILITY ──────────────────────────────────────


def test_chip_agent_in_source_reliability() -> None:
    """AgentType.CHIP must be registered in SOURCE_RELIABILITY with a valid weight."""
    assert AgentType.CHIP in SOURCE_RELIABILITY
    value = SOURCE_RELIABILITY[AgentType.CHIP]
    assert 0.5 <= value <= 1.0, f"CHIP reliability {value} out of expected range [0.5, 1.0]"


# ─── Test 2: Empty list → NEUTRAL ────────────────────────────────────────────


def test_aggregate_agentic_no_reports() -> None:
    """Empty DomainReport list should return a valid SupervisorOutput with NEUTRAL."""
    sup = Supervisor()
    output = sup.aggregate_agentic([])
    assert isinstance(output, SupervisorOutput)
    assert output.overall_recommendation == Signal.NEUTRAL


# ─── Test 3: Two BULLISH reports → BULLISH ───────────────────────────────────


def test_aggregate_agentic_converts_domain_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two BULLISH DomainReports without breaches → overall_recommendation is BULLISH."""
    # Patch synthesize_reports to avoid real LLM call
    mock_synthesis = MagicMock()
    mock_synthesis.narrative = ""  # empty → keep deterministic
    monkeypatch.setattr(
        "supervisor.graph.Supervisor.aggregate_agentic.__wrapped__"
        if hasattr(Supervisor.aggregate_agentic, "__wrapped__")
        else "supervisor.synthesis.synthesize_reports",
        lambda *_a, **_kw: mock_synthesis,
    )

    reports = [
        _make_domain_report(agent=AgentType.CHIP, signal=Signal.BULLISH),
        _make_domain_report(agent=AgentType.TECHNICAL, signal=Signal.BULLISH),
    ]

    sup = Supervisor()
    # We patch inside aggregate_agentic at import level by monkeypatching the module
    import supervisor.synthesis as synth_mod  # noqa: PLC0415
    monkeypatch.setattr(synth_mod, "synthesize_reports", lambda *_a, **_kw: mock_synthesis)

    output = sup.aggregate_agentic(reports, symbol="2330")
    assert output.overall_recommendation == Signal.BULLISH
    assert not output.risk_override


# ─── Test 4: Breached constraint → risk_override, synthesis NOT called ───────


def test_aggregate_agentic_risk_override_skips_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A breached HardConstraint → risk_override=True and synthesize_reports is never called."""
    call_count = {"n": 0}

    def _fake_synthesize(*_args: object, **_kwargs: object) -> object:
        call_count["n"] += 1
        return MagicMock(narrative="should not appear")

    import supervisor.synthesis as synth_mod  # noqa: PLC0415
    monkeypatch.setattr(synth_mod, "synthesize_reports", _fake_synthesize)

    report = _make_domain_report(breached=True)
    sup = Supervisor()
    output = sup.aggregate_agentic([report])

    assert output.risk_override is True
    assert output.overall_recommendation == Signal.BEARISH
    assert call_count["n"] == 0, "synthesize_reports must NOT be called when risk_override=True"


# ─── Test 5: Synthesis narrative replaces overall_narrative ──────────────────


def test_aggregate_agentic_narrative_replaced_by_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When synthesis returns a non-empty narrative, it replaces overall_narrative."""
    from supervisor.synthesis import SynthesisOutput  # noqa: PLC0415

    fake_synthesis = SynthesisOutput(
        signal=Signal.BULLISH,
        confidence=0.80,
        narrative="LLM 說看多",
        key_drivers=["外資買超"],
        key_risks=[],
        domain_consensus={"chip": "bullish"},
        conflicts=[],
        method="llm",
    )

    import supervisor.synthesis as synth_mod  # noqa: PLC0415
    monkeypatch.setattr(synth_mod, "synthesize_reports", lambda *_a, **_kw: fake_synthesis)

    reports = [
        _make_domain_report(agent=AgentType.CHIP, signal=Signal.BULLISH),
    ]
    sup = Supervisor()
    output = sup.aggregate_agentic(reports, symbol="2330")

    assert "LLM 說看多" in output.overall_narrative


# ─── Test 6: Synthesis raises → deterministic narrative kept ─────────────────


def test_aggregate_agentic_synthesis_failure_keeps_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When synthesize_reports raises RuntimeError, deterministic narrative is kept."""

    def _raise_synthesis(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("LLM 不可用")

    import supervisor.synthesis as synth_mod  # noqa: PLC0415
    monkeypatch.setattr(synth_mod, "synthesize_reports", _raise_synthesis)

    reports = [
        _make_domain_report(agent=AgentType.CHIP, signal=Signal.BULLISH),
    ]
    sup = Supervisor()
    output = sup.aggregate_agentic(reports, symbol="2330")

    # Deterministic narrative must still be a non-empty string
    assert isinstance(output.overall_narrative, str)
    assert len(output.overall_narrative) > 0
    # Must NOT contain the LLM text (fallback should not have called LLM)
    assert "LLM 說看多" not in output.overall_narrative
