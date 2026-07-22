"""
Supervisor 三層仲裁引擎 (Phase 5)

三層邏輯：
  Layer 1 — 硬約束規則引擎（強制，不可被 LLM 覆蓋）
  Layer 2 — 時間框架分層（不強融成單一結論）
  Layer 3 — 信心加權（同一時間框架內多 agent 融合）

設計原則（CLAUDE.md §三條不可違反）：
  ① LLM 只寫 narrative，不產出數字，也不決定是否忽略 hard_constraint
  ② 任何 hard_constraint.breached==True → 強制降級 + mandatory_warning（規則引擎執行）
  ③ 所有排除 / 背景化決策由 _should_exclude_from_directional_vote() 規則引擎決定，
     不由 LLM 自由裁量

cross_market 特殊地位（spec §5.3）：
  is_background_only=True → 從方向性投票池排除，
  但 regime 資訊進入 background_context 供 narrative 使用。

MEDIUM 層空層處理（Phase 5 設計拍板）：
  排除 cross_market 後若 MEDIUM 投票池為空 → 顯示「無方向性訊號」，不繼承其他層結論。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from schemas.agent_signal import AgentSignal, AgentType, HardConstraint, Signal
from supervisor.signal import HorizonResult, SupervisorOutput

# ── Source reliability（靜態暫定值；Phase 6 動態回測校準）───────────────────
SOURCE_RELIABILITY: Final[dict[AgentType, float]] = {
    AgentType.FUNDAMENTAL: 1.0,   # 財報數字確定性最高
    AgentType.MACRO:       0.85,  # ⚠️ 暫定值；TD-MACRO-01 仍有未解決項目
    AgentType.TECHNICAL:   0.80,  # ⚠️ 暫定值
    AgentType.NEWS:        0.60,  # ⚠️ 暫定值；LLM 依賴度高
    # CROSS_MARKET: 0.0 → 不參與方向性投票，不加入此 dict
    # RISK: None → 走 Layer 1，不參與方向性投票
}

# ⚠️ 暫定值：hard constraint 觸發時統一壓縮至此信心值。
# 設計理由：
#   - 低於多數「可執行閾值」（通常 ≥ 0.50），強迫使用者先處理風控問題再行動。
#   - 不設為 0.0，因為 horizon_breakdown 各層的分析結論仍然有效，
#     只是最終建議層面加了強制警告；設為 0 會誤導「沒有資訊」。
#   - Phase 6 可考慮依 breach 嚴重程度分級（critical=0.20，high=0.30，medium=0.35），
#     目前先用單一暫定值。
_RISK_OVERRIDE_CONFIDENCE: Final[float] = 0.35

# 優先序：決定 overall_recommendation 從哪個 horizon 取
_HORIZON_PRIORITY: Final[list[str]] = ["long", "medium", "short", "intraday"]


# ─────────────────────────────────────────────────────────────────────────────
# 規則引擎函數（純函數，不呼叫 LLM）
# ─────────────────────────────────────────────────────────────────────────────

def _should_exclude_from_directional_vote(
    sig: AgentSignal,
) -> tuple[bool, str]:
    """規則引擎層：決定 agent 是否排除出方向性投票池。非 LLM 自由裁量。

    三種排除條件：
      1. CROSS_MARKET → 永遠排除（spec §5.3 強制）
      2. RISK → 走 Layer 1，不參與方向投票
      3. completeness==0.0 → 明確降級旗標（LLM 失敗 or 無近期事件）

    「明確旗標優於隱性閾值」原則：
      不使用 confidence <= threshold 猜測，因為無法區分
      「正常分析結果剛好低信心」與「降級輸出的信心底線」。
    """
    if sig.agent == AgentType.CROSS_MARKET:
        return True, "cross_market is background-only (spec §5.3)"
    if sig.agent == AgentType.RISK:
        return True, "risk agent routes to Layer 1, not directional vote"
    if sig.data_quality.completeness == 0.0:
        # 細化排除原因：讓下游測試可區分兩種降級情境
        cause = (
            "llm_analysis_failed"
            if sig.metrics.get("llm_analysis_failed")
            else "no_recent_events"
        )
        return True, f"completeness=0.0 ({cause})"
    return False, ""


def _effective_weight(sig: AgentSignal) -> float:
    """Layer 3 有效權重：confidence × completeness × SOURCE_RELIABILITY。"""
    rel = SOURCE_RELIABILITY.get(sig.agent, 0.0)
    return sig.confidence * sig.data_quality.completeness * rel


def _compute_horizon_result(
    sigs: list[AgentSignal],
) -> tuple[HorizonResult, list[AgentType], dict[AgentType, str], list[AgentType]]:
    """單一時間框架內的加權仲裁（Layer 3）。

    Returns
    -------
    (HorizonResult, excluded_agents, exclusion_reasons, participating_agents)

    HorizonResult 包含兩個獨立信心概念（見 signal.py HorizonResult docstring）：
      consensus_share    — 贏方向的加權份額（方向一致度）
      evidence_confidence — 底層 raw confidence 的加權平均（底層信心）
    """
    contributors: list[tuple[AgentType, Signal, float]] = []
    # 用於計算 evidence_confidence：(raw_confidence, base_weight)
    # base_weight = completeness × SOURCE_RELIABILITY（不含 confidence 因子）
    conf_base: list[tuple[float, float]] = []
    excluded: list[AgentType] = []
    exc_reasons: dict[AgentType, str] = {}
    participating: list[AgentType] = []

    for sig in sigs:
        should_exc, reason = _should_exclude_from_directional_vote(sig)
        if should_exc:
            excluded.append(sig.agent)
            exc_reasons[sig.agent] = reason
            continue
        rel = SOURCE_RELIABILITY.get(sig.agent, 0.0)
        base_weight = sig.data_quality.completeness * rel
        eff_weight = sig.confidence * base_weight
        contributors.append((sig.agent, sig.signal, eff_weight))
        conf_base.append((sig.confidence, base_weight))
        participating.append(sig.agent)

    if not contributors:
        # 投票池為空 → 顯示「無方向性訊號」（不繼承其他層結論）
        return (
            HorizonResult(
                direction=Signal.NEUTRAL,
                consensus_share=0.0,
                evidence_confidence=0.0,
                contributing_agents=[],
                excluded_agents=excluded,
            ),
            excluded,
            exc_reasons,
            participating,
        )

    # ── consensus_share：贏方向有效權重 ÷ 總有效權重 ─────────────────────
    direction_weights: dict[Signal, float] = {s: 0.0 for s in Signal}
    total_eff_weight = 0.0
    for _, sig_val, w in contributors:
        direction_weights[sig_val] += w
        total_eff_weight += w

    if total_eff_weight == 0.0:
        return (
            HorizonResult(
                direction=Signal.NEUTRAL,
                consensus_share=0.0,
                evidence_confidence=0.0,
                contributing_agents=contributors,
                excluded_agents=excluded,
            ),
            excluded,
            exc_reasons,
            participating,
        )

    winning_signal = max(direction_weights, key=lambda s: direction_weights[s])
    consensus_share = direction_weights[winning_signal] / total_eff_weight

    # ── evidence_confidence：raw confidence 以 base_weight 為權加權平均 ──
    # = Σ(confidence_i × base_weight_i) / Σ(base_weight_i)
    # 不把 confidence 自身再乘進來，避免高信心 agent 的基數過度自我放大。
    total_base_weight = sum(bw for _, bw in conf_base)
    if total_base_weight > 0.0:
        evidence_confidence = sum(c * bw for c, bw in conf_base) / total_base_weight
    else:
        evidence_confidence = 0.0

    return (
        HorizonResult(
            direction=winning_signal,
            consensus_share=consensus_share,
            evidence_confidence=evidence_confidence,
            contributing_agents=contributors,
            excluded_agents=excluded,
        ),
        excluded,
        exc_reasons,
        participating,
    )


def _build_narrative(output: SupervisorOutput) -> str:
    """確定性規則化 narrative（不呼叫 LLM）。數字全來自 output 結構化欄位。

    格式：
      ⚠️ 風控強制降級（若 risk_override）
      【short】 direction（加權信心 x.xx）
      【medium】...
      【long】...
      【跨市場背景】regime=...
      【排除投票】[agent_names]
    """
    parts: list[str] = []

    if output.risk_override:
        parts.append("⚠️ 風控強制降級：")
        for _, hc in output.hard_constraint_breaches:
            detail = f"（{hc.detail}）" if hc.detail else ""
            parts.append(
                f"  [{hc.type}] 觸限：目前 {hc.current}，上限 {hc.limit}{detail}"
            )

    for h in _HORIZON_PRIORITY:
        hr = output.horizon_breakdown.get(h)
        if hr is None:
            continue
        if not hr.contributing_agents:
            parts.append(f"【{h}】無方向性訊號（投票池為空）")
        else:
            agree_pct = f"{hr.consensus_share:.0%}"
            ev_conf = f"{hr.evidence_confidence:.2f}"
            n = len(hr.contributing_agents)
            if n == 1:
                sole = hr.contributing_agents[0][0].value
                src_note = f"僅單一來源：{sole}"
            else:
                names = ", ".join(a.value for a, _, _ in hr.contributing_agents)
                src_note = f"來源：{names}"
            parts.append(
                f"【{h}】{hr.direction.value}"
                f"（方向一致度 {agree_pct}，底層信心 {ev_conf}，{src_note}）"
            )

    if output.background_context:
        bg = output.background_context[0]
        regime = bg.metrics.get("regime", "unknown")
        corr_20d = bg.metrics.get("tw_us_corr_20d", "")
        parts.append(
            f"【跨市場背景】regime={regime}，corr_20d={corr_20d}"
            "（不參與投票，僅背景參考）"
        )

    if output.excluded_from_voting:
        exc_names = [a.value for a in output.excluded_from_voting]
        parts.append(f"【排除投票】{exc_names}")

    return "  ".join(parts) if parts else "Supervisor 彙總完成，無顯著方向性訊號。"


# ─────────────────────────────────────────────────────────────────────────────
# Supervisor 主類別
# ─────────────────────────────────────────────────────────────────────────────

class Supervisor:
    """Phase 5：三層仲裁 Supervisor。

    .aggregate(signals) → SupervisorOutput

    backward compat（test_phase0 使用 .summary / .forced_warnings / .layered_view）
    見 SupervisorOutput 的 property 定義。
    """

    def aggregate(self, signals: list[AgentSignal]) -> SupervisorOutput:
        asof = datetime.now(UTC)
        target = signals[0].target if signals else None

        # ── Layer 1: 硬約束規則引擎 ──────────────────────────────────────────
        breaches: list[tuple[AgentType, HardConstraint]] = [
            (sig.agent, hc)
            for sig in signals
            for hc in sig.hard_constraints
            if hc.breached
        ]
        risk_override = bool(breaches)
        # mandatory_warnings 存 constraint type string（方便下游 `in` 判斷）
        mandatory_warnings: list[str] = [hc.type for _, hc in breaches]

        # ── Layer 2: 時間框架分層 ─────────────────────────────────────────────
        horizon_groups: dict[str, list[AgentSignal]] = {}
        for sig in signals:
            horizon_groups.setdefault(sig.time_horizon.value, []).append(sig)

        # ── Layer 3: 各 horizon 獨立加權仲裁 ─────────────────────────────────
        horizon_breakdown: dict[str, HorizonResult] = {}
        all_excluded: list[AgentType] = []
        all_reasons: dict[AgentType, str] = {}
        all_participating: list[AgentType] = []
        background_context: list[AgentSignal] = [
            sig for sig in signals if sig.agent == AgentType.CROSS_MARKET
        ]

        for horizon_key, sigs in horizon_groups.items():
            hr, excl, reasons, participating = _compute_horizon_result(sigs)
            horizon_breakdown[horizon_key] = hr
            for agent in excl:
                if agent not in all_excluded:
                    all_excluded.append(agent)
                    all_reasons[agent] = reasons[agent]
            for agent in participating:
                if agent not in all_participating:
                    all_participating.append(agent)

        # ── Overall recommendation ────────────────────────────────────────────
        if risk_override:
            # 硬約束觸發 → 強制 BEARISH + 壓縮信心
            overall_signal = Signal.BEARISH
            final_confidence = _RISK_OVERRIDE_CONFIDENCE
        else:
            # 從最高優先 horizon 取第一個有有效貢獻的結論
            overall_signal = Signal.NEUTRAL
            final_confidence = 0.0
            for h in _HORIZON_PRIORITY:
                candidate: HorizonResult | None = horizon_breakdown.get(h)
                if candidate is not None and candidate.contributing_agents:
                    overall_signal = candidate.direction
                    # 用 evidence_confidence（底層信心），不用 consensus_share（方向一致度）
                    # 原因：consensus_share=1.0 只代表「無對立票」，不代表分析品質高；
                    # evidence_confidence 才反映底層來源的實際 raw confidence。
                    final_confidence = candidate.evidence_confidence
                    break

        output = SupervisorOutput(
            target=target,
            asof=asof,
            hard_constraint_breaches=breaches,
            risk_override=risk_override,
            mandatory_warnings=mandatory_warnings,
            horizon_breakdown=horizon_breakdown,
            excluded_from_voting=all_excluded,
            exclusion_reasons=all_reasons,
            background_context=background_context,
            directional_vote_pool=all_participating,
            overall_recommendation=overall_signal,
            confidence=final_confidence,
            overall_narrative="",          # filled below
            raw_agent_signals=signals,
        )
        output.overall_narrative = _build_narrative(output)
        return output
