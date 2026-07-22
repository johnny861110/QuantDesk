"""
Macro Agent — Phase 4: 總經面 Surprise 分析引擎

Public API
----------
run_macro_agent(...)      full pipeline (fetch → compute → signal)
create_macro_graph()      LangGraph CompiledGraph for Supervisor integration

Pipeline order
--------------
fetch → compute_surprises → build_signal

Design (CLAUDE.md §三條不可違反)
---------------------------------
① Deterministic / LLM separation
    Surprise computation, signal scoring, and narrative generation are ALL
    pure deterministic Python — NO LLM involved.  The narrative is templated
    text derived from computed labels (not keyword counts or free-form generation).

    Rationale for no-LLM: macro signals are highly structured (surprise direction
    × category type → policy implication is a small decision tree), and the
    most critical risk ("good data might be bad for markets") is best encoded
    as an explicit rule rather than left to LLM inference.

② No hard constraints
    Macro agent does not issue hard_constraints.  A surprise is context signal,
    not a portfolio limit.  hard_constraints=[].

③ Provenance on every evidence item
    All Evidence entries carry source="macro:<category>:<country>" and asof
    = release_date of the event.

Event-driven silence
--------------------
If no events with computable surprises exist in the look-back window, the agent
emits NEUTRAL with confidence=0.10 and metrics["no_recent_events"]=True.  This
is analogous to the LLM-failure degradation in news_agent — the Supervisor must
treat a low-confidence NEUTRAL as "no data" rather than "genuinely neutral."

「好數據未必好事」(Hot data paradox)
----------------------------------------
For GROWTH-positive categories (GDP, NFP, PMI), a positive surprise is normally
bullish.  HOWEVER, when recent inflation is also elevated, strong growth data
amplifies rate-hike expectations → can be net BEARISH.
Implementation:
  - The base signal uses category_direction × surprise_sign (standard logic).
  - A "hot_data_warning" flag is set when: category ∈ GROWTH_CATEGORIES AND
    surprise_sign = +1 AND any inflation event in the same batch also has
    surprise_sign = +1.
  - The narrative explicitly annotates this tension.  The signal itself is
    NOT automatically flipped — that regime-level inference belongs in Phase 5
    Supervisor.  Phase 4 surfaces the tension; Phase 5 resolves it.

time_horizon
------------
Macro is MEDIUM: rate-hike implications take weeks to months to propagate
into equity valuations.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph

from adapters.base import DataSourceAdapter
from adapters.macro_adapter import (
    MacroEvent,
    MacroResult,
    TradingEconomicsAdapter,
    _require_te_key,
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

# ─── Category direction table ─────────────────────────────────────────────────
#
# CATEGORY_DIRECTION[category] = D, where:
#   D = +1 : positive surprise (actual > consensus) is bullish for markets
#   D = -1 : positive surprise is bearish (inflation, unemployment)
#   D =  0 : highly context-dependent; surprise direction alone is ambiguous
#
# ⚠️ 暫定分類：基於傳統財經理論，未考量特定市場 regime。
#    例如：當 Fed 已進入降息週期，正向通膨 surprise 的利空影響較小。
#    Phase 5 Supervisor 應結合當前 regime 動態調整。

CATEGORY_DIRECTION: dict[str, int] = {
    # Inflation (positive surprise = bad for markets → rate hike risk)
    "Inflation Rate":         -1,
    "Core Inflation Rate":    -1,
    "CPI":                    -1,
    "Core CPI":               -1,
    "PCE Price Index":        -1,
    "Core PCE Price Index":   -1,
    "PPI":                    -1,
    "Producer Price Index":   -1,
    # Growth (positive surprise = good for markets)
    "GDP Growth Rate":        +1,
    "GDP":                    +1,
    "Industrial Production":  +1,
    "Retail Sales":           +1,
    "Consumer Confidence":    +1,
    # Labour market
    "Non Farm Payrolls":      +1,   # more jobs = initially bullish
    "ADP Employment Change":  +1,
    "Unemployment Rate":      -1,   # positive surprise = more unemployment = bad
    "Initial Jobless Claims": -1,   # positive surprise = more claims = bad
    # PMI (above 50 = expansion, positive surprise = good)
    "Manufacturing PMI":      +1,
    "Services PMI":           +1,
    "Composite PMI":          +1,
    "ISM Manufacturing PMI":  +1,
    "ISM Non Manufacturing PMI": +1,
    # Central bank (directionally ambiguous — rate decision depends on context)
    "Interest Rate":          0,
    "Fed Funds Rate":         0,
    # Trade (ambiguous without context)
    "Balance Of Trade":       0,
    "Current Account":        0,
}

# Categories whose positive surprise signals hot-economy risk
# (eligible for the "好數據未必好事" warning when paired with inflation beat)
GROWTH_CATEGORIES: frozenset[str] = frozenset({
    "GDP Growth Rate", "GDP", "Non Farm Payrolls", "ADP Employment Change",
    "Retail Sales", "Consumer Confidence",
    "Manufacturing PMI", "Services PMI", "Composite PMI",
    "ISM Manufacturing PMI", "ISM Non Manufacturing PMI",
    "Industrial Production",
})

# Inflation categories used to detect the hot-data context
INFLATION_CATEGORIES: frozenset[str] = frozenset({
    "Inflation Rate", "Core Inflation Rate", "CPI", "Core CPI",
    "PCE Price Index", "Core PCE Price Index", "PPI", "Producer Price Index",
})

# Importance weight: TE importance 3 events outweigh importance 2
# ⚠️ 暫定值：線性比例，未做統計最佳化
IMPORTANCE_WEIGHTS: dict[int, float] = {1: 0.5, 2: 1.0, 3: 2.0}

# Surprise thresholds (expressed as fraction of |consensus|)
# e.g. 0.05 means actual differs from consensus by ≥ 5% of |consensus|
# ⚠️ 暫定值：5% / 20% 是主觀設定，不同指標應有個別閾值（後期可校準）
_LARGE_SURPRISE_PCT: float = 0.20   # |surprise_pct| ≥ 20% → large
_SMALL_SURPRISE_PCT: float = 0.05   # |surprise_pct| < 5%  → in_line

# Final signal thresholds
# ⚠️ 暫定值：±0.15，未經回測最佳化
_BULLISH_THRESHOLD: float = 0.15
_BEARISH_THRESHOLD: float = -0.15

# Degraded-state confidence floor (no events = no information)
_NO_EVENTS_CONFIDENCE: float = 0.10

# Recency half-life for event weighting: events older than this many days
# count for half as much.
# ⚠️ 暫定值：3 天
_RECENCY_HALF_LIFE_DAYS: float = 3.0

# SYMBOL and MARKET used when macro agent is run in standalone mode
# (not targeting a specific stock — macro applies to the broad market)
_MACRO_SYMBOL: str = "MARKET"
_MACRO_MARKET: str = "TW"


# ─── Pure analytics functions ─────────────────────────────────────────────────


def compute_surprise(actual: float, consensus: float) -> float:
    """
    Absolute surprise: actual − consensus.

    Positive = actual beat consensus; negative = missed.
    """
    return actual - consensus


def compute_surprise_pct(actual: float, consensus: float) -> float:
    """
    Relative surprise: (actual − consensus) / |consensus|.

    Returns nan if consensus is zero (avoid division by zero).
    Preferred metric for comparing surprises across different-scale indicators
    (e.g. CPI % vs NFP thousands).
    """
    if consensus == 0.0:
        return float("nan")
    return (actual - consensus) / abs(consensus)


def classify_surprise(surprise_pct: float) -> str:
    """
    Map relative surprise to a categorical label.

    Returns: "large_beat" | "beat" | "in_line" | "miss" | "large_miss"
    Thresholds are ⚠️ 暫定值.
    """
    if math.isnan(surprise_pct):
        return "in_line"
    if surprise_pct >= _LARGE_SURPRISE_PCT:
        return "large_beat"
    if surprise_pct >= _SMALL_SURPRISE_PCT:
        return "beat"
    if surprise_pct <= -_LARGE_SURPRISE_PCT:
        return "large_miss"
    if surprise_pct <= -_SMALL_SURPRISE_PCT:
        return "miss"
    return "in_line"


def event_market_direction(category: str, surprise_sign: int) -> int:
    """
    Given a category and the surprise sign (+1 / 0 / -1), return the
    expected market impact direction (+1 = bullish, -1 = bearish, 0 = neutral).

    Logic:
      category_dir = CATEGORY_DIRECTION.get(category, 0)
      direction = category_dir × surprise_sign

    Examples
    --------
      CPI (+1 surprise, -1 category dir)  → -1 × +1 = -1  (bearish: hot inflation)
      GDP (+1 surprise, +1 category dir)  → +1 × +1 = +1  (bullish: growth beat)
      NFP (-1 surprise, +1 category dir)  → +1 × -1 = -1  (bearish: weak jobs)
      Unemployment (+1 surprise, -1 dir)  → -1 × +1 = -1  (bearish: more jobless)

    ⚠️ This does NOT encode the hot-data paradox.  That warning is set separately
    via _detect_hot_data_warning() and reflected in the narrative only.
    """
    cat_dir = CATEGORY_DIRECTION.get(category, 0)
    return cat_dir * surprise_sign


def _recency_weight(release_date: datetime, as_of: datetime) -> float:
    """
    Exponential decay: weight = exp(-ln2 × days_ago / HALF_LIFE).
    Events released today → weight=1.0; at half-life → weight=0.5.
    """
    # Normalise to naive UTC for comparison
    rd = release_date.replace(tzinfo=None) if release_date.tzinfo else release_date
    ao = as_of.replace(tzinfo=None) if as_of.tzinfo else as_of
    days_ago = max(0.0, (ao - rd).total_seconds() / 86400.0)
    return math.exp(-math.log(2) * days_ago / _RECENCY_HALF_LIFE_DAYS)


def _detect_hot_data_warning(events_with_scores: list[tuple[MacroEvent, int]]) -> bool:
    """
    Return True if the current batch contains both:
    1. A growth-category positive surprise (beats estimates).
    2. An inflation-category positive surprise (hot inflation).

    This triggers the "好數據未必好事" annotation in the narrative.
    The signal itself is NOT flipped — this is a narrative-layer warning.
    """
    has_growth_beat = any(
        cat in GROWTH_CATEGORIES and direction > 0
        for (event, direction) in events_with_scores
        for cat in [event.category]
    )
    has_inflation_beat = any(
        cat in INFLATION_CATEGORIES and direction < 0  # direction=-1 means inflation beat = bad
        for (event, direction) in events_with_scores
        for cat in [event.category]
    )
    return has_growth_beat and has_inflation_beat


def compute_macro_score(
    events: list[MacroEvent],
    as_of: datetime,
) -> tuple[float, list[tuple[MacroEvent, int, float, float]]]:
    """
    Aggregate weighted event scores into a single market signal score.

    Returns
    -------
    (score, event_details)
    score        : float in [-1, +1]; positive = net bullish macro backdrop
    event_details: list of (event, market_direction, surprise_pct, weight)
                   for building Evidence and narrative

    Scoring formula per event:
      importance_weight = IMPORTANCE_WEIGHTS.get(event.importance, 1.0)
      recency_weight    = exp decay from release_date
      surprise_weight   = min(|surprise_pct|, 0.5)  # cap at 50% deviation
      direction         = event_market_direction(category, sign(surprise_pct))
      contribution      = direction × surprise_weight × importance_weight × recency_weight

    Normalise by sum of (importance_weight × recency_weight) across all events
    with computable surprises.

    ⚠️ 暫定加權公式：未經回測，各權重組合方式可後期調校。
    """
    details: list[tuple[MacroEvent, int, float, float]] = []
    total_raw_weight = 0.0
    weighted_sum = 0.0

    for event in events:
        if event.actual is None or event.consensus is None:
            continue

        spct = compute_surprise_pct(event.actual, event.consensus)
        if math.isnan(spct):
            continue

        surprise_sign = 1 if spct > 0 else (-1 if spct < 0 else 0)
        direction = event_market_direction(event.category, surprise_sign)

        imp_w = IMPORTANCE_WEIGHTS.get(event.importance, 1.0)
        rec_w = _recency_weight(event.release_date, as_of)
        sur_w = min(abs(spct), 0.5)   # cap to avoid single extreme event dominating

        raw_weight = imp_w * rec_w
        contribution = direction * sur_w * raw_weight

        weighted_sum += contribution
        total_raw_weight += raw_weight

        details.append((event, direction, spct, raw_weight))

    if total_raw_weight == 0.0:
        return 0.0, details

    score = max(-1.0, min(1.0, weighted_sum / total_raw_weight))
    return score, details


def _score_to_signal(score: float) -> Signal:
    """
    Map aggregate score to Signal enum.
    ⚠️ 暫定閾值：±0.15，未經統計最佳化。
    """
    if score >= _BULLISH_THRESHOLD:
        return Signal.BULLISH
    if score <= _BEARISH_THRESHOLD:
        return Signal.BEARISH
    return Signal.NEUTRAL


# ─── Deterministic narrative ──────────────────────────────────────────────────


_SURPRISE_LABEL: dict[str, str] = {
    "large_beat":  "大幅超越預期",
    "beat":        "小幅超越預期",
    "in_line":     "符合預期",
    "miss":        "小幅不如預期",
    "large_miss":  "大幅不如預期",
}

_DIRECTION_LABEL: dict[int, str] = {
    +1: "對市場偏多",
     0: "方向不明",
    -1: "對市場偏空",
}


def _build_narrative(
    events_with_details: list[tuple[MacroEvent, int, float, float]],
    score: float,
    signal: Signal,
    hot_data_warning: bool,
    no_events: bool,
) -> str:
    """
    Build deterministic qualitative narrative.  NO numeric literals.

    All numbers (actual, consensus, score) live in metrics/key_evidence;
    the narrative contains only categorical descriptions.
    """
    if no_events:
        return "近期無重大總經數據公布，總經面靜默，無方向性訊號。"

    parts: list[str] = []

    # Signal-level summary
    signal_desc: dict[Signal, str] = {
        Signal.BULLISH: "整體總經面偏多，近期數據公布結果優於市場預期",
        Signal.BEARISH: "整體總經面偏空，近期數據公布結果差於市場預期或通膨壓力升溫",
        Signal.NEUTRAL: "整體總經面中性，各項數據 surprise 方向互相抵消或影響有限",
    }
    parts.append(signal_desc.get(signal, "總經面方向未明"))

    # Per-event highlights (top 2 by weight)
    top_events = sorted(events_with_details, key=lambda x: x[3], reverse=True)[:2]
    for event, direction, spct, _w in top_events:
        label = _SURPRISE_LABEL.get(classify_surprise(spct), "不明")
        dir_desc = _DIRECTION_LABEL.get(direction, "不明")
        parts.append(
            f"{event.country} {event.category}：{label}，{dir_desc}"
        )

    # Hot-data paradox warning
    if hot_data_warning:
        parts.append(
            "⚠️ 注意：成長數據超預期同時出現通膨超預期，"
            "強勁成長可能加劇升息預期，「好數據未必是好事」，"
            "實際股市影響需結合央行政策路徑綜合判斷"
        )

    parts.append(
        "總經訊號為中期背景（time_horizon=MEDIUM），"
        "短期股價波動需另參考技術面與新聞面"
    )
    return "。".join(parts) + "。"


# ─── LangGraph state ──────────────────────────────────────────────────────────


class MacroAgentState(TypedDict):
    symbol:          str
    market:          str
    countries:       list[str]
    days_back:       int
    macro_adapter:   Optional[DataSourceAdapter]
    asof:            datetime
    raw_events:      list[MacroEvent]
    computed:        Optional[dict[str, Any]]
    pipeline_errors: list[str]
    signal:          Optional[AgentSignal]


# ─── Node functions ────────────────────────────────────────────────────────────


def _node_fetch(state: MacroAgentState) -> MacroAgentState:
    """Fetch macro events from Trading Economics (or injected adapter)."""
    errors = list(state["pipeline_errors"])
    adapter = state["macro_adapter"]
    if adapter is None:
        try:
            adapter = TradingEconomicsAdapter(api_key=_require_te_key())
        except RuntimeError as exc:
            errors.append(f"[降級] TE API key 不可用：{exc}")
            errors.append(
                "[降級] 總經分析不可用：此 signal 為降級輸出，"
                "請勿與正常分析結果同等對待。"
            )
            return {**state, "raw_events": [], "pipeline_errors": errors}

    countries = state["countries"] or ["united states", "taiwan"]
    try:
        sourced = adapter.fetch(  # type: ignore[call-arg]
            countries=countries,
            days_back=state["days_back"],
        )
        result: MacroResult = sourced.payload
        return {**state, "raw_events": result.events, "asof": sourced.asof}
    except Exception as exc:
        errors.append(f"macro fetch failed: {exc}")
        errors.append(
            "[降級] 總經數據擷取失敗：此 signal 為降級輸出。"
        )
        return {**state, "raw_events": [], "pipeline_errors": errors}


def _node_compute(state: MacroAgentState) -> MacroAgentState:
    """Compute surprise scores for all fetched events."""
    events = state["raw_events"]
    asof = state["asof"]
    errors = list(state["pipeline_errors"])

    try:
        score, details = compute_macro_score(events, asof)
        events_with_scores = [(e, d) for (e, d, _, _) in details]
        hot_warning = _detect_hot_data_warning(events_with_scores)

        # Summarise per-event surprise labels for metrics
        event_summaries: list[dict[str, Any]] = []
        for event, direction, spct, weight in details:
            event_summaries.append({
                "category":    event.category,
                "country":     event.country,
                "actual":      event.actual,
                "consensus":   event.consensus,
                "surprise_pct": round(spct, 4),
                "label":       classify_surprise(spct),
                "direction":   direction,
                "importance":  event.importance,
                "release_date": event.release_date.isoformat(),
            })

        computed: dict[str, Any] = {
            "macro_score":          score,
            "event_count":          len(events),
            "computable_count":     len(details),
            "hot_data_warning":     hot_warning,
            "no_recent_events":     len(details) == 0,
            "event_summaries":      event_summaries,
            "_details":             details,  # passed to signal node, not in final metrics
        }
        return {**state, "computed": computed}
    except Exception as exc:
        errors.append(f"macro computation failed: {exc}")
        return {**state, "computed": None, "pipeline_errors": errors}


def _node_build_signal(state: MacroAgentState) -> MacroAgentState:
    """Deterministically assemble AgentSignal from computed metrics."""
    symbol = state["symbol"]
    market = state["market"]
    asof = state["asof"]
    errors = list(state["pipeline_errors"])
    computed = state["computed"]

    if computed is None:
        sig = _error_signal(symbol, market, asof, errors)
        return {**state, "signal": sig}

    score: float = float(computed.get("macro_score", 0.0))
    no_events: bool = bool(computed.get("no_recent_events", True))
    hot_warning: bool = bool(computed.get("hot_data_warning", False))
    details: list[tuple[MacroEvent, int, float, float]] = computed.get("_details", [])
    degraded: bool = any("[降級]" in e for e in errors)

    signal = _score_to_signal(score)

    # Confidence
    if degraded or no_events:
        confidence = _NO_EVENTS_CONFIDENCE  # 0.10 — hard floor, matches news_agent pattern
    else:
        n_events = int(computed.get("computable_count", 0))
        confidence = min(0.80, 0.50 + 0.05 * n_events)   # +5% per event, cap 0.80

    # Narrative (fully deterministic — no LLM)
    narrative = _build_narrative(
        details, score, signal, hot_warning, no_events
    )
    verifier_errors = check_narrative(narrative, {"macro_score": score})
    if verifier_errors:
        errors.extend(verifier_errors)

    # Evidence — one per event with actual vs consensus
    key_evidence: list[Evidence] = []
    for event, direction, spct, _w in sorted(details, key=lambda x: x[3], reverse=True)[:5]:
        key_evidence.append(
            Evidence(
                claim=(
                    f"{event.country} {event.category} "
                    f"[{_SURPRISE_LABEL.get(classify_surprise(spct), '不明')}]"
                ),
                value=event.actual,
                source=f"macro:{event.category.lower().replace(' ', '_')}:{event.country.lower().replace(' ', '_')}",
                asof=event.release_date,
            )
        )

    # Data quality
    completeness = 0.0 if no_events else min(1.0, computed.get("computable_count", 0) / 3.0)
    if degraded:
        completeness = 0.0
    staleness_sec = 0.0
    if details:
        oldest = min(e.release_date for (e, _, _, _) in details)
        oldest_naive = oldest.replace(tzinfo=None) if oldest.tzinfo else oldest
        asof_naive = asof.replace(tzinfo=None) if asof.tzinfo else asof
        staleness_sec = max(0.0, (asof_naive - oldest_naive).total_seconds())

    # Strip internal detail from published metrics
    metrics: dict[str, Any] = {k: v for k, v in computed.items() if k != "_details"}
    metrics["degraded"] = degraded

    sig = AgentSignal(
        agent=AgentType.MACRO,
        target=Target(symbol=symbol, market=market, asof=asof),
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


def _error_signal(
    symbol: str, market: str, asof: datetime, errors: list[str]
) -> AgentSignal:
    return AgentSignal(
        agent=AgentType.MACRO,
        target=Target(symbol=symbol, market=market, asof=asof),
        signal=Signal.NEUTRAL,
        confidence=_NO_EVENTS_CONFIDENCE,
        time_horizon=TimeHorizon.MEDIUM,
        key_evidence=[],
        hard_constraints=[],
        metrics={"no_recent_events": True, "degraded": True},
        narrative="總經數據擷取或計算失敗，無法進行分析。",
        data_quality=DataQuality(
            completeness=0.0, staleness_sec=0.0, confidence=_NO_EVENTS_CONFIDENCE
        ),
        errors=errors,
    )


# ─── Public pipeline ──────────────────────────────────────────────────────────


def run_macro_agent(
    symbol: str = _MACRO_SYMBOL,
    market: str = _MACRO_MARKET,
    countries: list[str] | None = None,
    days_back: int = 7,
    macro_adapter: DataSourceAdapter | None = None,
    asof: datetime | None = None,
) -> AgentSignal:
    """
    Run the macro agent pipeline and return AgentSignal.

    Parameters
    ----------
    symbol        : Target symbol ("MARKET" for broad macro; or specific ticker).
    market        : Market context.
    countries     : Countries to monitor. Default: ["united states", "taiwan"].
    days_back     : Look-back window for recent events.
    macro_adapter : Inject mock in tests; default reads TE_API_KEY from env.
    asof          : Override timestamp (default: now UTC).
    """
    if asof is None:
        asof = datetime.now(UTC)

    initial_state: MacroAgentState = {
        "symbol":          symbol,
        "market":          market,
        "countries":       countries or ["united states", "taiwan"],
        "days_back":       days_back,
        "macro_adapter":   macro_adapter,
        "asof":            asof,
        "raw_events":      [],
        "computed":        None,
        "pipeline_errors": [],
        "signal":          None,
    }

    graph = create_macro_graph()
    final_state = graph.invoke(initial_state)
    sig = final_state.get("signal")
    if sig is None:
        return _error_signal(
            symbol, market, asof, final_state.get("pipeline_errors", [])
        )
    return sig  # type: ignore[return-value]


def create_macro_graph() -> Any:
    """Build and compile the LangGraph CompiledGraph for the macro agent."""
    graph: StateGraph = StateGraph(MacroAgentState)
    graph.add_node("fetch",        _node_fetch)
    graph.add_node("compute",      _node_compute)
    graph.add_node("build_signal", _node_build_signal)

    graph.set_entry_point("fetch")
    graph.add_edge("fetch",        "compute")
    graph.add_edge("compute",      "build_signal")
    graph.add_edge("build_signal", END)

    return graph.compile()
