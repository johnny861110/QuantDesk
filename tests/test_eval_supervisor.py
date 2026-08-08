"""
Phase 17 L3 —— Supervisor 仲裁 golden set。

兌現 docs/spec.md §8.2「Supervisor 層：建立 golden set，評估匯總結論是否合理、
硬約束是否正確觸發、時間框架分層是否恰當」。

定位（重要）
-----------
本檔**不取代** tests/test_phase5_supervisor.py 的 91 個測試。
那些測試已讓 supervisor/graph.py 達到 98% 覆蓋率——把可運作的測試重寫成
YAML 是 churn 而非改善，這是刻意的範圍決定。

本檔的價值是**降低新增仲裁情境的成本**：未來調權重、加 agent、改仲裁規則時，
可以先在 tests/data/supervisor_golden_set.yaml 用幾行描述期望行為，
不必每次都寫一支 Python 測試。

不呼叫任何 LLM——Supervisor 是純規則引擎。
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from schemas.agent_signal import (
    AgentSignal,
    AgentType,
    DataQuality,
    Evidence,
    HardConstraint,
    Signal,
    Target,
    TimeHorizon,
)
from supervisor.graph import Supervisor
from supervisor.signal import SupervisorOutput

GOLDEN_SET_PATH = Path(__file__).parent / "data" / "supervisor_golden_set.yaml"
_ASOF = datetime(2026, 8, 9, 9, 0, 0, tzinfo=UTC)


def _load_scenarios() -> list[dict[str, Any]]:
    with GOLDEN_SET_PATH.open(encoding="utf-8") as fh:
        data: list[dict[str, Any]] = yaml.safe_load(fh)
    return data


SCENARIOS: list[dict[str, Any]] = _load_scenarios()


# ─── YAML → AgentSignal ──────────────────────────────────────────────────────


def _build_signal(spec: dict[str, Any]) -> AgentSignal:
    """把 golden set 的簡化描述建成真正的 AgentSignal。"""
    constraints: list[HardConstraint] = []
    if "breached" in spec:
        constraints.append(
            HardConstraint(
                type=str(spec["breached"]),
                current=1.0,
                limit=0.5,
                breached=True,
            )
        )
    if "unverifiable" in spec:
        constraints.append(
            HardConstraint(
                type=str(spec["unverifiable"]),
                current=0.0,
                limit=1.0,
                breached=False,
                verifiable=False,
            )
        )

    completeness = float(spec.get("completeness", 1.0))
    return AgentSignal(
        agent=AgentType(spec["agent"]),
        target=Target(symbol="2330", market="TW", asof=_ASOF),
        signal=Signal(spec["signal"]),
        confidence=float(spec["confidence"]),
        time_horizon=TimeHorizon(spec["horizon"]),
        key_evidence=[
            Evidence(
                claim="golden set 合成訊號",
                value=None,
                source="test:supervisor_golden_set",
                asof=_ASOF,
            )
        ],
        hard_constraints=constraints,
        metrics={},
        narrative="",
        data_quality=DataQuality(
            completeness=completeness,
            staleness_sec=0.0,
            confidence=float(spec["confidence"]),
        ),
        errors=[],
    )


def _run(scenario: dict[str, Any]) -> SupervisorOutput:
    signals = [_build_signal(s) for s in scenario["signals"]]
    return Supervisor().aggregate(signals)


def _sid(scenario: dict[str, Any]) -> str:
    return str(scenario["name"])


# ─── golden set 自身健全性 ───────────────────────────────────────────────────


class TestGoldenSetIntegrity:
    def test_scenarios_present(self) -> None:
        assert len(SCENARIOS) >= 5

    def test_required_fields(self) -> None:
        for s in SCENARIOS:
            assert "name" in s and "why" in s, f"情境缺 name/why: {s}"
            assert s.get("signals"), f"{s.get('name')} 沒有 signals"
            assert s.get("expect"), f"{s.get('name')} 沒有 expect"

    def test_names_unique(self) -> None:
        names = [s["name"] for s in SCENARIOS]
        assert len(names) == len(set(names)), "情境名稱重複"

    def test_agent_values_are_valid(self) -> None:
        for s in SCENARIOS:
            for sig in s["signals"]:
                AgentType(sig["agent"])   # 非法值會直接 ValueError
                Signal(sig["signal"])
                TimeHorizon(sig["horizon"])


# ─── 仲裁行為 ────────────────────────────────────────────────────────────────


class TestSupervisorGolden:
    @pytest.mark.parametrize("scenario", SCENARIOS, ids=_sid)
    def test_scenario(self, scenario: dict[str, Any]) -> None:
        out = _run(scenario)
        exp: dict[str, Any] = scenario["expect"]
        ctx = f"\n情境：{scenario['name']}\n用途：{scenario['why'].strip()}"

        if "risk_override" in exp:
            assert out.risk_override is exp["risk_override"], (
                f"{ctx}\nrisk_override 期望 {exp['risk_override']}，實際 {out.risk_override}"
            )

        if "recommendation" in exp:
            assert out.overall_recommendation == Signal(exp["recommendation"]), (
                f"{ctx}\n建議期望 {exp['recommendation']}，"
                f"實際 {out.overall_recommendation.value}"
            )

        if "max_confidence" in exp:
            assert out.confidence <= float(exp["max_confidence"]) + 1e-9, (
                f"{ctx}\n信心 {out.confidence} 應 ≤ {exp['max_confidence']}"
            )

        if "requires_human_review" in exp:
            assert out.requires_human_review is exp["requires_human_review"], (
                f"{ctx}\nHITL 期望 {exp['requires_human_review']}，"
                f"實際 {out.requires_human_review}（reasons={out.review_reasons}）"
            )

        for agent_name in exp.get("excluded_agents", []):
            assert AgentType(agent_name) in out.excluded_from_voting, (
                f"{ctx}\n{agent_name} 應被排除出方向性投票，"
                f"實際 excluded={[a.value for a in out.excluded_from_voting]}"
            )

        for agent_name in exp.get("voting_agents", []):
            assert AgentType(agent_name) in out.directional_vote_pool, (
                f"{ctx}\n{agent_name} 應參與方向性投票，"
                f"實際 pool={[a.value for a in out.directional_vote_pool]}"
            )

        for warn in exp.get("mandatory_warning_types", []):
            assert warn in out.mandatory_warnings, (
                f"{ctx}\n缺少強制警告 {warn!r}，實際 {out.mandatory_warnings}"
            )

        for horizon, want in exp.get("horizon_signals", {}).items():
            assert horizon in out.horizon_breakdown, (
                f"{ctx}\nhorizon_breakdown 缺 {horizon}，"
                f"實際 keys={list(out.horizon_breakdown)}"
            )
            got = out.horizon_breakdown[horizon].direction
            assert got == Signal(want), (
                f"{ctx}\n{horizon} 層期望 {want}，實際 {got.value}\n"
                f"（分層結論不得被跨層平均消去——spec.md §5.1②）"
            )


# ─── 跨情境不變量 ────────────────────────────────────────────────────────────


class TestCrossScenarioInvariants:
    @pytest.mark.parametrize("scenario", SCENARIOS, ids=_sid)
    def test_output_always_carries_disclaimer(self, scenario: dict[str, Any]) -> None:
        """任何仲裁結果都必須帶免責聲明（Phase 6 硬化要求）。"""
        assert _run(scenario).disclaimer

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=_sid)
    def test_confidence_within_bounds(self, scenario: dict[str, Any]) -> None:
        out = _run(scenario)
        assert 0.0 <= out.confidence <= 1.0

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=_sid)
    def test_excluded_and_voting_are_disjoint(self, scenario: dict[str, Any]) -> None:
        """同一個 agent 不能既被排除又在投票池裡。"""
        out = _run(scenario)
        overlap = set(out.excluded_from_voting) & set(out.directional_vote_pool)
        assert not overlap, f"agent 同時出現在排除與投票池：{overlap}"

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=_sid)
    def test_risk_override_implies_human_review(self, scenario: dict[str, Any]) -> None:
        """
        風控強制降級必然低於 HITL 門檻（_HITL_CONFIDENCE_THRESHOLD=0.40
        > _RISK_OVERRIDE_CONFIDENCE=0.35），故必定要求人工複核。
        這條不變量守護兩個常數之間的相對關係，避免日後有人單獨調其中一個。
        """
        out = _run(scenario)
        if out.risk_override:
            assert out.requires_human_review, (
                "risk_override=True 但未要求人工複核——"
                "檢查 _HITL_CONFIDENCE_THRESHOLD 與 _RISK_OVERRIDE_CONFIDENCE 的相對大小"
            )
