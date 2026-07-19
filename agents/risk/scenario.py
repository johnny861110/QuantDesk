"""
Scenario stress-tester — ΔP ≈ Δ×ΔS + ½×Γ×(ΔS)² + ν×ΔIV + Θ×Δt

Scope of the index shock
------------------------
The ΔS shock is applied ONLY to index-linked derivatives — positions whose
symbol is in INDEX_DERIVATIVE_SYMBOLS (TXFF, TXO, TXF, MXF, …).  These are
the instruments whose price is mechanically tied 1:1 to the TAIEX index level;
applying "index moves X %" to them requires no beta assumption.

Individual stocks (2330.TW, AAPL) and stock options (AAPL calls) are NOT
included in the scenario P&L.  Their price co-movement with the index depends
on an unknown beta, which Phase 3 will estimate.  Silently applying
shock × 1 would embed a hidden beta=1 assumption that contradicts
aggregation.py's explicit refusal to assume beta (unmapped_single_name_exposure).

Unmapped positions are tracked in ScenarioResult.unmapped_symbols with a
"beta 未估計" note.  The tech-debt is recorded in docs/tasks/phase_2.md.

⚠️  Δt day-count convention (must match black_scholes.py)
-----------------------------------------------------------
bs_theta is already expressed in calendar-day units (annual_theta / 365).
Δt here must therefore use CALENDAR days / 365:
  Δt = calendar_days / 365   (e.g. 1 day held → Δt = 1/365 ≈ 0.002740)

DO NOT switch to trading-day convention (/ 252).  Mixing conventions produces
a systematic ~45 % theta-P&L over-estimate that is invisible to most callers.
See docs/tasks/phase_2.md ⚠️ note.

Default scenario matrix
-----------------------
INDEX_SHOCKS = (−5%, −3%, −1%, +1%, +3%, +5%)  — 6 shocks
IV_SHOCKS    = (−20 pp, −10 pp, 0, +10 pp, +20 pp)  — 5 shocks
= 30 scenarios in total.

Units
-----
All P&L figures are in TWD.  Callers are expected to pass TWD-denominated
spots (twd_spot_map).  For USD-denominated index derivatives the caller must
pre-convert (spot_twd = spot_usd × fx) — the same contract as aggregation.py.

Output types
------------
LegPnL        — P&L components for one index-linked position leg in one scenario
ScenarioRow   — one (index_shock, iv_shock) scenario: per-leg + aggregate
ScenarioResult — all 30 scenarios + list of symbols excluded (beta unknown)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

# INDEX_DERIVATIVE_SYMBOLS is the single source-of-truth for which symbols are
# index-linked.  Importing from aggregation.py keeps this list in sync with the
# set that aggregation routes to index_point_exposure.
from agents.risk.aggregation import INDEX_DERIVATIVE_SYMBOLS
from agents.risk.position_loader import Position
from agents.risk.pricing_router import GreeksResult

# ─── Day-count constant ───────────────────────────────────────────────────────

# Calendar-day denominator: MUST match black_scholes.py theta convention.
# DO NOT change to 252 (trading-day basis) — see module docstring.
CALENDAR_DAYS_PER_YEAR: int = 365

# ─── Beta / unmapped note ─────────────────────────────────────────────────────

# Displayed alongside symbols excluded from scenario P&L because their
# beta vs. the index shock is unknown.  Phase 3 will supply beta estimates.
BETA_NOT_ESTIMATED_NOTE: str = "beta 未估計，此情境下無法精確評估該部位損益"

# ─── Default scenario matrix ──────────────────────────────────────────────────

# Six underlying shocks in decimal (−5 % to +5 %)
INDEX_SHOCKS: tuple[float, ...] = (-0.05, -0.03, -0.01, +0.01, +0.03, +0.05)

# Five IV shocks in absolute decimal points (−20 pp to +20 pp)
IV_SHOCKS: tuple[float, ...] = (-0.20, -0.10, 0.00, +0.10, +0.20)


# ─── Output dataclasses ───────────────────────────────────────────────────────

@dataclass
class LegPnL:
    """
    P&L decomposition for one index-linked position leg under one scenario.

    Only positions whose symbol is in INDEX_DERIVATIVE_SYMBOLS appear here.
    All values are in TWD.

    delta_pnl = delta × ΔS × qty × mult
    gamma_pnl = ½ × gamma × ΔS² × qty × mult     (convexity term)
    vega_pnl  = vega × Δσ × qty × mult
    theta_pnl = theta × Δt × qty × mult           (Δt = days / 365)
    total_pnl = sum of the above four terms

    For futures (degenerate Greeks): only delta_pnl is non-zero.
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

    legs
        LegPnL for every index-linked position.  Individual stocks and
        non-index options are absent — see ScenarioResult.unmapped_symbols.

    agg_* fields
        Sums over legs only (index-linked subset of the portfolio).
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
    Full scenario analysis: 30 ScenarioRows (6 index × 5 IV shocks).

    scenarios
        One ScenarioRow per (index_shock, iv_shock) pair.
        P&L covers index-linked positions only.

    unmapped_symbols
        Symbols whose positions were excluded because their beta vs. the
        TAIEX index shock is unknown.  Each symbol appears at most once.
        Tech-debt: Phase 3 will supply beta estimates to include these.

    days_held, index_shocks, iv_shocks
        Parameters used to build this result — stored for traceability.
    """
    scenarios: list[ScenarioRow]
    days_held: float
    index_shocks: tuple[float, ...]
    iv_shocks: tuple[float, ...]
    unmapped_symbols: list[str] = field(default_factory=list)


# ─── Internal helpers ─────────────────────────────────────────────────────────

class _PositionGreeks(NamedTuple):
    """Per-unit Greeks for one position (before qty × mult scaling)."""
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
    Return per-unit Greeks for an index-linked position.

    For futures: degenerate delta=1, rest=0.
    For options: from greeks_map[pos_idx]; returns None if entry is missing.
    """
    if pos.instrument_type == "futures":
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
    Apply ΔP ≈ Δ×ΔS + ½×Γ×(ΔS)² + ν×ΔIV + Θ×Δt to one index-linked leg.

    ΔS = index_shock × spot_twd   (TWD)
    All monetary values are in TWD (assumes spot_twd was pre-converted).
    """
    delta_s = index_shock * spot_twd

    qty  = pos.quantity
    mult = pos.multiplier

    delta_pnl = pg.delta * delta_s            * qty * mult
    gamma_pnl = 0.5 * pg.gamma * delta_s ** 2 * qty * mult
    vega_pnl  = pg.vega  * iv_shock            * qty * mult
    theta_pnl = pg.theta * delta_t             * qty * mult

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

    Only index-linked positions (symbol ∈ INDEX_DERIVATIVE_SYMBOLS) are
    included in the P&L.  Individual stocks and non-index options are
    tracked in ScenarioResult.unmapped_symbols with BETA_NOT_ESTIMATED_NOTE.

    Parameters
    ----------
    positions     : list[Position] from position_loader.
    greeks_map    : {position_index: GreeksResult} for option legs.
                    Futures legs use degenerate Greeks (delta=1, rest=0).
    twd_spot_map  : {symbol: spot_in_TWD}.  For USD-denominated index
                    derivatives the caller must pre-convert (spot × fx).
    days_held     : holding period in calendar days → Δt = days / 365.
    index_shocks  : fractional index-level shocks. Default: INDEX_SHOCKS.
    iv_shocks     : absolute IV shocks (decimal). Default: IV_SHOCKS.

    Returns
    -------
    ScenarioResult with one ScenarioRow per (index_shock, iv_shock) pair.
    Positions that are invalid or lack a twd_spot_map entry are silently
    skipped (mirrors aggregation.py's fail-soft approach).
    """
    delta_t: float = days_held / CALENDAR_DAYS_PER_YEAR

    # ── Pre-classify positions ─────────────────────────────────────────────
    # Split into index-linked (include in P&L) and unmapped (beta unknown).
    # Resolve Greeks once — avoids repeating per scenario.
    index_resolved: list[tuple[int, Position, _PositionGreeks, float]] = []
    unmapped_set: set[str] = set()

    for pos_idx, pos in enumerate(positions):
        if not pos.is_valid:
            continue

        spot = twd_spot_map.get(pos.symbol)
        if spot is None:
            continue

        if pos.symbol not in INDEX_DERIVATIVE_SYMBOLS:
            # Individual stock / non-index option — beta vs. TAIEX unknown.
            # Do NOT apply index_shock: that would embed beta=1 silently.
            unmapped_set.add(pos.symbol)
            continue

        pg = _resolve_greeks(pos, pos_idx, greeks_map)
        if pg is None:
            # Option without a GreeksResult — skip (missing IV/pricing)
            continue

        index_resolved.append((pos_idx, pos, pg, spot))

    unmapped_symbols = sorted(unmapped_set)

    # ── Build scenario grid ────────────────────────────────────────────────
    scenarios: list[ScenarioRow] = []

    for idx_shock in index_shocks:
        for iv_shock in iv_shocks:
            legs: list[LegPnL] = []

            for pos_idx, pos, pg, spot_twd in index_resolved:
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
        scenarios        = scenarios,
        days_held        = days_held,
        index_shocks     = index_shocks,
        iv_shocks        = iv_shocks,
        unmapped_symbols = unmapped_symbols,
    )
