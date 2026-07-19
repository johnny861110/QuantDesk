"""
Black-Scholes analytical pricing and Greeks for European options.

Design contract (CLAUDE.md §1)
-------------------------------
All functions are pure deterministic math — no LLM, no I/O, no side effects.
Every assumption is a named constant with an explanatory comment.

Conventions
-----------
sigma  : implied volatility, annualised, decimal  (0.20 = 20 %)
r, q   : continuously compounded rates, annualised, decimal
T      : time to expiry in years
vega   : ∂V/∂σ where σ is in decimal units.
         P&L per 1 vol-point (1 pp) move = vega × 0.01.
theta  : per calendar day (annual theta ÷ DAYS_PER_YEAR).
rho    : ∂V/∂r × 0.01 — price change per 1 pp move in the risk-free rate.
"""
from __future__ import annotations

import math

from scipy.stats import norm  # type: ignore[import]

# ─── Named constants (phase_2.md ⚠️ — no magic numbers) ─────────────────────

# Calendar days used for theta.  365 = standard equity convention.
# Some desks use 252 (trading-day theta); change here to switch globally.
DAYS_PER_YEAR: int = 365

# Newton-Raphson IV solver
IV_MAX_ITER: int = 100       # NR typically converges in < 10 iterations
IV_PRICE_TOL: float = 1e-6   # price convergence threshold (option currency)
IV_VEGA_MIN: float = 1e-12   # floor below which NR is numerically unsafe → bisection

# Bisection IV solver search range
# Lower bound: 1e-4 avoids division-by-zero in d1 formula.
# Upper bound: 20.0 (2000 % vol) covers any realistic or stressed scenario.
IV_BISECT_LO: float = 1e-4
IV_BISECT_HI: float = 20.0
IV_BISECT_MAX_ITER: int = 200   # 200 halvings → ~1e-60 precision, always sufficient


# ─── Internal d1 / d2 helpers ────────────────────────────────────────────────

def _d1(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """d1 in the Black-Scholes formula."""
    return (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))


