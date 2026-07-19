"""
Portfolio Greeks aggregation — three-layer output.

Layers
------
① by_currency
    Per-currency subtotals with no cross-currency conversion.

② consolidated_twd
    Partial or full consolidation to TWD.
    Always produced (never None).  When a currency has no FX rate, those
    positions are excluded and listed in ConsolidatedTWD.excluded_currencies.
    This implements fail-safe: TWD-dominated constraints are still evaluated
    even if USD positions cannot be converted.

③ index_point_exposure / unmapped_single_name_exposure
    TAIEX-linked derivatives (TXO, TXFF, …) are converted to exact delta
    exposure using actual contract specs — no beta proxy.

    Two metrics per IndexPointRecord:
      net_dollar_delta_per_point  : Σ(delta × qty × mult)
                                    Unit: TWD per 1-TAIEX-point move.
                                    This is NOT "points" — it is a sensitivity.
      txf_lot_equivalent          : net_dollar_delta_per_point / TXF_MULTIPLIER
                                    Number of TXFF contracts with equivalent
                                    first-order TAIEX exposure.

    Individual stocks (2330.TW, AAPL, …) go into unmapped_single_name_exposure
    with note "beta 未估計，見 Phase 3".

Hard constraints (fail-safe: always computed from available data)
-----------------------------------------------------------------
net_delta_pct_nav  : consolidated_net_delta_twd / portfolio_nav vs ±30 %
gamma_limit        : |net_gamma_twd| vs 1 M TWD
vega_limit         : |net_vega_twd|  vs 500 K TWD

If a currency was excluded from consolidated_twd due to missing FX rate,
the constraint detail field records which currencies were excluded so the
operator knows the figure is partial.  The constraint is still evaluated —
omitting a constraint because of a data gap is the unsafe choice.

Greek units and pre-conversion contract
---------------------------------------
delta_notional              = delta × qty × mult × spot  (position currency)
net_gamma                   = Σ(gamma × qty × mult)       (per spot²)
net_vega                    = Σ(vega  × qty × mult)       (per unit vol Δ)
net_theta                   = Σ(theta × qty × mult)       (per calendar day)
net_dollar_delta_per_point  = Σ(delta × qty × mult)       (TWD / TAIEX point)

Caller contract for non-TWD (e.g. USD) positions
-------------------------------------------------
For consolidated_twd, delta_notional is converted via FX (correct for a linear,
dimensionless quantity).  gamma/vega/theta obey DIFFERENT scaling laws under
currency redenomination — in particular, gamma ∝ 1/spot so γ_TWD = γ_USD / fx,
not γ_USD × fx.  Fixing the coefficient inside aggregate() would require per-Greek
conversion rules that are easy to copy-paste incorrectly.

Instead, the contract is: callers must price non-TWD options with spot and strike
already redenominated to TWD (spot_twd = spot_usd × fx, strike_twd = K_usd × fx)
before populating greeks_map.  Greeks that come out of pricing_router with a TWD
spot are natively in TWD units for all four Greeks — no per-Greek coefficient is
needed here.  aggregate() then sums gamma/vega/theta directly without any FX
factor; only delta_notional (computed from original-currency spot in by_currency)
is multiplied by the FX rate when building consolidated_twd.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from adapters.base import DataSourceAdapter
from agents.risk.position_loader import Position
from agents.risk.pricing_router import GreeksResult
from schemas.agent_signal import HardConstraint

# ─── Named constants ──────────────────────────────────────────────────────────

INDEX_DERIVATIVE_SYMBOLS: frozenset[str] = frozenset({"TXO", "TXFF", "TXF", "MXF"})
TAIEX_UNDERLYING: str = "TAIEX"

# Reference multiplier for TXFF (台指期) used to convert delta sensitivity to
# equivalent contract lots: lot_equivalent = net_dollar_delta_per_point / TXF_MULTIPLIER.
TXF_MULTIPLIER: float = 200.0   # TWD per 1-TAIEX-point per contract

NET_DELTA_PCT_NAV_LIMIT: float = 0.30
GAMMA_LIMIT_TWD: float         = 1_000_000.0
VEGA_LIMIT_TWD: float          = 500_000.0

UNMAPPED_BETA_NOTE: str = "beta 未估計，見 Phase 3"


# ─── Output dataclasses ───────────────────────────────────────────────────────

@dataclass
class CurrencySubtotal:
    """
    Layer ①: per-currency Greek totals, no FX conversion.

    net_delta_notional : Σ(delta × qty × mult × spot)  — position currency
    net_gamma          : Σ(gamma × qty × mult)
    net_vega           : Σ(vega  × qty × mult)
    net_theta          : Σ(theta × qty × mult), per calendar day
    """
    currency: str
    net_delta_notional: float
    net_gamma: float
    net_vega: float
    net_theta: float


@dataclass
class FXRateSnapshot:
    """FX rate used for TWD consolidation — full provenance carried."""
    pair: str
    rate: float
    source: str
    asof: datetime


@dataclass
class ConsolidatedTWD:
    """
    Layer ②: available positions converted to TWD (partial or full).

    Always produced.  When a currency has no FX rate, those positions are
    skipped and their currency codes appear in excluded_currencies.
    Constraints are computed from whatever is available, with exclusions
    noted in each HardConstraint.detail string.

    excluded_currencies : currencies whose positions could not be converted
                          (empty list = full consolidation)
    """
    net_delta_notional_twd: float
    net_gamma_twd: float
    net_vega_twd: float
    net_theta_twd: float
    fx_rates: list[FXRateSnapshot]
    excluded_currencies: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """True when all currencies were successfully converted."""
        return not self.excluded_currencies


@dataclass
class IndexPointRecord:
    """
    Layer ③a: TAIEX-linked derivatives — exact delta exposure.

    net_dollar_delta_per_point
        = Σ(delta × qty × mult) for positions in this group.
        Unit: TWD per 1-TAIEX-point move.
        This is a price sensitivity, NOT a point count or contract count.
        Example: −400 means the portfolio loses 400 TWD for every 1-point
        rise in TAIEX (equivalent to being short 2 TXFF contracts).

    txf_lot_equivalent
        = net_dollar_delta_per_point / TXF_MULTIPLIER
        The number of TXFF contracts (multiplier=200) whose delta matches
        this portfolio's TAIEX sensitivity.  Fractional lots are normal.
        Example: −400 / 200 = −2.0 lots (matches the 2 short TXFF directly).

    net_delta_notional_twd
        = net_dollar_delta_per_point × spot_index
        Total notional TAIEX delta in TWD (1 % index move approximation:
        divide by 100 to get TWD P&L for a 1 % move).

    method = "exact" — no beta proxy; uses actual contract specs.
    """
    underlying: str
    contributing_symbols: list[str]
    net_dollar_delta_per_point: float   # TWD / 1-TAIEX-point  (NOT in "points")
    txf_lot_equivalent: float           # = net_dollar_delta_per_point / TXF_MULTIPLIER
    spot_index: float
    net_delta_notional_twd: float       # = net_dollar_delta_per_point × spot_index
    method: str = "exact"


@dataclass
class UnmappedSingleName:
    """
    Layer ③b: individual stock / non-index option.
    Cannot be expressed in TAIEX points without a beta estimate (Phase 3).
    """
    symbol: str
    currency: str
    net_delta_notional: float
    note: str = UNMAPPED_BETA_NOTE


@dataclass
class AggregationResult:
    """Full three-layer aggregation output."""
    by_currency: dict[str, CurrencySubtotal]
    consolidated_twd: ConsolidatedTWD          # always present; may be partial
    index_point_exposure: list[IndexPointRecord]
    unmapped_single_name_exposure: list[UnmappedSingleName]
    hard_constraints: list[HardConstraint]
    errors: list[str] = field(default_factory=list)


# ─── Public API ───────────────────────────────────────────────────────────────

def aggregate(
    positions: list[Position],
    greeks_map: dict[int, GreeksResult],
    spot_map: dict[str, float],
    portfolio_nav: float,
    fx_adapter: DataSourceAdapter | None = None,
) -> AggregationResult:
    """
    Aggregate portfolio Greeks across three layers.

    Fail-safe design
    ----------------
    consolidated_twd is always produced from whatever currencies have FX rates.
    Currencies without a rate are excluded and noted in
    ConsolidatedTWD.excluded_currencies and in each HardConstraint.detail.
    Constraints are always evaluated — skipping a constraint because of a
    data gap is the unsafe choice.

    Parameters
    ----------
    positions     : from position_loader.load_positions() — all instrument types.
    greeks_map    : position_index → GreeksResult; required for option positions.
                    Stocks and futures use degenerate Greeks (delta=1, rest=0).
    spot_map      : symbol → underlying spot price in position currency.
                    Use the TAIEX index level for TXO/TXFF.
    portfolio_nav : NAV in TWD.  Denominator for net_delta_pct_nav constraint.
    fx_adapter    : FXAdapter for USDTWD.  When None (or fetch fails), USD
                    positions are excluded from consolidated_twd with an error
                    logged — TWD positions are still consolidated and checked.
    """
    errors: list[str] = []

    ccy_totals: dict[str, dict[str, float]] = {}
    idx_delta_pts: dict[str, float]    = {}
    idx_symbols:   dict[str, set[str]] = {}
    idx_spot:      dict[str, float]    = {}
    unmapped_map:  dict[tuple[str, str], float] = {}

    # ── Per-position pass ─────────────────────────────────────────────────────
    for pos_idx, pos in enumerate(positions):
        if not pos.is_valid:
            errors.append(
                f"[{pos_idx}] {pos.symbol}: invalid position skipped — {pos.errors}"
            )
            continue

        spot = spot_map.get(pos.symbol)
        if spot is None:
            errors.append(f"[{pos_idx}] {pos.symbol}: missing from spot_map, skipped")
            continue

        delta, gamma, vega, theta = _resolve_greeks(pos, pos_idx, greeks_map, errors)
        if delta is None:
            continue

        qty  = pos.quantity
        mult = pos.multiplier
        ccy  = pos.currency

        delta_notional = delta * qty * mult * spot
        gamma_contrib  = gamma * qty * mult
        vega_contrib   = vega  * qty * mult
        theta_contrib  = theta * qty * mult

        # Layer ①
        if ccy not in ccy_totals:
            ccy_totals[ccy] = {
                "net_delta_notional": 0.0,
                "net_gamma":          0.0,
                "net_vega":           0.0,
                "net_theta":          0.0,
            }
        ccy_totals[ccy]["net_delta_notional"] += delta_notional
        ccy_totals[ccy]["net_gamma"]          += gamma_contrib
        ccy_totals[ccy]["net_vega"]           += vega_contrib
        ccy_totals[ccy]["net_theta"]          += theta_contrib

        # Layer ③ routing
        if pos.symbol in INDEX_DERIVATIVE_SYMBOLS:
            und = TAIEX_UNDERLYING
            idx_delta_pts[und] = idx_delta_pts.get(und, 0.0) + delta * qty * mult
            idx_symbols.setdefault(und, set()).add(pos.symbol)
            idx_spot[und] = spot
        else:
            key = (pos.symbol, ccy)
            unmapped_map[key] = unmapped_map.get(key, 0.0) + delta_notional

    # ── Layer ①: output ───────────────────────────────────────────────────────
    by_currency = {
        ccy: CurrencySubtotal(currency=ccy, **totals)
        for ccy, totals in ccy_totals.items()
    }

    # ── Layer ②: FX rates ─────────────────────────────────────────────────────
    fx_rates_map: dict[str, float]     = {"TWD": 1.0}
    fx_snapshots: list[FXRateSnapshot] = []
    fx_fetch_failed: bool              = False

    if fx_adapter is not None:
        try:
            result = fx_adapter.fetch(pair="USDTWD")
            rate = float(result.payload.rate)
            fx_rates_map["USD"] = rate
            fx_snapshots.append(FXRateSnapshot(
                pair="USDTWD",
                rate=rate,
                source=result.source,
                asof=result.asof,
            ))
        except Exception as exc:
            errors.append(f"FX rate fetch failed: {exc}")
            fx_fetch_failed = True

    # ── Layer ②: partial consolidated_twd (fail-safe) ────────────────────────
    # Always produced.  Currencies with no rate are excluded and tracked.
    partial: dict[str, float] = {
        "net_delta_notional_twd": 0.0,
        "net_gamma_twd":          0.0,
        "net_vega_twd":           0.0,
        "net_theta_twd":          0.0,
    }
    excluded_ccys: list[str] = []

    for ccy, totals in ccy_totals.items():
        rate = fx_rates_map.get(ccy)
        if rate is not None:
            # delta_notional is in position currency → multiply by FX to get TWD.
            partial["net_delta_notional_twd"] += totals["net_delta_notional"] * rate
            # gamma/vega/theta: caller pre-converts non-TWD spot to TWD before
            # calling price_option, so greeks_map values are already in TWD units.
            # Do NOT apply FX here — that would double-convert (γ × fx² instead of γ/fx).
            partial["net_gamma_twd"]  += totals["net_gamma"]
            partial["net_vega_twd"]   += totals["net_vega"]
            partial["net_theta_twd"]  += totals["net_theta"]
        else:
            excluded_ccys.append(ccy)
            reason = "FX fetch failed" if fx_fetch_failed else "fx_adapter not provided"
            errors.append(
                f"No FX rate for {ccy!r} ({reason}): "
                f"{ccy} exposure excluded from consolidated_twd. "
                f"Constraints computed from available currencies only."
            )

    consolidated_twd = ConsolidatedTWD(
        **partial,
        fx_rates=fx_snapshots,
        excluded_currencies=excluded_ccys,
    )

    # ── Layer ③a ──────────────────────────────────────────────────────────────
    index_records = [
        IndexPointRecord(
            underlying=und,
            contributing_symbols=sorted(idx_symbols[und]),
            net_dollar_delta_per_point=dp,
            txf_lot_equivalent=dp / TXF_MULTIPLIER,
            spot_index=idx_spot[und],
            net_delta_notional_twd=dp * idx_spot[und],
            method="exact",
        )
        for und, dp in idx_delta_pts.items()
    ]

    # ── Layer ③b ──────────────────────────────────────────────────────────────
    unmapped = [
        UnmappedSingleName(symbol=sym, currency=ccy, net_delta_notional=dn)
        for (sym, ccy), dn in unmapped_map.items()
    ]

    # ── Hard constraints (always evaluated) ───────────────────────────────────
    hard_constraints = _build_hard_constraints(consolidated_twd, portfolio_nav)

    return AggregationResult(
        by_currency=by_currency,
        consolidated_twd=consolidated_twd,
        index_point_exposure=index_records,
        unmapped_single_name_exposure=unmapped,
        hard_constraints=hard_constraints,
        errors=errors,
    )


# ─── Private helpers ──────────────────────────────────────────────────────────

def _resolve_greeks(
    pos: Position,
    idx: int,
    greeks_map: dict[int, GreeksResult],
    errors: list[str],
) -> tuple[float | None, float, float, float]:
    if pos.instrument_type in ("stock", "futures"):
        return 1.0, 0.0, 0.0, 0.0
    if pos.is_option:
        if idx not in greeks_map:
            errors.append(
                f"[{idx}] {pos.symbol}: option Greeks missing from greeks_map, skipped"
            )
            return None, 0.0, 0.0, 0.0
        g = greeks_map[idx]
        return g.delta, g.gamma, g.vega, g.theta
    errors.append(
        f"[{idx}] {pos.symbol}: unknown instrument_type {pos.instrument_type!r}, skipped"
    )
    return None, 0.0, 0.0, 0.0


def _build_hard_constraints(
    consolidated_twd: ConsolidatedTWD,
    portfolio_nav: float,
) -> list[HardConstraint]:
    """
    Build the three standard hard constraints.

    Always called — constraints use whatever is in consolidated_twd (partial
    or full).  Excluded currencies are noted in each constraint's detail field
    so operators know the figure may understate or overstate the true exposure.
    """
    nav = portfolio_nav if portfolio_nav > 0.0 else math.nan

    net_delta_pct = consolidated_twd.net_delta_notional_twd / nav
    gamma_abs     = abs(consolidated_twd.net_gamma_twd)
    vega_abs      = abs(consolidated_twd.net_vega_twd)

    # Exclusion note appended to detail when consolidation is partial
    excl = consolidated_twd.excluded_currencies
    excl_note = (
        f"  ⚠ 以下幣別因缺少匯率已排除: {excl}" if excl else ""
    )

    return [
        HardConstraint(
            type="net_delta_pct_nav",
            current=net_delta_pct,
            limit=NET_DELTA_PCT_NAV_LIMIT,
            breached=abs(net_delta_pct) > NET_DELTA_PCT_NAV_LIMIT,
            detail=(
                f"net_delta_twd={consolidated_twd.net_delta_notional_twd:+,.0f} TWD / "
                f"nav={portfolio_nav:,.0f} TWD = {net_delta_pct:+.1%}"
                f"{excl_note}"
            ),
        ),
        HardConstraint(
            type="gamma_limit",
            current=gamma_abs,
            limit=GAMMA_LIMIT_TWD,
            breached=gamma_abs > GAMMA_LIMIT_TWD,
            detail=f"|net_gamma_twd|={gamma_abs:,.4f}{excl_note}",
        ),
        HardConstraint(
            type="vega_limit",
            current=vega_abs,
            limit=VEGA_LIMIT_TWD,
            breached=vega_abs > VEGA_LIMIT_TWD,
            detail=f"|net_vega_twd|={vega_abs:,.2f}{excl_note}",
        ),
    ]
