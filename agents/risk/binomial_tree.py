"""
CRR (Cox-Ross-Rubinstein) binomial tree pricing for American and European options.

Why binomial for American options?
  Black-Scholes has no closed-form for American-style early exercise.
  The binomial tree handles it naturally: at each node compare the
  hold value (discounted expected continuation) against immediate exercise.

Greeks computation
------------------
Delta and gamma are read directly from the first two backward-induction steps
(finite difference on the tree itself — no extra repricings needed):

  Delta = (V[1,up] − V[1,dn]) / (S·u − S·d)
  Gamma = (Δ_up − Δ_dn) / (0.5·(S·u² − S·d²))

  where Δ_up = (V[2,uu]−V[2,ud])/(S·u²−S·u·d)
        Δ_dn = (V[2,ud]−V[2,dd])/(S·u·d−S·d²)
        and in CRR u·d = 1, so S·u·d = S.

Theta is extracted from the centre node at time 2·Δt:
  theta_per_year = (V[2,ud] − V[0]) / (2·Δt)
  theta_per_day  = theta_per_year / DAYS_PER_YEAR

Vega and rho are computed by finite difference (one extra re-pricing each):
  vega = (price(σ+Δσ) − price(σ−Δσ)) / (2·Δσ)
  rho  = (price(r+Δr) − price(r−Δr)) / (2·Δr) × 0.01   [per 1 pp rate move]

Convergence note
----------------
The CRR tree oscillates for even/odd step counts around the BS value.
For accurate results use n_steps ≥ 100 (default 200 gives < 0.01 error vs BS
for standard ATM options; tested in tests/test_phase2_pricing.py).
"""
from __future__ import annotations

import math

import numpy as np

from agents.risk.black_scholes import DAYS_PER_YEAR

# ─── Named constants (phase_2.md ⚠️ — no magic numbers) ─────────────────────

# Finite-difference bumps for vega and rho
# Vega bump: 0.1 pp is small enough to be local but large enough to avoid
# floating-point cancellation near zero vega.
VEGA_BUMP: float = 0.001    # Δσ = 0.1 pp

# Rho bump: 1 bp (0.01 %) keeps the finite difference well within linear regime.
RHO_BUMP: float = 0.0001    # Δr = 1 bp


# ─── Core tree builder ────────────────────────────────────────────────────────

def _build_tree(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    option_type: str,
    style: str,
    n_steps: int,
) -> tuple[float, np.ndarray, np.ndarray, float]:
    """
    Run CRR backward induction.

    Returns
    -------
    price : float              — option fair value at t=0
    V1    : ndarray shape (2,) — option values at time 1·Δt  [up, down]
    V2    : ndarray shape (3,) — option values at time 2·Δt  [uu, ud, dd]
    dt    : float              — time per step (years)
    """
    dt = T / n_steps
    sqrt_dt = math.sqrt(dt)

    # CRR parameters
    u = math.exp(sigma * sqrt_dt)
    d = 1.0 / u                        # u·d = 1 by construction
    disc = math.exp(-r * dt)           # per-step risk-neutral discount factor
    p = (math.exp((r - q) * dt) - d) / (u - d)   # risk-neutral up probability

    is_call = option_type == "call"

    # ── Terminal stock prices and payoffs ─────────────────────────────────────
    j = np.arange(n_steps + 1, dtype=np.float64)
    # j-th node: j up-moves → S · u^j · d^(n-j) = S · u^(2j-n)
    S_T = S * (u ** (n_steps - 2.0 * j))
    V = np.maximum(S_T - K, 0.0) if is_call else np.maximum(K - S_T, 0.0)

    # ── Backward induction ────────────────────────────────────────────────────
    # Save snapshots for Greek extraction
    V2_saved: np.ndarray | None = None
    V1_saved: np.ndarray | None = None

    for step in range(n_steps - 1, -1, -1):
        V = disc * (p * V[:-1] + (1.0 - p) * V[1:])

        if style == "american":
            # Early exercise: stock prices at this step
            j_step = np.arange(step + 1, dtype=np.float64)
            S_step = S * (u ** (step - 2.0 * j_step))
            exercise = (
                np.maximum(S_step - K, 0.0) if is_call
                else np.maximum(K - S_step, 0.0)
            )
            V = np.maximum(V, exercise)

        # Save values at time steps 2 and 1 (counting from t=0)
        # After processing loop index `step`, V holds values at time step `step`.
        if step == 2:
            V2_saved = V.copy()   # shape (3,): [V_uu, V_ud, V_dd]
        elif step == 1:
            V1_saved = V.copy()   # shape (2,): [V_u, V_d]

    # V now has shape (1,) with the option price at t=0
    price = float(V[0])

    # Graceful fallback if n_steps < 3 (Greek extraction needs 3 nodes minimum)
    if V1_saved is None:
        V1_saved = np.zeros(2)
    if V2_saved is None:
        V2_saved = np.zeros(3)

    return price, V1_saved, V2_saved, dt


