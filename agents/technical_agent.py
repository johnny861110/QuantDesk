"""
Technical Agent — Phase 3: 技術面分析引擎

Public API
----------
run_technical_agent(...)   full pipeline (fetch → compute → signal)
create_technical_graph()   LangGraph CompiledGraph for Supervisor integration

Pipeline order
--------------
fetch → compute → signal

Design (CLAUDE.md §三條不可違反)
---------------------------------
① Deterministic / LLM separation
    All indicator math is pure Python / numpy (module-level functions).
    _build_narrative() is deterministic (qualitative f-strings — NO numbers).
    Numbers live in metrics + key_evidence only.

② No hard constraints
    Technical agent has no hard_constraints per spec.  hard_constraints=[].

③ Provenance on every evidence item
    All Evidence entries carry source="technical_agent:<indicator>" and asof.

Consolidation self-annotation
------------------------------
When Bollinger Band width < 4 % of mid price, the market is deemed to be in a
consolidation / range-bound phase.  Confidence is reduced and capped, and the
narrative explicitly flags the low-reliability state.  This satisfies the
CLAUDE.md requirement: 「盤整盤要自我標註可信度低」.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional, TypedDict

import numpy as np
from langgraph.graph import END, StateGraph

from adapters.base import DataSourceAdapter, SourcedData
from adapters.price_adapter import FinMindPriceAdapter, OHLCVData, YFinancePriceAdapter
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


# ─── Pure indicator functions ─────────────────────────────────────────────────
# Each function is deterministic and takes numpy arrays only.
# LLM is NEVER involved in indicator computation.

def _ema_series(arr: np.ndarray, period: int) -> np.ndarray:
    """
    Full EMA series.

    k = 2 / (period + 1)
    Seed = arr[0]; then EMA[i] = k * arr[i] + (1 - k) * EMA[i-1].
    """
    if len(arr) == 0:
        return np.array([], dtype=np.float64)
    k = 2.0 / (period + 1)
    out = np.empty(len(arr), dtype=np.float64)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = k * arr[i] + (1.0 - k) * out[i - 1]
    return out


def compute_sma(close: np.ndarray, period: int) -> float:
    """
    SMA of the last *period* values.

    If len(close) < period, use all available values.
    """
    if len(close) == 0:
        return float("nan")
    n = min(period, len(close))
    return float(np.mean(close[-n:]))


def compute_macd(
    close: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[float, float, float]:
    """
    MACD line, signal line, and histogram — all using EMA.

    Returns (macd_line, signal_line, histogram).
    macd_line  = EMA(fast) - EMA(slow)  (last element)
    signal_line = EMA(macd_series, signal_period)  (last element)
    histogram  = macd_line - signal_line
    """
    if len(close) == 0:
        return 0.0, 0.0, 0.0
    fast_ema  = _ema_series(close, fast)
    slow_ema  = _ema_series(close, slow)
    macd_vals = fast_ema - slow_ema
    sig_vals  = _ema_series(macd_vals, signal_period)
    ml = float(macd_vals[-1])
    sl = float(sig_vals[-1])
    return ml, sl, ml - sl


def compute_rsi(close: np.ndarray, period: int = 14) -> float:
    """
    Wilder's RSI (J. Welles Wilder, *New Concepts in Technical Trading Systems*, 1978).

    Algorithm
    ---------
    1. Compute *period* price diffs.  Seed avg_gain and avg_loss with their
       simple mean (standard Wilder initialisation).
    2. For each subsequent diff *d*:
           avg_gain = (avg_gain × (period−1) + max(d, 0)) / period
           avg_loss = (avg_loss × (period−1) + max(−d, 0)) / period
       This EMA-like recursion is Wilder's smoothing — NOT a rolling simple
       average of the last *period* diffs.  The two are numerically distinct
       and diverge on mixed-direction series.
    3. RS = avg_gain / avg_loss ; RSI = 100 − 100 / (1 + RS)

    Minimum bars required: period + 1.

    Edge cases
    ----------
    - len(close) < period + 1  : insufficient data → 50.0
    - avg_gain == avg_loss == 0 : flat series      → 50.0
    - avg_loss == 0             : pure uptrend     → 100.0
    """
    if len(close) < period + 1:
        return 50.0
    diffs  = np.diff(close.astype(np.float64))
    gains  = np.maximum(diffs, 0.0)
    losses = np.maximum(-diffs, 0.0)

    # Seed: simple average of first `period` diffs
    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())

    # Wilder's recursive smoothing for all subsequent diffs
    for i in range(period, len(diffs)):
        avg_gain = (avg_gain * (period - 1) + float(gains[i])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses[i])) / period

    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def compute_stochastic(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    k_period: int = 9,
    d_period: int = 3,
) -> tuple[float, float]:
    """
    Slow Stochastic %K and %D — 台股慣用的慢速 KD。

    採用台股慣用的**慢速 KD（Slow Stochastic）**，RSV 經過兩次 d_period 日 SMA 平滑：
      raw_%K[i]（即 RSV）= (close[i] − lowest_low_{k_period}) /
                            (highest_high_{k_period} − lowest_low_{k_period}) × 100
      %K = d_period-period SMA of raw_%K
      %D = d_period-period SMA of %K

    與歐美常見的 **Fast Stochastic（無平滑，raw_%K 直接輸出為 %K）** 不同。
    若未來對接美股技術面分析，需確認是否要切換或另外提供 Fast 版本。

    Returns (K, D).
    Returns (50.0, 50.0) if there is not enough data.

    min_bars needed = k_period + d_period + d_period - 2
    """
    min_bars = k_period + d_period + d_period - 2
    n = len(close)
    if n < min_bars or n < k_period:
        return 50.0, 50.0

    # Compute raw %K series
    raw_k: list[float] = []
    for i in range(k_period - 1, n):
        h_max = float(np.max(high[i - k_period + 1 : i + 1]))
        l_min = float(np.min(low[i - k_period + 1 : i + 1]))
        denom = h_max - l_min
        if denom == 0.0:
            raw_k.append(50.0)
        else:
            raw_k.append((close[i] - l_min) / denom * 100.0)

    raw_k_arr = np.array(raw_k, dtype=np.float64)

    # %K = SMA(d_period) of raw_%K
    if len(raw_k_arr) < d_period:
        return 50.0, 50.0
    k_series: list[float] = []
    for i in range(d_period - 1, len(raw_k_arr)):
        k_series.append(float(np.mean(raw_k_arr[i - d_period + 1 : i + 1])))

    k_arr = np.array(k_series, dtype=np.float64)

    # %D = SMA(d_period) of %K
    if len(k_arr) < d_period:
        return 50.0, 50.0
    d_series: list[float] = []
    for i in range(d_period - 1, len(k_arr)):
        d_series.append(float(np.mean(k_arr[i - d_period + 1 : i + 1])))

    return float(k_arr[-1]), float(d_series[-1])


def compute_bollinger(
    close: np.ndarray,
    period: int = 20,
    std_mult: float = 2.0,
) -> tuple[float, float, float]:
    """
    Bollinger Bands: (upper, mid, lower).

    mid   = SMA(period)
    std   = population std of last *period* bars
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    """
    if len(close) == 0:
        return float("nan"), float("nan"), float("nan")
    n = min(period, len(close))
    window = close[-n:]
    mid = float(np.mean(window))
    std = float(np.std(window))  # population std (ddof=0)
    return mid + std_mult * std, mid, mid - std_mult * std


def compute_volume_ratio(volume: np.ndarray, period: int = 20) -> float:
    """
    Current volume relative to recent average.

    Returns volume[-1] / mean(volume[-period:]).
    Returns 1.0 if insufficient data or zero mean.
    """
    if len(volume) == 0:
        return 1.0
    n = min(period, len(volume))
    avg = float(np.mean(volume[-n:]))
    if avg == 0.0:
        return 1.0
    return float(volume[-1]) / avg


# ─── Composite indicator computation ──────────────────────────────────────────

def _compute_all_indicators(data: OHLCVData) -> dict[str, float]:
    """
    Compute all indicators from OHLCVData and return a flat dict.

    Keys
    ----
    close, sma5, sma20, sma60,
    macd_line, macd_signal, macd_hist,
    rsi, k, d,
    bb_upper, bb_mid, bb_lower, bb_width_pct,
    vol_ratio
    """
    c = data.close
    h = data.high
    lo = data.low
    v = data.volume

    sma5  = compute_sma(c, 5)
    sma20 = compute_sma(c, 20)
    sma60 = compute_sma(c, 60)

    macd_line, macd_signal, macd_hist = compute_macd(c)

    rsi = compute_rsi(c)

    k, d = compute_stochastic(h, lo, c)

    bb_upper, bb_mid, bb_lower = compute_bollinger(c)

    # Bollinger Band width as a fraction of the mid price
    if bb_mid != 0.0 and not (
        np.isnan(bb_upper) or np.isnan(bb_mid) or np.isnan(bb_lower)
    ):
        bb_width_pct = (bb_upper - bb_lower) / bb_mid
    else:
        bb_width_pct = float("nan")

    vol_ratio = compute_volume_ratio(v)

    close_last = float(c[-1]) if len(c) > 0 else float("nan")

    return {
        "close":       close_last,
        "sma5":        sma5,
        "sma20":       sma20,
        "sma60":       sma60,
        "macd_line":   macd_line,
        "macd_signal": macd_signal,
        "macd_hist":   macd_hist,
        "rsi":         rsi,
        "k":           k,
        "d":           d,
        "bb_upper":    bb_upper,
        "bb_mid":      bb_mid,
        "bb_lower":    bb_lower,
        "bb_width_pct": bb_width_pct,
        "vol_ratio":   vol_ratio,
    }


# ─── Signal determination (pure, deterministic) ───────────────────────────────

def _determine_signal_and_confidence(
    indicators: dict[str, float],
    is_consolidating: bool,
) -> tuple[Signal, float]:
    """
    7-factor scoring.  Each factor contributes +1 (bullish), -1 (bearish), or 0.

    Factors
    -------
    1. close > sma20 → +1; close < sma20 → -1
    2. sma5  > sma20 → +1; sma5  < sma20 → -1
    3. sma20 > sma60 → +1; sma20 < sma60 → -1
    4. macd_hist > 0 → +1; macd_hist < 0 → -1
    5. rsi > 60 → +1; rsi < 40 → -1
    6. k > d → +1; k < d → -1
    7. bb_pct > 0.55 → +1; bb_pct < 0.45 → -1
       where bb_pct = (close - bb_lower) / (bb_upper - bb_lower)

    Signal thresholds on avg_score (range [-1, +1])
    -----------------------------------------------
    avg > +0.30 → BULLISH
    avg < -0.30 → BEARISH
    else        → NEUTRAL

    Threshold rationale
    -------------------
    With 7 binary factors each in {−1, 0, +1}, the possible avg_score values are
    integer multiples of 1/7: …, −2/7≈−0.286, −1/7≈−0.143, 0, +1/7, +2/7, +3/7≈+0.429, …
    ±0.30 sits between 2/7 (≈0.286) and 3/7 (≈0.429), so the effective rule is:
      BULLISH  when net-bullish factor count ≥ 3  (#bullish − #bearish ≥ 3)
      BEARISH  when net-bearish factor count ≥ 3
      NEUTRAL  when net agreement ≤ 2 in either direction
    Expressing it as "at least 3 net factors same direction" is more intuitive
    than the decimal 0.30 for communicating to stakeholders.
    ⚠️ 暫定閾值：3/7 為直覺設定，未經統計最佳化。若樣本回測後需調整，
    改這裡的 0.30 並同步更新上方說明即可（等效為改「至少幾個因子同向」）。

    Confidence
    ----------
    confidence = max(0.15, min(0.95, 0.30 + abs(avg_score) * 0.65))
    If consolidating: confidence *= 0.65, capped at 0.50.
    """
    close    = indicators.get("close", float("nan"))
    sma5     = indicators.get("sma5",  float("nan"))
    sma20    = indicators.get("sma20", float("nan"))
    sma60    = indicators.get("sma60", float("nan"))
    macd_h   = indicators.get("macd_hist", 0.0)
    rsi      = indicators.get("rsi",    50.0)
    k        = indicators.get("k",      50.0)
    d_val    = indicators.get("d",      50.0)
    bb_upper = indicators.get("bb_upper", float("nan"))
    bb_lower = indicators.get("bb_lower", float("nan"))

    def _score(a: float, b: float) -> int:
        if np.isnan(a) or np.isnan(b):
            return 0
        if a > b:
            return 1
        if a < b:
            return -1
        return 0

    # Factor 7: bb_pct
    if (
        not np.isnan(bb_upper)
        and not np.isnan(bb_lower)
        and not np.isnan(close)
        and (bb_upper - bb_lower) != 0.0
    ):
        bb_pct = (close - bb_lower) / (bb_upper - bb_lower)
        if bb_pct > 0.55:
            f7 = 1
        elif bb_pct < 0.45:
            f7 = -1
        else:
            f7 = 0
    else:
        f7 = 0

    scores = [
        _score(close, sma20),     # factor 1
        _score(sma5, sma20),      # factor 2
        _score(sma20, sma60),     # factor 3
        1 if macd_h > 0 else (-1 if macd_h < 0 else 0),  # factor 4
        1 if rsi > 60 else (-1 if rsi < 40 else 0),       # factor 5
        _score(k, d_val),          # factor 6
        f7,                        # factor 7
    ]

    avg_score = float(np.mean(scores))

    if avg_score > 0.30:
        signal = Signal.BULLISH
    elif avg_score < -0.30:
        signal = Signal.BEARISH
    else:
        signal = Signal.NEUTRAL

    confidence = max(0.15, min(0.95, 0.30 + abs(avg_score) * 0.65))

    if is_consolidating:
        confidence = min(0.50, confidence * 0.65)

    return signal, confidence


# ─── Deterministic narrative ──────────────────────────────────────────────────

def _build_narrative(
    indicators: dict[str, float],
    signal: Signal,
    is_consolidating: bool,
) -> str:
    """
    Qualitative-only narrative — NO numbers, NO percentages, NO decimal values.

    The Verifier (agents/verifier.py) will flag any numeric literal in the
    returned string that does not appear in the metrics dict.  This function
    deliberately avoids all numeric output to satisfy the CLAUDE.md constraint.
    """
    parts: list[str] = []

    # MA alignment
    sma5  = indicators.get("sma5",  float("nan"))
    sma20 = indicators.get("sma20", float("nan"))
    sma60 = indicators.get("sma60", float("nan"))
    if not (np.isnan(sma5) or np.isnan(sma20) or np.isnan(sma60)):
        if sma5 > sma20 > sma60:
            parts.append("均線多頭排列")
        elif sma5 < sma20 < sma60:
            parts.append("均線空頭排列")
        else:
            parts.append("均線糾結")

    # MACD
    macd_hist = indicators.get("macd_hist", 0.0)
    if macd_hist > 0:
        parts.append("MACD 柱狀圖翻正")
    elif macd_hist < 0:
        parts.append("MACD 柱狀圖翻負")
    else:
        parts.append("MACD 柱狀圖持平")

    # RSI
    rsi = indicators.get("rsi", 50.0)
    if rsi > 60:
        parts.append("RSI 處於強勢區")
    elif rsi < 40:
        parts.append("RSI 處於弱勢區")
    else:
        parts.append("RSI 中性")

    # Stochastic K/D
    k    = indicators.get("k", 50.0)
    d_v  = indicators.get("d", 50.0)
    if k > d_v:
        parts.append("KD 黃金交叉")
    elif k < d_v:
        parts.append("KD 死亡交叉")
    else:
        parts.append("KD 持平")

    # Volume
    vol_ratio = indicators.get("vol_ratio", 1.0)
    if vol_ratio > 1.5:
        parts.append("成交量顯著放大")
    elif vol_ratio < 0.7:
        parts.append("成交量明顯萎縮")

    # Overall signal
    signal_label = {
        Signal.BULLISH: "技術面偏多",
        Signal.BEARISH: "技術面偏空",
        Signal.NEUTRAL: "技術面中性",
    }[signal]
    parts.append(signal_label)

    # Consolidation warning — must appear last and is mandatory when detected
    if is_consolidating:
        parts.append(
            "⚠️ 布林通道收窄，市場處於區間震盪，技術訊號可信度低"
        )

    return "，".join(parts) + "。"


# ─── LangGraph state ──────────────────────────────────────────────────────────

class TechnicalAgentState(TypedDict):
    symbol: str
    market: str
    price_adapter: Optional[DataSourceAdapter]
    asof: datetime
    price_data: Optional[OHLCVData]
    indicators: Optional[dict[str, float]]
    is_consolidating: bool
    pipeline_errors: list[str]
    signal: Optional[AgentSignal]


# ─── Node functions ────────────────────────────────────────────────────────────

def _node_fetch(state: TechnicalAgentState) -> TechnicalAgentState:
    """Fetch OHLCV data via price_adapter."""
    errors = list(state["pipeline_errors"])
    adapter = state["price_adapter"]
    if adapter is None:
        # 台股優先用 FinMind；其他市場用 yfinance
        if state["market"] == "TW":
            adapter = FinMindPriceAdapter()
        else:
            adapter = YFinancePriceAdapter()

    try:
        sourced: SourcedData = adapter.fetch(  # type: ignore[call-arg]
            symbol=state["symbol"],
            period="6mo",
            interval="1d",
            market=state["market"],
        )
        price_data: OHLCVData = sourced.payload
        # Override asof with actual data timestamp
        return {**state, "price_data": price_data, "asof": sourced.asof}
    except Exception as exc:
        errors.append(f"price fetch failed: {exc}")
        return {**state, "price_data": None, "pipeline_errors": errors}


def _node_compute(state: TechnicalAgentState) -> TechnicalAgentState:
    """Compute all indicators from price data."""
    price_data = state["price_data"]
    errors = list(state["pipeline_errors"])
    if price_data is None:
        errors.append("no price data — skipping indicator computation")
        return {**state, "indicators": None, "is_consolidating": False, "pipeline_errors": errors}

    try:
        indicators = _compute_all_indicators(price_data)
        bb_width_pct = indicators.get("bb_width_pct", float("nan"))
        is_consolidating = (
            not np.isnan(bb_width_pct) and bb_width_pct < 0.04
        )
        return {**state, "indicators": indicators, "is_consolidating": is_consolidating}
    except Exception as exc:
        errors.append(f"indicator computation failed: {exc}")
        return {**state, "indicators": None, "is_consolidating": False, "pipeline_errors": errors}


def _node_signal(state: TechnicalAgentState) -> TechnicalAgentState:
    """Assemble AgentSignal from indicators."""
    indicators = state["indicators"]
    price_data = state["price_data"]
    asof = state["asof"]
    symbol = state["symbol"]
    market = state["market"]
    errors = list(state["pipeline_errors"])

    if indicators is None or price_data is None:
        sig = _error_signal(
            symbol=symbol,
            market=market,
            asof=asof,
            errors=errors + ["missing price data or indicators"],
        )
        return {**state, "signal": sig}

    is_consolidating = state["is_consolidating"]
    signal, confidence = _determine_signal_and_confidence(indicators, is_consolidating)

    narrative = _build_narrative(indicators, signal, is_consolidating)
    verifier_errors = check_narrative(narrative, indicators)
    if verifier_errors:
        errors.extend(verifier_errors)

    # Data quality
    n_bars = len(price_data.close)
    completeness = min(1.0, n_bars / 60)
    last_bar_dt = price_data.dates[-1] if price_data.dates else asof
    # Ensure both are tz-naive for subtraction
    asof_naive = asof.replace(tzinfo=None) if asof.tzinfo is not None else asof
    last_bar_naive = last_bar_dt.replace(tzinfo=None) if last_bar_dt.tzinfo is not None else last_bar_dt
    staleness_sec = max(0.0, (asof_naive - last_bar_naive).total_seconds())

    # Key evidence (source + asof on every item)
    close   = indicators.get("close", float("nan"))
    sma20   = indicators.get("sma20", float("nan"))
    bb_width = indicators.get("bb_width_pct", float("nan"))

    close_vs_sma20: float = (
        (close - sma20)
        if not (np.isnan(close) or np.isnan(sma20))
        else float("nan")
    )

    key_evidence: list[Evidence] = [
        Evidence(
            claim="RSI",
            value=indicators.get("rsi"),
            source="technical_agent:rsi",
            asof=asof,
        ),
        Evidence(
            claim="MACD 柱狀圖",
            value=indicators.get("macd_hist"),
            source="technical_agent:macd",
            asof=asof,
        ),
        Evidence(
            claim="收盤價偏離 SMA20",
            value=close_vs_sma20,
            source="technical_agent:ma",
            asof=asof,
        ),
        Evidence(
            claim="隨機震盪 K/D",
            value=indicators.get("k"),
            source="technical_agent:stochastic",
            asof=asof,
        ),
        Evidence(
            claim="布林通道寬度百分比",
            value=bb_width,
            source="technical_agent:bollinger",
            asof=asof,
        ),
    ]

    metrics: dict[str, Any] = {**indicators, "is_consolidating": is_consolidating}

    sig = AgentSignal(
        agent=AgentType.TECHNICAL,
        target=Target(symbol=symbol, market=market, asof=asof),
        signal=signal,
        confidence=confidence,
        time_horizon=TimeHorizon.SHORT,
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

def _error_signal(
    symbol: str,
    market: str,
    asof: datetime,
    errors: list[str],
) -> AgentSignal:
    """Fallback when the pipeline fails — confidence=0 signals total failure."""
    return AgentSignal(
        agent=AgentType.TECHNICAL,
        target=Target(symbol=symbol, market=market, asof=asof),
        signal=Signal.NEUTRAL,
        confidence=0.0,
        time_horizon=TimeHorizon.SHORT,
        key_evidence=[],
        hard_constraints=[],
        metrics={},
        narrative="技術分析管線初始化失敗，無法產出訊號。詳見 errors 欄位。",
        data_quality=DataQuality(completeness=0.0, staleness_sec=0.0, confidence=0.0),
        errors=errors,
    )


# ─── Full pipeline (convenience wrapper) ──────────────────────────────────────

def run_technical_agent(
    *,
    symbol: str,
    market: str = "TW",
    price_adapter: Optional[DataSourceAdapter] = None,
    asof: Optional[datetime] = None,
) -> AgentSignal:
    """
    Run the full technical analysis pipeline and return AgentSignal.

    Parameters
    ----------
    symbol        : Ticker symbol, e.g. "2330.TW", "AAPL".
    market        : Market code, e.g. "TW", "US".
    price_adapter : OHLCV adapter. Defaults to YFinancePriceAdapter (live network).
    asof          : Timestamp for provenance.  Defaults to UTC now.
    """
    eff_asof = asof if asof is not None else datetime.now(tz=UTC)

    state: TechnicalAgentState = {
        "symbol":          symbol,
        "market":          market,
        "price_adapter":   price_adapter,
        "asof":            eff_asof,
        "price_data":      None,
        "indicators":      None,
        "is_consolidating": False,
        "pipeline_errors": [],
        "signal":          None,
    }

    state = _node_fetch(state)
    state = _node_compute(state)
    state = _node_signal(state)

    return state["signal"] or _error_signal(
        symbol=symbol,
        market=market,
        asof=eff_asof,
        errors=state["pipeline_errors"],
    )


# ─── LangGraph graph ──────────────────────────────────────────────────────────

def create_technical_graph() -> Any:
    """
    Build and compile the technical agent as a LangGraph CompiledGraph.

    Usage in Supervisor
    -------------------
        from agents.technical_agent import create_technical_graph
        tech_graph = create_technical_graph()
        result = tech_graph.invoke(initial_state)
        signal: AgentSignal = result["signal"]
    """
    builder: StateGraph = StateGraph(TechnicalAgentState)

    builder.add_node("fetch",   _node_fetch)
    builder.add_node("compute", _node_compute)
    builder.add_node("signal",  _node_signal)

    builder.set_entry_point("fetch")
    builder.add_edge("fetch",   "compute")
    builder.add_edge("compute", "signal")
    builder.add_edge("signal",  END)

    return builder.compile()
