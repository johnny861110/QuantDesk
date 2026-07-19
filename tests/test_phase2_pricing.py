"""
Phase 2 pricing tests — Black-Scholes, Binomial CRR, and routing.

All expected values are derived analytically from the Black-Scholes
formulae by hand and cross-checked against scipy; they are NOT produced
by the code under test.  This way the tests catch numerical drift.

Reference case (used throughout unless otherwise noted)
-------------------------------------------------------
  S=100, K=100, T=1.0, r=0.05, q=0.0, sigma=0.20

Analytical values:
  d1 = (ln(1) + (0.05+0.02)·1) / 0.20 = 0.07/0.20 = 0.35
  d2 = 0.35 − 0.20 = 0.15
  N(0.35) ≈ 0.636831,  N(0.15) ≈ 0.559618
  N'(0.35) ≈ 0.375244  [standard normal PDF]

  Call price  ≈ 10.4506
  Put  price  ≈  5.5735
  Call delta  ≈  0.6368
  Put  delta  ≈ −0.3632
  Gamma       ≈  0.018762   (identical for call and put)
  Vega        ≈ 37.524      (per unit σ, i.e. ∂V/∂σ with σ in decimal)
  Theta call  ≈ −0.01757 /day
  Theta put   ≈ −0.00454 /day
  Rho   call  ≈  0.5323     (per 1 pp rate move)
  Rho   put   ≈ −0.4188     (per 1 pp rate move)
"""
from __future__ import annotations

import math

import pytest

from agents.risk.black_scholes import (
    _d1,
    _d2,
    bs_delta,
    bs_gamma,
    bs_greeks,
    bs_price,
    bs_rho,
    bs_theta,
    bs_vega,
    implied_volatility,
)
from agents.risk.binomial_tree import binomial_greeks, binomial_price
from agents.risk.pricing_router import GreeksResult, OptionSpec, price_option, price_option_with_iv_solve


# ─── Reference parameters ────────────────────────────────────────────────────

REF = dict(S=100.0, K=100.0, T=1.0, r=0.05, q=0.0, sigma=0.20)


# ═══════════════════════════════════════════════════════════════════════════════
# Black-Scholes: d1 / d2
# ═══════════════════════════════════════════════════════════════════════════════

def test_d1_reference_case():
    d1 = _d1(**REF)
    assert d1 == pytest.approx(0.35, abs=1e-6)


def test_d2_reference_case():
    d2 = _d2(**REF)
    assert d2 == pytest.approx(0.15, abs=1e-6)


def test_d1_deep_itm():
    """Deep ITM call: S >> K → d1 large and positive."""
    d1 = _d1(S=200, K=100, T=1.0, r=0.05, q=0.0, sigma=0.20)
    assert d1 > 2.0


def test_d1_deep_otm():
    """Deep OTM call: S << K → d1 large and negative."""
    d1 = _d1(S=50, K=100, T=1.0, r=0.05, q=0.0, sigma=0.20)
    assert d1 < -2.0


# ═══════════════════════════════════════════════════════════════════════════════
# Black-Scholes: price
# ═══════════════════════════════════════════════════════════════════════════════

def test_bs_call_price_reference():
    price = bs_price(**REF, option_type="call")
    assert price == pytest.approx(10.4506, abs=5e-4)


def test_bs_put_price_reference():
    price = bs_price(**REF, option_type="put")
    assert price == pytest.approx(5.5735, abs=5e-4)


def test_put_call_parity():
    """C − P = S·e^{−qT} − K·e^{−rT}  (put-call parity)."""
    call = bs_price(**REF, option_type="call")
    put  = bs_price(**REF, option_type="put")
    fwd_diff = REF["S"] * math.exp(-REF["q"] * REF["T"]) - REF["K"] * math.exp(-REF["r"] * REF["T"])
    assert (call - put) == pytest.approx(fwd_diff, abs=1e-8)


