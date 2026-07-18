"""Phase 0 完成標準：這些測試全過，代表骨架可用。"""
from datetime import datetime

from schemas.agent_signal import (
    AgentSignal, AgentType, DataQuality, Evidence,
    HardConstraint, Signal, Target, TimeHorizon,
)
from supervisor.graph import Supervisor


def _make_signal(agent: AgentType, breached: bool = False) -> AgentSignal:
    return AgentSignal(
        agent=agent,
        target=Target(symbol="2330", market="TW", asof=datetime.now()),
        signal=Signal.NEUTRAL,
        confidence=0.8,
        time_horizon=TimeHorizon.MEDIUM,
        key_evidence=[Evidence(claim="測試", value=1.0, source="test#1", asof=datetime.now())],
        hard_constraints=(
            [HardConstraint(type="gamma_limit", current=-850, limit=-500, breached=True)]
            if breached else []
        ),
        data_quality=DataQuality(completeness=1.0, staleness_sec=0, confidence=0.9),
    )


def test_agent_signal_serializes():
    sig = _make_signal(AgentType.FUNDAMENTAL)
    dumped = sig.model_dump_json()
    restored = AgentSignal.model_validate_json(dumped)
    assert restored.agent == AgentType.FUNDAMENTAL
    assert restored.target.symbol == "2330"


def test_supervisor_runs_empty():
    result = Supervisor().aggregate([])
    assert result.summary  # 空流程也能跑通


def test_supervisor_collects_signals_by_horizon():
    signals = [_make_signal(AgentType.TECHNICAL), _make_signal(AgentType.FUNDAMENTAL)]
    result = Supervisor().aggregate(signals)
    assert "medium" in result.layered_view
    assert len(result.layered_view["medium"]) == 2


def test_hard_constraint_produces_forced_warning():
    signals = [_make_signal(AgentType.RISK, breached=True)]
    result = Supervisor().aggregate(signals)
    assert len(result.forced_warnings) == 1
    assert "風控強制警告" in result.forced_warnings[0]
