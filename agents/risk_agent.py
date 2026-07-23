"""
Risk Agent — Phase 2: Greeks 風控引擎整合 (subtask 8)

Integrates subtasks 1-7 and emits AgentSignal.

Public API
----------
build_risk_signal(...)  pure deterministic assembly from pre-computed results
run_risk_agent(...)     full pipeline (load → fx → price → aggregate → scenario → signal)
create_risk_graph()     LangGraph CompiledGraph for Supervisor integration

Pipeline order
--------------
load_positions → fetch_fx → price_options → aggregate → run_scenarios → build_signal

Design (CLAUDE.md §三條不可違反)
---------------------------------
① Deterministic / LLM separation
    All Greeks, constraints, and scenario P&L are pure-math Python (subtasks 1-7).
    _build_narrative() is deterministic (f-strings from metrics).
    Numbers are intentionally absent from narrative prose to avoid Verifier
    false-positives from unit/percentage formatting mismatches; they live in
    metrics + key_evidence instead.

② Risk is a hard constraint, not a vote
    hard_constraints from aggregation.py flow directly into AgentSignal.
    breached=True → Signal.BEARISH.  Supervisor MUST honour this;
    RiskAgent does not soften or filter any constraint.

③ Provenance on every evidence item
    All Evidence entries carry source="risk_agent:aggregation" and asof=datetime.

Tech-debt (Phase 3/4)
---------------------
- PLACEHOLDER_IV=0.20: replace with FinMind backed-out IV (Phase 4).
- DEFAULT_SPOT_MAP:    replace with live FinMind spot prices (Phase 4).
- sector_concentration constraint: Phase 3 sector classification.
- beta_map for unmapped single-name scenario P&L: Phase 3 rolling-beta.
- staleness_sec=0.0: wire up proper source timestamp from adapter (Phase 4).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Optional, TypedDict

from langgraph.graph import END, StateGraph

from adapters.base import DataSourceAdapter
from agents.verifier import check_narrative
from agents.risk.aggregation import AggregationResult, aggregate
from agents.risk.position_loader import (
    PortfolioConfig,
    Position,
    load_portfolio,
)
from agents.risk.pricing_router import (
    DEFAULT_DIVIDEND_YIELD,
    DEFAULT_RISK_FREE_RATE,
    GreeksResult,
    OptionSpec,
    price_option,
)
from agents.risk.scenario import ScenarioResult, run_scenarios
from schemas.agent_signal import (
    AgentSignal,
    AgentType,
    DataQuality,
    Evidence,
    HardConstraint,
    Signal,
    Target,
    TimeHorizon,
)

# ─── Placeholder market data ──────────────────────────────────────────────────
# tech-debt: Phase 4 will replace these with live FinMind prices.

PLACEHOLDER_IV: float = 0.20   # 20 % annualised — used for all options

# Directional threshold: |net_delta_pct_nav| > 1% NAV is considered directional.
# Inside this band the portfolio is treated as effectively delta-neutral.
DELTA_NEUTRAL_BAND_PCT: float = 0.01

# ⚠️ 暫定值：IV 缺失時每單位 missing_fraction 的信心扣分係數。
# 設計理由：
#   - 最差情況（全部 IV 缺失，missing_fraction=1.0）扣 0.30，使 confidence
#     從 1.0 降至 0.70，低於多數「可執行閾值」（通常 ≥ 0.80），強迫使用者先處理資料問題。
#   - 不設為 0.30 以外的較大值，因為 Greeks 僅低估（不是完全錯誤）；
#     placeholder IV=20% 仍有一定參考性，但需告知使用者不可全信。
#   - 公式：conf -= _IV_MISSING_CONFIDENCE_PENALTY × missing_fraction
#     missing_fraction = (n_options − n_priced) / n_options
#     此為線性比例，確保「部分失敗」到「完全失敗」之間連續無跳動。
#   - Phase 4 FinMind 接入後，若 IV 反推成功率提高，可重新校準此值。
_IV_MISSING_CONFIDENCE_PENALTY: Final[float] = 0.30

DEFAULT_SPOT_MAP: dict[str, float] = {
    "2330.TW": 850.0,      # TWD — TSMC placeholder
    "AAPL":    195.0,      # USD — AAPL placeholder
    "TXFF":  22_000.0,     # TWD — TAIEX placeholder
    "TXO":   22_000.0,     # TWD — same TAIEX level for option pricing
}


# ─── LangGraph state ──────────────────────────────────────────────────────────

class RiskAgentState(TypedDict):
    # inputs
    positions_path: Optional[Path]
    spot_map: dict[str, float]
    fx_adapter: Optional[DataSourceAdapter]
    asof: datetime
    days_held: float
    # intermediates
    portfolio_cfg: Optional[PortfolioConfig]
    usdtwd: Optional[float]
    greeks_map: dict[int, GreeksResult]
    twd_spot_map: dict[str, float]
    agg_result: Optional[AggregationResult]
    scenario_result: Optional[ScenarioResult]
    pipeline_errors: list[str]
    # output
    signal: Optional[AgentSignal]


# ─── Node functions (each pure, testable in isolation) ────────────────────────

def _node_load(state: RiskAgentState) -> RiskAgentState:
    """Load positions.yaml → PortfolioConfig."""
    errors = list(state["pipeline_errors"])
    try:
        path = state["positions_path"]
        cfg: PortfolioConfig | None = (
            load_portfolio(path) if path else load_portfolio()
        )
    except Exception as exc:
        errors.append(f"load_portfolio failed: {exc}")
        cfg = None
    return {**state, "portfolio_cfg": cfg, "pipeline_errors": errors}


def _node_fetch_fx(state: RiskAgentState) -> RiskAgentState:
    """Fetch USDTWD.  Degrades gracefully (usdtwd=None) if adapter absent."""
    adapter = state["fx_adapter"]
    errors = list(state["pipeline_errors"])
    if adapter is None:
        return {**state, "usdtwd": None}
    try:
        data = adapter.fetch(pair="USDTWD")
        return {**state, "usdtwd": float(data.payload.rate)}
    except Exception as exc:
        errors.append(f"FX fetch failed: {exc}")
        return {**state, "usdtwd": None, "pipeline_errors": errors}


def _node_price(state: RiskAgentState) -> RiskAgentState:
    """
    Price all valid option legs with placeholder IV.

    USD spot/strike are pre-converted to TWD (caller contract for pricing_router
    and aggregation.py: γ_TWD = γ_USD/fx, not × fx).  twd_spot_map is built
    here for scenario.py consumption.
    """
    cfg = state["portfolio_cfg"]
    if cfg is None:
        return {**state, "greeks_map": {}, "twd_spot_map": {}}

    positions = cfg.positions
    spot_map  = state["spot_map"]
    usdtwd    = state["usdtwd"]
    today     = state["asof"].date()
    errors    = list(state["pipeline_errors"])

    # Build symbol → currency lookup from first occurrence in positions
    sym_ccy: dict[str, str] = {}
    for pos in positions:
        if pos.symbol not in sym_ccy:
            sym_ccy[pos.symbol] = pos.currency

    # Pre-convert USD spot prices → TWD
    twd_spot_map: dict[str, float] = {}
    for sym, px in spot_map.items():
        if sym_ccy.get(sym) == "USD" and usdtwd is not None:
            twd_spot_map[sym] = px * usdtwd
        else:
            twd_spot_map[sym] = px

    greeks_map: dict[int, GreeksResult] = {}

    for pos_idx, pos in enumerate(positions):
        if not pos.is_option or not pos.is_valid:
            continue
        # All option fields are guaranteed non-None for valid positions by position_loader.
        if pos.option_type is None or pos.style is None or pos.strike is None:
            continue

        spot_orig = spot_map.get(pos.symbol)
        if spot_orig is None:
            errors.append(f"[{pos_idx}] {pos.symbol}: not in spot_map, skipped")
            continue

        expiry_date = pos.expiry_date
        if expiry_date is None:
            errors.append(f"[{pos_idx}] {pos.symbol}: expiry parse failed, skipped")
            continue

        T = (expiry_date - today).days / 365.0
        if T <= 0:
            errors.append(f"[{pos_idx}] {pos.symbol}: T={T:.4f} ≤ 0, skipped")
            continue

        # USD positions: pre-convert to TWD for correct gamma units
        if pos.currency == "USD" and usdtwd is not None:
            spot_twd   = spot_orig * usdtwd
            strike_twd = pos.strike * usdtwd
        else:
            spot_twd   = spot_orig
            strike_twd = pos.strike

        spec = OptionSpec(
            S=spot_twd, K=strike_twd, T=T,
            r=DEFAULT_RISK_FREE_RATE,
            q=DEFAULT_DIVIDEND_YIELD,
            sigma=PLACEHOLDER_IV,
            option_type=pos.option_type,
            style=pos.style,
            spot_currency="TWD",
        )
        try:
            greeks_map[pos_idx] = price_option(spec)
        except Exception as exc:
            errors.append(f"[{pos_idx}] {pos.symbol}: pricing error: {exc}")

    return {
        **state,
        "greeks_map": greeks_map,
        "twd_spot_map": twd_spot_map,
        "pipeline_errors": errors,
    }


def _node_aggregate(state: RiskAgentState) -> RiskAgentState:
    """Aggregate portfolio Greeks — three-layer output + hard_constraints."""
    cfg = state["portfolio_cfg"]
    if cfg is None:
        return {**state, "agg_result": None}

    result = aggregate(
        positions   = cfg.positions,
        greeks_map  = state["greeks_map"],
        spot_map    = state["spot_map"],
        portfolio_nav = cfg.portfolio_nav,
        fx_adapter  = state["fx_adapter"],
    )
    errors = list(state["pipeline_errors"]) + result.errors
    return {**state, "agg_result": result, "pipeline_errors": errors}


def _node_scenario(state: RiskAgentState) -> RiskAgentState:
    """Run 30-scenario stress test (index ±1%/3%/5% × IV ±10%/20%)."""
    cfg = state["portfolio_cfg"]
    if cfg is None:
        return {**state, "scenario_result": None}

    sc = run_scenarios(
        positions    = cfg.positions,
        greeks_map   = state["greeks_map"],
        twd_spot_map = state["twd_spot_map"],
        days_held    = state["days_held"],
    )
    return {**state, "scenario_result": sc}


def _node_signal(state: RiskAgentState) -> RiskAgentState:
    """Assemble AgentSignal from all intermediate results."""
    cfg = state["portfolio_cfg"]
    agg = state["agg_result"]
    sc  = state["scenario_result"]

    if cfg is None or agg is None or sc is None:
        missing = [
            name for name, val in [("positions", cfg), ("agg", agg), ("scenario", sc)]
            if val is None
        ]
        signal = _error_signal(
            asof   = state["asof"],
            errors = state["pipeline_errors"] + [f"missing intermediates: {missing}"],
        )
    else:
        signal = build_risk_signal(
            positions     = cfg.positions,
            greeks_map    = state["greeks_map"],
            agg_result    = agg,
            scenario_result = sc,
            portfolio_nav = cfg.portfolio_nav,
            asof          = state["asof"],
            errors        = state["pipeline_errors"],
        )

    return {**state, "signal": signal}


# ─── Core: deterministic AgentSignal assembly ─────────────────────────────────

def build_risk_signal(
    positions: list[Position],
    greeks_map: dict[int, GreeksResult],
    agg_result: AggregationResult,
    scenario_result: ScenarioResult,
    portfolio_nav: float,
    asof: datetime,
    errors: list[str] | None = None,
) -> AgentSignal:
    """
    Assemble AgentSignal from pre-computed Greeks, aggregation, and scenarios.

    This is a pure deterministic function (no I/O).  Tests can call it directly
    with synthetic inputs without touching the network or YAML files.

    All numbers flow from agg_result / scenario_result / positions into metrics
    and key_evidence.  The narrative is qualitative-only (no numbers) to keep
    the Verifier contract clean.

    Parameters
    ----------
    positions       : list[Position] from position_loader
    greeks_map      : {position_index: GreeksResult} for priced options
    agg_result      : output of aggregation.aggregate()
    scenario_result : output of scenario.run_scenarios()
    portfolio_nav   : NAV in TWD (from PortfolioConfig.portfolio_nav)
    asof            : timestamp propagated to every Evidence entry
    errors          : accumulated pipeline errors (carried into AgentSignal.errors)
    """
    all_errors: list[str] = list(errors or [])
    c = agg_result.consolidated_twd
    covered_symbols = sorted({p.symbol for p in positions if p.is_valid})

    # ── Derived scalars ───────────────────────────────────────────────────────
    nav = portfolio_nav if portfolio_nav > 0.0 else 1.0
    net_delta_pct_nav = c.net_delta_notional_twd / nav

    txf_lot  = 0.0
    taiex_ddp = 0.0
    if agg_result.index_point_exposure:
        rec       = agg_result.index_point_exposure[0]
        txf_lot   = rec.txf_lot_equivalent
        taiex_ddp = rec.net_dollar_delta_per_point

    # Worst-case scenario P&L across all 30 scenarios
    worst_row      = min(scenario_result.scenarios, key=lambda r: r.agg_total_pnl)
    worst_pnl      = worst_row.agg_total_pnl
    worst_shock_str = (
        f"index{worst_row.index_shock:+.0%}/IV{worst_row.iv_shock:+.0%}"
    )

    # Option pricing completeness
    option_valid = [p for p in positions if p.is_option and p.is_valid]
    n_options    = len(option_valid)
    n_priced     = sum(
        1 for idx, p in enumerate(positions)
        if p.is_option and p.is_valid and idx in greeks_map
    )
    completeness = (n_priced / n_options) if n_options > 0 else 1.0

    # ── Metrics (all deterministic) ───────────────────────────────────────────
    metrics: dict[str, Any] = {
        # Consolidated Greeks
        "net_delta_notional_twd":   c.net_delta_notional_twd,
        "net_delta_pct_nav":        net_delta_pct_nav,
        "net_gamma_twd":            c.net_gamma_twd,
        "net_vega_twd":             c.net_vega_twd,
        "net_theta_twd":            c.net_theta_twd,
        # TAIEX index exposure
        "txf_lot_equivalent":       txf_lot,
        "taiex_delta_per_point_twd": taiex_ddp,
        # Scenario (index-linked positions only)
        "scenario_worst_pnl_twd":   worst_pnl,
        "scenario_worst_shock":     worst_shock_str,
        "scenario_coverage":        "INDEX_DERIVATIVE_SYMBOLS（TXFF、TXO）",
        "scenario_unmapped_symbols": scenario_result.unmapped_symbols,
        # Data provenance
        "positions_count":          len(positions),
        "options_total":            n_options,
        "options_priced":           n_priced,
        "iv_source":                "placeholder_0.20",   # tech-debt Phase 4
        "consolidation_complete":   c.is_complete,
        "excluded_currencies":      c.excluded_currencies,
        "portfolio_nav":            portfolio_nav,
        "covered_symbols":          covered_symbols,
        "target_note":              "PORTFOLIO — aggregate over all positions; see covered_symbols for scope",
    }

    # ── Key evidence (source + asof on every item) ────────────────────────────
    key_evidence: list[Evidence] = [
        Evidence(
            claim  = "portfolio net delta (% of NAV)",
            value  = net_delta_pct_nav,
            source = "risk_agent:aggregation",
            asof   = asof,
        ),
        Evidence(
            claim  = "portfolio net gamma (TWD per spot² unit)",
            value  = c.net_gamma_twd,
            source = "risk_agent:aggregation",
            asof   = asof,
        ),
        Evidence(
            claim  = "portfolio net vega (TWD per 1-unit vol move)",
            value  = c.net_vega_twd,
            source = "risk_agent:aggregation",
            asof   = asof,
        ),
        Evidence(
            claim  = "TAIEX lot equivalent (台指期等值口數)",
            value  = txf_lot,
            source = "risk_agent:aggregation",
            asof   = asof,
        ),
        Evidence(
            claim  = f"worst-case scenario P&L ({worst_shock_str})",
            value  = worst_pnl,
            source = "risk_agent:scenario",
            asof   = asof,
        ),
    ]

    # ── IV missing count ──────────────────────────────────────────────────────
    iv_missing_count = n_options - n_priced
    metrics["iv_missing_count"] = iv_missing_count

    # ── Signal and confidence ─────────────────────────────────────────────────
    signal     = _determine_signal(net_delta_pct_nav)
    confidence = _compute_confidence(agg_result, positions, greeks_map)

    # ── Hard constraint annotation for partial IV missing ─────────────────────
    # Follow FX-exclusion pattern from aggregation.py: append note to detail.
    if iv_missing_count > 0:
        iv_note = (
            f"  ⚠ IV缺失：{iv_missing_count}/{n_options}個選擇權未定價，"
            "Greeks可能低估"
        )
        annotated_constraints = [
            HardConstraint(
                type=hc.type,
                current=hc.current,
                limit=hc.limit,
                breached=hc.breached,
                detail=(hc.detail or "") + iv_note,
                verifiable=False,   # Greeks underestimated; breached=False is unreliable
            )
            for hc in agg_result.hard_constraints
        ]
    else:
        annotated_constraints = list(agg_result.hard_constraints)

    narrative = _build_narrative(metrics, annotated_constraints)
    verifier_errors = check_narrative(narrative, metrics)
    if verifier_errors:
        all_errors.extend(verifier_errors)

    # IV failure error messages
    if n_options > 0 and n_priced == 0:
        all_errors.append(
            "所有選擇權 Greeks 計算失敗（IV/spot 缺失）。"
            "無法評估凸性風險（gamma/vega）。"
            "保守處置：confidence 已降低；請確認 spot_map 與到期日。"
        )
    elif iv_missing_count > 0:
        all_errors.append(
            f"部分選擇權 Greeks 計算失敗（{iv_missing_count}/{n_options}）。"
            "凸性風險（gamma/vega）可能低估；請確認 spot_map 與到期日。"
        )

    return AgentSignal(
        agent           = AgentType.RISK,
        target          = Target(symbol="PORTFOLIO", market="TW", asof=asof),
        signal          = signal,
        confidence      = confidence,
        time_horizon    = TimeHorizon.SHORT,
        key_evidence    = key_evidence,
        hard_constraints = annotated_constraints,
        metrics         = metrics,
        narrative       = narrative,
        data_quality    = DataQuality(
            completeness  = completeness,
            staleness_sec = 0.0,   # tech-debt Phase 4: wire timestamp from adapter
            confidence    = confidence,
        ),
        errors = all_errors,
    )


# ─── Private helpers ──────────────────────────────────────────────────────────

def _determine_signal(net_delta_pct_nav: float) -> Signal:
    """
    Signal reflects directional delta exposure, NOT constraint breach status.

    Breach status belongs exclusively in hard_constraints.  Supervisor Phase 5
    applies the hard-constraint rule engine independently; contaminating signal
    with breach status would corrupt the directional dimension.

    BEARISH  : net short > 1 % of NAV
    BULLISH  : net long  > 1 % of NAV
    NEUTRAL  : effectively delta-neutral (|pct| ≤ 1 %)
    """
    if net_delta_pct_nav < -DELTA_NEUTRAL_BAND_PCT:
        return Signal.BEARISH
    if net_delta_pct_nav > DELTA_NEUTRAL_BAND_PCT:
        return Signal.BULLISH
    return Signal.NEUTRAL


def _compute_confidence(
    agg_result: AggregationResult,
    positions: list[Position],
    greeks_map: dict[int, GreeksResult],
) -> float:
    """
    Start at 1.0; reduce for data gaps.

    −0.20                   : FX missing → USD exposure excluded from consolidated_twd
    −0.10                   : any invalid position (bad schema / expired option)
    −_IV_MISSING_CONFIDENCE_PENALTY × missing_fraction: proportional IV penalty（⚠️ 暫定值）
                              complete failure (0 priced):   −0.30
                              partial failure (k/n priced):  −0.30 × (1 − k/n)
                              all priced:                     0
                              連續線性：部分→完全失敗無跳動。
    Minimum: 0.10
    """
    conf = 1.0

    if agg_result.consolidated_twd.excluded_currencies:
        conf -= 0.20

    if any(not p.is_valid for p in positions):
        conf -= 0.10

    option_valid = [p for p in positions if p.is_option and p.is_valid]
    if option_valid:
        n_options_conf = len(option_valid)
        n_priced_conf = sum(
            1 for idx, p in enumerate(positions)
            if p.is_option and p.is_valid and idx in greeks_map
        )
        # 線性比例懲罰：missing_fraction × _IV_MISSING_CONFIDENCE_PENALTY（暫定 0.30）
        # 單一公式覆蓋全範圍，確保部分失敗→完全失敗之間無不連續跳動：
        #   missing_fraction=0.0 (all priced) → penalty=0.00
        #   missing_fraction=0.5 (half priced)→ penalty=0.15
        #   missing_fraction=1.0 (none priced)→ penalty=0.30
        missing_fraction = 1.0 - n_priced_conf / n_options_conf
        conf -= _IV_MISSING_CONFIDENCE_PENALTY * missing_fraction

    return max(0.10, conf)


def _build_narrative(
    metrics: dict[str, Any],
    hard_constraints: list[HardConstraint],
) -> str:
    """
    Deterministic risk summary — qualitative only, no numbers in prose.

    Numbers are omitted from the text to avoid Verifier false-positives
    (e.g. "−10.5 %" in text vs 0.105 decimal in metrics).
    Exact values are in metrics and key_evidence.
    """
    net_delta_pct  = float(metrics.get("net_delta_pct_nav", 0.0))
    net_gamma      = float(metrics.get("net_gamma_twd", 0.0))
    net_vega       = float(metrics.get("net_vega_twd", 0.0))
    unmapped: list[str] = list(metrics.get("scenario_unmapped_symbols", []))
    breached       = [hc.type for hc in hard_constraints if hc.breached]

    delta_dir = "空頭偏重" if net_delta_pct < 0 else "多頭偏重"
    gamma_dir = "偏空（凸性不利大行情）" if net_gamma < 0 else "偏多（有利大行情）"
    vega_dir  = "偏空（IV 上升受損）"    if net_vega  < 0 else "偏多（IV 上升受益）"

    parts: list[str] = [
        f"組合淨 delta {delta_dir}，淨 gamma {gamma_dir}，淨 vega {vega_dir}。"
    ]

    if breached:
        parts.append(
            f"⚠️ 風控限制已觸限：{', '.join(breached)}。請立即檢視並調整部位。"
        )
    else:
        parts.append("所有風控限制均在範圍內。")

    if unmapped:
        parts.append(
            f"個股部位（{', '.join(unmapped)}）beta 未估計，"
            "未納入情境壓力測試（Phase 3 補 rolling-beta 後補入）。"
        )

    parts.append(
        "IV 來源：placeholder（Phase Four FinMind 接入後替換）。"
        "數值詳見 metrics 及 key_evidence。"
    )

    return " ".join(parts)


def _error_signal(asof: datetime, errors: list[str]) -> AgentSignal:
    """Fallback when the pipeline fails early — confidence=0 signals total failure."""
    return AgentSignal(
        agent            = AgentType.RISK,
        target           = Target(symbol="PORTFOLIO", market="TW", asof=asof),
        signal           = Signal.NEUTRAL,
        confidence       = 0.0,
        time_horizon     = TimeHorizon.SHORT,
        key_evidence     = [],
        hard_constraints = [],
        metrics          = {},
        narrative        = "風控評估管線初始化失敗，無法產出信號。詳見 errors 欄位。",
        data_quality     = DataQuality(
            completeness=0.0, staleness_sec=0.0, confidence=0.0
        ),
        errors = errors,
    )


# ─── Full pipeline (convenience wrapper) ──────────────────────────────────────

def run_risk_agent(
    *,
    spot_map: dict[str, float] | None = None,
    positions_path: Path | None = None,
    fx_adapter: DataSourceAdapter | None = None,
    asof: datetime | None = None,
    days_held: float = 1.0,
) -> AgentSignal:
    """
    Run the full risk pipeline and return AgentSignal.

    Parameters
    ----------
    spot_map       : {symbol: price_in_position_currency} — USD for AAPL, TWD for TXO.
                     Defaults to DEFAULT_SPOT_MAP (placeholder values).
    positions_path : path to positions YAML.  Defaults to config/positions.yaml.
    fx_adapter     : USDTWD adapter.  None → USD positions excluded from consolidated_twd.
    asof           : timestamp for provenance.  Defaults to UTC now.
    days_held      : scenario theta holding period in calendar days (default: 1).
    """
    eff_spot_map = spot_map if spot_map is not None else DEFAULT_SPOT_MAP
    eff_asof     = asof     if asof     is not None else datetime.now(tz=UTC)

    state: RiskAgentState = {
        "positions_path":  positions_path,
        "spot_map":        eff_spot_map,
        "fx_adapter":      fx_adapter,
        "asof":            eff_asof,
        "days_held":       days_held,
        "portfolio_cfg":   None,
        "usdtwd":          None,
        "greeks_map":      {},
        "twd_spot_map":    {},
        "agg_result":      None,
        "scenario_result": None,
        "pipeline_errors": [],
        "signal":          None,
    }

    state = _node_load(state)
    state = _node_fetch_fx(state)
    state = _node_price(state)
    state = _node_aggregate(state)
    state = _node_scenario(state)
    state = _node_signal(state)

    return state["signal"] or _error_signal(eff_asof, state["pipeline_errors"])


# ─── LangGraph graph ──────────────────────────────────────────────────────────

def create_risk_graph() -> Any:
    """
    Build and compile the risk agent as a LangGraph CompiledGraph.

    Usage in Supervisor
    -------------------
        from agents.risk_agent import create_risk_graph
        risk_graph = create_risk_graph()
        result = risk_graph.invoke(initial_state)
        signal: AgentSignal = result["signal"]
    """
    builder: StateGraph = StateGraph(RiskAgentState)

    builder.add_node("load",      _node_load)
    builder.add_node("fetch_fx",  _node_fetch_fx)
    builder.add_node("price",     _node_price)
    builder.add_node("aggregate", _node_aggregate)
    builder.add_node("scenario",  _node_scenario)
    builder.add_node("signal",    _node_signal)

    builder.set_entry_point("load")
    builder.add_edge("load",      "fetch_fx")
    builder.add_edge("fetch_fx",  "price")
    builder.add_edge("price",     "aggregate")
    builder.add_edge("aggregate", "scenario")
    builder.add_edge("scenario",  "signal")
    builder.add_edge("signal",    END)

    return builder.compile()
