"""
Phase 5 Supervisor 三層仲裁測試。

五個情境（S1-S5）直接來自 docs/tasks/phase_5_supervisor_design.md §三，
斷言逐字照設計文件，不另外發明測試案例。

情境概覽
--------
S1  硬約束優先 A  — Risk Delta 超限，其他全看多 → Layer 1 強制降級
S2  硬約束優先 B  — Fundamental EWS Critical  → 任意 agent 均可觸發 Layer 1
S3  時間框架分層  — 技術面(SHORT)看空 + 基本面(LONG)看多 → 不強融，按層呈現
S4  信心加權      — NEWS LLM 降級 (completeness=0) → 有效權重歸零，排出投票池
S5  Cross_Market  — is_background_only=True，即使 signal=BEARISH 也不進投票

額外涵蓋
---------
- Phase 0 backward-compat（.summary / .forced_warnings / .layered_view）
- empty aggregate()
- Layer 1 mandatory_warnings / risk_override fields
- Layer 3 SOURCE_RELIABILITY constants
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

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
from supervisor.graph import SOURCE_RELIABILITY, Supervisor, _should_exclude_from_directional_vote
from supervisor.signal import HorizonResult, SupervisorOutput

# ─────────────────────────────────────────────────────────────────────────────
# Helper factories
# ─────────────────────────────────────────────────────────────────────────────

_ASOF = datetime(2026, 7, 22, 0, 0, 0, tzinfo=UTC)
_TARGET = Target(symbol="2330", market="TW", asof=_ASOF)
_EVIDENCE = Evidence(claim="test evidence", source="test#1", asof=_ASOF)


def _dq(completeness: float = 1.0, staleness_sec: float = 0.0) -> DataQuality:
    return DataQuality(completeness=completeness, staleness_sec=staleness_sec, confidence=0.9)


def _sig(
    agent: AgentType,
    signal: Signal = Signal.NEUTRAL,
    confidence: float = 0.70,
    horizon: TimeHorizon = TimeHorizon.SHORT,
    hard_constraints: list[HardConstraint] | None = None,
    completeness: float = 1.0,
    metrics: dict | None = None,
    errors: list[str] | None = None,
) -> AgentSignal:
    return AgentSignal(
        agent=agent,
        target=_TARGET,
        signal=signal,
        confidence=confidence,
        time_horizon=horizon,
        key_evidence=[_EVIDENCE],
        hard_constraints=hard_constraints or [],
        metrics=metrics or {},
        data_quality=_dq(completeness=completeness),
        errors=errors or [],
    )


def _hc(
    type_: str,
    current: float,
    limit: float,
    breached: bool,
    detail: str | None = None,
) -> HardConstraint:
    return HardConstraint(type=type_, current=current, limit=limit, breached=breached, detail=detail)


# ─────────────────────────────────────────────────────────────────────────────
# S1: 硬約束優先 A — Risk Delta 超限，其他全看多
# ─────────────────────────────────────────────────────────────────────────────

class TestS1HardConstraintRiskDelta:
    """S1 驗證：Layer 1 規則引擎在所有 agent 看多時仍強制觸發。"""

    @pytest.fixture()
    def signals(self) -> list[AgentSignal]:
        return [
            _sig(
                AgentType.RISK,
                Signal.BEARISH,
                confidence=0.80,
                horizon=TimeHorizon.SHORT,
                hard_constraints=[
                    _hc("net_delta_pct_nav", current=-2.33, limit=0.3, breached=True,
                        detail="組合淨 delta 空頭嚴重超限"),
                    _hc("gamma_limit", current=0.01, limit=1_000_000, breached=False),
                    _hc("vega_limit", current=154_994, limit=500_000, breached=False),
                ],
            ),
            _sig(AgentType.TECHNICAL, Signal.BULLISH, confidence=0.65, horizon=TimeHorizon.SHORT),
            _sig(AgentType.FUNDAMENTAL, Signal.BULLISH, confidence=0.75, horizon=TimeHorizon.LONG),
            _sig(AgentType.NEWS, Signal.BULLISH, confidence=0.55, horizon=TimeHorizon.SHORT),
            _sig(AgentType.MACRO, Signal.BULLISH, confidence=0.60, horizon=TimeHorizon.MEDIUM),
            _sig(
                AgentType.CROSS_MARKET,
                Signal.NEUTRAL,
                confidence=0.70,
                horizon=TimeHorizon.MEDIUM,
                metrics={"is_background_only": True, "regime": "strong_coupling"},
            ),
        ]

    def test_hard_constraint_type_in_mandatory_warnings(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S1: "net_delta_pct_nav" in mandatory_warnings"""
        result = Supervisor().aggregate(signals)
        assert "net_delta_pct_nav" in result.mandatory_warnings

    def test_overall_recommendation_not_bullish(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S1: overall_recommendation != Signal.BULLISH"""
        result = Supervisor().aggregate(signals)
        assert result.overall_recommendation != Signal.BULLISH

    def test_confidence_compressed(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S1: confidence <= 0.40（強制壓縮）"""
        result = Supervisor().aggregate(signals)
        assert result.confidence <= 0.40

    def test_risk_override_true(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S1: risk_override == True"""
        result = Supervisor().aggregate(signals)
        assert result.risk_override is True

    def test_five_bullish_cannot_override_one_breach(self, signals: list[AgentSignal]) -> None:
        """CLAUDE.md §二：五個看多訊號不能「抵銷」一個 breached constraint。"""
        result = Supervisor().aggregate(signals)
        # Regardless of how many bullish signals exist, Layer 1 overrides
        bullish_count = sum(1 for s in signals if s.signal == Signal.BULLISH)
        # TECHNICAL, FUNDAMENTAL, NEWS, MACRO = 4 bullish; RISK=BEARISH, CROSS_MARKET=NEUTRAL
        assert bullish_count == 4
        assert result.risk_override is True
        assert result.overall_recommendation != Signal.BULLISH

    def test_forced_warnings_backward_compat(self, signals: list[AgentSignal]) -> None:
        """backward-compat: forced_warnings 有格式化字串，含「風控強制警告」。"""
        result = Supervisor().aggregate(signals)
        assert len(result.forced_warnings) >= 1
        assert any("風控強制警告" in w for w in result.forced_warnings)

    def test_hard_constraint_breaches_populated(self, signals: list[AgentSignal]) -> None:
        result = Supervisor().aggregate(signals)
        assert len(result.hard_constraint_breaches) == 1
        agent, hc = result.hard_constraint_breaches[0]
        assert agent == AgentType.RISK
        assert hc.type == "net_delta_pct_nav"
        assert hc.breached is True

    def test_non_breached_constraints_not_in_warnings(self, signals: list[AgentSignal]) -> None:
        """gamma_limit 和 vega_limit 未觸限，不應出現在 mandatory_warnings。"""
        result = Supervisor().aggregate(signals)
        assert "gamma_limit" not in result.mandatory_warnings
        assert "vega_limit" not in result.mandatory_warnings


# ─────────────────────────────────────────────────────────────────────────────
# S2: 硬約束優先 B — Fundamental EWS Critical
# ─────────────────────────────────────────────────────────────────────────────

class TestS2HardConstraintFundamental:
    """S2 驗證：fundamental agent 的 EWS critical 也觸發 Layer 1（不只 risk agent）。"""

    @pytest.fixture()
    def signals(self) -> list[AgentSignal]:
        return [
            _sig(AgentType.RISK, Signal.NEUTRAL, confidence=0.70, horizon=TimeHorizon.SHORT),
            _sig(AgentType.TECHNICAL, Signal.BULLISH, confidence=0.65, horizon=TimeHorizon.SHORT),
            _sig(
                AgentType.FUNDAMENTAL,
                Signal.BEARISH,
                confidence=0.80,
                horizon=TimeHorizon.LONG,
                hard_constraints=[
                    _hc(
                        "ews_receivables_spike",
                        current=0.85,
                        limit=0.50,
                        breached=True,
                        detail="EWS critical：應收帳款佔營收比率異常飆升，財務健康疑慮",
                    )
                ],
            ),
            _sig(AgentType.NEWS, Signal.BULLISH, confidence=0.55, horizon=TimeHorizon.SHORT),
            _sig(AgentType.MACRO, Signal.BULLISH, confidence=0.60, horizon=TimeHorizon.MEDIUM),
            _sig(
                AgentType.CROSS_MARKET,
                Signal.NEUTRAL,
                confidence=0.70,
                horizon=TimeHorizon.MEDIUM,
                metrics={"is_background_only": True},
            ),
        ]

    def test_fundamental_hard_constraint_exists(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S2（前提驗證）：fundamental agent 確實有 breached constraint。"""
        assert any(
            hc.breached
            for sig in signals
            if sig.agent == AgentType.FUNDAMENTAL
            for hc in sig.hard_constraints
        )

    def test_ews_in_mandatory_warnings(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S2: "ews_receivables_spike" in mandatory_warnings"""
        result = Supervisor().aggregate(signals)
        assert "ews_receivables_spike" in result.mandatory_warnings

    def test_risk_override_from_fundamental(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S2: risk_override == True（由 fundamental agent 觸發）"""
        result = Supervisor().aggregate(signals)
        assert result.risk_override is True

    def test_any_agent_can_trigger_layer1(self, signals: list[AgentSignal]) -> None:
        """Supervisor 對所有 agent 的 hard_constraints 統一處理，不只檢查 risk。"""
        result = Supervisor().aggregate(signals)
        triggered_agents = [agent for agent, _ in result.hard_constraint_breaches]
        assert AgentType.FUNDAMENTAL in triggered_agents
        assert AgentType.RISK not in triggered_agents  # risk has no breach in S2

    def test_confidence_compressed(self, signals: list[AgentSignal]) -> None:
        result = Supervisor().aggregate(signals)
        assert result.confidence <= 0.40


# ─────────────────────────────────────────────────────────────────────────────
# S3: 時間框架分層 — 技術面(SHORT)看空 + 基本面(LONG)看多
# ─────────────────────────────────────────────────────────────────────────────

class TestS3HorizonBreakdown:
    """S3 驗證：不同時間框架的訊號不被強融成單一方向，按層獨立輸出。"""

    @pytest.fixture()
    def signals(self) -> list[AgentSignal]:
        return [
            _sig(AgentType.RISK, Signal.NEUTRAL, confidence=0.70, horizon=TimeHorizon.SHORT),
            _sig(AgentType.TECHNICAL, Signal.BEARISH, confidence=0.70, horizon=TimeHorizon.SHORT),
            _sig(AgentType.FUNDAMENTAL, Signal.BULLISH, confidence=0.80, horizon=TimeHorizon.LONG),
            _sig(AgentType.NEWS, Signal.BEARISH, confidence=0.55, horizon=TimeHorizon.SHORT),
            _sig(AgentType.MACRO, Signal.BULLISH, confidence=0.60, horizon=TimeHorizon.MEDIUM),
            _sig(
                AgentType.CROSS_MARKET,
                Signal.NEUTRAL,
                confidence=0.70,
                horizon=TimeHorizon.MEDIUM,
                metrics={"is_background_only": True},
            ),
        ]

    def test_short_horizon_present(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S3: "short" in horizon_breakdown"""
        result = Supervisor().aggregate(signals)
        assert "short" in result.horizon_breakdown

    def test_long_horizon_present(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S3: "long" in horizon_breakdown"""
        result = Supervisor().aggregate(signals)
        assert "long" in result.horizon_breakdown

    def test_short_horizon_bearish(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S3: horizon_breakdown["short"].direction == Signal.BEARISH"""
        result = Supervisor().aggregate(signals)
        assert result.horizon_breakdown["short"].direction == Signal.BEARISH

    def test_long_horizon_bullish(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S3: horizon_breakdown["long"].direction == Signal.BULLISH"""
        result = Supervisor().aggregate(signals)
        assert result.horizon_breakdown["long"].direction == Signal.BULLISH

    def test_horizons_not_averaged_into_neutral(self, signals: list[AgentSignal]) -> None:
        """設計文件補充：兩個方向相反的層不允許被平均成 NEUTRAL 然後消去資訊。"""
        result = Supervisor().aggregate(signals)
        short_dir = result.horizon_breakdown["short"].direction
        long_dir = result.horizon_breakdown["long"].direction
        # They must be kept separate and opposing
        assert short_dir != long_dir

    def test_no_risk_override(self, signals: list[AgentSignal]) -> None:
        result = Supervisor().aggregate(signals)
        assert result.risk_override is False

    def test_medium_horizon_present(self, signals: list[AgentSignal]) -> None:
        """MEDIUM 層有 MACRO，cross_market 排除後仍有有效投票。"""
        result = Supervisor().aggregate(signals)
        assert "medium" in result.horizon_breakdown
        hr = result.horizon_breakdown["medium"]
        # cross_market excluded, only macro contributes
        assert hr.direction == Signal.BULLISH

    def test_horizon_result_is_instance(self, signals: list[AgentSignal]) -> None:
        result = Supervisor().aggregate(signals)
        for _key, hr in result.horizon_breakdown.items():
            assert isinstance(hr, HorizonResult)


# ─────────────────────────────────────────────────────────────────────────────
# S4: 信心加權 — NEWS LLM 降級，completeness=0，有效權重歸零
# ─────────────────────────────────────────────────────────────────────────────

class TestS4ConfidenceWeighting:
    """S4 驗證：LLM 降級的 NEWS 完全排出投票池，不影響方向。"""

    @pytest.fixture()
    def signals(self) -> list[AgentSignal]:
        return [
            _sig(AgentType.RISK, Signal.NEUTRAL, confidence=0.70, horizon=TimeHorizon.SHORT,
                 completeness=1.00),
            _sig(AgentType.TECHNICAL, Signal.BULLISH, confidence=0.70, horizon=TimeHorizon.SHORT,
                 completeness=1.00),
            _sig(AgentType.FUNDAMENTAL, Signal.BULLISH, confidence=0.78, horizon=TimeHorizon.LONG,
                 completeness=1.00),
            _sig(
                AgentType.NEWS,
                Signal.NEUTRAL,
                confidence=0.10,
                horizon=TimeHorizon.SHORT,
                completeness=0.00,
                metrics={"llm_analysis_failed": True, "weighted_sentiment_score": 0.0},
                errors=["[降級] LLM 分析不可用"],
            ),
            _sig(AgentType.MACRO, Signal.BULLISH, confidence=0.60, horizon=TimeHorizon.MEDIUM,
                 completeness=0.67),
            _sig(
                AgentType.CROSS_MARKET,
                Signal.NEUTRAL,
                confidence=0.70,
                horizon=TimeHorizon.MEDIUM,
                completeness=1.00,
                metrics={"is_background_only": True},
            ),
        ]

    def test_news_signal_has_expected_fields(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S4（前提）：確認 NEWS signal 的 confidence / completeness / metrics。"""
        news_sig = next(s for s in signals if s.agent == AgentType.NEWS)
        assert news_sig.confidence == pytest.approx(0.10)
        assert news_sig.data_quality.completeness == 0.0
        assert news_sig.metrics.get("llm_analysis_failed") is True

    def test_news_excluded_from_voting(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S4: AgentType.NEWS in excluded_from_voting"""
        result = Supervisor().aggregate(signals)
        excluded = result.excluded_from_voting
        assert AgentType.NEWS in excluded

    def test_news_exclusion_reason_contains_llm_analysis_failed(
        self, signals: list[AgentSignal]
    ) -> None:
        """設計文件斷言 §S4: "llm_analysis_failed" in exclusion_reasons[NEWS]"""
        result = Supervisor().aggregate(signals)
        assert "llm_analysis_failed" in result.exclusion_reasons[AgentType.NEWS]

    def test_news_degraded_does_not_influence_direction(self, signals: list[AgentSignal]) -> None:
        """NEWS weight ≈ 0 → SHORT 完全由 TECHNICAL 主導 → BULLISH。"""
        result = Supervisor().aggregate(signals)
        short_hr = result.horizon_breakdown.get("short")
        assert short_hr is not None
        contributing = [a for a, _, _ in short_hr.contributing_agents]
        assert AgentType.NEWS not in contributing
        assert AgentType.TECHNICAL in contributing
        assert short_hr.direction == Signal.BULLISH

    def test_risk_agent_excluded_not_news_path(self, signals: list[AgentSignal]) -> None:
        """RISK 因 Layer 1 路由被排除，原因應不含 llm_analysis_failed。"""
        result = Supervisor().aggregate(signals)
        assert AgentType.RISK in result.excluded_from_voting
        risk_reason = result.exclusion_reasons[AgentType.RISK]
        assert "llm_analysis_failed" not in risk_reason
        assert "Layer 1" in risk_reason

    def test_directional_pool_excludes_news_and_risk(self, signals: list[AgentSignal]) -> None:
        result = Supervisor().aggregate(signals)
        pool = result.directional_vote_pool
        assert AgentType.NEWS not in pool
        assert AgentType.RISK not in pool

    def test_no_risk_override(self, signals: list[AgentSignal]) -> None:
        result = Supervisor().aggregate(signals)
        assert result.risk_override is False


# ─────────────────────────────────────────────────────────────────────────────
# S5: Cross_Market 背景資訊不參與方向投票
# ─────────────────────────────────────────────────────────────────────────────

class TestS5CrossMarketExclusion:
    """S5 驗證：spec §5.3 強制行為——cross_market 不進方向性投票，但進 background_context。"""

    @pytest.fixture()
    def signals(self) -> list[AgentSignal]:
        return [
            _sig(AgentType.RISK, Signal.NEUTRAL, confidence=0.70, horizon=TimeHorizon.SHORT),
            _sig(AgentType.TECHNICAL, Signal.BULLISH, confidence=0.65, horizon=TimeHorizon.SHORT,
                 metrics={"sma_bullish": True}),
            _sig(AgentType.FUNDAMENTAL, Signal.BULLISH, confidence=0.75, horizon=TimeHorizon.LONG,
                 metrics={"roic": 8.70, "wacc": 6.50}),
            _sig(AgentType.NEWS, Signal.BULLISH, confidence=0.55, horizon=TimeHorizon.SHORT,
                 metrics={"has_official_disclosure": True}),
            _sig(AgentType.MACRO, Signal.NEUTRAL, confidence=0.55, horizon=TimeHorizon.MEDIUM,
                 metrics={"macro_score": 0.05}),
            _sig(
                AgentType.CROSS_MARKET,
                Signal.BEARISH,          # 台美 20 日背離 — 但這不代表標的看空
                confidence=0.65,
                horizon=TimeHorizon.MEDIUM,
                metrics={
                    "is_background_only": True,
                    "regime": "short_term_counter",
                    "tw_us_corr_20d": -0.35,
                },
            ),
        ]

    def test_cross_market_signal_is_bearish(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S5（前提）：cross_market 確實輸出 BEARISH。"""
        cm_sig = next(s for s in signals if s.agent == AgentType.CROSS_MARKET)
        assert cm_sig.metrics["is_background_only"] is True
        assert cm_sig.signal == Signal.BEARISH

    def test_cross_market_not_in_directional_pool(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S5: AgentType.CROSS_MARKET not in directional_vote_pool"""
        result = Supervisor().aggregate(signals)
        directional_pool = result.directional_vote_pool
        assert AgentType.CROSS_MARKET not in directional_pool

    def test_cross_market_in_background_context(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S5: cm_sig in background_context"""
        result = Supervisor().aggregate(signals)
        cm_sig = next(s for s in signals if s.agent == AgentType.CROSS_MARKET)
        assert cm_sig in result.background_context

    def test_short_horizon_direction_bullish(self, signals: list[AgentSignal]) -> None:
        """設計文件斷言 §S5: short_horizon_direction == Signal.BULLISH"""
        result = Supervisor().aggregate(signals)
        assert result.short_horizon_direction == Signal.BULLISH

    def test_cross_market_bearish_does_not_reduce_to_neutral(
        self, signals: list[AgentSignal]
    ) -> None:
        """cross_market BEARISH 混入投票 → 會系統性低估多頭共識。正確實作不允許此行為。"""
        result = Supervisor().aggregate(signals)
        # SHORT: TECHNICAL(BULLISH) + NEWS(BULLISH) — RISK excluded, cross_market excluded
        short_hr = result.horizon_breakdown.get("short")
        assert short_hr is not None
        assert short_hr.direction == Signal.BULLISH

    def test_cross_market_excluded_from_voting(self, signals: list[AgentSignal]) -> None:
        result = Supervisor().aggregate(signals)
        assert AgentType.CROSS_MARKET in result.excluded_from_voting
        reason = result.exclusion_reasons[AgentType.CROSS_MARKET]
        assert "background-only" in reason or "spec §5.3" in reason

    def test_no_risk_override(self, signals: list[AgentSignal]) -> None:
        result = Supervisor().aggregate(signals)
        assert result.risk_override is False

    def test_background_context_has_regime(self, signals: list[AgentSignal]) -> None:
        """background_context 的 cross_market signal 保留 regime 資訊。"""
        result = Supervisor().aggregate(signals)
        bg = result.background_context[0]
        assert bg.metrics.get("regime") == "short_term_counter"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 0 backward-compat
# ─────────────────────────────────────────────────────────────────────────────

class TestPhase0BackwardCompat:
    """確認 Phase 5 實作不破壞 test_phase0 的 API 契約。"""

    def test_aggregate_empty_returns_summary(self) -> None:
        result = Supervisor().aggregate([])
        assert result.summary  # truthy

    def test_aggregate_groups_by_horizon(self) -> None:
        signals = [
            _sig(AgentType.TECHNICAL, horizon=TimeHorizon.MEDIUM),
            _sig(AgentType.FUNDAMENTAL, horizon=TimeHorizon.MEDIUM),
        ]
        result = Supervisor().aggregate(signals)
        assert "medium" in result.layered_view
        assert len(result.layered_view["medium"]) == 2

    def test_hard_constraint_produces_forced_warning(self) -> None:
        signals = [
            _sig(
                AgentType.RISK,
                hard_constraints=[_hc("gamma_limit", current=-850, limit=-500, breached=True)],
            )
        ]
        result = Supervisor().aggregate(signals)
        assert len(result.forced_warnings) == 1
        assert "風控強制警告" in result.forced_warnings[0]


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1: 硬約束邊界測試
# ─────────────────────────────────────────────────────────────────────────────

class TestLayer1HardConstraints:
    """Layer 1 邊界條件測試。"""

    def test_no_breach_no_override(self) -> None:
        signals = [
            _sig(AgentType.RISK, hard_constraints=[_hc("gamma_limit", 100, 500, breached=False)]),
            _sig(AgentType.TECHNICAL, Signal.BULLISH),
        ]
        result = Supervisor().aggregate(signals)
        assert result.risk_override is False
        assert result.mandatory_warnings == []

    def test_multiple_breaches_all_in_warnings(self) -> None:
        signals = [
            _sig(
                AgentType.RISK,
                hard_constraints=[
                    _hc("net_delta_pct_nav", -2.33, 0.3, breached=True),
                    _hc("vega_limit", 600_000, 500_000, breached=True),
                ],
            ),
        ]
        result = Supervisor().aggregate(signals)
        assert "net_delta_pct_nav" in result.mandatory_warnings
        assert "vega_limit" in result.mandatory_warnings
        assert len(result.mandatory_warnings) == 2

    def test_breach_from_multiple_agents(self) -> None:
        signals = [
            _sig(AgentType.RISK, hard_constraints=[_hc("delta", -2, 0.3, breached=True)]),
            _sig(AgentType.FUNDAMENTAL, Signal.BEARISH, horizon=TimeHorizon.LONG,
                 hard_constraints=[_hc("ews_spike", 0.9, 0.5, breached=True)]),
        ]
        result = Supervisor().aggregate(signals)
        assert len(result.hard_constraint_breaches) == 2
        assert "delta" in result.mandatory_warnings
        assert "ews_spike" in result.mandatory_warnings

    def test_override_confidence_exactly_035(self) -> None:
        signals = [
            _sig(AgentType.RISK, hard_constraints=[_hc("x", 1.0, 0.5, breached=True)]),
        ]
        result = Supervisor().aggregate(signals)
        assert result.confidence == pytest.approx(0.35)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3: 排除規則 & SOURCE_RELIABILITY 常數
# ─────────────────────────────────────────────────────────────────────────────

class TestExclusionRules:
    """_should_exclude_from_directional_vote() 純函數測試。"""

    def test_cross_market_always_excluded(self) -> None:
        sig = _sig(AgentType.CROSS_MARKET, metrics={"is_background_only": True})
        exc, reason = _should_exclude_from_directional_vote(sig)
        assert exc is True
        assert "background-only" in reason or "5.3" in reason

    def test_risk_always_excluded(self) -> None:
        sig = _sig(AgentType.RISK)
        exc, reason = _should_exclude_from_directional_vote(sig)
        assert exc is True
        assert "Layer 1" in reason

    def test_completeness_zero_excluded(self) -> None:
        sig = _sig(AgentType.NEWS, completeness=0.0, metrics={"llm_analysis_failed": True})
        exc, reason = _should_exclude_from_directional_vote(sig)
        assert exc is True
        assert "llm_analysis_failed" in reason

    def test_completeness_zero_no_llm_flag(self) -> None:
        """completeness=0.0 但 llm_analysis_failed 未設定 → no_recent_events 路徑。"""
        sig = _sig(AgentType.MACRO, completeness=0.0)
        exc, reason = _should_exclude_from_directional_vote(sig)
        assert exc is True
        assert "no_recent_events" in reason

    def test_normal_agent_not_excluded(self) -> None:
        for agent in (AgentType.TECHNICAL, AgentType.FUNDAMENTAL, AgentType.MACRO, AgentType.NEWS):
            sig = _sig(agent, completeness=1.0)
            exc, _ = _should_exclude_from_directional_vote(sig)
            assert exc is False, f"{agent} should not be excluded"

    def test_news_completeness_nonzero_not_excluded(self) -> None:
        """NEWS 正常分析（completeness > 0）不應因 agent type 被排除。"""
        sig = _sig(AgentType.NEWS, completeness=0.80)
        exc, _ = _should_exclude_from_directional_vote(sig)
        assert exc is False


class TestSourceReliability:
    """SOURCE_RELIABILITY 常數驗證。"""

    def test_fundamental_highest(self) -> None:
        assert SOURCE_RELIABILITY[AgentType.FUNDAMENTAL] == pytest.approx(1.0)

    def test_macro_value(self) -> None:
        assert SOURCE_RELIABILITY[AgentType.MACRO] == pytest.approx(0.85)

    def test_technical_value(self) -> None:
        assert SOURCE_RELIABILITY[AgentType.TECHNICAL] == pytest.approx(0.80)

    def test_news_lowest(self) -> None:
        assert SOURCE_RELIABILITY[AgentType.NEWS] == pytest.approx(0.60)

    def test_risk_not_in_reliability_map(self) -> None:
        assert AgentType.RISK not in SOURCE_RELIABILITY

    def test_cross_market_not_in_reliability_map(self) -> None:
        assert AgentType.CROSS_MARKET not in SOURCE_RELIABILITY


# ─────────────────────────────────────────────────────────────────────────────
# SupervisorOutput 結構驗證
# ─────────────────────────────────────────────────────────────────────────────

class TestSupervisorOutputStructure:
    def test_output_is_supervisor_output(self) -> None:
        result = Supervisor().aggregate([_sig(AgentType.TECHNICAL, Signal.BULLISH)])
        assert isinstance(result, SupervisorOutput)

    def test_raw_agent_signals_preserved(self) -> None:
        signals = [_sig(AgentType.TECHNICAL), _sig(AgentType.MACRO, horizon=TimeHorizon.MEDIUM)]
        result = Supervisor().aggregate(signals)
        assert result.raw_agent_signals is signals

    def test_asof_is_utc_datetime(self) -> None:
        result = Supervisor().aggregate([])
        assert result.asof.tzinfo is not None

    def test_narrative_nonempty(self) -> None:
        result = Supervisor().aggregate([_sig(AgentType.TECHNICAL, Signal.BULLISH)])
        assert len(result.overall_narrative) > 0

    def test_empty_aggregate_neutral(self) -> None:
        result = Supervisor().aggregate([])
        assert result.overall_recommendation == Signal.NEUTRAL
        assert result.risk_override is False
        assert result.mandatory_warnings == []

    def test_overall_recommendation_long_horizon_priority(self) -> None:
        """overall_recommendation 優先取 LONG 層（LONG > MEDIUM > SHORT）。"""
        signals = [
            _sig(AgentType.TECHNICAL, Signal.BEARISH, horizon=TimeHorizon.SHORT),
            _sig(AgentType.FUNDAMENTAL, Signal.BULLISH, confidence=0.80, horizon=TimeHorizon.LONG),
        ]
        result = Supervisor().aggregate(signals)
        # LONG(BULLISH) should win over SHORT(BEARISH) in overall_recommendation
        assert result.overall_recommendation == Signal.BULLISH

    def test_disclaimer_is_nonempty(self) -> None:
        """Phase 6 P0-1: disclaimer 欄位必須存在且非空。"""
        result = Supervisor().aggregate([_sig(AgentType.TECHNICAL, Signal.BULLISH)])
        assert hasattr(result, "disclaimer")
        assert isinstance(result.disclaimer, str)
        assert len(result.disclaimer) > 0

    def test_disclaimer_contains_key_phrase(self) -> None:
        """disclaimer 必須包含「研究輔助」與「自行判斷」，確認免責聲明內容正確。"""
        result = Supervisor().aggregate([_sig(AgentType.TECHNICAL, Signal.BULLISH)])
        assert "研究輔助" in result.disclaimer
        assert "自行判斷" in result.disclaimer


# ─────────────────────────────────────────────────────────────────────────────
# 迴歸測試：evidence_confidence 公式 — completeness 不可在分母消除
#
# 修正記錄（commit 25446bf）：
#   舊公式 Σ(c×bw)/Σ(bw)，bw = compl×rel
#   → compl 同時出現在分子與分母，單一來源時互相抵銷
#   → MACRO(conf=0.60, compl=0.67) 與 MACRO(conf=0.60, compl=1.00) 輸出相同值 0.60 (BUG)
#
#   新公式 Σ(eff_weight) / Σ(rel)，eff_weight = conf×compl×rel
#   → compl 只在分子，compl<1.0 時正確壓低 evidence_confidence
#   → MACRO(conf=0.60, compl=0.67) → 0.60 × 0.67 = 0.402 (CORRECT)
#
# 若有人把公式改回舊版，這組測試會立即失敗。
# ─────────────────────────────────────────────────────────────────────────────

class TestEvidenceConfidenceCompletenessRegression:
    """迴歸測試：evidence_confidence 公式中 completeness 不可被分母消除。

    核心不變式：
      對任意單一貢獻 agent，若 completeness < 1.0，則
      evidence_confidence 必須嚴格小於 raw confidence。

    等價陳述（單一來源時）：
      evidence_confidence = raw_confidence × completeness
    """

    def test_single_source_partial_completeness_lower_than_raw_confidence(self) -> None:
        """主斷言：單一貢獻者 compl=0.67 → evconf 嚴格 < raw confidence（0.60）。"""
        signals = [
            _sig(
                AgentType.MACRO,
                Signal.BULLISH,
                confidence=0.60,
                horizon=TimeHorizon.MEDIUM,
                completeness=0.67,
            ),
        ]
        result = Supervisor().aggregate(signals)
        hr = result.horizon_breakdown["medium"]
        assert len(hr.contributing_agents) == 1
        raw_conf = 0.60
        assert hr.evidence_confidence < raw_conf, (
            f"evidence_confidence={hr.evidence_confidence} should be < raw_confidence={raw_conf} "
            f"when completeness=0.67 < 1.0. "
            f"If this fails, completeness is being cancelled in the denominator (regression to commit before 25446bf)."
        )

    def test_single_source_full_completeness_equals_raw_confidence(self) -> None:
        """對照組：compl=1.0 時 evidence_confidence == raw confidence（不受損失）。"""
        signals = [
            _sig(
                AgentType.MACRO,
                Signal.BULLISH,
                confidence=0.60,
                horizon=TimeHorizon.MEDIUM,
                completeness=1.00,
            ),
        ]
        result = Supervisor().aggregate(signals)
        hr = result.horizon_breakdown["medium"]
        assert hr.evidence_confidence == pytest.approx(0.60)

    def test_single_source_evidence_confidence_equals_conf_times_completeness(self) -> None:
        """單一來源時公式退化為 conf × compl（rel 互相抵銷，此行為為有意設計）。"""
        conf, compl = 0.60, 0.67
        signals = [
            _sig(
                AgentType.MACRO,
                Signal.BULLISH,
                confidence=conf,
                horizon=TimeHorizon.MEDIUM,
                completeness=compl,
            ),
        ]
        result = Supervisor().aggregate(signals)
        hr = result.horizon_breakdown["medium"]
        # = Σ(conf×compl×rel) / Σ(rel) = (conf×compl×rel) / rel = conf×compl
        assert hr.evidence_confidence == pytest.approx(conf * compl, rel=1e-6)

    def test_different_completeness_same_confidence_produces_different_evconf(self) -> None:
        """同 raw confidence、不同 completeness → evidence_confidence 必須不同。

        這是 bug 的直接複現：舊公式兩者都輸出 0.60，新公式正確區分。
        """
        def _evconf(compl: float) -> float:
            sigs = [
                _sig(
                    AgentType.MACRO,
                    Signal.BULLISH,
                    confidence=0.60,
                    horizon=TimeHorizon.MEDIUM,
                    completeness=compl,
                )
            ]
            return Supervisor().aggregate(sigs).horizon_breakdown["medium"].evidence_confidence

        evconf_full = _evconf(1.00)
        evconf_partial = _evconf(0.67)

        assert evconf_full != evconf_partial, (
            "evidence_confidence must differ when completeness differs. "
            "If equal, completeness is being cancelled (regression)."
        )
        assert evconf_partial < evconf_full

    def test_completeness_020_extreme_discount(self) -> None:
        """극단적인 compl=0.20：evidence_confidence = 0.70 × 0.20 = 0.14。"""
        conf, compl = 0.70, 0.20
        signals = [
            _sig(
                AgentType.TECHNICAL,
                Signal.BULLISH,
                confidence=conf,
                horizon=TimeHorizon.SHORT,
                completeness=compl,
            ),
        ]
        result = Supervisor().aggregate(signals)
        hr = result.horizon_breakdown["short"]
        assert hr.evidence_confidence == pytest.approx(conf * compl, rel=1e-6)
        assert hr.evidence_confidence < 0.20  # 明確低於任何合理的「可信」閾值

    def test_two_sources_one_partial_lowers_evconf_vs_both_full(self) -> None:
        """多來源：其中一個 compl<1.0 → evconf 低於兩者都 compl=1.0 的情況。"""
        def _evconf(news_compl: float) -> float:
            sigs = [
                _sig(AgentType.TECHNICAL, Signal.BULLISH, confidence=0.65,
                     horizon=TimeHorizon.SHORT, completeness=1.00),
                _sig(AgentType.NEWS, Signal.BULLISH, confidence=0.55,
                     horizon=TimeHorizon.SHORT, completeness=news_compl),
            ]
            return Supervisor().aggregate(sigs).horizon_breakdown["short"].evidence_confidence

        evconf_both_full = _evconf(1.00)
        evconf_news_partial = _evconf(0.50)

        assert evconf_news_partial < evconf_both_full, (
            "Partial completeness of one contributor must reduce overall evidence_confidence."
        )

    def test_s4_macro_medium_evidence_confidence_reflects_067_completeness(self) -> None:
        """S4 真實情境驗證：MACRO(compl=0.67) 的 MEDIUM 層 evconf=0.402，非 0.60。

        0.60 是舊公式 bug 值；0.402 = 0.60 × 0.67 是正確值。
        """
        signals = [
            _sig(AgentType.RISK, Signal.NEUTRAL, 0.70, TimeHorizon.SHORT, completeness=1.00),
            _sig(AgentType.TECHNICAL, Signal.BULLISH, 0.70, TimeHorizon.SHORT, completeness=1.00),
            _sig(AgentType.FUNDAMENTAL, Signal.BULLISH, 0.78, TimeHorizon.LONG, completeness=1.00),
            _sig(AgentType.NEWS, Signal.NEUTRAL, 0.10, TimeHorizon.SHORT, completeness=0.00,
                 metrics={"llm_analysis_failed": True}),
            _sig(AgentType.MACRO, Signal.BULLISH, 0.60, TimeHorizon.MEDIUM, completeness=0.67),
            _sig(AgentType.CROSS_MARKET, Signal.NEUTRAL, 0.70, TimeHorizon.MEDIUM,
                 completeness=1.00, metrics={"is_background_only": True}),
        ]
        result = Supervisor().aggregate(signals)
        medium_hr = result.horizon_breakdown["medium"]

        # 確認只有 MACRO 貢獻（cross_market 已排除）
        assert len(medium_hr.contributing_agents) == 1
        assert medium_hr.contributing_agents[0][0] == AgentType.MACRO

        # 核心斷言：不是舊 bug 值 0.60，是正確值 0.402
        assert medium_hr.evidence_confidence != pytest.approx(0.60, abs=1e-3), (
            "evidence_confidence=0.60 is the old bug value where completeness cancelled. "
            "Expected 0.60 × 0.67 = 0.402."
        )
        assert medium_hr.evidence_confidence == pytest.approx(0.60 * 0.67, rel=1e-6)
