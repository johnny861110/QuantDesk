"""
Scenario stress-tester — ΔP ≈ Δ×ΔS + ½×Γ×(ΔS)² + ν×ΔIV + Θ×Δt

Design
------
Given a portfolio represented as a list of Positions + a Greeks map (from
pricing_router) + pre-converted TWD spot prices, this module computes the
first/second-order P&L approximation for each combination of:
  • index_shock   : ΔS/S  (e.g. +0.03 = +3 % underlying move)
  • iv_shock      : Δσ    (e.g. +0.10 = +10 vol-point absolute move)

The formula is applied per position-leg, then summed to a portfolio total.

⚠️  Δt day-count convention (must match black_scholes.py)
-----------------------------------------------------------
bs_theta is already expressed in calendar-day units (annual_theta / 365).
Δt here must therefore use CALENDAR days / 365:
  Δt = calendar_days / 365   (e.g. 1 day held → Δt = 1/365 ≈ 0.002740)

DO NOT switch to trading-day convention (/ 252).  Mixing conventions produces
a systematic ~45 % theta-P&L over-estimate that is invisible to most callers.
See docs/tasks/phase_2.md ⚠️ note and feedback memory feedback_theta_day_convention.md.

Negative convexity illustration (positions in positions.yaml)
-------------------------------------------------------------
The mixed portfolio (short TXFF + short TXO call + long TXO put) has a
significant short-gamma exposure via the naked short call.  In a +3 %/+5 %
shock the gamma_pnl term is negative and larger in magnitude than the linear
delta gain, resulting in net portfolio loss — classic negative convexity.

Default scenario matrix
-----------------------
INDEX_SHOCKS   = [-0.05, -0.03, -0.01, +0.01, +0.03, +0.05]  (6 shocks)
IV_SHOCKS      = [-0.20, -0.10,  0.00, +0.10, +0.20]          (5 shocks)

36 = 6 × 6 = wait, 6 × 5 = 30 scenarios in total.

Units
-----
All P&L figures are in TWD.  Callers are expected to pass TWD-denominated
spots (twd_spot_map).  For USD positions the caller should pre-convert spot
(spot_twd = spot_usd × fx) before building the map — the same contract as
aggregation.py's caller contract for non-TWD positions.

Output types
------------
LegPnL        — P&L components for one position leg in one scenario
ScenarioRow   — one (index_shock, iv_shock) scenario: per-leg + aggregate
ScenarioResult — all 30 scenarios for the portfolio
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from agents.risk.position_loader import Position
from agents.risk.pricing_router import GreeksResult

# ─── Day-count constant ───────────────────────────────────────────────────────

# Calendar-day denominator: MUST match black_scholes.py theta convention.
# DO NOT change to 252 (trading-day basis) — see module docstring.
CALENDAR_DAYS_PER_YEAR: int = 365

# ─── Default scenario matrix ──────────────────────────────────────────────────

# Six underlying shocks in decimal (−5 % to +5 %)
INDEX_SHOCKS: tuple[float, ...] = (-0.05, -0.03, -0.01, +0.01, +0.03, +0.05)

# Five IV shocks in absolute decimal points (−20 pp to +20 pp)
IV_SHOCKS: tuple[float, ...] = (-0.20, -0.10, 0.00, +0.10, +0.20)


# ─── Output dataclasses ───────────────────────────────────────────────────────

@dataclass
class LegPnL:
    """
    P&L decomposition for one position leg under one scenario.

    All values in TWD.

    delta_pnl   = delta  × ΔS × qty × mult
    gamma_pnl   = ½ × gamma × ΔS² × qty × mult      (convexity term)
    vega_pnl    = vega   × Δσ × qty × mult
    theta_pnl   = theta  × Δt × qty × mult           (Δt = days / 365)
    total_pnl   = delta_pnl + gamma_pnl + vega_pnl + theta_pnl

    For stocks and futures (degenerate Greeks): only delta_pnl is non-zero.
    """
    position_idx: int
    symbol: str
    instrument_type: str
    delta_pnl: float
    gamma_pnl: float
    vega_pnl: float
    theta_pnl: float
    total_pnl: float


@dataclass
class ScenarioRow:
    """
    One (index_shock, iv_shock) scenario.

    index_shock : fractional underlying move (e.g. 0.03 = +3 %)
    iv_shock    : absolute IV move in decimal (e.g. 0.10 = +10 vol pts)
    delta_S_twd : ΔS in TWD points / dollars (index_shock × spot_twd for
                  index derivatives; same for single-name but each has its
                  own ΔS — stored per leg in legs list)
    legs        : LegPnL for every position in the portfolio
    agg_delta_pnl  : Σ leg.delta_pnl
    agg_gamma_pnl  : Σ leg.gamma_pnl    (negative = short convexity)
    agg_vega_pnl   : Σ leg.vega_pnl
    agg_theta_pnl  : Σ leg.theta_pnl
    agg_total_pnl  : Σ leg.total_pnl
    """
    index_shock: float
    iv_shock: float
    legs: list[LegPnL]
    agg_delta_pnl: float
    agg_gamma_pnl: float
    agg_vega_pnl: float
    agg_theta_pnl: float
    agg_total_pnl: float


@dataclass
class ScenarioResult:
    """
    Full scenario analysis: one ScenarioRow per (index_shock, iv_shock) pair.

    scenarios       : len = len(INDEX_SHOCKS) × len(IV_SHOCKS)
    days_held       : the holding period used for theta calculation
    index_shocks    : the shock grid used
    iv_shocks       : the shock grid used
    """
    scenarios: list[ScenarioRow]
    days_held: float
    index_shocks: tuple[float, ...]
    iv_shocks: tuple[float, ...]


# ─── Helpers ──────────────────────────────────────────────────────────────────

class _PositionGreeks(NamedTuple):
    """Resolved Greeks for one position after qty/mult scaling is removed."""
    delta: float
    gamma: float
    vega: float
    theta: float


def _resolve_greeks(
    pos: Position,
    pos_idx: int,
    greeks_map: dict[int, GreeksResult],
) -> _PositionGreeks | None:
    """
    Return per-unit-per-contract Greeks for one position.

    Returns None and logs nothing if the position is invalid — callers
    should validate positions before calling run_scenarios.

    Degenerate cases
    ----------------
    stock / futures : delta = sign(quantity) × 1  (effectively delta=1 per unit),
                      but to match aggregation.py convention we return delta=1
                      for long and -1 for short… wait — actually delta per share/
                      contract is simply 1.0 for long and we multiply by qty.
                      So we return delta=1.0 always; qty sign handles direction.
    option          : from greeks_map[pos_idx].
    """
    if pos.instrument_type in ("stock", "futures"):
        return _PositionGreeks(delta=1.0, gamma=0.0, vega=0.0, theta=0.0)

    # option
    greeks = greeks_map.get(pos_idx)
    if greeks is None:
        return None
    return _PositionGreeks(
        delta=greeks.delta,
        gamma=greeks.gamma,
        vega=greeks.vega,
        theta=greeks.theta,
    )


def _compute_leg_pnl(
    pos: Position,
    pos_idx: int,
    pg: _PositionGreeks,
    spot_twd: float,
    index_shock: float,
    iv_shock: float,
    delta_t: float,
) -> LegPnL:
    """
    Apply the scenario formula to one position leg.

    ΔP ≈ Δ×ΔS + ½×Γ×(ΔS)² + ν×ΔIV + Θ×Δt

    ΔS = index_shock × spot_twd   (TWD)
    Scaling: every Greek is per-unit (1 contract of 1 underlying unit).
    Full position P&L = Greek_per_unit × qty × mult.
    """
    delta_s = index_shock * spot_twd

    qty  = pos.quantity
    mult = pos.multiplier

    delta_pnl = pg.delta * delta_s           * qty * mult
    gamma_pnl = 0.5 * pg.gamma * delta_s**2 * qty * mult
    vega_pnl  = pg.vega  * iv_shock          * qty * mult
    theta_pnl = pg.theta * delta_t           * qty * mult

    return LegPnL(
        position_idx    = pos_idx,
        symbol          = pos.symbol,
        instrument_type = pos.instrument_type,
        delta_pnl       = delta_pnl,
        gamma_pnl       = gamma_pnl,
        vega_pnl        = vega_pnl,
        theta_pnl       = theta_pnl,
        total_pnl       = delta_pnl + gamma_pnl + vega_pnl + theta_pnl,
    )


# ─── Public API ───────────────────────────────────────────────────────────────

def run_scenarios(
    positions: list[Position],
    greeks_map: dict[int, GreeksResult],
    twd_spot_map: dict[str, float],
    days_held: float = 1.0,
    index_shocks: tuple[float, ...] = INDEX_SHOCKS,
    iv_shocks: tuple[float, ...] = IV_SHOCKS,
) -> ScenarioResult:
    """
    Run the scenario matrix and return per-scenario P&L decompositions.

    Parameters
    ----------
    positions     : list[Position] from position_loader (may include invalid,
                    which are skipped with None Greeks).
    greeks_map    : {position_index: GreeksResult} for option legs.
                    Stock/futures legs use degenerate Greeks (delta=1, rest=0).
    twd_spot_map  : {symbol: spot_in_TWD}.  For USD positions the caller must
                    pre-convert: spot_twd = spot_usd × fx_rate.
                    This is the same caller contract as aggregation.py.
    days_held     : holding period for theta — in calendar days.
                    Converted to years as days / CALENDAR_DAYS_PER_YEAR (365).
    index_shocks  : iterable of fractional underlying-price shocks (decimal).
                    Default: INDEX_SHOCKS = (−5%, −3%, −1%, +1%, +3%, +5%).
    iv_shocks     : iterable of absolute IV shocks (decimal points).
                    Default: IV_SHOCKS = (−20 pp, −10 pp, 0, +10 pp, +20 pp).

    Returns
    -------
    ScenarioResult with one ScenarioRow per (index_shock, iv_shock) pair.

    Positions that are invalid (pos.is_valid is False) or lack a spot in
    twd_spot_map are silently skipped at the leg level — the aggregate P&L
    is computed from the available legs only.  (Mirrors aggregation.py's
    fail-soft approach.)
    """
    # Δt in years (calendar-day convention — must match bs_theta)
    delta_t: float = days_held / CALENDAR_DAYS_PER_YEAR

    # Pre-resolve Greeks once — avoids re-resolving per scenario
    resolved: list[tuple[int, Position, _PositionGreeks, float] | None] = []
    for pos_idx, pos in enumerate(positions):
        if not pos.is_valid:
            resolved.append(None)
            continue
        spot = twd_spot_map.get(pos.symbol)
        if spot is None:
            resolved.append(None)
            continue
        pg = _resolve_greeks(pos, pos_idx, greeks_map)
        if pg is None:
            resolved.append(None)
            continue
        resolved.append((pos_idx, pos, pg, spot))

    scenarios: list[ScenarioRow] = []

    for idx_shock in index_shocks:
        for iv_shock in iv_shocks:
            legs: list[LegPnL] = []
            for entry in resolved:
                if entry is None:
                    continue
                pos_idx, pos, pg, spot_twd = entry
                leg = _compute_leg_pnl(
                    pos, pos_idx, pg, spot_twd, idx_shock, iv_shock, delta_t
                )
                legs.append(leg)

            agg_delta = sum(leg.delta_pnl for leg in legs)
            agg_gamma = sum(leg.gamma_pnl for leg in legs)
            agg_vega  = sum(leg.vega_pnl  for leg in legs)
            agg_theta = sum(leg.theta_pnl for leg in legs)
            agg_total = sum(leg.total_pnl for leg in legs)

            scenarios.append(ScenarioRow(
                index_shock   = idx_shock,
                iv_shock      = iv_shock,
                legs          = legs,
                agg_delta_pnl = agg_delta,
                agg_gamma_pnl = agg_gamma,
                agg_vega_pnl  = agg_vega,
                agg_theta_pnl = agg_theta,
                agg_total_pnl = agg_total,
            ))

    return ScenarioResult(
        scenarios    = scenarios,
        days_held    = days_held,
        index_shocks = index_shocks,
        iv_shocks    = iv_shocks,
    )