def test_bs_price_at_expiry_call():
    """T=0 returns intrinsic value (no time value)."""
    assert bs_price(110, 100, 0.0, 0.05, 0.0, 0.20, "call") == pytest.approx(10.0)
    assert bs_price(90,  100, 0.0, 0.05, 0.0, 0.20, "call") == pytest.approx(0.0)


def test_bs_price_at_expiry_put():
    assert bs_price(90,  100, 0.0, 0.05, 0.0, 0.20, "put") == pytest.approx(10.0)
    assert bs_price(110, 100, 0.0, 0.05, 0.0, 0.20, "put") == pytest.approx(0.0)


def test_bs_call_price_with_dividend():
    """Dividend reduces call price (continuous yield q lowers forward price)."""
    price_no_div = bs_price(**{**REF, "q": 0.0}, option_type="call")
    price_with_div = bs_price(**{**REF, "q": 0.03}, option_type="call")
    assert price_with_div < price_no_div


# ═══════════════════════════════════════════════════════════════════════════════
# Black-Scholes: Greeks
# ═══════════════════════════════════════════════════════════════════════════════

def test_bs_call_delta_reference():
    delta = bs_delta(**REF, option_type="call")
    assert delta == pytest.approx(0.6368, abs=5e-4)


def test_bs_put_delta_reference():
    delta = bs_delta(**REF, option_type="put")
    assert delta == pytest.approx(-0.3632, abs=5e-4)


def test_delta_call_put_relation():
    """delta_call − delta_put = e^{−qT} (generalised put-call delta parity)."""
    dc = bs_delta(**REF, option_type="call")
    dp = bs_delta(**REF, option_type="put")
    assert (dc - dp) == pytest.approx(math.exp(-REF["q"] * REF["T"]), abs=1e-8)


def test_bs_gamma_reference():
    gamma = bs_gamma(**REF)
    assert gamma == pytest.approx(0.018762, abs=1e-4)


def test_gamma_identical_for_call_and_put():
    """Gamma is the same for calls and puts at the same strike."""
    gamma_call = bs_gamma(**REF)   # gamma has no option_type argument
    # Verify it matches the call-side numerically via finite difference on delta
    h = 0.01
    delta_up = bs_delta(**{**REF, "S": REF["S"] + h}, option_type="call")
    delta_dn = bs_delta(**{**REF, "S": REF["S"] - h}, option_type="call")
    gamma_fd = (delta_up - delta_dn) / (2 * h)
    assert gamma_call == pytest.approx(gamma_fd, abs=1e-4)


def test_bs_vega_reference():
    vega = bs_vega(**REF)
    assert vega == pytest.approx(37.524, abs=0.05)


def test_vega_per_vol_point():
    """Vega × 0.01 should approximate price change for a 1 pp IV move."""
    p0 = bs_price(**REF, option_type="call")
    p1 = bs_price(**{**REF, "sigma": REF["sigma"] + 0.01}, option_type="call")
    vega = bs_vega(**REF)
    assert (p1 - p0) == pytest.approx(vega * 0.01, abs=1e-3)


def test_bs_theta_call_reference():
    theta = bs_theta(**REF, option_type="call")
    assert theta == pytest.approx(-0.01757, abs=5e-5)


def test_bs_theta_put_reference():
    theta = bs_theta(**REF, option_type="put")
    assert theta == pytest.approx(-0.00454, abs=5e-5)


def test_theta_negative_for_long_options():
    """Long options always lose time value — theta must be negative."""
    assert bs_theta(**REF, option_type="call") < 0
    assert bs_theta(**REF, option_type="put") < 0


def test_bs_rho_call_reference():
    rho = bs_rho(**REF, option_type="call")
    assert rho == pytest.approx(0.5323, abs=5e-4)


def test_bs_rho_put_reference():
    rho = bs_rho(**REF, option_type="put")
    assert rho == pytest.approx(-0.4188, abs=5e-4)


