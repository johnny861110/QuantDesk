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
def mock_agg_balanced() -> AggregationResult:
    """Delta-neutral portfolio (net_delta=0) — NEUTRAL signal, no breach."""
    c = _make_consolidated(net_delta_twd=0.0)
    hcs = [
        _hc("net_delta_pct_nav", current=0.0,   limit=0.30,        breached=False),
        _hc("gamma_limit",       current=0.10,  limit=1_000_000.0, breached=False),
        _hc("vega_limit",        current=1000.0, limit=500_000.0,  breached=False),
    ]
    return _make_agg(c, hcs)


@pytest.fixture
def mock_agg_net_long() -> AggregationResult:
    """Net long portfolio (net_delta=+500K / +10% NAV) — BULLISH signal, no breach."""
    c = _make_consolidated(net_delta_twd=+500_000.0)
    hcs = [
        _hc("net_delta_pct_nav", current=+0.10, limit=0.30,        breached=False),
        _hc("gamma_limit",       current=0.10,  limit=1_000_000.0, breached=False),
        _hc("vega_limit",        current=1000.0, limit=500_000.0,  breached=False),
    ]
    return _make_agg(c, hcs)


@pytest.fixture
def mock_scenario() -> ScenarioResult:
    return _make_scenario()


# ─── Unit tests: build_risk_signal() ─────────────────────────────────────────

