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

import asyncio
from datetime import UTC, datetime
from typing import Any, Final

from schemas.agent_signal import AgentSignal, AgentType, HardConstraint, Signal
from supervisor.signal import HorizonResult, SupervisorOutput

# ── Source reliability（靜態暫定值；Phase 6 動態回測校準）───────────────────
SOURCE_RELIABILITY: Final[dict[AgentType, float]] = {
    AgentType.FUNDAMENTAL: 1.0,   # 財報數字確定性最高
    AgentType.MACRO:       0.85,  # ⚠️ 暫定值；TD-MACRO-01 仍有未解決項目
    AgentType.TECHNICAL:   0.80,  # ⚠️ 暫定值
    AgentType.CHIP:        0.75,  # ⚠️ 暫定值；籌碼面，FinMind 資料來源
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

# ⚠️ 暫定值：HITL Gate 信心門檻，未經統計校準。
# 設計理由：低信心輸出不應被當作可執行建議，需人工確認底層假設是否成立。
# 典型觸發：底層來源稀少、資料覆蓋不足，導致 evidence_confidence 偏低。
# 注意：risk_override 時 confidence 被強制壓縮至 _RISK_OVERRIDE_CONFIDENCE(=0.35)，
#       低於此門檻，故 risk_override 場景 condition1 與 condition2 均會觸發，
#       review_reasons 完整列出兩者（符合多條件並列設計）。
_HITL_CONFIDENCE_THRESHOLD: Final[float] = 0.40

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
    # 用於計算 evidence_confidence：(eff_weight, rel)
    # evidence_confidence = Σ(eff_weight) / Σ(rel)
    #                     = Σ(conf × compl × rel) / Σ(rel)
    # 不用 base_weight（compl×rel）做分母，否則 compl 同時出現在分子與分母會互相消除：
    #   Σ(conf × compl×rel) / Σ(compl×rel) → compl 在單一來源時直接抵銷，
    #   導致 completeness=0.67 和 completeness=1.0 的 evidence_confidence 完全相同。
    # 用 rel 做分母，compl 只在分子，才能正確懲罰部分覆蓋的資料：
    #   MACRO(conf=0.60, compl=0.67, rel=0.85) → evidence_confidence = 0.60×0.67 = 0.402
    #   MACRO(conf=0.60, compl=1.00, rel=0.85) → evidence_confidence = 0.60×1.00 = 0.600
    eff_rel_pairs: list[tuple[float, float]] = []
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
        eff_weight = sig.confidence * sig.data_quality.completeness * rel
        contributors.append((sig.agent, sig.signal, eff_weight))
        eff_rel_pairs.append((eff_weight, rel))
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

    # ── evidence_confidence = Σ(eff_weight_i) / Σ(rel_i) ───────────────────
    # 等價於：conf×compl 的 reliability 加權平均。
    # compl 留在分子（不在分母），確保部分覆蓋的資料被正確懲罰。
    total_rel = sum(r for _, r in eff_rel_pairs)
    if total_rel > 0.0:
        evidence_confidence = sum(ew for ew, _ in eff_rel_pairs) / total_rel
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


def _compute_hitl_gate(
    output: SupervisorOutput,
    signals: list[AgentSignal],
) -> tuple[bool, list[str]]:
    """
    HITL Gate 機器可讀觸發判斷（不解析 narrative 文字）。

    三種觸發條件（各自獨立評估，多條件時全部列出）：

    1. overall confidence < _HITL_CONFIDENCE_THRESHOLD (0.40)
       reason format:
         獨立低信心：  "low_confidence:{conf:.2f}"
         risk_override 引發："low_confidence:{conf:.2f} (caused_by:risk_override)"
       ─ 耦合說明 ─────────────────────────────────────────────────────────
       _HITL_CONFIDENCE_THRESHOLD (0.40) > _RISK_OVERRIDE_CONFIDENCE (0.35)：
       任何真實觸發 risk_override 的情況，confidence 都被強制壓至 0.35，
       必然同時觸發 condition1。兩者不是獨立事件，而是同一根因（risk_override）
       的兩層表現。reason 字串加 "(caused_by:risk_override)" 標記，
       讓讀者區分「真正底層信心不足」與「風控強制降級的附帶效果」。
       ─────────────────────────────────────────────────────────────────────

    2. hard_constraint_breach 或 unverifiable_constraint（各自獨立項目）
       → reason: "hard_constraint_breach:{hc.type}"
                 "unverifiable_constraint:{hc.type}"

    3. fundamental agent 的 EWS critical（讀 metrics，獨立於 hard_constraints）
       → reason: "ews_critical"
       理由：EWS critical 本質需要人工確認財務健康，即使 hard_constraints
             未觸發（降級/測試情境）也應標記；與 verifiable 同樣設計哲學。
    """
    reasons: list[str] = []

    # Condition 1：低信心
    # 若 risk_override 是根因（confidence 被強制壓至 _RISK_OVERRIDE_CONFIDENCE），
    # 在 reason 字串標記，避免與真正的底層信心不足混淆。
    if output.confidence < _HITL_CONFIDENCE_THRESHOLD:
        if output.risk_override and output.confidence == _RISK_OVERRIDE_CONFIDENCE:
            reasons.append(
                f"low_confidence:{output.confidence:.2f} (caused_by:risk_override)"
            )
        else:
            reasons.append(f"low_confidence:{output.confidence:.2f}")

    # Condition 2a：hard constraint 已確認觸限
    for _, hc in output.hard_constraint_breaches:
        reasons.append(f"hard_constraint_breach:{hc.type}")

    # Condition 2b：unverifiable constraint（底層 Greeks 缺失，無法確認安全）
    for _, hc in output.unverifiable_constraints:
        reasons.append(f"unverifiable_constraint:{hc.type}")

    # Condition 3：EWS critical（讀 fundamental agent metrics，獨立於 hard_constraints）
    for sig in signals:
        if sig.agent == AgentType.FUNDAMENTAL:
            if sig.metrics.get("ews_warning_level") == "critical":
                reasons.append("ews_critical")

    return bool(reasons), reasons


def _build_narrative(output: SupervisorOutput) -> str:
    """確定性規則化 narrative（不呼叫 LLM）。數字全來自 output 結構化欄位。

    格式：
      🔴 需要人工複核（若 requires_human_review，最高優先顯示）
      ⚠️ 風控強制降級（若 risk_override）
      【short】 direction（加權信心 x.xx）
      【medium】...
      【long】...
      【跨市場背景】regime=...
      【排除投票】[agent_names]
    """
    parts: list[str] = []

    # HITL Gate 通知（最高優先，人工複核提示給人看，機器判斷只看欄位不解析這段文字）
    if output.requires_human_review:
        reasons_text = "；".join(output.review_reasons)
        parts.append(f"🔴 需要人工複核，觸發原因：{reasons_text}")

    if output.hard_constraint_breaches:
        parts.append("⚠️ 風控強制降級：")
        for _, hc in output.hard_constraint_breaches:
            detail = f"（{hc.detail}）" if hc.detail else ""
            parts.append(
                f"  [{hc.type}] 觸限：目前 {hc.current}，上限 {hc.limit}{detail}"
            )

    if output.unverifiable_constraints:
        parts.append("⚠️ 風控待人工複核（底層 Greeks 缺失，無法確認安全）：")
        for _, hc in output.unverifiable_constraints:
            detail = f"（{hc.detail}）" if hc.detail else ""
            parts.append(
                f"  [{hc.type}] 目前值 {hc.current}，限制 {hc.limit}{detail}"
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
        # unverifiable: verifiable=False → 無法確認 breached=False 是否可信
        # 保守處置：視同需人工複核（與 breached=True 同等觸發 risk_override）
        unverifiable: list[tuple[AgentType, HardConstraint]] = [
            (sig.agent, hc)
            for sig in signals
            for hc in sig.hard_constraints
            if not hc.verifiable
        ]
        risk_override = bool(breaches) or bool(unverifiable)
        # mandatory_warnings: breached → type; unverifiable → "unverifiable:{type}"
        mandatory_warnings: list[str] = (
            [hc.type for _, hc in breaches]
            + [f"unverifiable:{hc.type}" for _, hc in unverifiable]
        )

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
            unverifiable_constraints=unverifiable,
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
            disclaimer=(
                "本系統輸出為研究輔助與風險提示，"
                "非自動下單或保證獲利建議，"
                "實際投資決策需自行判斷並承擔風險"
            ),
            # requires_human_review / review_reasons: filled below by HITL gate
        )

        # ── HITL Gate（Phase 6）──────────────────────────────────────────────
        # 必須在 narrative 之前計算，使 _build_narrative 能讀到 HITL 欄位
        output.requires_human_review, output.review_reasons = _compute_hitl_gate(
            output, signals
        )
        output.overall_narrative = _build_narrative(output)
        return output

    def aggregate_agentic(
        self,
        domain_reports: "list[Any]",
        symbol: str = "",
        scenario: str = "single_stock",
    ) -> SupervisorOutput:
        """Agentic 架構入口：接受 DomainReport 列表，整合後回傳 SupervisorOutput。

        架構設計（CLAUDE.md §三條不可違反）：
          1. 規則引擎（三層仲裁）結果不被 LLM 影響
          2. LLM Synthesis 僅替換 overall_narrative（白話說明）
          3. risk_override=True 時跳過 LLM synthesis（硬約束已強制決定）

        Parameters
        ----------
        domain_reports : list[DomainReport]
            所有 domain agent 輸出的報告列表。
        symbol         : str
            分析標的代碼（供 Synthesis LLM 組織報告用）。
        scenario       : str
            場景類型："single_stock" / "portfolio_risk" / "multi_stock_scan"。

        Returns
        -------
        SupervisorOutput
            同 aggregate() 的輸出型別，向後相容。
        """
        # 延遲 import，避免 circular import
        from schemas.domain_report import domain_report_to_agent_signal  # noqa: PLC0415
        from supervisor.synthesis import synthesize_reports              # noqa: PLC0415

        # Step 1: DomainReport → AgentSignal（橋接）
        agent_signals = [domain_report_to_agent_signal(r) for r in domain_reports]

        # Step 2: 三層規則引擎（Layer 1 硬約束 + Layer 2 時間框架 + Layer 3 信心加權）
        output = self.aggregate(agent_signals)

        # Step 3: LLM Synthesis（只替換 narrative，不改變任何規則引擎結論）
        if output.risk_override:
            # 硬約束已強制決定方向，跳過 LLM synthesis
            # 規則引擎結論（signal=BEARISH, confidence=0.35）保持不變
            output.overall_narrative = (
                output.overall_narrative
                + "  【Synthesis 已跳過：風控強制降級，不呼叫 LLM】"
            )
        else:
            try:
                synthesis = synthesize_reports(domain_reports, symbol=symbol, scenario=scenario)
                if synthesis.narrative:
                    # 只替換 narrative，其他欄位（signal, confidence, risk_override）不動
                    output.overall_narrative = synthesis.narrative
            except Exception:  # noqa: BLE001
                # LLM synthesis 失敗 → 保留確定性 narrative，系統仍可用
                pass

        return output

    async def aggregate_debate(
        self,
        domain_reports: "list[Any]",
        symbol: str = "",
        scenario: str = "single_stock",
    ) -> "tuple[SupervisorOutput, Any]":
        """Debate 架構入口：三層規則引擎 + Multi-agent Debate 並行執行。

        架構（CLAUDE.md §三條不可違反）：
          1. 規則引擎（三層仲裁）與 Debate LLM 並行執行，互不阻塞
          2. Debate PM 裁決只替換 overall_narrative（不覆蓋規則引擎 signal/confidence）
          3. risk_override=True 時 Debate narrative 被跳過（硬約束優先）

        執行流程：
          asyncio.gather(
            aggregate_agentic(),   ← 三層規則引擎 + Synthesis LLM（in thread）
            run_debate(),          ← Bull + Bear 並行 + PM 仲裁（async）
          )
          ↓
          若 not risk_override → pm_verdict.thesis 取代 overall_narrative

        Parameters
        ----------
        domain_reports : list[DomainReport]
        symbol         : str
        scenario       : str

        Returns
        -------
        tuple[SupervisorOutput, DebateOutput]
        """
        from agents.debate_agents import run_debate  # noqa: PLC0415

        # Step 1: 規則引擎 + Debate 並行
        # asyncio.to_thread 讓同步的 aggregate_agentic 不阻塞 event loop
        supervisor_output, debate_output = await asyncio.gather(
            asyncio.to_thread(self.aggregate_agentic, domain_reports, symbol, scenario),
            run_debate(domain_reports, symbol=symbol, scenario=scenario),
        )

        # Step 2: PM 裁決替換 narrative（硬約束跳過）
        if supervisor_output.risk_override:
            supervisor_output.overall_narrative += (
                "  【Debate 已跳過：風控強制降級，PM 裁決不適用】"
            )
        elif debate_output.pm_verdict.thesis:
            supervisor_output.overall_narrative = debate_output.pm_verdict.thesis

        return supervisor_output, debate_output