def test_bs_greeks_dict_keys():
    """bs_greeks returns all expected keys."""
    g = bs_greeks(**REF, option_type="call")
    assert set(g) == {"price", "delta", "gamma", "vega", "theta", "rho"}


def test_bs_greeks_matches_individual_functions():
    """bs_greeks values must match individual Greek functions exactly."""
    g = bs_greeks(**REF, option_type="call")
    assert g["price"] == bs_price(**REF, option_type="call")
    assert g["delta"] == bs_delta(**REF, option_type="call")
    assert g["gamma"] == bs_gamma(**REF)
    assert g["vega"]  == bs_vega(**REF)
    assert g["theta"] == bs_theta(**REF, option_type="call")
    assert g["rho"]   == bs_rho(**REF, option_type="call")


# ═══════════════════════════════════════════════════════════════════════════════
# Implied Volatility — round-trip and edge cases
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("sigma_true, option_type", [
    (0.10, "call"),   # low vol
    (0.20, "call"),   # reference ATM call
    (0.20, "put"),    # reference ATM put
    (0.40, "call"),   # high vol
    (0.60, "put"),    # very high vol put
])
def test_iv_round_trip_atm(sigma_true, option_type):
    """
    Round-trip: BS(σ) → price → IV_recovered.
    Error must be < 1e-4 (phase_2.md completion criterion).
    """
    price = bs_price(**{**REF, "sigma": sigma_true}, option_type=option_type)
    iv = implied_volatility(
        market_price=price,
        S=REF["S"], K=REF["K"], T=REF["T"], r=REF["r"], q=REF["q"],
        option_type=option_type,
    )
    assert abs(iv - sigma_true) < 1e-4, (
        f"IV round-trip failed: sigma_true={sigma_true}, recovered={iv:.6f}, "
        f"diff={abs(iv - sigma_true):.2e}"
    )


@pytest.mark.parametrize("K_otm", [80.0, 90.0, 110.0, 120.0])
def test_iv_round_trip_otm(K_otm):
    """IV round-trip for OTM options at various strikes."""
    sigma_true = 0.25
    price = bs_price(S=100, K=K_otm, T=0.5, r=0.03, q=0.0,
                     sigma=sigma_true, option_type="call")
    iv = implied_volatility(
        market_price=price, S=100, K=K_otm, T=0.5, r=0.03, q=0.0,
        option_type="call",
    )
    assert abs(iv - sigma_true) < 1e-4


def test_iv_raises_for_expired_option():
    with pytest.raises(ValueError, match="T must be > 0"):
        implied_volatility(1.0, 100, 100, 0.0, 0.05, 0.0, "call")


def test_iv_raises_outside_no_arb_bounds():
    """A price below the lower no-arb bound (i.e., negative time value) is rejected."""
    # Intrinsic value of this call = max(100 - 95 * e^-0.05, 0) ≈ 9.7
    # Requesting IV for price=0.01 should fail
    with pytest.raises(ValueError, match="no-arbitrage"):
        implied_volatility(0.01, 100, 95, 1.0, 0.05, 0.0, "call")


# ═══════════════════════════════════════════════════════════════════════════════
# Binomial tree: price accuracy and convergence
# ═══════════════════════════════════════════════════════════════════════════════

def test_binomial_european_call_convergence_to_bs():
    """
    European call: binomial with 200 steps must be within 0.01 of BS.
    (Completion criterion: convergence test, phase_2.md §完成標準)
    """
    bs  = bs_price(**REF, option_type="call")
    bin = binomial_price(**REF, option_type="call", style="european", n_steps=200)
    assert abs(bin - bs) < 0.01, f"|binomial({bin:.4f}) - bs({bs:.4f})| = {abs(bin-bs):.4f}"


def test_binomial_european_put_convergence_to_bs():
    bs  = bs_price(**REF, option_type="put")
    bin = binomial_price(**REF, option_type="put", style="european", n_steps=200)
    assert abs(bin - bs) < 0.01


