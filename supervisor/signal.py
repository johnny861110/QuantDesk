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

from dataclasses import dataclass, field
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
        公式：Σ(eff_weight_i) / Σ(rel_i)
            = Σ(confidence_i × completeness_i × rel_i) / Σ(rel_i)

        completeness 在分子（不在分母），所以部分覆蓋的資料會被懲罰：
          MACRO(conf=0.60, compl=1.00) → evidence_confidence = 0.60
          MACRO(conf=0.60, compl=0.67) → evidence_confidence = 0.60 × 0.67 = 0.402

        若改用 Σ(c×bw)/Σ(bw)（bw=compl×rel），compl 同時出現在分子與分母會
        互相抵銷，導致 compl=0.67 與 compl=1.0 的結果相同——那才是 bug。

        範圍 [0, 1]；這是「這層的訊號有多可信」的真實估算，同時考慮了
        來源可靠度（rel）、資料完整性（compl）和分析信心（confidence）。
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
    unverifiable_constraints: list[tuple[AgentType, HardConstraint]]  # verifiable=False
    risk_override: bool
    mandatory_warnings: list[str]       # breached → "type"; unverifiable → "unverifiable:type"
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
    disclaimer: str
    # ── HITL Gate（Phase 6）──────────────────────────────────────────────────
    # 機器可讀的人工複核標記；有 default 值，向後相容既有建構呼叫。
    requires_human_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    # review_reasons 語意：
    #   "low_confidence:{conf:.2f}"        — overall confidence 低於門檻
    #   "hard_constraint_breach:{hc.type}" — 硬約束已確認觸限
    #   "unverifiable_constraint:{hc.type}"— 底層 Greeks 缺失，無法確認安全
    #   "ews_critical"                     — fundamental agent EWS critical 預警

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
        result = [
            f"[風控強制警告] {hc.type} 觸限：目前 {hc.current} vs 上限 {hc.limit}"
            for _, hc in self.hard_constraint_breaches
        ]
        result += [
            f"[風控無法驗證] {hc.type}：資料缺失，目前 {hc.current} vs 上限 {hc.limit} 無法確認安全"
            for _, hc in self.unverifiable_constraints
        ]
        return result

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