def _d2(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """d2 = d1 − σ√T."""
    return _d1(S, K, T, r, q, sigma) - sigma * math.sqrt(T)


# ─── Price ───────────────────────────────────────────────────────────────────

def bs_price(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    option_type: str,
) -> float:
    """
    Black-Scholes fair value for a European option.

    Parameters
    ----------
    S, K        : spot and strike price
    T           : time to expiry (years); T ≤ 0 returns intrinsic value
    r           : risk-free rate (continuous, annualised, decimal)
    q           : dividend yield (continuous, annualised, decimal)
    sigma       : implied volatility (annualised, decimal)
    option_type : "call" or "put"
    """
    if T <= 0.0:
        return max(S - K, 0.0) if option_type == "call" else max(K - S, 0.0)

    d1 = _d1(S, K, T, r, q, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)

    if option_type == "call":
        return S * disc_q * norm.cdf(d1) - K * disc_r * norm.cdf(d2)
    return K * disc_r * norm.cdf(-d2) - S * disc_q * norm.cdf(-d1)


# ─── Individual Greeks ────────────────────────────────────────────────────────

def bs_delta(
    S: float, K: float, T: float, r: float, q: float, sigma: float, option_type: str
) -> float:
    """∂V/∂S. Call ∈ (0, 1), put ∈ (−1, 0)."""
    if T <= 0.0:
        if option_type == "call":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1 = _d1(S, K, T, r, q, sigma)
    disc_q = math.exp(-q * T)
    return disc_q * norm.cdf(d1) if option_type == "call" else disc_q * (norm.cdf(d1) - 1.0)


def bs_gamma(
    S: float, K: float, T: float, r: float, q: float, sigma: float
) -> float:
    """∂²V/∂S² — identical for calls and puts."""
    if T <= 0.0:
        return 0.0
    d1 = _d1(S, K, T, r, q, sigma)
    return math.exp(-q * T) * norm.pdf(d1) / (S * sigma * math.sqrt(T))


def bs_vega(
    S: float, K: float, T: float, r: float, q: float, sigma: float
) -> float:
    """
    ∂V/∂σ — identical for calls and puts.
    σ is in decimal units; multiply by 0.01 to get P&L per 1 vol-point move.
    """
    if T <= 0.0:
        return 0.0
    d1 = _d1(S, K, T, r, q, sigma)
    return S * math.exp(-q * T) * norm.pdf(d1) * math.sqrt(T)


def bs_theta(
    S: float, K: float, T: float, r: float, q: float, sigma: float, option_type: str
) -> float:
    """
    Time decay per **calendar day** — NOT per trading day.

    Convention (explicit):
        theta_per_calendar_day = annual_theta / 365

    Why 365, not 252?
    - T in the Black-Scholes formula is measured in calendar years (e.g. 90
      calendar days = T = 90/365 ≈ 0.2466).  Using 252 would be internally
      inconsistent: the formula already encodes calendar time, so dividing by
      252 would understate the daily decay by ≈ 45 %.
    - 365 matches standard exchange margin and risk-system convention for
      equity options (Bloomberg, CBOE, most prime brokers).
    - Some fixed-income desks use actual/360 or actual/252; those would need
      a different DAYS_PER_YEAR constant and T expressed in the same basis.

    ⚠️  Downstream consumers (scenario.py Θ×Δt term) MUST express Δt in
    calendar days (Δt = calendar_days / 365) to keep units consistent.
    Mixing with a 252-trading-day Δt produces a systematic ~45 % error on
    the theta P&L term.  See docs/tasks/phase_2.md §子任務 7 for the
    explicit alignment requirement.

    Returns
    -------
    float : negative for long options (time erodes extrinsic value)
    """
    if T <= 0.0:
        return 0.0
    d1 = _d1(S, K, T, r, q, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    sqrt_T = math.sqrt(T)

    # Decay component: same sign and magnitude for calls and puts
    decay = -S * disc_q * norm.pdf(d1) * sigma / (2.0 * sqrt_T)

    if option_type == "call":
        annual = decay - r * K * disc_r * norm.cdf(d2) + q * S * disc_q * norm.cdf(d1)
    else:
        annual = decay + r * K * disc_r * norm.cdf(-d2) - q * S * disc_q * norm.cdf(-d1)

    return annual / DAYS_PER_YEAR


def bs_rho(
    S: float, K: float, T: float, r: float, q: float, sigma: float, option_type: str
) -> float:
    """
    ∂V/∂r × 0.01 — price change per 1 pp move in the risk-free rate.
    Positive for calls, negative for puts.
    """
    if T <= 0.0:
        return 0.0
    d2 = _d2(S, K, T, r, q, sigma)
    disc_r = math.exp(-r * T)
    if option_type == "call":
        return K * T * disc_r * norm.cdf(d2) * 0.01
    return -K * T * disc_r * norm.cdf(-d2) * 0.01


def bs_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    option_type: str,
) -> dict[str, float]:
    """
    All Black-Scholes values in one call.

    Returns
    -------
    dict with keys: price, delta, gamma, vega, theta, rho
    See individual function docstrings for unit conventions.
    """
    return {
        "price": bs_price(S, K, T, r, q, sigma, option_type),
        "delta": bs_delta(S, K, T, r, q, sigma, option_type),
        "gamma": bs_gamma(S, K, T, r, q, sigma),
        "vega":  bs_vega(S, K, T, r, q, sigma),
        "theta": bs_theta(S, K, T, r, q, sigma, option_type),
        "rho":   bs_rho(S, K, T, r, q, sigma, option_type),
    }


# ─── Implied volatility ───────────────────────────────────────────────────────

def implied_volatility(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    option_type: str,
    initial_sigma: float = 0.30,
) -> float:
    """
    Recover implied volatility from a market price.

    Algorithm
    ---------
    1. Newton-Raphson:  σ_{n+1} = σ_n − (BS(σ_n) − price) / vega(σ_n)
       Fast convergence near ATM; switches to bisection when vega < IV_VEGA_MIN.
    2. Bisection on [IV_BISECT_LO, IV_BISECT_HI]: always converges within bounds.

    Returns
    -------
    float : implied volatility in decimal form (e.g. 0.20 for 20 %)

    Raises
    ------
    ValueError : price outside no-arbitrage bounds, or solver failed to converge
    """
    if T <= 0.0:
        raise ValueError("T must be > 0 for IV computation")

    # No-arbitrage bounds check
    disc_q = math.exp(-q * T)
    disc_r = math.exp(-r * T)
    if option_type == "call":
        lo_bound = max(S * disc_q - K * disc_r, 0.0)
        hi_bound = S * disc_q
    else:
        lo_bound = max(K * disc_r - S * disc_q, 0.0)
        hi_bound = K * disc_r

    if market_price < lo_bound - IV_PRICE_TOL or market_price > hi_bound + IV_PRICE_TOL:
        raise ValueError(
            f"market_price={market_price:.6f} outside no-arbitrage bounds "
            f"[{lo_bound:.6f}, {hi_bound:.6f}] for {option_type}"
        )

    # ── Newton-Raphson ────────────────────────────────────────────────────────
    sigma = max(IV_BISECT_LO, min(initial_sigma, IV_BISECT_HI))

    for _ in range(IV_MAX_ITER):
        diff = bs_price(S, K, T, r, q, sigma, option_type) - market_price
        if abs(diff) < IV_PRICE_TOL:
            return sigma
        vega = bs_vega(S, K, T, r, q, sigma)
        if vega < IV_VEGA_MIN:
            break   # numerically unsafe — fall through to bisection
        sigma = max(IV_BISECT_LO, min(sigma - diff / vega, IV_BISECT_HI))

    # ── Bisection fallback ────────────────────────────────────────────────────
    lo, hi = IV_BISECT_LO, IV_BISECT_HI
    for _ in range(IV_BISECT_MAX_ITER):
        mid = 0.5 * (lo + hi)
        diff = bs_price(S, K, T, r, q, mid, option_type) - market_price
        if abs(diff) < IV_PRICE_TOL:
            return mid
        if diff > 0.0:
            hi = mid
        else:
            lo = mid

    raise ValueError(
        f"IV solver did not converge: market_price={market_price:.6f}, "
        f"S={S}, K={K}, T={T:.4f}, r={r}, q={q}, type={option_type}"
    )
