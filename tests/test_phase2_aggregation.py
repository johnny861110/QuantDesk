"""
Tests for agents/risk/aggregation.py

Scenario used throughout
------------------------
Four synthetic positions, all with explicit spot prices and pre-built GreeksResult
objects — no network calls, no BS/binomial pricing.

  idx 0: "2330.TW" stock  qty=+100, mult=1,   ccy=TWD, spot=100
           → delta=1, delta_notional = +10 000 TWD  (unmapped)
  idx 1: "TXFF"   futures qty=-1,   mult=200,  ccy=TWD, spot=1000
           → delta=1, delta_notional = -200 000 TWD  (index)
           → delta_points = 1 × (-1) × 200 = -200
  idx 2: "TXO"    call opt qty=-2,  mult=50,   ccy=TWD, spot=1000, delta=0.40
           → delta_notional = 0.4 × (-2) × 50 × 1000 = -40 000 TWD  (index)
           → delta_points = 0.4 × (-2) × 50 = -40
  idx 3: "AAPL"   stock   qty=+10,  mult=1,   ccy=USD, spot=200
           → delta=1, delta_notional = +2 000 USD  (unmapped)

Mock FX: USDTWD = 32.5
portfolio_nav = 1 000 000 TWD

Expected results
----------------
by_currency:
  TWD: net_delta_notional = 10 000 - 200 000 - 40 000 = -230 000
  USD: net_delta_notional = +2 000

consolidated_twd:
  = -230 000 + 2 000 × 32.5 = -230 000 + 65 000 = -165 000 TWD

index_point_exposure (TAIEX):
  net_delta_points = -200 + (-40) = -240
  net_delta_notional_twd = -240 × 1 000 = -240 000 TWD

unmapped_single_name:
  ("2330.TW", TWD) → 10 000 TWD
  ("AAPL",    USD) → 2 000 USD

net_delta_pct_nav = -165 000 / 1 000 000 = -16.5 %  → NOT breached (< 30 %)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from adapters.base import DataSourceAdapter, SourcedData
from adapters.fx_adapter import FXRate
from agents.risk.aggregation import (
    GAMMA_LIMIT_TWD,
    NET_DELTA_PCT_NAV_LIMIT,
    TAIEX_UNDERLYING,
    UNMAPPED_BETA_NOTE,
    VEGA_LIMIT_TWD,
    AggregationResult,
    aggregate,
)
from agents.risk.position_loader import Position
from agents.risk.pricing_router import GreeksResult


# ─── Test doubles ─────────────────────────────────────────────────────────────

MOCK_USDTWD   = 32.5
MOCK_ASOF     = datetime(2026, 7, 19, 12, 0, 0)
PORTFOLIO_NAV = 1_000_000.0


class MockFXAdapter(DataSourceAdapter):
    """Returns a fixed USDTWD rate — no network call."""

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


class FailingFXAdapter(DataSourceAdapter):
    """Simulates a network failure."""

    @property
    def source_name(self) -> str:
        return "failing_fx"

    def fetch(self, **kwargs: Any) -> SourcedData:
        raise ConnectionError("simulated FX fetch failure")


# ─── Shared fixtures ──────────────────────────────────────────────────────────

def _stock(symbol: str, qty: float, spot: float, ccy: str = "TWD") -> Position:
    return Position(symbol=symbol, instrument_type="stock",
                    quantity=qty, currency=ccy, multiplier=1.0)


def _futures(symbol: str, qty: float, mult: float, spot: float) -> Position:
    return Position(symbol=symbol, instrument_type="futures",
                    quantity=qty, currency="TWD", multiplier=mult)


def _option(symbol: str, qty: float, mult: float, ccy: str = "TWD") -> Position:
    return Position(
        symbol=symbol, instrument_type="option",
        quantity=qty, currency=ccy, multiplier=mult,
        strike=1000.0, expiry="2026-09-16", option_type="call", style="european",
    )


def _greeks(delta: float, gamma: float = 0.0, vega: float = 0.0,
            theta: float = 0.0) -> GreeksResult:
    return GreeksResult(
        price=0.0, delta=delta, gamma=gamma, vega=vega, theta=theta, rho=0.0,
        model="mock", iv=0.20,
    )


SPOT_MAP = {
    "2330.TW": 100.0,
    "TXFF":    1000.0,
    "TXO":     1000.0,
    "AAPL":    200.0,
}


@pytest.fixture
def base_positions() -> list[Position]:
    return [
        _stock("2330.TW", qty=100,  spot=100,  ccy="TWD"),   # idx 0
        _futures("TXFF",  qty=-1,   mult=200,  spot=1000),    # idx 1
        _option("TXO",    qty=-2,   mult=50,   ccy="TWD"),    # idx 2
        _stock("AAPL",    qty=10,   spot=200,  ccy="USD"),    # idx 3
    ]


@pytest.fixture
def base_greeks() -> dict[int, GreeksResult]:
    # idx 2 (TXO call) gets delta=0.40, small gamma/vega/theta for constraint tests
    return {2: _greeks(delta=0.40, gamma=0.002, vega=10.0, theta=-0.05)}


@pytest.fixture
def result(base_positions, base_greeks) -> AggregationResult:
    return aggregate(
        positions=base_positions,
        greeks_map=base_greeks,
        spot_map=SPOT_MAP,
        portfolio_nav=PORTFOLIO_NAV,
        fx_adapter=MockFXAdapter(),
    )


# ─── Layer ①: by_currency ─────────────────────────────────────────────────────

class TestByCurrency:
    def test_has_twd_and_usd_keys(self, result):
        assert "TWD" in result.by_currency
        assert "USD" in result.by_currency

    def test_twd_net_delta_notional(self, result):
        # 10 000 (2330) - 200 000 (TXFF) - 40 000 (TXO) = -230 000
        assert result.by_currency["TWD"].net_delta_notional == pytest.approx(-230_000.0)

    def test_usd_net_delta_notional(self, result):
        # 1.0 × 10 × 1 × 200 = 2 000
        assert result.by_currency["USD"].net_delta_notional == pytest.approx(2_000.0)

    def test_twd_net_gamma_comes_from_option(self, result):
        # gamma × qty × mult = 0.002 × (-2) × 50 = -0.2
        assert result.by_currency["TWD"].net_gamma == pytest.approx(-0.2)

    def test_twd_net_vega_comes_from_option(self, result):
        # vega × qty × mult = 10.0 × (-2) × 50 = -1 000
        assert result.by_currency["TWD"].net_vega == pytest.approx(-1_000.0)

    def test_twd_net_theta_comes_from_option(self, result):
        # theta × qty × mult = -0.05 × (-2) × 50 = +5.0 (sold option → positive theta)
        assert result.by_currency["TWD"].net_theta == pytest.approx(5.0)

    def test_currency_subtotal_has_correct_currency_field(self, result):
        assert result.by_currency["TWD"].currency == "TWD"
        assert result.by_currency["USD"].currency == "USD"

    def test_twd_only_portfolio_no_usd_key(self, base_positions, base_greeks):
        twd_only = [p for p in base_positions if p.currency == "TWD"]
        r = aggregate(twd_only, base_greeks, SPOT_MAP, PORTFOLIO_NAV)
        assert "USD" not in r.by_currency


# ─── Layer ②: consolidated_twd ────────────────────────────────────────────────

class TestConsolidatedTWD:
    def test_not_none_when_fx_adapter_provided(self, result):
        assert result.consolidated_twd is not None

    def test_net_delta_notional_twd(self, result):
        # -230 000 + 2 000 × 32.5 = -230 000 + 65 000 = -165 000
        assert result.consolidated_twd.net_delta_notional_twd == pytest.approx(-165_000.0)

    def test_net_vega_twd(self, result):
        # TWD vega = -1 000; USD vega = 0; total = -1 000 × 1.0 = -1 000 TWD
        assert result.consolidated_twd.net_vega_twd == pytest.approx(-1_000.0)

    def test_net_theta_twd(self, result):
        # TWD theta = +5.0; USD theta = 0
        assert result.consolidated_twd.net_theta_twd == pytest.approx(5.0)

    def test_fx_rate_snapshot_pair(self, result):
        assert len(result.consolidated_twd.fx_rates) == 1
        snap = result.consolidated_twd.fx_rates[0]
        assert snap.pair == "USDTWD"

    def test_fx_rate_snapshot_rate(self, result):
        snap = result.consolidated_twd.fx_rates[0]
        assert snap.rate == pytest.approx(MOCK_USDTWD)

    def test_fx_rate_snapshot_source(self, result):
        snap = result.consolidated_twd.fx_rates[0]
        assert snap.source == "mock_fx"

    def test_fx_rate_snapshot_asof(self, result):
        snap = result.consolidated_twd.fx_rates[0]
        assert snap.asof == MOCK_ASOF

    def test_usd_excluded_when_no_fx_adapter(self, base_positions, base_greeks):
        """USD positions excluded (not None) when no FX adapter — fail-safe consolidation."""
        r = aggregate(base_positions, base_greeks, SPOT_MAP, PORTFOLIO_NAV, fx_adapter=None)
        assert r.consolidated_twd is not None
        assert "USD" in r.consolidated_twd.excluded_currencies

    def test_partial_twd_when_no_fx_adapter(self, base_positions, base_greeks):
        """TWD positions still consolidated even without USD FX rate."""
        r = aggregate(base_positions, base_greeks, SPOT_MAP, PORTFOLIO_NAV, fx_adapter=None)
        # Only TWD positions contribute: 10 000 - 200 000 - 40 000 = -230 000
        assert r.consolidated_twd.net_delta_notional_twd == pytest.approx(-230_000.0)

    def test_error_logged_when_no_fx_adapter_and_usd_positions(self, base_positions, base_greeks):
        r = aggregate(base_positions, base_greeks, SPOT_MAP, PORTFOLIO_NAV, fx_adapter=None)
        assert any("USD" in e for e in r.errors)

    def test_usd_excluded_when_fx_adapter_fails(self, base_positions, base_greeks):
        """USD positions excluded but consolidated_twd still produced when FX fetch fails."""
        r = aggregate(base_positions, base_greeks, SPOT_MAP, PORTFOLIO_NAV,
                      fx_adapter=FailingFXAdapter())
        assert r.consolidated_twd is not None
        assert "USD" in r.consolidated_twd.excluded_currencies

    def test_error_logged_when_fx_adapter_fails(self, base_positions, base_greeks):
        r = aggregate(base_positions, base_greeks, SPOT_MAP, PORTFOLIO_NAV,
                      fx_adapter=FailingFXAdapter())
        assert any("FX rate fetch failed" in e for e in r.errors)

    def test_is_complete_true_when_all_currencies_converted(self, result):
        assert result.consolidated_twd.is_complete

    def test_is_complete_false_when_currency_excluded(self, base_positions, base_greeks):
        r = aggregate(base_positions, base_greeks, SPOT_MAP, PORTFOLIO_NAV, fx_adapter=None)
        assert not r.consolidated_twd.is_complete

    def test_twd_only_consolidated_without_fx_adapter(self, base_positions, base_greeks):
        """TWD-only portfolio can be consolidated even without a FX adapter."""
        twd_only = [p for p in base_positions if p.currency == "TWD"]
        r = aggregate(twd_only, base_greeks, SPOT_MAP, PORTFOLIO_NAV, fx_adapter=None)
        assert r.consolidated_twd is not None
        assert r.consolidated_twd.net_delta_notional_twd == pytest.approx(-230_000.0)
        assert r.consolidated_twd.fx_rates == []   # no FX rate needed


# ─── Layer ③a: index_point_exposure ──────────────────────────────────────────

class TestIndexPointExposure:
    def test_has_one_taiex_record(self, result):
        assert len(result.index_point_exposure) == 1
        assert result.index_point_exposure[0].underlying == TAIEX_UNDERLYING

    def test_contributing_symbols(self, result):
        rec = result.index_point_exposure[0]
        assert "TXFF" in rec.contributing_symbols
        assert "TXO"  in rec.contributing_symbols

    def test_net_dollar_delta_per_point(self, result):
        # TXFF: 1.0 × (-1) × 200 = -200  (TWD per 1-TAIEX-point)
        # TXO:  0.4 × (-2) × 50  = -40
        # total: -240 TWD/point
        rec = result.index_point_exposure[0]
        assert rec.net_dollar_delta_per_point == pytest.approx(-240.0)

    def test_txf_lot_equivalent(self, result):
        # -240 TWD/point ÷ 200 TWD/point/lot = -1.2 lots
        rec = result.index_point_exposure[0]
        assert rec.txf_lot_equivalent == pytest.approx(-1.2)

    def test_spot_index(self, result):
        assert result.index_point_exposure[0].spot_index == pytest.approx(1000.0)

    def test_net_delta_notional_twd_consistent_with_points(self, result):
        rec = result.index_point_exposure[0]
        assert rec.net_delta_notional_twd == pytest.approx(
            rec.net_dollar_delta_per_point * rec.spot_index
        )

    def test_net_delta_notional_twd_value(self, result):
        assert result.index_point_exposure[0].net_delta_notional_twd == pytest.approx(-240_000.0)

    def test_method_is_exact(self, result):
        assert result.index_point_exposure[0].method == "exact"

    def test_empty_when_no_index_derivatives(self):
        positions = [_stock("2330.TW", qty=100, spot=100)]
        r = aggregate(positions, {}, {"2330.TW": 100.0}, PORTFOLIO_NAV)
        assert r.index_point_exposure == []


# ─── Layer ③b: unmapped_single_name_exposure ─────────────────────────────────

class TestUnmappedSingleName:
    def test_has_two_entries(self, result):
        symbols = {e.symbol for e in result.unmapped_single_name_exposure}
        assert "2330.TW" in symbols
        assert "AAPL"    in symbols

    def test_tsmc_notional(self, result):
        tsmc = next(e for e in result.unmapped_single_name_exposure if e.symbol == "2330.TW")
        assert tsmc.net_delta_notional == pytest.approx(10_000.0)
        assert tsmc.currency == "TWD"

    def test_aapl_notional(self, result):
        aapl = next(e for e in result.unmapped_single_name_exposure if e.symbol == "AAPL")
        assert aapl.net_delta_notional == pytest.approx(2_000.0)
        assert aapl.currency == "USD"

    def test_note_references_phase_3(self, result):
        for entry in result.unmapped_single_name_exposure:
            assert "Phase 3" in entry.note

    def test_note_text(self, result):
        for entry in result.unmapped_single_name_exposure:
            assert entry.note == UNMAPPED_BETA_NOTE

    def test_txo_txff_not_in_unmapped(self, result):
        symbols = {e.symbol for e in result.unmapped_single_name_exposure}
        assert "TXO"  not in symbols
        assert "TXFF" not in symbols


# ─── Hard constraints ─────────────────────────────────────────────────────────

class TestHardConstraints:
    def test_three_constraints_produced(self, result):
        assert len(result.hard_constraints) == 3

    def test_constraint_types(self, result):
        types = {c.type for c in result.hard_constraints}
        assert types == {"net_delta_pct_nav", "gamma_limit", "vega_limit"}

    def test_net_delta_pct_nav_current(self, result):
        c = next(c for c in result.hard_constraints if c.type == "net_delta_pct_nav")
        # -165 000 / 1 000 000 = -0.165
        assert c.current == pytest.approx(-0.165)

    def test_net_delta_pct_nav_limit(self, result):
        c = next(c for c in result.hard_constraints if c.type == "net_delta_pct_nav")
        assert c.limit == pytest.approx(NET_DELTA_PCT_NAV_LIMIT)

    def test_net_delta_pct_nav_not_breached(self, result):
        c = next(c for c in result.hard_constraints if c.type == "net_delta_pct_nav")
        # |-16.5%| < 30% → not breached
        assert not c.breached

    def test_net_delta_pct_nav_breached_when_large(self, base_positions, base_greeks):
        """Scale up the futures short so |delta%| > 30%."""
        # TXFF qty=-3 → delta_notional = -600 000 TWD
        # consolidated: (-600 000 + 10 000) + 65 000 = -525 000
        # delta_pct = -525 000 / 1 000 000 = -52.5% → breached
        positions = [
            _stock("2330.TW", qty=100,  spot=100,  ccy="TWD"),
            _futures("TXFF",  qty=-3,   mult=200,  spot=1000),   # bigger short
            _option("TXO",    qty=-2,   mult=50,   ccy="TWD"),
            _stock("AAPL",    qty=10,   spot=200,  ccy="USD"),
        ]
        r = aggregate(positions, base_greeks, SPOT_MAP, PORTFOLIO_NAV, MockFXAdapter())
        c = next(c for c in r.hard_constraints if c.type == "net_delta_pct_nav")
        assert c.breached

    def test_gamma_limit_not_breached_small(self, result):
        # |net_gamma_twd| = 0.2  <<  1_000_000
        c = next(c for c in result.hard_constraints if c.type == "gamma_limit")
        assert not c.breached

    def test_gamma_limit_breached_when_large(self):
        positions = [_option("TXO", qty=-1000, mult=50)]
        greeks = {0: _greeks(delta=0.5, gamma=50.0)}   # huge gamma
        r = aggregate(positions, greeks, {"TXO": 1000.0}, PORTFOLIO_NAV, MockFXAdapter())
        c = next(c for c in r.hard_constraints if c.type == "gamma_limit")
        # |50 × (-1000) × 50| = 2 500 000 >> 1 000 000
        assert c.breached

    def test_vega_limit_not_breached_small(self, result):
        c = next(c for c in result.hard_constraints if c.type == "vega_limit")
        # |net_vega_twd| = 1 000  <  500 000
        assert not c.breached

    def test_vega_limit_breached_when_large(self):
        positions = [_option("TXO", qty=-1000, mult=50)]
        greeks = {0: _greeks(delta=0.5, vega=20.0)}   # large vega
        r = aggregate(positions, greeks, {"TXO": 1000.0}, PORTFOLIO_NAV, MockFXAdapter())
        c = next(c for c in r.hard_constraints if c.type == "vega_limit")
        # |20 × (-1000) × 50| = 1 000 000 >> 500 000
        assert c.breached

    def test_three_constraints_even_when_fx_missing(self, base_positions, base_greeks):
        """Constraints always produced — fail-safe: partial data is better than no check."""
        r = aggregate(base_positions, base_greeks, SPOT_MAP, PORTFOLIO_NAV, fx_adapter=None)
        assert len(r.hard_constraints) == 3

    def test_constraint_detail_notes_excluded_currencies(self, base_positions, base_greeks):
        """When currencies are excluded, detail field must flag them explicitly."""
        r = aggregate(base_positions, base_greeks, SPOT_MAP, PORTFOLIO_NAV, fx_adapter=None)
        for c in r.hard_constraints:
            assert "USD" in c.detail

    def test_detail_field_contains_nav(self, result):
        c = next(c for c in result.hard_constraints if c.type == "net_delta_pct_nav")
        assert "nav" in c.detail.lower()


# ─── Gamma unit pre-conversion regression ────────────────────────────────────

class TestGammaPreConversion:
    """
    Regression suite for the gamma unit-conversion bug.

    Root cause
    ----------
    gamma has units 1/spot² (Δdelta per Δspot).  Under currency redenomination
    S_twd = S_usd × fx:

        γ_TWD = γ_USD / fx        (NOT γ_USD × fx)

    The previous code multiplied ALL greeks_map values by fx_rate in the
    consolidated_twd loop, which was accidentally correct for delta/vega/theta
    (all scale linearly with fx) but wrong for gamma (γ × fx ≈ γ × fx² error).

    Fix
    ---
    Callers pre-convert USD spot and strike to TWD before calling price_option.
    pricing_router returns γ already in TWD units.  aggregate() sums gamma/vega/
    theta directly without any FX factor — only delta_notional (computed from
    original-currency spot in by_currency) is multiplied by fx.

    Numbers from verify_phase2_aggregation.py (real run, USDTWD≈32.37)
    -------------------------------------------------------------------
    AAPL call: Σ(γ_usd × qty × mult) = 4.8866
      Wrong (old):  4.8866 × 32.37 ≈ 158.14   ← ×fx² error
      Correct:      4.8866 / 32.37 ≈  0.151    ← γ_twd × qty × mult
    """

    # Fixture: SINGLE position — one AAPL USD call (qty=2, mult=100, spot=200 USD).
    # No TXO / TXFF / 2330 — this class unit-tests aggregation's gamma summing,
    # not the full portfolio. verify_phase2_aggregation.py (0.1411) differs because
    # it includes TXO positions whose negative gamma brings the total down from 0.1503.
    #
    # Mirror of the real-run numbers, adapted for MOCK_USDTWD=32.5:
    # GAMMA_CONTRIB_USD = γ_usd × qty × mult from old code (no pre-conversion).
    # After pre-conversion: expected consolidated gamma = GAMMA_CONTRIB_USD / MOCK_USDTWD.
    GAMMA_CONTRIB_USD: float = 4.8866   # γ_usd × 2 × 100 from AAPL call scenario

    def _result_with_usd_option(self, gamma_preconv: float) -> AggregationResult:
        """Single USD option; gamma passed in must already be in TWD units."""
        positions = [_option("AAPL", qty=2, mult=100, ccy="USD")]
        greeks    = {0: _greeks(delta=0.41, gamma=gamma_preconv)}
        return aggregate(positions, greeks, {"AAPL": 200.0}, PORTFOLIO_NAV, MockFXAdapter())

    @property
    def _gamma_twd_per_contract(self) -> float:
        """γ_USD / fx → per-contract TWD gamma (what pricing_router returns with TWD spot)."""
        return (self.GAMMA_CONTRIB_USD / (2 * 100)) / MOCK_USDTWD

    def test_gamma_sums_without_fx_in_consolidation(self):
        """consolidated_twd.net_gamma_twd = γ_twd × qty × mult (no additional FX factor)."""
        r = self._result_with_usd_option(self._gamma_twd_per_contract)
        # γ_twd_per_contract × 2 × 100 = GAMMA_CONTRIB_USD / MOCK_USDTWD
        expected = self.GAMMA_CONTRIB_USD / MOCK_USDTWD
        assert r.consolidated_twd.net_gamma_twd == pytest.approx(expected, rel=1e-4)

    def test_gamma_not_fx_multiplied_bug(self):
        """
        Regression: consolidated net_gamma must NOT be ≈ 158.

        With MOCK_USDTWD=32.5, the old code would produce:
            γ_twd_per_contract × 2 × 100 × 32.5 = GAMMA_CONTRIB_USD ≈ 4.887
        (the pre-converted γ is re-multiplied by fx → back to original USD magnitude).

        Real run (USDTWD≈32.37) produced 158.1444 because the raw γ_usd (not pre-
        converted) was multiplied by fx: γ_usd × qty × mult × fx = 4.8866 × 32.37 ≈ 158.
        """
        r = self._result_with_usd_option(self._gamma_twd_per_contract)
        # Anti-example: both the ×fx form (≈4.887) and the ×fx² form (≈158) must be absent.
        buggy_fx_once    = self.GAMMA_CONTRIB_USD                  # γ_twd×qty×mult × fx ≈ 4.887
        buggy_fx_squared = self.GAMMA_CONTRIB_USD * MOCK_USDTWD    # raw γ_usd×qty×mult × fx ≈ 158.8
        assert r.consolidated_twd.net_gamma_twd != pytest.approx(buggy_fx_once,    rel=1e-2)
        assert r.consolidated_twd.net_gamma_twd != pytest.approx(buggy_fx_squared, rel=1e-2)

    def test_delta_notional_still_uses_fx(self):
        """delta_notional (linear in spot) is still multiplied by fx — unchanged."""
        r = self._result_with_usd_option(self._gamma_twd_per_contract)
        # delta × qty × mult × spot_usd × fx = 0.41 × 2 × 100 × 200 × 32.5 = 532 500
        expected_delta_twd = 0.41 * 2 * 100 * 200.0 * MOCK_USDTWD
        assert r.consolidated_twd.net_delta_notional_twd == pytest.approx(expected_delta_twd, rel=1e-4)

    def test_by_currency_delta_notional_in_usd(self):
        """by_currency USD delta_notional still reflects original USD spot (not TWD-converted)."""
        r = self._result_with_usd_option(self._gamma_twd_per_contract)
        # delta × qty × mult × spot_usd = 0.41 × 2 × 100 × 200 = 16 400 USD
        assert r.by_currency["USD"].net_delta_notional == pytest.approx(16_400.0, rel=1e-4)

    def test_consolidated_gamma_consistent_with_by_currency(self):
        """
        consolidated_twd.net_gamma == Σ by_currency.net_gamma (no FX applied in either).

        Since greeks_map contains TWD-unit gammas, by_currency["USD"].net_gamma is
        already in TWD.  consolidated_twd just sums across currencies directly.
        """
        r = self._result_with_usd_option(self._gamma_twd_per_contract)
        total_gamma = sum(sub.net_gamma for sub in r.by_currency.values())
        assert r.consolidated_twd.net_gamma_twd == pytest.approx(total_gamma, rel=1e-10)


# ─── Error handling ───────────────────────────────────────────────────────────

class TestErrorHandling:
    def test_missing_spot_logged(self, base_positions, base_greeks):
        spot_map_missing = {"2330.TW": 100.0}   # TXFF, TXO, AAPL missing
        r = aggregate(base_positions, base_greeks, spot_map_missing, PORTFOLIO_NAV)
        assert any("spot_map" in e for e in r.errors)

    def test_missing_option_greeks_logged(self, base_positions):
        r = aggregate(base_positions, {}, SPOT_MAP, PORTFOLIO_NAV, MockFXAdapter())
        # idx 2 (TXO) has no entry in greeks_map={}
        assert any("Greeks missing" in e for e in r.errors)

    def test_invalid_position_skipped(self, base_positions, base_greeks):
        bad = Position(symbol="", instrument_type="option",
                       quantity=0.0, errors=["test error"])
        r = aggregate([bad] + base_positions, base_greeks, SPOT_MAP, PORTFOLIO_NAV,
                      MockFXAdapter())
        assert any("invalid position" in e for e in r.errors)

    def test_no_errors_in_clean_scenario(self, result):
        assert result.errors == []


# ─── Integration: load from positions.yaml ────────────────────────────────────

class TestPortfolioConfigIntegration:
    def test_load_portfolio_nav(self):
        from agents.risk.position_loader import load_portfolio
        cfg = load_portfolio()
        assert cfg.portfolio_nav == pytest.approx(5_000_000.0)
        assert cfg.nav_currency == "TWD"

    def test_load_positions_backward_compat(self):
        from agents.risk.position_loader import load_positions
        positions = load_positions()
        assert len(positions) == 6
