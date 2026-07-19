"""
Pricing router — dispatches European options to Black-Scholes, American to Binomial.

Public API
----------
OptionSpec    : input dataclass (all parameters needed for pricing)
GreeksResult  : output dataclass (price + all Greeks, model tag, IV used)
price_option  : the single entry point callers use

Design
------
Upper layers (aggregation, scenario, risk_agent) always call `price_option(spec)`.
They never import black_scholes or binomial_tree directly — the routing logic
(which model, which parameters) is encapsulated here.  Swapping in a new model
(e.g. finite-difference PDE for barrier options) only requires adding an elif
branch here and a new style constant.

Default rate assumptions
------------------------
Named constants below are the starting point when a position does not carry its
own rate/yield.  Callers should override them per position's market:
  - Taiwan equity options: domestic rate ~1.5–2 %
  - US equity options: ~4–5 % in current environment
  - Cost-of-carry for futures: embed in q (storage cost − convenience yield)

tech-debt: these should be pulled from a RatesAdapter rather than hardcoded.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agents.risk.black_scholes import bs_greeks, implied_volatility
from agents.risk.binomial_tree import binomial_greeks

# ─── Default rate assumptions (phase_2.md ⚠️ — named constants) ──────────────

# Approximate 1-year government bond yield; used when position has no explicit r.
# Taiwan: ~1.5–2.0 % p.a.  US: ~4–5 % p.a. (as of 2024-2025).
# tech-debt: fetch live rates from RatesAdapter; replace this constant.
DEFAULT_RISK_FREE_RATE: float = 0.02    # 2 % p.a., continuous compounding

# Continuous dividend yield when not specified per position.
# 0.0 is the conservative choice: no dividends assumed.
# tech-debt: pull from FundamentalAdapter (last 12-month dividend yield).
DEFAULT_DIVIDEND_YIELD: float = 0.0    # 0 % p.a.

# Default number of binomial steps for American option pricing.
# 200 steps gives price accuracy < 0.01 vs BS for standard ATM options;
# convergence tests are in tests/test_phase2_pricing.py.
DEFAULT_BINOMIAL_STEPS: int = 200


# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class OptionSpec:
    """
    All inputs needed to price one option leg.

    Fields
    ------
    S             : underlying spot price — MUST be in TWD (see spot_currency below)
    K             : strike price — MUST be in the same currency as S
    T             : time to expiry in years (> 0)
    r             : risk-free rate, continuous, annualised, decimal
    q             : dividend yield / cost-of-carry, continuous, annualised, decimal
    sigma         : implied volatility, annualised, decimal (0.20 = 20 %)
    option_type   : "call" or "put"
    style         : "european" → Black-Scholes; "american" → CRR binomial tree
    n_steps       : binomial tree steps (ignored for style="european")
    spot_currency : currency of S and K.  MUST be "TWD".  price_option() raises
                    ValueError for any other value.

    Currency contract (enforced at runtime)
    ----------------------------------------
    All Greeks returned by price_option() are in TWD units.  This only holds if
    S and K are already expressed in TWD.  For non-TWD positions (e.g. USD-quoted
    AAPL options), the caller MUST pre-convert before creating OptionSpec:

        spot_twd   = spot_usd   × usdtwd_rate
        strike_twd = strike_usd × usdtwd_rate
        spec = OptionSpec(S=spot_twd, K=strike_twd, ..., spot_currency="TWD")

    Why this matters for gamma
    --------------------------
    gamma scales as 1/spot², so γ_TWD = γ_USD / fx (NOT × fx).  Pre-converting
    spot is the only way to get the right γ without per-Greek conversion rules
    that are easy to copy-paste incorrectly.  Passing non-TWD spot is rejected
    by price_option() so callers cannot silently skip this step.
    """
    S: float
    K: float
    T: float
    r: float = DEFAULT_RISK_FREE_RATE
    q: float = DEFAULT_DIVIDEND_YIELD
    sigma: float = 0.20
    option_type: str = "call"
    style: str = "european"
    n_steps: int = DEFAULT_BINOMIAL_STEPS
    spot_currency: str = "TWD"   # must be "TWD" — price_option() raises otherwise


@dataclass
class GreeksResult:
    """
    Unified output from pricing_router — identical structure regardless of model.

    Unit conventions (same as black_scholes.py)
    --------------------------------------------
    price  : option fair value
    delta  : ∂V/∂S
    gamma  : ∂²V/∂S²
    vega   : ∂V/∂σ, σ in decimal; multiply by 0.01 for P&L per 1 vol-point move
    theta  : ∂V/∂t per calendar day (negative for long options)
    rho    : ∂V/∂r × 0.01; P&L per 1 pp risk-free rate move
    model  : "black_scholes" or "binomial_crr"
    iv     : the σ used for pricing (decimal)
    """
    price: float
    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float
    model: str
    iv: float
    errors: list[str] = field(default_factory=list)


# ─── Router ───────────────────────────────────────────────────────────────────

def price_option(spec: OptionSpec) -> GreeksResult:
    """
    Price one option leg and return all Greeks.

    Routing rules
    -------------
    style = "european"  →  Black-Scholes analytical solution (exact, fast)
    style = "american"  →  CRR binomial tree (handles early exercise)

    Any unknown style raises ValueError so callers see a clear error rather
    than silently falling back to a wrong model.

    Currency enforcement
    --------------------
    spec.spot_currency must be "TWD".  If it is anything else (e.g. "USD"),
    this function raises ValueError immediately — callers must pre-convert S
    and K to TWD before constructing OptionSpec.  See OptionSpec docstring.
    """
    if spec.spot_currency != "TWD":
        raise ValueError(
            f"OptionSpec.spot_currency={spec.spot_currency!r}: "
            "S and K must be pre-converted to TWD before calling price_option(). "
            "For a USD position: spot_twd = spot_usd × fx_rate, "
            "strike_twd = strike_usd × fx_rate, then set spot_currency='TWD'. "
            "Reason: gamma scales as 1/spot², so multiplying raw USD gamma by fx "
            "gives the wrong sign of the FX effect (γ_TWD = γ_USD / fx, not × fx)."
        )

    if spec.style == "european":
        g = bs_greeks(spec.S, spec.K, spec.T, spec.r, spec.q, spec.sigma, spec.option_type)
        model = "black_scholes"
    elif spec.style == "american":
        g = binomial_greeks(
            spec.S, spec.K, spec.T, spec.r, spec.q, spec.sigma,
            spec.option_type, spec.style, spec.n_steps,
        )
        model = "binomial_crr"
    else:
        raise ValueError(
            f"Unknown option style '{spec.style}'. "
            "Supported: 'european' (Black-Scholes) or 'american' (CRR binomial)."
        )

    return GreeksResult(
        price=g["price"],
        delta=g["delta"],
        gamma=g["gamma"],
        vega=g["vega"],
        theta=g["theta"],
        rho=g["rho"],
        model=model,
        iv=spec.sigma,
    )


def price_option_with_iv_solve(
    market_price: float,
    spec: OptionSpec,
) -> GreeksResult:
    """
    Solve for IV from a market price, then price and return Greeks.

    Used by OptionsAdapter after backing out IV from FinMind trade prices.
    The solved IV is stored in GreeksResult.iv for downstream traceability.

    Note: IV solve always uses Black-Scholes (BS vega is the derivative).
    For American options the Greeks are then computed with the binomial tree
    at the solved IV — this is the standard industry practice (use BS to
    solve IV, use binomial/PDE for pricing/greeks).

    Errors (e.g. IV outside no-arb bounds) are caught and surfaced in
    GreeksResult.errors rather than propagating as exceptions, so the caller
    can degrade gracefully (mark confidence low, skip this leg).
    """
    try:
        iv = implied_volatility(
            market_price=market_price,
            S=spec.S,
            K=spec.K,
            T=spec.T,
            r=spec.r,
            q=spec.q,
            option_type=spec.option_type,
        )
    except ValueError as exc:
        # Return a zero-greeks result with the error recorded
        return GreeksResult(
            price=market_price,
            delta=0.0, gamma=0.0, vega=0.0, theta=0.0, rho=0.0,
            model="iv_solve_failed",
            iv=float("nan"),
            errors=[f"IV solve failed: {exc}"],
        )

    solved_spec = OptionSpec(
        S=spec.S, K=spec.K, T=spec.T, r=spec.r, q=spec.q,
        sigma=iv,
        option_type=spec.option_type,
        style=spec.style,
        n_steps=spec.n_steps,
        spot_currency=spec.spot_currency,
    )
    result = price_option(solved_spec)
    return result