def test_binomial_convergence_improves_with_more_steps():
    """Doubling steps should reduce the error (monotone convergence property)."""
    bs = bs_price(**REF, option_type="call")
    err_100  = abs(binomial_price(**REF, option_type="call", style="european", n_steps=100) - bs)
    err_1000 = abs(binomial_price(**REF, option_type="call", style="european", n_steps=1000) - bs)
    assert err_1000 < err_100, (
        f"Expected error to decrease with more steps: "
        f"err_100={err_100:.5f}, err_1000={err_1000:.5f}"
    )


def test_american_put_ge_european_put():
    """
    American put ≥ European put due to early exercise premium.
    With S=K=100, r=5%, no div, T=1, σ=20%: early exercise is not optimal
    in this case, but American price should still be ≥ European.
    """
    eur = binomial_price(**REF, option_type="put", style="european", n_steps=200)
    ame = binomial_price(**REF, option_type="put", style="american", n_steps=200)
    assert ame >= eur - 1e-6, f"american({ame:.6f}) < european({eur:.6f})"


def test_american_put_early_exercise_itm():
    """
    Deep ITM American put: S=60, K=100, r=5%, T=1, σ=10%.
    Early exercise is clearly optimal here → American price > European price.
    """
    params = dict(S=60, K=100, T=1.0, r=0.05, q=0.0, sigma=0.10)
    eur = binomial_price(**params, option_type="put", style="european", n_steps=500)
    ame = binomial_price(**params, option_type="put", style="american", n_steps=500)
    assert ame > eur, f"Expected early exercise premium: american={ame:.4f}, european={eur:.4f}"
    # Sanity: American put cannot exceed K (maximum payoff)
    assert ame <= params["K"]


