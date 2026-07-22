"""
Phase 3 Cross-Market Agent Tests

All unit tests — no network calls.
FakeCrossMarketAdapter returns synthetic correlated data without any I/O.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pytest

from adapters.base import SourcedData
from adapters.cross_market_adapter import CrossMarketData, _align_series
from agents.cross_market_agent import (
    DIVERGENCE_WINDOW,
    SHORT_WINDOW,
    classify_regime,
    compute_rolling_beta,
    compute_rolling_correlation,
    detect_divergence,
    find_lead_lag,
    run_cross_market_agent,
)
from agents.verifier import check_narrative
from schemas.agent_signal import AgentType, Signal, TimeHorizon


# ─── Fake adapter (no network) ────────────────────────────────────────────────

class FakeCrossMarketAdapter:
    """Returns synthetic correlated series (no network)."""

    @property
    def source_name(self) -> str:
        return "fake_cross_market"

    def fetch(
        self,
        symbols: dict[str, str] | None = None,
        period: str = "6mo",
        interval: str = "1d",
        **kwargs: Any,
    ) -> SourcedData:
        n = 120
        rng0 = np.random.default_rng(0)
        rng1 = np.random.default_rng(1)
        rng2 = np.random.default_rng(2)

        base = np.cumsum(rng0.normal(0, 1, n))
        tw = base + rng1.normal(0, 0.3, n)
        us = base + rng2.normal(0, 0.3, n)

        dates = [datetime(2024, 1, 1) + timedelta(days=i) for i in range(n)]

        tw_ret = np.log(np.exp(tw[1:]) / np.exp(tw[:-1]))
        us_ret = np.log(np.exp(us[1:]) / np.exp(us[:-1]))

        tw_close = np.exp(tw) * 20000
        us_close = np.exp(us) * 5000

        payload = CrossMarketData(
            symbols=["^TWII", "^GSPC"],
            labels={"^TWII": "TAIEX", "^GSPC": "S&P 500"},
            close={"^TWII": tw_close, "^GSPC": us_close},
            returns_={"^TWII": tw_ret, "^GSPC": us_ret},
            dates=dates,
        )
        return SourcedData(payload=payload, source="fake", asof=dates[-1])


# ─── Rolling correlation tests ────────────────────────────────────────────────

def test_rolling_corr_identical_series() -> None:
    """x == y → rolling_corr[-1] ≈ 1.0."""
    rng = np.random.default_rng(42)
    x = rng.normal(0, 1, 50)
    y = x.copy()
    result = compute_rolling_correlation(x, y, SHORT_WINDOW)
    assert not np.isnan(result[-1]), "last value should not be nan"
    assert abs(result[-1] - 1.0) < 1e-9, f"expected 1.0, got {result[-1]}"


def test_rolling_corr_anticorrelated() -> None:
    """y == -x → rolling_corr[-1] ≈ -1.0."""
    rng = np.random.default_rng(42)
    x = rng.normal(0, 1, 50)
    y = -x
    result = compute_rolling_correlation(x, y, SHORT_WINDOW)
    assert not np.isnan(result[-1])
    assert abs(result[-1] - (-1.0)) < 1e-9, f"expected -1.0, got {result[-1]}"


def test_rolling_corr_min_window() -> None:
    """First (window-1) elements are all nan."""
    x = np.arange(40, dtype=np.float64)
    y = np.arange(40, dtype=np.float64)
    window = 10
    result = compute_rolling_correlation(x, y, window)
    assert len(result) == len(x)
    assert all(np.isnan(result[i]) for i in range(window - 1)), (
        "first window-1 elements must be nan"
    )
    # element at index window-1 should be valid (or nan due to zero std on a
    # constant-rate sequence — but we should have at least checked)
    assert len(result) == 40


def test_rolling_corr_zero_std() -> None:
    """x is constant → rolling_corr has nan (not an error)."""
    x = np.ones(40, dtype=np.float64)
    y = np.arange(40, dtype=np.float64)
    result = compute_rolling_correlation(x, y, SHORT_WINDOW)
    # All valid windows should produce nan because std(x_window) == 0
    valid_indices = range(SHORT_WINDOW - 1, len(x))
    assert all(
        np.isnan(result[i]) for i in valid_indices
    ), "zero-std windows must produce nan"


# ─── Rolling beta tests ───────────────────────────────────────────────────────

def test_rolling_beta_unit() -> None:
    """x == y → beta[-1] ≈ 1.0."""
    rng = np.random.default_rng(7)
    x = rng.normal(0, 1, 50)
    y = x.copy()
    result = compute_rolling_beta(y, x, SHORT_WINDOW)
    assert not np.isnan(result[-1])
    assert abs(result[-1] - 1.0) < 1e-6, f"expected 1.0, got {result[-1]}"


def test_rolling_beta_scaled() -> None:
    """y == 2*x → beta[-1] ≈ 2.0."""
    rng = np.random.default_rng(7)
    x = rng.normal(0, 1, 50)
    y = 2.0 * x
    result = compute_rolling_beta(y, x, SHORT_WINDOW)
    assert not np.isnan(result[-1])
    assert abs(result[-1] - 2.0) < 1e-6, f"expected 2.0, got {result[-1]}"


# ─── Lead-lag tests ───────────────────────────────────────────────────────────

def test_find_lead_lag_known() -> None:
    """
    y = np.roll(x, 2) shifts x RIGHT by 2 positions.
    y[2:] == x[:-2], so at lag=+2: correlate x[:-2] with y[2:] = x[:-2] → perfect.
    Optimal lag should be +2.
    """
    rng = np.random.default_rng(99)
    x = rng.normal(0, 1, 80)
    y = np.roll(x, 2)

    lag_map = find_lead_lag(x, y, max_lag=5)

    # At lag=2: x[:-2] vs y[2:] = x[:-2] → correlation = 1.0
    assert lag_map[2] == pytest.approx(1.0, abs=1e-6), (
        f"lag=2 should have correlation 1.0, got {lag_map[2]}"
    )

    # Optimal lag must be 2
    valid = {lag: v for lag, v in lag_map.items() if not np.isnan(v)}
    optimal = max(valid, key=lambda lag: abs(valid[lag]))
    assert optimal == 2, f"optimal lag should be 2, got {optimal}"


# ─── Divergence detection tests ───────────────────────────────────────────────

def test_detect_divergence_true() -> None:
    """Positive corr, target went up, ref went down → divergence True."""
    # Construct returns where last 5 days: target positive, ref negative
    n = 30
    target_ret = np.zeros(n)
    ref_ret = np.zeros(n)
    # Last DIVERGENCE_WINDOW days: target up, ref down
    target_ret[-DIVERGENCE_WINDOW:] = 0.01
    ref_ret[-DIVERGENCE_WINDOW:] = -0.01

    long_corr = 0.8  # strongly coupled (positive)
    result = detect_divergence(target_ret, ref_ret, long_corr)
    assert result is True, "should detect divergence when correlated markets move opposite"


def test_detect_divergence_false() -> None:
    """Positive corr, both went up → no divergence."""
    n = 30
    target_ret = np.zeros(n)
    ref_ret = np.zeros(n)
    target_ret[-DIVERGENCE_WINDOW:] = 0.01
    ref_ret[-DIVERGENCE_WINDOW:] = 0.01

    long_corr = 0.8
    result = detect_divergence(target_ret, ref_ret, long_corr)
    assert result is False, "same-direction move for positively correlated pair is not divergence"


# ─── Regime classification tests ─────────────────────────────────────────────

def test_classify_regime_all_cases() -> None:
    """Test each of the 6 regime strings."""
    assert classify_regime(0.75, False) == "strong_coupling"
    assert classify_regime(0.45, False) == "moderate_coupling"
    assert classify_regime(0.10, False) == "decoupled"
    assert classify_regime(-0.50, False) == "negative_coupling"
    # diverging overrides everything
    assert classify_regime(0.75, True) == "divergent"
    assert classify_regime(-0.50, True) == "divergent"
    assert classify_regime(0.10, True) == "divergent"


def test_classify_regime_short_term_counter() -> None:
    """
    short_term_counter: corr_20d < -0.30 while corr_60d is neutral or unavailable.

    Real example: 2026-03-09 to 2026-04-07 (tariff-shock period).
    Taiwan absorbed the shock first while US partially rebounded → corr20=-0.29,
    corr60 near 0 or nan.  Should NOT be classified as "decoupled" (which implies
    no clear relationship) but as "short_term_counter" (active counter-movement).
    """
    # corr60 nan (early in history, < 60 bars)
    assert classify_regime(float("nan"), False, corr_20d=-0.35) == "short_term_counter"
    # corr60 near zero (decoupled zone) but corr20 is negative
    assert classify_regime(0.05, False, corr_20d=-0.31) == "short_term_counter"
    assert classify_regime(-0.10, False, corr_20d=-0.45) == "short_term_counter"
    # corr20 negative but above threshold → still decoupled
    assert classify_regime(0.05, False, corr_20d=-0.20) == "decoupled"
    # corr60 already negative_coupling → corr20 doesn't change the label
    assert classify_regime(-0.50, False, corr_20d=-0.45) == "negative_coupling"
    # diverging overrides short_term_counter
    assert classify_regime(float("nan"), True, corr_20d=-0.45) == "divergent"


# ─── Full pipeline / schema tests ─────────────────────────────────────────────

def test_build_signal_schema() -> None:
    """FakeCrossMarketAdapter → valid AgentSignal with correct types."""
    adapter = FakeCrossMarketAdapter()
    asof = datetime(2024, 5, 1)

    sig = run_cross_market_agent(
        symbols={"^TWII": "TAIEX", "^GSPC": "S&P 500"},
        cross_adapter=adapter,  # type: ignore[arg-type]
        asof=asof,
    )

    # Agent type
    assert sig.agent == AgentType.CROSS_MARKET

    # Time horizon
    assert sig.time_horizon == TimeHorizon.MEDIUM

    # No hard constraints
    assert sig.hard_constraints == []

    # All evidence items have source and asof
    assert len(sig.key_evidence) == 5
    for ev in sig.key_evidence:
        assert ev.source, f"evidence missing source: {ev}"
        assert ev.asof is not None, f"evidence missing asof: {ev}"

    # Required metrics keys
    required_keys = {
        "tw_us_corr_20d", "tw_us_corr_60d", "tw_us_beta_20d",
        "lead_lag_optimal", "divergence_detected", "regime",
        "taiex_5d_return", "n_bars", "is_background_only",
    }
    for k in required_keys:
        assert k in sig.metrics, f"missing metric key: {k}"

    assert sig.metrics["is_background_only"] is True

    # Signal is one of the valid enum values
    assert sig.signal in (Signal.NEUTRAL, Signal.BEARISH)

    # Confidence in valid range
    assert 0.0 <= sig.confidence <= 1.0

    # Data quality
    assert 0.0 <= sig.data_quality.completeness <= 1.0
    assert sig.data_quality.staleness_sec >= 0.0


def test_narrative_no_numbers() -> None:
    """check_narrative passes (no unregistered numbers in narrative)."""
    adapter = FakeCrossMarketAdapter()
    asof = datetime(2024, 5, 1)

    sig = run_cross_market_agent(
        symbols={"^TWII": "TAIEX", "^GSPC": "S&P 500"},
        cross_adapter=adapter,  # type: ignore[arg-type]
        asof=asof,
    )

    verifier_errors = check_narrative(sig.narrative, sig.metrics)
    assert verifier_errors == [], (
        "Narrative contains unregistered numbers:\n"
        + "\n".join(verifier_errors)
        + f"\n\nNarrative: {sig.narrative!r}"
    )


# ─── Pure helper: _align_series ───────────────────────────────────────────────

def test_align_series_pure() -> None:
    """
    3 symbols, one missing a date → intersection is correct.

    sym_a has dates 1,2,3,4,5
    sym_b has dates 1,2,3,4    (missing 5)
    sym_c has dates   2,3,4,5  (missing 1)

    intersection = {2, 3, 4}
    """
    d1 = datetime(2024, 1, 1)
    d2 = datetime(2024, 1, 2)
    d3 = datetime(2024, 1, 3)
    d4 = datetime(2024, 1, 4)
    d5 = datetime(2024, 1, 5)

    raw: dict[str, dict[datetime, float]] = {
        "A": {d1: 100.0, d2: 101.0, d3: 102.0, d4: 103.0, d5: 104.0},
        "B": {d1: 200.0, d2: 201.0, d3: 202.0, d4: 203.0},
        "C": {d2: 300.0, d3: 301.0, d4: 302.0, d5: 303.0},
    }

    sorted_dates, aligned = _align_series(raw)

    assert sorted_dates == [d2, d3, d4], f"unexpected dates: {sorted_dates}"
    assert len(aligned) == 3

    np.testing.assert_array_equal(aligned["A"], [101.0, 102.0, 103.0])
    np.testing.assert_array_equal(aligned["B"], [201.0, 202.0, 203.0])
    np.testing.assert_array_equal(aligned["C"], [300.0, 301.0, 302.0])
