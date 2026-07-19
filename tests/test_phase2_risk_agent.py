"""
Tests for agents/risk_agent.py — Phase 2 subtask 8

Test strategy
-------------
① Unit tests target build_risk_signal() with fully synthetic inputs.
   No network calls, no YAML loading, no option pricing.
   Each test verifies one specific contract.

② Integration tests target run_risk_agent() with the real positions.yaml
   and a MockFXAdapter (no network).  They verify end-to-end pipeline
   connectivity, not specific Greek values (those are in subtask 1-7 tests).

Fixture layout
--------------
mock_agg_breach   : AggregationResult with net_delta breached
mock_agg_ok       : AggregationResult with all constraints within limits
mock_agg_no_fx    : AggregationResult built without FX (excluded_currencies=['USD'])
mock_scenario     : minimal ScenarioResult (one row, index-linked only)
minimal_positions : [TXFF short futures] — no options, no invalid positions
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from adapters.base import DataSourceAdapter, SourcedData
from adapters.fx_adapter import FXRate
from agents.risk.aggregation import (
    AggregationResult,
    ConsolidatedTWD,
    IndexPointRecord,
)
from agents.risk.position_loader import Position
from agents.risk.pricing_router import GreeksResult
from agents.risk.scenario import ScenarioResult, ScenarioRow
from agents.risk_agent import (
    build_risk_signal,
    run_risk_agent,
)
from schemas.agent_signal import (
    AgentSignal,
    AgentType,
    HardConstraint,
    Signal,
    TimeHorizon,
)

# ─── Constants ─────────────────────────────────────────────────────────────────

MOCK_USDTWD   = 32.5
MOCK_ASOF     = datetime(2026, 7, 20, 9, 0, 0)
PORTFOLIO_NAV = 5_000_000.0


# ─── Mock FX adapter ──────────────────────────────────────────────────────────

class MockFXAdapter(DataSourceAdapter):
    def __init__(self, rate: float = MOCK_USDTWD) -> None:
        self._rate = rate

    @property
    def source_name(self) -> str:
        return "mock_fx"

    def fetch(self, pair: str = "USDTWD", **kwargs: Any) -> SourcedData:
        return SourcedData(
            payload=FXRate(pair=pair, rate=self._rate),
            source=self.source_name,
            asof=MOCK_ASOF,
        )


# ─── Fixture builders ─────────────────────────────────────────────────────────

def _hc(tp: str, current: float, limit: float, breached: bool) -> HardConstraint:
    return HardConstraint(type=tp, current=current, limit=limit, breached=breached)


def _make_consolidated(
    net_delta_twd: float = -500_000.0,
    net_gamma: float = -0.1,
    net_vega: float = -1_000.0,
    net_theta: float = 10.0,
    excluded: list[str] | None = None,
) -> ConsolidatedTWD:
    return ConsolidatedTWD(
        net_delta_notional_twd = net_delta_twd,
        net_gamma_twd          = net_gamma,
        net_vega_twd           = net_vega,
        net_theta_twd          = net_theta,
        fx_rates               = [],
        excluded_currencies    = excluded or [],
    )


def _make_agg(
    consolidated: ConsolidatedTWD,
    hard_constraints: list[HardConstraint],
    index_exposure: list[IndexPointRecord] | None = None,
) -> AggregationResult:
    return AggregationResult(
        by_currency                  = {},
        consolidated_twd             = consolidated,
        index_point_exposure         = index_exposure or [],
        unmapped_single_name_exposure = [],
        hard_constraints             = hard_constraints,
        errors                       = [],
    )


def _make_scenario(
    index_shock: float = 0.03,
    iv_shock: float    = 0.0,
    total_pnl: float   = -300_000.0,
    unmapped: list[str] | None = None,
) -> ScenarioResult:
    row = ScenarioRow(
        index_shock   = index_shock,
        iv_shock      = iv_shock,
        legs          = [],
        agg_delta_pnl = total_pnl * 0.95,
        agg_gamma_pnl = total_pnl * 0.05,
        agg_vega_pnl  = 0.0,
        agg_theta_pnl = 1.0,
        agg_total_pnl = total_pnl,
    )
    return ScenarioResult(
        scenarios        = [row],
        days_held        = 1.0,
        index_shocks     = (index_shock,),
        iv_shocks        = (iv_shock,),
        unmapped_symbols = unmapped or [],
    )


# ─── Shared fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def minimal_positions() -> list[Position]:
    """One valid TXFF futures position — no options, no invalid rows."""
    return [
        Position(
            symbol="TXFF", instrument_type="futures",
            quantity=-2, currency="TWD", multiplier=200.0,
        )
    ]


@pytest.fixture
def greeks_map_empty() -> dict[int, GreeksResult]:
    return {}


@pytest.fixture
def mock_agg_breach() -> AggregationResult:
    """net_delta_pct_nav = -10_500_000 / 5_000_000 = -210% → breached."""
    c = _make_consolidated(net_delta_twd=-10_500_000.0)
    hcs = [
        _hc("net_delta_pct_nav", current=-2.10, limit=0.30, breached=True),
        _hc("gamma_limit",       current=0.10,  limit=1_000_000.0, breached=False),
        _hc("vega_limit",        current=1000.0, limit=500_000.0,  breached=False),
    ]
    return _make_agg(c, hcs)


@pytest.fixture
def mock_agg_ok() -> AggregationResult:
    """All three constraints within limits."""
    c = _make_consolidated(net_delta_twd=-500_000.0)
    hcs = [
        _hc("net_delta_pct_nav", current=-0.10, limit=0.30,        breached=False),
        _hc("gamma_limit",       current=0.10,  limit=1_000_000.0, breached=False),
        _hc("vega_limit",        current=1000.0, limit=500_000.0,  breached=False),
    ]
    return _make_agg(c, hcs)


@pytest.fixture
def mock_agg_no_fx() -> AggregationResult:
    """Consolidated without USD FX — excluded_currencies=['USD']."""
    c = _make_consolidated(net_delta_twd=-500_000.0, excluded=["USD"])
    hcs = [
        _hc("net_delta_pct_nav", current=-0.10, limit=0.30,        breached=False),
        _hc("gamma_limit",       current=0.10,  limit=1_000_000.0, breached=False),
        _hc("vega_limit",        current=1000.0, limit=500_000.0,  breached=False),
    ]
    return _make_agg(c, hcs)


@pytest.fixture
def mock_scenario() -> ScenarioResult:
    return _make_scenario()


# ─── Unit tests: build_risk_signal() ─────────────────────────────────────────

class TestSignalDetermination:
    def test_breach_produces_bearish(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_breach: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_breach, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.signal == Signal.BEARISH
        assert signal.has_breach()

    def test_no_breach_produces_neutral(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_ok, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.signal == Signal.NEUTRAL
        assert not signal.has_breach()

    def test_agent_type_is_risk(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_ok, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.agent == AgentType.RISK

    def test_time_horizon_is_short(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_ok, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.time_horizon == TimeHorizon.SHORT


class TestHardConstraints:
    def test_hard_constraints_carried_through(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_breach: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """hard_constraints in signal must exactly match aggregation output."""
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_breach, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.hard_constraints == mock_agg_breach.hard_constraints

    def test_exactly_three_constraints_from_aggregation(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_ok, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert len(signal.hard_constraints) == 3

    def test_gamma_breach_produces_bearish(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_scenario: ScenarioResult,
    ) -> None:
        c = _make_consolidated(net_gamma=2_000_000.0)  # > 1M limit
        hcs = [
            _hc("net_delta_pct_nav", current=-0.10, limit=0.30,        breached=False),
            _hc("gamma_limit",       current=2e6,   limit=1_000_000.0, breached=True),
            _hc("vega_limit",        current=1000.0, limit=500_000.0,  breached=False),
        ]
        agg = _make_agg(c, hcs)
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=agg, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.signal == Signal.BEARISH


class TestConfidence:
    def test_full_data_confidence_is_one(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """No FX missing, no invalid positions, no options → confidence=1.0."""
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_ok, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.confidence == pytest.approx(1.0)

    def test_fx_missing_reduces_confidence(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_no_fx: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """excluded_currencies=['USD'] → confidence reduced by 0.20."""
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_no_fx, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.confidence == pytest.approx(0.80)

    def test_invalid_position_reduces_confidence(
        self,
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """Position missing required fields → is_valid=False → −0.10."""
        bad_pos = Position(
            symbol="BAD", instrument_type="option",
            quantity=1, currency="TWD", multiplier=50.0,
            # is_valid is driven by .errors; populate it as _parse_row would
            errors=["[0] option is missing required field: strike"],
        )
        signal = build_risk_signal(
            positions=[bad_pos], greeks_map=greeks_map_empty,
            agg_result=mock_agg_ok, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.confidence == pytest.approx(0.90)

    def test_all_options_unpriced_reduces_confidence(
        self,
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """Valid option but no greeks_map entry → −0.30 (cannot evaluate convexity)."""
        opt = Position(
            symbol="TXO", instrument_type="option",
            quantity=-5, currency="TWD", multiplier=50.0,
            strike=22500.0, expiry="2026-09-16",
            option_type="call", style="european",
        )
        signal = build_risk_signal(
            positions=[opt], greeks_map={},   # no Greeks for this option
            agg_result=mock_agg_ok, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.confidence == pytest.approx(0.70)

    def test_confidence_also_in_data_quality(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_ok, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.data_quality.confidence == pytest.approx(signal.confidence)


class TestKeyEvidence:
    def test_all_evidence_has_source(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_ok, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        for ev in signal.key_evidence:
            assert ev.source != "", f"Evidence missing source: {ev.claim}"

    def test_all_evidence_has_asof(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_ok, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        for ev in signal.key_evidence:
            assert ev.asof == MOCK_ASOF, f"Evidence wrong asof: {ev.claim}"

    def test_at_least_five_evidence_items(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_ok, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert len(signal.key_evidence) >= 5


class TestMetrics:
    @pytest.fixture(autouse=True)
    def _signal(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        self.signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_ok, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )

    def test_required_keys_present(self) -> None:
        required = {
            "net_delta_notional_twd", "net_delta_pct_nav",
            "net_gamma_twd", "net_vega_twd", "net_theta_twd",
            "txf_lot_equivalent", "scenario_worst_pnl_twd",
            "iv_source", "positions_count", "consolidation_complete",
        }
        assert required.issubset(self.signal.metrics.keys())

    def test_net_delta_pct_matches_consolidation(self) -> None:
        # Verify the calculation: -500_000 / 5_000_000 = -0.10
        assert self.signal.metrics["net_delta_pct_nav"] == pytest.approx(-0.10, rel=1e-6)

    def test_iv_source_is_placeholder(self) -> None:
        assert "placeholder" in str(self.signal.metrics["iv_source"])

    def test_scenario_coverage_mentions_index_symbols(self) -> None:
        coverage = str(self.signal.metrics.get("scenario_coverage", ""))
        assert "TXFF" in coverage or "INDEX_DERIVATIVE" in coverage

    def test_worst_pnl_is_float(self) -> None:
        assert isinstance(self.signal.metrics["scenario_worst_pnl_twd"], float)


class TestNarrativeContract:
    """Verify _build_narrative produces qualitative text, no raw numbers."""

    def _get_signal(
        self,
        positions: list[Position],
        agg: AggregationResult,
        scenario: ScenarioResult,
    ) -> AgentSignal:
        return build_risk_signal(
            positions=positions, greeks_map={},
            agg_result=agg, scenario_result=scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )

    def test_narrative_is_nonempty_string(
        self,
        minimal_positions: list[Position],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        signal = self._get_signal(minimal_positions, mock_agg_ok, mock_scenario)
        assert isinstance(signal.narrative, str)
        assert len(signal.narrative) > 10

    def test_breach_narrative_mentions_breach(
        self,
        minimal_positions: list[Position],
        mock_agg_breach: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        signal = self._get_signal(minimal_positions, mock_agg_breach, mock_scenario)
        assert "觸限" in signal.narrative or "BREACHED" in signal.narrative.upper()

    def test_unmapped_mentioned_in_narrative(
        self,
        minimal_positions: list[Position],
        mock_agg_ok: AggregationResult,
    ) -> None:
        sc = _make_scenario(unmapped=["AAPL", "2330.TW"])
        signal = self._get_signal(minimal_positions, mock_agg_ok, sc)
        assert "AAPL" in signal.narrative or "2330" in signal.narrative


# ─── Integration tests: run_risk_agent() ─────────────────────────────────────

class TestRunRiskAgent:
    def test_returns_agent_signal(self) -> None:
        signal = run_risk_agent(asof=MOCK_ASOF)
        assert isinstance(signal, AgentSignal)

    def test_agent_type_is_risk(self) -> None:
        signal = run_risk_agent(asof=MOCK_ASOF)
        assert signal.agent == AgentType.RISK

    def test_target_symbol_is_portfolio(self) -> None:
        signal = run_risk_agent(asof=MOCK_ASOF)
        assert signal.target.symbol == "PORTFOLIO"

    def test_with_mock_fx_higher_confidence_than_without(self) -> None:
        """With FX adapter, USD positions are consolidated → higher confidence."""
        sig_with_fx    = run_risk_agent(fx_adapter=MockFXAdapter(), asof=MOCK_ASOF)
        sig_without_fx = run_risk_agent(fx_adapter=None,            asof=MOCK_ASOF)
        assert sig_with_fx.confidence >= sig_without_fx.confidence

    def test_hard_constraints_present(self) -> None:
        signal = run_risk_agent(asof=MOCK_ASOF)
        assert len(signal.hard_constraints) > 0

    def test_three_standard_constraints(self) -> None:
        """aggregation always emits exactly 3 standard constraints."""
        signal = run_risk_agent(fx_adapter=MockFXAdapter(), asof=MOCK_ASOF)
        types = {hc.type for hc in signal.hard_constraints}
        assert "net_delta_pct_nav" in types
        assert "gamma_limit"       in types
        assert "vega_limit"        in types

    def test_key_evidence_all_sourced(self) -> None:
        signal = run_risk_agent(asof=MOCK_ASOF)
        for ev in signal.key_evidence:
            assert ev.source != ""
            assert ev.asof is not None

    def test_metrics_has_scenario_worst_pnl(self) -> None:
        signal = run_risk_agent(asof=MOCK_ASOF)
        assert "scenario_worst_pnl_twd" in signal.metrics

    def test_signal_in_valid_enum(self) -> None:
        signal = run_risk_agent(asof=MOCK_ASOF)
        from schemas.agent_signal import Signal as Sig
        assert signal.signal in {Sig.BEARISH, Sig.NEUTRAL, Sig.BULLISH}

    def test_errors_list_is_list(self) -> None:
        signal = run_risk_agent(asof=MOCK_ASOF)
        assert isinstance(signal.errors, list)

    def test_positions_yaml_delta_breach(self) -> None:
        """
        positions.yaml contains 2330.TW (large TWD long), TXFF (short 2 lots),
        TXO short call, etc.  The net delta is expected to breach 30 % of NAV
        (as confirmed by verify_phase2_aggregation.py Step 4 output).
        With or without FX, the TWD positions alone are enough to breach.
        """
        signal = run_risk_agent(fx_adapter=MockFXAdapter(), asof=MOCK_ASOF)
        delta_hc = next(
            (hc for hc in signal.hard_constraints if hc.type == "net_delta_pct_nav"),
            None,
        )
        assert delta_hc is not None, "net_delta_pct_nav constraint missing"
        # The verify script confirms breach at -210%; assert it's breached here too.
        assert delta_hc.breached, (
            f"Expected net_delta_pct_nav to be breached "
            f"(current={delta_hc.current:.2%}, limit=±{delta_hc.limit:.0%}) "
            "but was not. Check positions.yaml or portfolio_nav."
        )