# ─── Public functions ─────────────────────────────────────────────────────────

def binomial_price(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    option_type: str,
    style: str,
    n_steps: int = 200,
) -> float:
    """
    CRR binomial tree price.

    Parameters
    ----------
    style    : "european" or "american"
    n_steps  : number of time steps (≥ 3 for Greek extraction; ≥ 100 for accuracy)
    """
    price, _, _, _ = _build_tree(S, K, T, r, q, sigma, option_type, style, n_steps)
    return price


def binomial_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    option_type: str,
    style: str,
    n_steps: int = 200,
) -> dict[str, float]:
    """
    CRR binomial tree Greeks.

    Returns
    -------
    dict with keys: price, delta, gamma, vega, theta, rho
    Units are identical to Black-Scholes conventions in black_scholes.py.

    Delta / gamma: extracted analytically from the first two tree nodes.
    Theta:         extracted from the centre node at 2·Δt.
    Vega / rho:    finite-difference repricings (VEGA_BUMP / RHO_BUMP).
    """
    price, V1, V2, dt = _build_tree(S, K, T, r, q, sigma, option_type, style, n_steps)

    sqrt_dt = math.sqrt(dt)
    u = math.exp(sigma * sqrt_dt)
    d = 1.0 / u

    # ── Delta ─────────────────────────────────────────────────────────────────
    # V1[0] = value at S·u, V1[1] = value at S·d
    Su = S * u
    Sd = S * d
    delta = (V1[0] - V1[1]) / (Su - Sd) if (Su - Sd) != 0.0 else 0.0

    # ── Gamma ─────────────────────────────────────────────────────────────────
    # V2[0]=V_uu at S·u², V2[1]=V_ud at S·u·d=S, V2[2]=V_dd at S·d²
    Suu = S * u * u
    Sdd = S * d * d
    # In CRR: u·d = 1, so S·u·d = S (the centre node returns to the original price)
    delta_up = (V2[0] - V2[1]) / (Suu - S) if (Suu - S) != 0.0 else 0.0
    delta_dn = (V2[1] - V2[2]) / (S - Sdd) if (S - Sdd) != 0.0 else 0.0
    gamma_denom = 0.5 * (Suu - Sdd)
    gamma = (delta_up - delta_dn) / gamma_denom if gamma_denom != 0.0 else 0.0

    # ── Theta ─────────────────────────────────────────────────────────────────
    # V2[1] = option value at same spot S but 2·Δt later in time
    # (price − V_ud) represents the gain from waiting; theta is negative = loss
    theta_per_year = (V2[1] - price) / (2.0 * dt) if dt > 0.0 else 0.0
    theta = theta_per_year / DAYS_PER_YEAR

    # ── Vega (finite difference) ───────────────────────────────────────────────
    price_vhi = binomial_price(S, K, T, r, q, sigma + VEGA_BUMP, option_type, style, n_steps)
    price_vlo = binomial_price(S, K, T, r, q, sigma - VEGA_BUMP, option_type, style, n_steps)
    vega = (price_vhi - price_vlo) / (2.0 * VEGA_BUMP)

    # ── Rho (finite difference, per 1 pp rate move) ────────────────────────────
    price_rhi = binomial_price(S, K, T, r + RHO_BUMP, q, sigma, option_type, style, n_steps)
    price_rlo = binomial_price(S, K, T, r - RHO_BUMP, q, sigma, option_type, style, n_steps)
    rho = (price_rhi - price_rlo) / (2.0 * RHO_BUMP) * 0.01

    return {
        "price": price,
        "delta": delta,
        "gamma": gamma,
        "vega":  vega,
        "theta": theta,
        "rho":   rho,
    }
