"""
SupervisorOutput — Phase 5 仲裁層的輸出結構。

獨立 schema，不繼承 AgentSignal（兩者職責不同：AgentSignal 是單一 domain 的
signal；SupervisorOutput 是多 domain 融合後的仲裁結論）。

backward-compat properties（供 test_phase0 使用）：
  .summary         → overall_narrative
  .forced_warnings → formatted warning strings
  .layered_view    → dict[str, list[AgentSignal]]（按 horizon key 分組）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from schemas.agent_signal import AgentSignal, AgentType, HardConstraint, Signal, Target


@dataclass
class HorizonResult:
    """單一時間框架內的加權仲裁結果。

    兩個獨立概念，不可混用：

    consensus_share（方向一致度）
        「贏方向拿走的加權份額」÷ 全部有效權重。
        例：SHORT 層只有一個 agent 投票 → share=1.0（方向沒有對立聲音），
        但這**不代表該 agent 的分析可信度是 100%**。
        範圍 [0, 1]；越高代表層內方向越一致，越低代表層內分歧越大。

    evidence_confidence（底層信心）
        各貢獻 agent 的 raw confidence 以 (completeness × SOURCE_RELIABILITY)
        為權重的加權平均，反映該層底層證據本身的可信程度。
        例：LONG 層只有 FUNDAMENTAL(conf=0.75) → evidence_confidence=0.75。
        範圍 [0, 1]；這是「這層的訊號有多可信」的真實估算。
    """
    direction: Signal
    consensus_share: float          # 方向一致度（前身：weighted_confidence）
    evidence_confidence: float      # 底層信心（加權平均 raw confidence）
    contributing_agents: list[tuple[AgentType, Signal, float]]  # (agent, signal, eff_weight)
    excluded_agents: list[AgentType]


@dataclass
class SupervisorOutput:
    """
    三層仲裁的最終輸出。

    Layer 1 欄位：hard_constraint_breaches / risk_override / mandatory_warnings
    Layer 2 欄位：horizon_breakdown
    Layer 3 欄位：excluded_from_voting / exclusion_reasons / background_context /
                  directional_vote_pool
    Final：overall_recommendation / confidence / overall_narrative
    """
    target: Target | None
    asof: datetime
    # Layer 1
    hard_constraint_breaches: list[tuple[AgentType, HardConstraint]]
    risk_override: bool
    mandatory_warnings: list[str]       # constraint type strings, e.g. ["net_delta_pct_nav"]
    # Layer 2
    horizon_breakdown: dict[str, HorizonResult]   # key: "short"|"medium"|"long"|"intraday"
    # Layer 3
    excluded_from_voting: list[AgentType]
    exclusion_reasons: dict[AgentType, str]
    background_context: list[AgentSignal]         # cross_market + other background signals
    directional_vote_pool: list[AgentType]        # agents that participated in directional vote
    # Final
    overall_recommendation: Signal
    confidence: float
    overall_narrative: str
    raw_agent_signals: list[AgentSignal]

    # ── Backward-compat properties for test_phase0 ──────────────────────────

    @property
    def summary(self) -> str:
        """Alias for overall_narrative (test_phase0 compat)."""
        return self.overall_narrative

    @property
    def forced_warnings(self) -> list[str]:
        """Formatted warning strings (test_phase0 compat).
        Phase 5 callers should use mandatory_warnings (constraint type list) instead.
        """
        return [
            f"[風控強制警告] {hc.type} 觸限：目前 {hc.current} vs 上限 {hc.limit}"
            for _, hc in self.hard_constraint_breaches
        ]

    @property
    def layered_view(self) -> dict[str, list[AgentSignal]]:
        """dict[horizon_key, signals] rebuilt from raw_agent_signals (test_phase0 compat)."""
        result: dict[str, list[AgentSignal]] = {}
        for sig in self.raw_agent_signals:
            result.setdefault(sig.time_horizon.value, []).append(sig)
        return result

    @property
    def short_horizon_direction(self) -> Signal:
        """Shortcut: direction of the SHORT (or INTRADAY) horizon layer."""
        for key in ("short", "intraday"):
            hr = self.horizon_breakdown.get(key)
            if hr is not None:
                return hr.direction
        return Signal.NEUTRAL