def test_american_call_no_div_equals_european():
    """
    Without dividends, it is never optimal to exercise an American call early.
    American call price should match European call (within numerical tolerance).
    """
    eur = binomial_price(**REF, option_type="call", style="european", n_steps=500)
    ame = binomial_price(**REF, option_type="call", style="american", n_steps=500)
    assert abs(ame - eur) < 0.02, (
        f"American call without dividends should ≈ European call: "
        f"american={ame:.4f}, european={eur:.4f}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Binomial Greeks vs Black-Scholes Greeks
# ═══════════════════════════════════════════════════════════════════════════════

def test_binomial_delta_converges_to_bs():
    """Binomial delta (200 steps) within 0.005 of BS delta."""
    bs_d  = bs_delta(**REF, option_type="call")
    bin_g = binomial_greeks(**REF, option_type="call", style="european", n_steps=200)
    assert abs(bin_g["delta"] - bs_d) < 0.005, (
        f"|binomial_delta({bin_g['delta']:.5f}) - bs_delta({bs_d:.5f})| too large"
    )


def test_binomial_gamma_converges_to_bs():
    """Binomial gamma (200 steps) within 0.002 of BS gamma."""
    bs_g  = bs_gamma(**REF)
    bin_g = binomial_greeks(**REF, option_type="call", style="european", n_steps=200)
    assert abs(bin_g["gamma"] - bs_g) < 0.002, (
        f"|binomial_gamma({bin_g['gamma']:.5f}) - bs_gamma({bs_g:.5f})| too large"
    )


def test_binomial_vega_converges_to_bs():
    """Binomial vega (200 steps) within 0.5 of BS vega."""
    bs_v  = bs_vega(**REF)
    bin_g = binomial_greeks(**REF, option_type="call", style="european", n_steps=200)
    assert abs(bin_g["vega"] - bs_v) < 0.5, (
        f"|binomial_vega({bin_g['vega']:.4f}) - bs_vega({bs_v:.4f})| too large"
    )


def test_binomial_theta_negative_for_long_call():
    bin_g = binomial_greeks(**REF, option_type="call", style="european", n_steps=200)
    assert bin_g["theta"] < 0, f"Expected negative theta, got {bin_g['theta']}"


def test_binomial_rho_positive_for_call():
    bin_g = binomial_greeks(**REF, option_type="call", style="european", n_steps=200)
    assert bin_g["rho"] > 0, f"Expected positive rho for call, got {bin_g['rho']}"


# ═══════════════════════════════════════════════════════════════════════════════
# Pricing router
# ═══════════════════════════════════════════════════════════════════════════════

def test_router_european_uses_black_scholes():
    spec = OptionSpec(**REF, option_type="call", style="european")
    result = price_option(spec)
    assert result.model == "black_scholes"
    assert isinstance(result, GreeksResult)


def test_router_american_uses_binomial():
    spec = OptionSpec(**REF, option_type="put", style="american")
    result = price_option(spec)
    assert result.model == "binomial_crr"


def test_router_european_price_matches_bs():
    spec = OptionSpec(**REF, option_type="call", style="european")
    result = price_option(spec)
    expected = bs_price(**REF, option_type="call")
    assert result.price == pytest.approx(expected, abs=1e-8)


def test_router_stores_iv_used():
    spec = OptionSpec(**REF, option_type="call", style="european")
    result = price_option(spec)
    assert result.iv == REF["sigma"]


def test_router_raises_on_unknown_style():
    spec = OptionSpec(**REF, option_type="call", style="barrier")
    with pytest.raises(ValueError, match="Unknown option style"):
        price_option(spec)


def test_router_with_iv_solve_round_trip():
    """price_option_with_iv_solve: backing out IV from a BS price recovers the original σ."""
    spec = OptionSpec(**REF, option_type="call", style="european")
    bs_ref_price = bs_price(**REF, option_type="call")
    result = price_option_with_iv_solve(bs_ref_price, spec)
    assert result.errors == []
    assert abs(result.iv - REF["sigma"]) < 1e-4


def test_router_iv_solve_graceful_on_bad_price():
    """Prices outside no-arb bounds must not raise — errors list should be populated."""
    spec = OptionSpec(**REF, option_type="call", style="european")
    result = price_option_with_iv_solve(market_price=0.001, spec=spec)
    assert len(result.errors) >= 1
    assert "IV solve failed" in result.errors[0]
    assert math.isnan(result.iv)


# ─── Currency enforcement ─────────────────────────────────────────────────────

def test_price_option_raises_for_non_twd_spot():
    """
    price_option() must raise ValueError when spot_currency != "TWD".

    This is the runtime guard against the gamma unit-conversion bug: passing a
    USD spot price silently produces γ in USD units (γ_USD), which is ×fx² wrong
    when multiplied by FX in aggregation instead of being divided.  The check
    forces callers to pre-convert before creating OptionSpec.
    """
    spec = OptionSpec(**REF, option_type="call", style="european", spot_currency="USD")
    with pytest.raises(ValueError, match="spot_currency"):
        price_option(spec)


def test_price_option_raises_non_twd_error_mentions_preconversion():
    """Error message must explain the pre-conversion requirement, not just reject."""
    spec = OptionSpec(**REF, option_type="call", style="european", spot_currency="JPY")
    with pytest.raises(ValueError, match="pre-convert"):
        price_option(spec)


def test_price_option_accepts_explicit_twd():
    """Explicitly setting spot_currency='TWD' must work normally."""
    spec = OptionSpec(**REF, option_type="call", style="european", spot_currency="TWD")
    result = price_option(spec)
    assert result.model == "black_scholes"


def test_price_option_default_spot_currency_is_twd():
    """Default spot_currency='TWD' keeps all existing callers working unchanged."""
    spec = OptionSpec(**REF, option_type="call", style="european")
    assert spec.spot_currency == "TWD"
    result = price_option(spec)           # must not raise
    assert result.delta == pytest.approx(0.6368, abs=1e-3)