class TestSignalDetermination:
    """Signal reflects net delta direction, independent of hard_constraint breach status."""

    def test_net_short_large_produces_bearish(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_breach: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """net_delta_pct_nav = -210% → directionally BEARISH."""
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_breach, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.signal == Signal.BEARISH

    def test_delta_neutral_produces_neutral(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_balanced: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """net_delta=0 → NEUTRAL regardless of breach status."""
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_balanced, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.signal == Signal.NEUTRAL
        assert not signal.has_breach()

    def test_net_long_produces_bullish(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_net_long: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """net_delta_pct_nav = +10% → directionally BULLISH."""
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_net_long, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.signal == Signal.BULLISH
        assert not signal.has_breach()

    def test_breach_does_not_contaminate_directional_signal(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_scenario: ScenarioResult,
    ) -> None:
        """
        A gamma breach on a NET LONG portfolio must NOT flip signal to BEARISH.
        signal = BULLISH (from delta), has_breach() = True (from hard_constraints).
        These are independent dimensions; Supervisor Phase 5 handles the rule engine.
        """
        c = _make_consolidated(net_delta_twd=+500_000.0, net_gamma=2_000_000.0)
        hcs = [
            _hc("net_delta_pct_nav", current=+0.10, limit=0.30,        breached=False),
            _hc("gamma_limit",       current=2e6,   limit=1_000_000.0, breached=True),
            _hc("vega_limit",        current=1000.0, limit=500_000.0,  breached=False),
        ]
        agg = _make_agg(c, hcs)
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=agg, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.signal == Signal.BULLISH   # directional — net long
        assert signal.has_breach()               # breach is independently recorded

    def test_agent_type_is_risk(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_balanced: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_balanced, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        assert signal.agent == AgentType.RISK

    def test_time_horizon_is_short(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_balanced: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_balanced, scenario_result=mock_scenario,
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

    def test_gamma_breach_recorded_independently_of_signal(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_scenario: ScenarioResult,
    ) -> None:
        """Gamma breach is recorded in hard_constraints regardless of signal direction."""
        c = _make_consolidated(net_gamma=2_000_000.0)  # > 1M limit; delta default -500K
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
        # Breach is in hard_constraints; signal is directional (BEARISH from -10% delta)
        assert signal.has_breach()
        gamma_hc = next(hc for hc in signal.hard_constraints if hc.type == "gamma_limit")
        assert gamma_hc.breached
        assert signal.signal == Signal.BEARISH  # delta-driven, not breach-driven


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

    def test_covered_symbols_present_in_metrics(self) -> None:
        assert "covered_symbols" in self.signal.metrics

    def test_covered_symbols_is_list(self) -> None:
        assert isinstance(self.signal.metrics["covered_symbols"], list)


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


# ─── Unit tests: narrative Verifier guard ────────────────────────────────────

class TestNarrativeVerifier:
    """
    Shared agents.verifier.check_narrative is used by both risk_agent and
    fundamental_agent.  These tests exercise it at the shared-module level and
    verify it is wired into build_risk_signal().
    """

    def test_clean_narrative_passes(self) -> None:
        """No numbers in narrative → no Verifier errors."""
        from agents.verifier import check_narrative
        narrative = "組合淨 delta 空頭偏重，淨 gamma 偏空，所有風控限制均在範圍內。"
        errors = check_narrative(narrative, {"net_delta_pct_nav": -0.10})
        assert errors == []

    def test_rogue_number_flagged(self) -> None:
        """Number in narrative that is NOT in metrics → Verifier error emitted."""
        from agents.verifier import check_narrative
        # Use a 3-digit number the regex can catch (regex matches up to 3 digits without commas)
        narrative = "組合淨 delta 空頭偏重，損益約 -999 元。"
        errors = check_narrative(narrative, {"net_delta_pct_nav": -0.10})
        assert len(errors) > 0
        assert "[Verifier]" in errors[0]

    def test_build_risk_signal_narrative_is_clean(
        self,
        minimal_positions: list[Position],
        greeks_map_empty: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """
        _build_narrative() is deterministic and intentionally number-free.
        Verify that build_risk_signal() sees no Verifier errors from the
        current narrative (i.e., the Verifier guard is wired and the
        deterministic narrative is clean).
        """
        signal = build_risk_signal(
            positions=minimal_positions, greeks_map=greeks_map_empty,
            agg_result=mock_agg_ok, scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV, asof=MOCK_ASOF,
        )
        verifier_errors = [e for e in signal.errors if "[Verifier]" in e]
        assert verifier_errors == [], (
            f"Deterministic narrative triggered Verifier: {verifier_errors}"
        )


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

    def test_covered_symbols_in_metrics(self) -> None:
        signal = run_risk_agent(asof=MOCK_ASOF)
        assert "covered_symbols" in signal.metrics
        syms = signal.metrics["covered_symbols"]
        assert isinstance(syms, list)
        assert len(syms) > 0  # positions.yaml has at least one valid position


# ─── Phase 6 P0-2: IV missing fail-safe ──────────────────────────────────────

class TestIVMissingFailSafe:
    """
    Phase 6 P0-2: 部分 IV 缺失的 fail-safe 行為。

    Setup: 1 個期貨（TXFF）+ 2 個選擇權（TXO），只有第 1 個選擇權有 greeks_map
    → n_options=2, n_priced=1, missing_fraction=0.5
    → confidence deduction = 0.30 × 0.5 = 0.15
    → confidence = 1.0 − 0.15 = 0.85
    """

    @pytest.fixture
    def _partial_iv_positions(self) -> list[Position]:
        """1 futures + 2 options."""
        return [
            Position(
                symbol="TXFF", instrument_type="futures",
                quantity=-2, currency="TWD", multiplier=200.0,
            ),
            Position(
                symbol="TXO", instrument_type="option",
                quantity=-5, currency="TWD", multiplier=50.0,
                strike=22500.0, expiry="2026-09-16",
                option_type="call", style="european",
            ),
            Position(
                symbol="TXO", instrument_type="option",
                quantity=3, currency="TWD", multiplier=50.0,
                strike=22000.0, expiry="2026-09-16",
                option_type="put", style="european",
            ),
        ]

    @pytest.fixture
    def _partial_greeks_map(self) -> dict[int, GreeksResult]:
        """Only position index 1 (first TXO) is priced."""
        from agents.risk.pricing_router import GreeksResult
        return {
            1: GreeksResult(
                price=150.0, delta=-0.35, gamma=0.0001,
                vega=3.0, theta=-5.0, rho=-0.01,
                model="black_scholes", iv=0.20,
            )
        }

    def test_partial_iv_failure_reduces_confidence_proportionally(
        self,
        _partial_iv_positions: list[Position],
        _partial_greeks_map: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """1/2 options priced → missing_fraction=0.5 → conf = 1.0 − 0.15 = 0.85."""
        signal = build_risk_signal(
            positions=_partial_iv_positions,
            greeks_map=_partial_greeks_map,
            agg_result=mock_agg_ok,
            scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV,
            asof=MOCK_ASOF,
        )
        # missing_fraction = (2 - 1) / 2 = 0.5 → penalty = 0.30 × 0.5 = 0.15
        assert signal.confidence == pytest.approx(1.0 - 0.30 * 0.5, rel=1e-6)

    def test_partial_iv_failure_annotates_hard_constraints(
        self,
        _partial_iv_positions: list[Position],
        _partial_greeks_map: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """Every hard constraint detail should carry the IV-missing warning."""
        signal = build_risk_signal(
            positions=_partial_iv_positions,
            greeks_map=_partial_greeks_map,
            agg_result=mock_agg_ok,
            scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV,
            asof=MOCK_ASOF,
        )
        for hc in signal.hard_constraints:
            assert hc.detail is not None
            assert "IV缺失" in hc.detail, (
                f"Expected IV-missing note in constraint {hc.type!r} detail: {hc.detail!r}"
            )
            assert "1/2" in hc.detail, (
                f"Expected '1/2' count in constraint {hc.type!r} detail: {hc.detail!r}"
            )

    def test_partial_iv_failure_sets_verifiable_false(
        self,
        _partial_iv_positions: list[Position],
        _partial_greeks_map: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """Constraints must be machine-readable as unverifiable, not just text-annotated."""
        signal = build_risk_signal(
            positions=_partial_iv_positions,
            greeks_map=_partial_greeks_map,
            agg_result=mock_agg_ok,
            scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV,
            asof=MOCK_ASOF,
        )
        for hc in signal.hard_constraints:
            assert hc.verifiable is False, (
                f"Constraint {hc.type!r} should have verifiable=False when IV is missing; "
                f"got verifiable={hc.verifiable!r}"
            )

    def test_iv_missing_count_in_metrics(
        self,
        _partial_iv_positions: list[Position],
        _partial_greeks_map: dict[int, GreeksResult],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """metrics['iv_missing_count'] must equal n_options − n_priced."""
        signal = build_risk_signal(
            positions=_partial_iv_positions,
            greeks_map=_partial_greeks_map,
            agg_result=mock_agg_ok,
            scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV,
            asof=MOCK_ASOF,
        )
        assert "iv_missing_count" in signal.metrics
        assert signal.metrics["iv_missing_count"] == 1  # 2 options, 1 priced

    def test_all_iv_priced_no_annotation(
        self,
        _partial_iv_positions: list[Position],
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """When all options are priced, no IV-missing annotation and no penalty."""
        from agents.risk.pricing_router import GreeksResult
        full_greeks = {
            1: GreeksResult(
                price=150.0, delta=-0.35, gamma=0.0001,
                vega=3.0, theta=-5.0, rho=-0.01,
                model="black_scholes", iv=0.20,
            ),
            2: GreeksResult(
                price=80.0, delta=0.25, gamma=0.0001,
                vega=2.5, theta=-4.0, rho=0.01,
                model="black_scholes", iv=0.20,
            ),
        }
        signal = build_risk_signal(
            positions=_partial_iv_positions,
            greeks_map=full_greeks,
            agg_result=mock_agg_ok,
            scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV,
            asof=MOCK_ASOF,
        )
        assert signal.metrics["iv_missing_count"] == 0
        assert signal.confidence == pytest.approx(1.0)
        for hc in signal.hard_constraints:
            assert "IV缺失" not in (hc.detail or "")
            assert hc.verifiable is True


# ─── Phase 6 P0-2: IV missing confidence — 連續性與單調性邊界測試 ────────────

class TestIVMissingConfidenceMonotonicity:
    """
    _IV_MISSING_CONFIDENCE_PENALTY × missing_fraction 的邊界測試。

    目的：
      若「部分失敗」和「完全失敗」是兩條獨立 if 分支（例如 if n_priced==0:
      conf -= 0.30 else: conf -= 0.20），在 0.9 → 1.0 的邊界會出現不連續
      跳動（gap≈0.10 而非期望的 0.03）。
      單一線性公式覆蓋全範圍才能保證連續性，這組測試正是驗證此不變式。

    Baseline：無 FX 缺失、無無效部位 → 唯一懲罰來自 IV 缺失。
      conf = max(0.10, 1.0 − 0.30 × missing_fraction)
    """

    _MOCK_GR: dict  = {}   # filled in _conf() to avoid module-level import

    def _conf(
        self,
        n_options: int,
        n_priced: int,
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> float:
        """Build n_options option positions; put first n_priced into greeks_map."""
        from agents.risk.pricing_router import GreeksResult

        positions = [
            Position(
                symbol="TXO", instrument_type="option",
                quantity=-1, currency="TWD", multiplier=50.0,
                strike=22500.0, expiry="2026-09-16",
                option_type="call", style="european",
            )
            for _ in range(n_options)
        ]
        mock_gr = GreeksResult(
            price=100.0, delta=-0.30, gamma=0.0001,
            vega=2.0, theta=-4.0, rho=-0.01,
            model="black_scholes", iv=0.20,
        )
        greeks_map = {i: mock_gr for i in range(n_priced)}

        signal = build_risk_signal(
            positions=positions,
            greeks_map=greeks_map,
            agg_result=mock_agg_ok,
            scenario_result=mock_scenario,
            portfolio_nav=PORTFOLIO_NAV,
            asof=MOCK_ASOF,
        )
        return signal.confidence

    def test_monotonically_decreasing(
        self,
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """missing_fraction 0→0.1→0.5→0.9→1.0：confidence 嚴格單調遞減。"""
        # (n_options, n_priced) → missing_fraction
        cases = [
            (10, 10),  # missing_fraction = 0.0
            (10,  9),  # missing_fraction = 0.1
            ( 2,  1),  # missing_fraction = 0.5
            (10,  1),  # missing_fraction = 0.9
            (10,  0),  # missing_fraction = 1.0 (complete failure)
        ]
        confidences = [self._conf(n, k, mock_agg_ok, mock_scenario) for n, k in cases]
        for i in range(len(confidences) - 1):
            frac_lo = 1 - cases[i][1] / cases[i][0]
            frac_hi = 1 - cases[i + 1][1] / cases[i + 1][0]
            assert confidences[i] > confidences[i + 1], (
                f"confidence not strictly decreasing at step {i}→{i+1}: "
                f"conf({frac_lo:.1f})={confidences[i]:.4f} vs "
                f"conf({frac_hi:.1f})={confidences[i + 1]:.4f}"
            )

    def test_no_discontinuity_at_complete_failure_boundary(
        self,
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """最關鍵邊界：partial(9/10) → complete(10/10) 的 gap 必須等於 0.30×(1/10)=0.03。

        若舊的 if n_priced==0: conf -= 0.30 分支殘留，gap 會是 0.30（十倍跳動）。
        若 partial 用獨立固定扣分（如 -0.20），gap 也會是 0.10（非 0.03）。
        唯一正確的 gap 是 _IV_MISSING_CONFIDENCE_PENALTY / n_options = 0.30/10 = 0.03。
        """
        n = 10
        conf_partial  = self._conf(n, 1, mock_agg_ok, mock_scenario)   # 9/10 missing
        conf_complete = self._conf(n, 0, mock_agg_ok, mock_scenario)   # 10/10 missing

        assert conf_partial > conf_complete, (
            f"partial failure conf ({conf_partial:.4f}) must exceed "
            f"complete failure conf ({conf_complete:.4f})"
        )
        gap = conf_partial - conf_complete
        assert gap == pytest.approx(0.30 / n, rel=1e-6), (
            f"Gap at 9/10→10/10 boundary should be 0.30×(1/10)=0.030 (continuous linear). "
            f"Got gap={gap:.4f}. "
            f"If gap≈0.30, old flat-penalty branch is still firing. "
            f"If gap≈0.10, partial and complete failure use different base penalties."
        )

    def test_exact_values_at_each_fraction(
        self,
        mock_agg_ok: AggregationResult,
        mock_scenario: ScenarioResult,
    ) -> None:
        """精確數值驗證（baseline：無其他懲罰，conf = 1.0 - 0.30 × missing_fraction）。"""
        cases = [
            # (n_options, n_priced, missing_fraction, expected_confidence)
            (10, 10, 0.00, 1.00),   # all priced: zero penalty
            (10,  9, 0.10, 0.97),   # −0.30×0.1 = −0.03
            ( 2,  1, 0.50, 0.85),   # −0.30×0.5 = −0.15
            (10,  1, 0.90, 0.73),   # −0.30×0.9 = −0.27
            (10,  0, 1.00, 0.70),   # −0.30×1.0 = −0.30  (complete failure)
        ]
        for n_opt, n_pr, frac, expected in cases:
            actual = self._conf(n_opt, n_pr, mock_agg_ok, mock_scenario)
            assert actual == pytest.approx(expected, abs=1e-9), (
                f"missing_fraction={frac}: expected conf={expected}, "
                f"got {actual:.6f} (n={n_opt}, priced={n_pr})"
            )
