"""
Cross Market Agent — Phase 3: 跨市場連動分析引擎

Public API
----------
run_cross_market_agent(...)   full pipeline (fetch → compute → signal)
create_cross_market_graph()   LangGraph CompiledGraph for Supervisor integration

Pipeline order
--------------
fetch → compute → signal

Design (CLAUDE.md §三條不可違反)
---------------------------------
① Deterministic / LLM separation
    All correlation, beta, lead-lag, and divergence math is pure Python / numpy
    (module-level functions).  _build_narrative() is deterministic qualitative
    f-strings — NO numbers appear in the narrative text.
    Numbers live in metrics + key_evidence only.

② No hard constraints
    Cross-market is CONTEXT (market background), not a portfolio constraint.
    hard_constraints=[].

③ Provenance on every evidence item
    All Evidence entries carry source="cross_market:<indicator>" and asof.

Rolling window design
---------------------
Rolling correlation uses a sliding window (SHORT_WINDOW=20, LONG_WINDOW=60)
rather than a fixed historical correlation coefficient.  This satisfies
CLAUDE.md: 「跨市場用滾動窗口而非固定歷史相關係數」.

Consolidation / low-reliability self-annotation
------------------------------------------------
When n_bars < 60, the agent cannot compute a full 60-day rolling window.
Confidence is reduced to reflect insufficient history.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional, TypedDict

import numpy as np
from langgraph.graph import END, StateGraph

from adapters.base import DataSourceAdapter, SourcedData
from adapters.cross_market_adapter import (
    CrossMarketData,
    YFinanceCrossMarketAdapter,
)
from agents.verifier import check_narrative
from schemas.agent_signal import (
    AgentSignal,
    AgentType,
    DataQuality,
    Evidence,
    Signal,
    Target,
    TimeHorizon,
)

# ─── Constants ────────────────────────────────────────────────────────────────

SHORT_WINDOW: int = 20          # 1-month rolling correlation
LONG_WINDOW: int = 60           # 3-month rolling correlation
MAX_LAG: int = 5                # lead-lag analysis range ±5 trading days
DIVERGENCE_WINDOW: int = 5      # recent return window for divergence check
PRIMARY_PAIR: tuple[str, str] = ("^TWII", "^GSPC")   # TAIEX vs S&P 500
CORR_THRESHOLD: float = 0.30    # minimum |corr| to call "coupled"


# ─── Pure indicator functions ─────────────────────────────────────────────────
# Each function is deterministic and takes numpy arrays only.
# LLM is NEVER involved in indicator computation.

def compute_rolling_correlation(
    x: np.ndarray, y: np.ndarray, window: int
) -> np.ndarray:
    """
    Pearson correlation over rolling window.

    Returns array of same length as x; first (window-1) elements are nan.
    Returns nan for any window where std of x or y is zero.
    """
    n = len(x)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < window or window < 2:
        return result
    for i in range(window - 1, n):
        xw = x[i - window + 1 : i + 1]
        yw = y[i - window + 1 : i + 1]
        sx = float(np.std(xw))
        sy = float(np.std(yw))
        if sx == 0.0 or sy == 0.0:
            result[i] = np.nan
        else:
            corr_mat = np.corrcoef(xw, yw)
            result[i] = corr_mat[0, 1]
    return result


def compute_rolling_beta(
    target: np.ndarray, reference: np.ndarray, window: int
) -> np.ndarray:
    """
    Rolling OLS beta: beta = cov(target, ref) / var(ref).

    Returns array of same length; first (window-1) elements are nan.
    Returns nan for any window where var(ref) == 0.
    """
    n = len(target)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < window or window < 2:
        return result
    for i in range(window - 1, n):
        tw = target[i - window + 1 : i + 1]
        rw = reference[i - window + 1 : i + 1]
        var_ref = float(np.var(rw))
        if var_ref == 0.0:
            result[i] = np.nan
        else:
            cov = float(np.cov(tw, rw, ddof=1)[0, 1])
            result[i] = cov / float(np.var(rw, ddof=1))
    return result


def find_lead_lag(
    x: np.ndarray, y: np.ndarray, max_lag: int = MAX_LAG
) -> dict[int, float]:
    """
    Cross-correlation at lags -max_lag..+max_lag.

    Positive lag L means x leads y by L periods:
      correlation(x[:-L], y[L:])  for L > 0
      correlation(x[-L:], y[:L])  for L < 0  (actually x lags y by |L|)
      correlation(x, y)            for L = 0

    Returns {lag: corr_value}.
    Returns nan for any lag where std is zero or insufficient data.
    """
    result: dict[int, float] = {}
    for lag in range(-max_lag, max_lag + 1):
        if lag > 0:
            xa = x[:-lag]
            ya = y[lag:]
        elif lag < 0:
            xa = x[-lag:]
            ya = y[:lag]
        else:
            xa = x
            ya = y

        if len(xa) < 2 or len(ya) < 2:
            result[lag] = float("nan")
            continue

        # Remove nan pairs
        valid = ~(np.isnan(xa) | np.isnan(ya))
        xa_v = xa[valid]
        ya_v = ya[valid]

        if len(xa_v) < 2:
            result[lag] = float("nan")
            continue

        sx = float(np.std(xa_v))
        sy = float(np.std(ya_v))
        if sx == 0.0 or sy == 0.0:
            result[lag] = float("nan")
        else:
            result[lag] = float(np.corrcoef(xa_v, ya_v)[0, 1])

    return result


def detect_divergence(
    target_ret: np.ndarray,
    ref_ret: np.ndarray,
    long_corr: float,
    window: int = DIVERGENCE_WINDOW,
) -> bool:
    """
    True when recent price direction contradicts what long_corr predicts.

    Condition:
      1. |long_corr| > CORR_THRESHOLD  (markets are meaningfully coupled)
      2. recent cumulative return of target and reference have OPPOSITE signs
         if long_corr > 0 (positive coupling → expect same direction)
         OR the SAME sign if long_corr < 0 (negative coupling → expect opposite)

    If not enough data or |long_corr| ≤ CORR_THRESHOLD, return False.
    """
    if np.isnan(long_corr) or abs(long_corr) <= CORR_THRESHOLD:
        return False
    if len(target_ret) < window or len(ref_ret) < window:
        return False

    target_cum = float(np.sum(target_ret[-window:]))
    ref_cum = float(np.sum(ref_ret[-window:]))

    if long_corr > 0:
        # Positive coupling → divergence if they moved in opposite directions
        return (target_cum > 0) != (ref_cum > 0)
    else:
        # Negative coupling → divergence if they moved in the SAME direction
        return (target_cum > 0) == (ref_cum > 0)


def classify_regime(
    corr_60d: float,
    diverging: bool,
    corr_20d: float = float("nan"),
) -> str:
    """
    Returns one of: "strong_coupling", "moderate_coupling",
                    "decoupled", "short_term_counter",
                    "negative_coupling", "divergent"

    Priority chain (highest → lowest):
      1. divergent      — divergence_detected=True (regime rupture, overrides all)
      2. corr_60d-based — long window is statistically stable (≥60 days)
         strong_coupling (>0.6), moderate_coupling (0.3–0.6),
         decoupled (-0.3–0.3), negative_coupling (<-0.3)
      3. short_term_counter — corr_60d is unavailable (nan) OR near zero,
         AND corr_20d < -CORR_THRESHOLD.
         Meaning: 20-day window shows active counter-movement even though the
         long-term relationship hasn't been confirmed yet (or regime just flipped).
         NOT a "high-volatility" artifact — real examples include tariff-shock
         periods where one market absorbed external shocks independently.
      4. decoupled      — fallback when neither long nor short signal is clear.

    Note on "short_term_counter" vs "negative_coupling":
      negative_coupling  → corr_60d < -0.30  (60-day structural inversion)
      short_term_counter → corr_20d < -0.30 while corr_60d is neutral/nan
                           (20-day counter-movement, long regime not yet confirmed)
    """
    if diverging:
        return "divergent"

    # Long window available and meaningful — use it as primary signal
    if not np.isnan(corr_60d):
        if corr_60d > 0.6:
            return "strong_coupling"
        if corr_60d > 0.3:
            return "moderate_coupling"
        if corr_60d >= -0.3:
            # Long window shows near-zero correlation.
            # Check if short window has already flipped negative.
            if not np.isnan(corr_20d) and corr_20d < -CORR_THRESHOLD:
                return "short_term_counter"
            return "decoupled"
        return "negative_coupling"

    # Long window not yet available (< LONG_WINDOW bars)
    if not np.isnan(corr_20d) and corr_20d < -CORR_THRESHOLD:
        return "short_term_counter"
    return "decoupled"


# ─── Composite metrics computation ────────────────────────────────────────────

def _compute_all_metrics(data: CrossMarketData) -> dict[str, Any]:
    """
    Compute all cross-market metrics from CrossMarketData.

    Returns flat dict with keys:
      tw_us_corr_20d, tw_us_corr_60d, tw_us_beta_20d, tw_us_beta_60d,
      lead_lag_map (dict[int, float]), lead_lag_optimal (int),
      divergence_detected (bool), regime (str),
      taiex_5d_return, sp500_5d_return,
      n_bars, symbols_analyzed (list[str])

    If PRIMARY_PAIR symbols not all present in data, falls back gracefully
    to use whatever first two symbols are available.
    """
    syms = data.symbols
    available = set(syms)

    # Determine which pair to analyse
    tw_sym, us_sym = PRIMARY_PAIR
    if tw_sym not in available or us_sym not in available:
        if len(syms) >= 2:
            tw_sym, us_sym = syms[0], syms[1]
        else:
            # Only one symbol — can't compute pair metrics
            n_bars = len(data.dates)
            return {
                "tw_us_corr_20d": float("nan"),
                "tw_us_corr_60d": float("nan"),
                "tw_us_beta_20d": float("nan"),
                "tw_us_beta_60d": float("nan"),
                "lead_lag_map": {},
                "lead_lag_optimal": 0,
                "divergence_detected": False,
                "regime": "decoupled",
                "taiex_5d_return": float("nan"),
                "sp500_5d_return": float("nan"),
                "n_bars": n_bars,
                "symbols_analyzed": syms,
            }

    tw_ret = data.returns_[tw_sym]
    us_ret = data.returns_[us_sym]

    n_bars = len(data.dates)

    # Rolling correlations
    corr_20_arr = compute_rolling_correlation(tw_ret, us_ret, SHORT_WINDOW)
    corr_60_arr = compute_rolling_correlation(tw_ret, us_ret, LONG_WINDOW)

    tw_us_corr_20d = float(corr_20_arr[-1]) if len(corr_20_arr) > 0 else float("nan")
    tw_us_corr_60d = float(corr_60_arr[-1]) if len(corr_60_arr) > 0 else float("nan")

    # Rolling betas
    beta_20_arr = compute_rolling_beta(tw_ret, us_ret, SHORT_WINDOW)
    beta_60_arr = compute_rolling_beta(tw_ret, us_ret, LONG_WINDOW)

    tw_us_beta_20d = float(beta_20_arr[-1]) if len(beta_20_arr) > 0 else float("nan")
    tw_us_beta_60d = float(beta_60_arr[-1]) if len(beta_60_arr) > 0 else float("nan")

    # Lead-lag
    lead_lag_map = find_lead_lag(tw_ret, us_ret, MAX_LAG)

    # Optimal lag = lag with highest absolute correlation (ignoring nan)
    valid_lags = {lag: v for lag, v in lead_lag_map.items() if not np.isnan(v)}
    if valid_lags:
        lead_lag_optimal = max(valid_lags, key=lambda lag: abs(valid_lags[lag]))
    else:
        lead_lag_optimal = 0

    # Divergence detection
    divergence_detected = detect_divergence(tw_ret, us_ret, tw_us_corr_60d)

    # Regime classification — pass corr_20d so short-term counter-movement
    # is captured when corr_60d is neutral or unavailable.
    regime = classify_regime(tw_us_corr_60d, divergence_detected, tw_us_corr_20d)

    # 5-day cumulative returns
    window5 = DIVERGENCE_WINDOW
    taiex_5d_return = float(np.sum(tw_ret[-window5:])) if len(tw_ret) >= window5 else float("nan")
    sp500_5d_return = float(np.sum(us_ret[-window5:])) if len(us_ret) >= window5 else float("nan")

    return {
        "tw_us_corr_20d": tw_us_corr_20d,
        "tw_us_corr_60d": tw_us_corr_60d,
        "tw_us_beta_20d": tw_us_beta_20d,
        "tw_us_beta_60d": tw_us_beta_60d,
        "lead_lag_map": lead_lag_map,
        "lead_lag_optimal": lead_lag_optimal,
        "divergence_detected": divergence_detected,
        "regime": regime,
        "taiex_5d_return": taiex_5d_return,
        "sp500_5d_return": sp500_5d_return,
        "n_bars": n_bars,
        "symbols_analyzed": syms,
    }


# ─── Signal determination (pure, deterministic) ───────────────────────────────

def _determine_signal_and_confidence(
    metrics: dict[str, Any],
) -> tuple[Signal, float]:
    """
    Cross-market is CONTEXT, not direction.  Default NEUTRAL.

    Signal:
    - BEARISH if divergence_detected AND |corr_60d| > CORR_THRESHOLD
      (markets that "should" be coupled have come apart → regime uncertainty)
    - NEUTRAL otherwise (background info, not a trade signal)

    Confidence:
    - base = 0.70 (data is deterministic)
    - -0.20 if n_bars < 60 (insufficient history for 60d window)
    - -0.15 if divergence_detected (regime uncertainty)
    - minimum 0.15
    """
    divergence_detected: bool = bool(metrics.get("divergence_detected", False))
    tw_us_corr_60d: float = float(metrics.get("tw_us_corr_60d", 0.0))
    n_bars: int = int(metrics.get("n_bars", 0))

    if divergence_detected and abs(tw_us_corr_60d) > CORR_THRESHOLD:
        signal = Signal.BEARISH
    else:
        signal = Signal.NEUTRAL

    confidence = 0.70
    if n_bars < LONG_WINDOW:
        confidence -= 0.20
    if divergence_detected:
        confidence -= 0.15

    confidence = max(0.15, confidence)
    return signal, confidence


# ─── Deterministic narrative ──────────────────────────────────────────────────

def _build_narrative(metrics: dict[str, Any], signal: Signal) -> str:
    """
    Qualitative description of regime and lead-lag.  NO numeric literals.

    The Verifier (agents/verifier.py) will flag any numeric literal in the
    returned string that does not appear in the metrics dict.  This function
    deliberately avoids all numeric output to satisfy the CLAUDE.md constraint.
    """
    regime: str = str(metrics.get("regime", "decoupled"))
    lead_lag_optimal: int = int(metrics.get("lead_lag_optimal", 0))
    divergence_detected: bool = bool(metrics.get("divergence_detected", False))

    regime_map: dict[str, str] = {
        "strong_coupling":    "台美市場高度正向連動",
        "moderate_coupling":  "台美市場中度正向連動",
        "decoupled":          "台美市場連動性偏低，各自獨立走勢",
        "short_term_counter": "近期台美市場出現短期反向走勢，長期結構仍待觀察",
        "negative_coupling":  "台美市場呈反向連動",
        "divergent":          "台美市場出現連動背離",
    }
    regime_desc = regime_map.get(regime, "台美市場連動狀態未知")

    if lead_lag_optimal > 0:
        lead_lag_desc = "美股領先台股"
    elif lead_lag_optimal < 0:
        lead_lag_desc = "台股領先美股"
    else:
        lead_lag_desc = "台美股同步移動"

    parts: list[str] = [regime_desc, lead_lag_desc]

    if divergence_detected:
        parts.append("⚠️ 近期台美走勢出現背離，需警惕市場連動失效風險")

    parts.append("此為市場背景指標，非獨立進出場訊號")

    return "，".join(parts) + "。"


# ─── LangGraph state ──────────────────────────────────────────────────────────

class CrossMarketAgentState(TypedDict):
    symbols:         dict[str, str]              # {ticker: label}
    market:          str
    cross_adapter:   Optional[DataSourceAdapter]
    asof:            datetime
    price_data:      Optional[CrossMarketData]
    computed:        Optional[dict[str, Any]]    # _compute_all_metrics result
    pipeline_errors: list[str]
    signal:          Optional[AgentSignal]


# ─── Node functions ────────────────────────────────────────────────────────────

def _node_fetch(state: CrossMarketAgentState) -> CrossMarketAgentState:
    """Fetch multi-market close data via cross_adapter."""
    errors = list(state["pipeline_errors"])
    adapter = state["cross_adapter"]
    if adapter is None:
        adapter = YFinanceCrossMarketAdapter()

    symbols = state["symbols"] if state["symbols"] else None

    try:
        sourced: SourcedData = adapter.fetch(  # type: ignore[call-arg]
            symbols=symbols,
            period="6mo",
            interval="1d",
        )
        price_data: CrossMarketData = sourced.payload
        return {**state, "price_data": price_data, "asof": sourced.asof}
    except Exception as exc:
        errors.append(f"cross-market fetch failed: {exc}")
        return {**state, "price_data": None, "pipeline_errors": errors}


def _node_compute(state: CrossMarketAgentState) -> CrossMarketAgentState:
    """Compute all cross-market metrics from price data."""
    price_data = state["price_data"]
    errors = list(state["pipeline_errors"])
    if price_data is None:
        errors.append("no price data — skipping cross-market computation")
        return {**state, "computed": None, "pipeline_errors": errors}

    try:
        computed = _compute_all_metrics(price_data)
        return {**state, "computed": computed}
    except Exception as exc:
        errors.append(f"cross-market computation failed: {exc}")
        return {**state, "computed": None, "pipeline_errors": errors}


def _node_signal(state: CrossMarketAgentState) -> CrossMarketAgentState:
    """Assemble AgentSignal from computed metrics."""
    computed = state["computed"]
    price_data = state["price_data"]
    asof = state["asof"]
    errors = list(state["pipeline_errors"])

    if computed is None or price_data is None:
        sig = _error_signal(asof=asof, errors=errors + ["missing price data or metrics"])
        return {**state, "signal": sig}

    signal, confidence = _determine_signal_and_confidence(computed)

    narrative = _build_narrative(computed, signal)
    verifier_errors = check_narrative(narrative, computed)
    if verifier_errors:
        errors.extend(verifier_errors)

    # Data quality
    n_bars: int = int(computed.get("n_bars", 0))
    completeness = min(1.0, n_bars / LONG_WINDOW)

    last_bar_dt = price_data.dates[-1] if price_data.dates else asof
    asof_naive = asof.replace(tzinfo=None) if asof.tzinfo is not None else asof
    last_bar_naive = (
        last_bar_dt.replace(tzinfo=None)
        if last_bar_dt.tzinfo is not None
        else last_bar_dt
    )
    staleness_sec = max(0.0, (asof_naive - last_bar_naive).total_seconds())

    # Key evidence (5 items with source + asof on every item)
    key_evidence: list[Evidence] = [
        Evidence(
            claim="台美 20 日滾動相關係數",
            value=computed.get("tw_us_corr_20d"),
            source="cross_market:corr_20d",
            asof=asof,
        ),
        Evidence(
            claim="台美 60 日滾動相關係數",
            value=computed.get("tw_us_corr_60d"),
            source="cross_market:corr_60d",
            asof=asof,
        ),
        Evidence(
            claim="台美 20 日滾動 Beta",
            value=computed.get("tw_us_beta_20d"),
            source="cross_market:beta_20d",
            asof=asof,
        ),
        Evidence(
            claim="最優領先落後期數（正值：美股領先台股）",
            value=float(computed.get("lead_lag_optimal", 0)),
            source="cross_market:lead_lag",
            asof=asof,
        ),
        Evidence(
            claim="TAIEX 近五日累積報酬",
            value=computed.get("taiex_5d_return"),
            source="cross_market:return",
            asof=asof,
        ),
    ]

    metrics: dict[str, Any] = {**computed, "is_background_only": True}

    sig = AgentSignal(
        agent=AgentType.CROSS_MARKET,
        target=Target(symbol="TAIEX", market="TW", asof=asof),
        signal=signal,
        confidence=confidence,
        time_horizon=TimeHorizon.MEDIUM,
        key_evidence=key_evidence,
        hard_constraints=[],
        metrics=metrics,
        narrative=narrative,
        data_quality=DataQuality(
            completeness=completeness,
            staleness_sec=staleness_sec,
            confidence=confidence,
        ),
        errors=errors,
    )
    return {**state, "signal": sig}


# ─── Error fallback ────────────────────────────────────────────────────────────

def _error_signal(asof: datetime, errors: list[str]) -> AgentSignal:
    """Fallback when the pipeline fails — confidence=0 signals total failure."""
    return AgentSignal(
        agent=AgentType.CROSS_MARKET,
        target=Target(symbol="TAIEX", market="TW", asof=asof),
        signal=Signal.NEUTRAL,
        confidence=0.0,
        time_horizon=TimeHorizon.MEDIUM,
        key_evidence=[],
        hard_constraints=[],
        metrics={},
        narrative="跨市場分析管線初始化失敗，無法產出訊號。詳見 errors 欄位。",
        data_quality=DataQuality(completeness=0.0, staleness_sec=0.0, confidence=0.0),
        errors=errors,
    )


# ─── Full pipeline (convenience wrapper) ──────────────────────────────────────

def run_cross_market_agent(
    *,
    symbols: dict[str, str] | None = None,
    market: str = "TW",
    cross_adapter: Optional[DataSourceAdapter] = None,
    asof: Optional[datetime] = None,
) -> AgentSignal:
    """
    Run the full cross-market analysis pipeline and return AgentSignal.

    Parameters
    ----------
    symbols       : {ticker: label} mapping.  Defaults to DEFAULT_SYMBOLS.
    market        : Market code for target, e.g. "TW".
    cross_adapter : CrossMarketAdapter.  Defaults to YFinanceCrossMarketAdapter().
    asof          : Timestamp for provenance.  Defaults to UTC now.
    """
    from adapters.cross_market_adapter import DEFAULT_SYMBOLS

    eff_symbols = symbols if symbols is not None else DEFAULT_SYMBOLS
    eff_asof = asof if asof is not None else datetime.now(tz=UTC)

    state: CrossMarketAgentState = {
        "symbols":         eff_symbols,
        "market":          market,
        "cross_adapter":   cross_adapter,
        "asof":            eff_asof,
        "price_data":      None,
        "computed":        None,
        "pipeline_errors": [],
        "signal":          None,
    }

    state = _node_fetch(state)
    state = _node_compute(state)
    state = _node_signal(state)

    return state["signal"] or _error_signal(eff_asof, state["pipeline_errors"])


# ─── LangGraph graph ──────────────────────────────────────────────────────────

def create_cross_market_graph() -> Any:
    """
    Build and compile the cross-market agent as a LangGraph CompiledGraph.

    Usage in Supervisor
    -------------------
        from agents.cross_market_agent import create_cross_market_graph
        cm_graph = create_cross_market_graph()
        result = cm_graph.invoke(initial_state)
        signal: AgentSignal = result["signal"]
    """
    builder: StateGraph = StateGraph(CrossMarketAgentState)

    builder.add_node("fetch",   _node_fetch)
    builder.add_node("compute", _node_compute)
    builder.add_node("signal",  _node_signal)

    builder.set_entry_point("fetch")
    builder.add_edge("fetch",   "compute")
    builder.add_edge("compute", "signal")
    builder.add_edge("signal",  END)

    return builder.compile()
