"""
Phase 3 — Technical Agent unit tests.

All tests are pure-unit: no network calls, no yfinance, no filesystem.
Network-dependent integration tests (live yfinance) would be marked
@pytest.mark.integration and are excluded here.

Coverage
--------
test_sma_basic               : compute_sma basic case (period = 3, 5 values)
test_sma_short_series        : compute_sma falls back to all values when len < period
test_rsi_flat_series         : flat price → diffs = 0 → RSI = 50
test_rsi_all_up              : strictly increasing → RSI close to 100
test_rsi_all_down            : strictly decreasing → RSI close to 0
test_macd_structure          : histogram identity: macd_hist == macd_line - signal_line
test_bollinger_structure     : upper > mid > lower; manual spot-check
test_stochastic_all_at_high  : close == high every bar → K = 100
test_stochastic_all_at_low   : close == low  every bar → K = 0
test_volume_ratio_basic      : last volume = 2× average → ratio ≈ 2
test_determine_signal_bullish : all 7 factors bullish → BULLISH signal
test_determine_signal_bearish : all 7 factors bearish → BEARISH signal
test_determine_signal_neutral : mixed factors → NEUTRAL (avg ≈ 0)
test_consolidation_reduces_confidence : consolidating lowers confidence vs. not
test_narrative_no_numbers    : Verifier finds no unregistered numbers in narrative
test_build_signal_schema     : FakePriceAdapter → valid AgentSignal with correct type
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np
import pytest

from adapters.base import SourcedData
from adapters.price_adapter import OHLCVData, _parse_tw_price_rows, _strip_tw_suffix
from agents.technical_agent import (
    _build_narrative,
    _determine_signal_and_confidence,
    compute_bollinger,
    compute_macd,
    compute_rsi,
    compute_sma,
    compute_stochastic,
    compute_volume_ratio,
    run_technical_agent,
)
from agents.verifier import check_narrative
from schemas.agent_signal import AgentType, Signal, TimeHorizon


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_ohlcv(
    close: list[float],
    *,
    high_offset: float = 2.0,
    low_offset: float = 2.0,
    open_offset: float = 1.0,
    volume: float = 1000.0,
) -> OHLCVData:
    """Build a synthetic OHLCVData for testing."""
    n = len(close)
    c = np.array(close, dtype=np.float64)
    h = c + high_offset
    lo = c - low_offset
    op = c - open_offset
    v = np.full(n, volume, dtype=np.float64)
    dates = [datetime(2024, 1, 1, tzinfo=timezone.utc)] * n
    return OHLCVData(
        symbol="TEST", market="TW",
        close=c, high=h, low=lo, open_=op, volume=v, dates=dates,
    )


class FakePriceAdapter:
    """Synthetic adapter that returns a stable uptrend dataset (no network)."""

    @property
    def source_name(self) -> str:
        return "fake"

    def fetch(self, symbol: str, period: str = "6mo", interval: str = "1d", **kwargs: Any) -> SourcedData:
        n = 100
        close = np.linspace(100.0, 120.0, n)
        high  = close + 2.0
        low   = close - 2.0
        open_ = close - 1.0
        volume = np.ones(n) * 1000.0
        dates = [datetime(2024, 1, 1, tzinfo=timezone.utc)] * n
        payload = OHLCVData(
            symbol=symbol,
            market=kwargs.get("market", "TW"),
            close=close,
            high=high,
            low=low,
            open_=open_,
            volume=volume,
            dates=dates,
        )
        return SourcedData(
            payload=payload,
            source="fake",
            asof=dates[-1],
        )


# ─── SMA ──────────────────────────────────────────────────────────────────────

def test_sma_basic() -> None:
    arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    result = compute_sma(arr, 3)
    assert result == pytest.approx(4.0), f"Expected 4.0, got {result}"


def test_sma_short_series() -> None:
    """When len < period, use all available values: mean([1, 2]) = 1.5."""
    arr = np.array([1.0, 2.0])
    result = compute_sma(arr, 5)
    assert result == pytest.approx(1.5), f"Expected 1.5, got {result}"


# ─── RSI ──────────────────────────────────────────────────────────────────────

def test_rsi_flat_series() -> None:
    """All same price → diffs = 0 → avg_gain = avg_loss = 0 → RSI = 50."""
    arr = np.full(20, 100.0)
    assert compute_rsi(arr) == pytest.approx(50.0)


def test_rsi_all_up() -> None:
    """Strictly increasing → no losses → RSI = 100."""
    arr = np.arange(1.0, 30.0)
    result = compute_rsi(arr)
    assert result == pytest.approx(100.0)


def test_rsi_all_down() -> None:
    """Strictly decreasing → no gains → RSI close to 0 (computed as 0.0)."""
    arr = np.arange(30.0, 0.0, -1.0)
    result = compute_rsi(arr)
    # avg_loss > 0, avg_gain = 0  → RS = 0 → RSI = 100 - 100/(1+0) = 0
    assert result == pytest.approx(0.0)


def test_rsi_wilder_smoothing() -> None:
    """
    Verify Wilder's EMA-like smoothing using a hand-computed exact reference.

    Sequence: [1, 2, 3, 4, 5, 6, 7, 8, 7, 6, 5, 4, 3, 2, 1, 2]  (16 bars)

    First 14 diffs: +1,+1,+1,+1,+1,+1,+1,−1,−1,−1,−1,−1,−1,−1
      → 7 gains of +1, 7 losses of +1
      Seed: avg_gain = 7/14 = 1/2,  avg_loss = 7/14 = 1/2

    15th diff (index 14): +1  (gain=1, loss=0)
    Wilder smoothing step:
      avg_gain = (1/2 × 13 + 1) / 14 = 15/28
      avg_loss = (1/2 × 13 + 0) / 14 = 13/28
      RS  = (15/28) / (13/28) = 15/13
      RSI = 100 − 100/(1 + 15/13)
           = 100 − 100 × 13/28
           = 375/7
           ≈ 53.5714...

    This reference is derived from exact fractions with no floating-point
    approximation, making it independent of any other RSI implementation.
    Unlike a simple-average RSI (which would re-average the last 14 diffs
    and return 50.0 for the same data), Wilder's smoothing produces 375/7.
    The difference proves the two algorithms are distinct.
    """
    prices = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0,
                       7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0, 2.0])
    result = compute_rsi(prices, period=14)
    expected = 375.0 / 7.0   # exact rational: ≈ 53.57142857...
    assert result == pytest.approx(expected, rel=1e-9), (
        f"Expected Wilder RSI = 375/7 ≈ {expected:.8f}, got {result:.8f}. "
        "If this fails, the implementation is using simple-average RSI instead of "
        "Wilder's EMA smoothing — they give identical results only on degenerate "
        "(all-up / all-down / flat) series."
    )


# ─── MACD ─────────────────────────────────────────────────────────────────────

def test_macd_structure() -> None:
    """histogram must equal macd_line - signal_line exactly."""
    close = np.linspace(100.0, 130.0, 60)
    ml, sl, hist = compute_macd(close)
    assert hist == pytest.approx(ml - sl, abs=1e-10)


# ─── Bollinger ────────────────────────────────────────────────────────────────

def test_bollinger_structure() -> None:
    """upper > mid > lower; mid = mean of last 20 bars."""
    close = np.linspace(100.0, 119.0, 30)
    upper, mid, lower = compute_bollinger(close, period=20)
    assert upper > mid > lower, "Bollinger band ordering violated"
    # mid should equal mean of last 20 bars
    expected_mid = float(np.mean(close[-20:]))
    assert mid == pytest.approx(expected_mid, rel=1e-9)


# ─── Stochastic ───────────────────────────────────────────────────────────────

def test_stochastic_all_at_high() -> None:
    """
    When close == high every bar, raw_%K = 100 → K = 100, D = 100.
    """
    n = 30
    high  = np.full(n, 110.0)
    low   = np.full(n, 100.0)
    close = np.full(n, 110.0)   # always at the high
    k, d = compute_stochastic(high, low, close)
    assert k == pytest.approx(100.0)
    assert d == pytest.approx(100.0)


def test_stochastic_all_at_low() -> None:
    """
    When close == low every bar, raw_%K = 0 → K = 0, D = 0.
    """
    n = 30
    high  = np.full(n, 110.0)
    low   = np.full(n, 100.0)
    close = np.full(n, 100.0)   # always at the low
    k, d = compute_stochastic(high, low, close)
    assert k == pytest.approx(0.0)
    assert d == pytest.approx(0.0)


# ─── Volume ratio ─────────────────────────────────────────────────────────────

def test_volume_ratio_basic() -> None:
    """Last volume = 2× the average of a flat series → ratio ≈ 2.0."""
    # 19 bars at 1000, last bar at 2000
    v = np.array([1000.0] * 19 + [2000.0])
    ratio = compute_volume_ratio(v, period=20)
    # avg = (19*1000 + 2000)/20 = 1050; 2000/1050 ≈ 1.905
    # But spec says volume[-1] / mean(volume[-period:])
    expected = 2000.0 / np.mean(v[-20:])
    assert ratio == pytest.approx(expected, rel=1e-9)


# ─── Signal determination ─────────────────────────────────────────────────────

def _all_bullish_indicators() -> dict[str, float]:
    """Indicators that push all 7 factors to +1."""
    return {
        "close":    105.0,   # > sma20 (100)
        "sma5":     103.0,   # > sma20 (100)
        "sma20":    100.0,   # > sma60 (90)
        "sma60":     90.0,
        "macd_hist":  1.0,   # > 0
        "rsi":        70.0,  # > 60
        "k":          80.0,  # > d
        "d":          60.0,
        "bb_upper":  115.0,
        "bb_lower":   85.0,  # bb_pct = (105-85)/(115-85) = 0.667 > 0.55
        "bb_mid":    100.0,
        "bb_width_pct": 0.30,
    }


def _all_bearish_indicators() -> dict[str, float]:
    """Indicators that push all 7 factors to -1."""
    return {
        "close":     95.0,   # < sma20 (100)
        "sma5":      97.0,   # < sma20 (100)
        "sma20":    100.0,   # < sma60 (110)
        "sma60":    110.0,
        "macd_hist": -1.0,  # < 0
        "rsi":        30.0, # < 40
        "k":          20.0, # < d
        "d":          40.0,
        "bb_upper":  115.0,
        "bb_lower":   85.0,  # bb_pct = (95-85)/(115-85) = 0.333 < 0.45
        "bb_mid":    100.0,
        "bb_width_pct": 0.30,
    }


def test_determine_signal_bullish() -> None:
    ind = _all_bullish_indicators()
    signal, confidence = _determine_signal_and_confidence(ind, is_consolidating=False)
    assert signal == Signal.BULLISH
    assert confidence > 0.30


def test_determine_signal_bearish() -> None:
    ind = _all_bearish_indicators()
    signal, confidence = _determine_signal_and_confidence(ind, is_consolidating=False)
    assert signal == Signal.BEARISH
    assert confidence > 0.30


def test_determine_signal_neutral() -> None:
    """Mixed signals where avg_score ≈ 0 → NEUTRAL."""
    # 3 bullish factors, 3 bearish factors, 1 neutral → avg = 0/7 = 0
    ind: dict[str, float] = {
        "close":    100.0,   # == sma20 → 0
        "sma5":     103.0,   # > sma20 → +1
        "sma20":    100.0,   # < sma60 → -1
        "sma60":    110.0,
        "macd_hist":  1.0,  # > 0 → +1
        "rsi":        50.0, # neutral → 0
        "k":          40.0, # < d → -1
        "d":          60.0,
        "bb_upper":  115.0,
        "bb_lower":   85.0,  # bb_pct = (100-85)/(115-85) = 0.5 → 0
        "bb_mid":    100.0,
        "bb_width_pct": 0.30,
    }
    # scores = [0, +1, -1, +1, 0, -1, 0] → avg = 0/7 = 0
    signal, _ = _determine_signal_and_confidence(ind, is_consolidating=False)
    assert signal == Signal.NEUTRAL


# ─── Consolidation ────────────────────────────────────────────────────────────

def test_consolidation_reduces_confidence() -> None:
    """is_consolidating=True must produce lower confidence than False."""
    ind = _all_bullish_indicators()
    _, conf_normal = _determine_signal_and_confidence(ind, is_consolidating=False)
    _, conf_consol = _determine_signal_and_confidence(ind, is_consolidating=True)
    assert conf_consol < conf_normal
    assert conf_consol <= 0.50, f"Consolidating confidence must be capped at 0.50, got {conf_consol}"


# ─── Narrative / Verifier ─────────────────────────────────────────────────────

def test_narrative_no_numbers() -> None:
    """Verifier must find no unregistered numbers in the deterministic narrative."""
    ind = _all_bullish_indicators()
    # Add all numeric indicator values to a metrics dict so Verifier can whitelist them
    metrics: dict[str, Any] = dict(ind)
    narrative = _build_narrative(ind, Signal.BULLISH, is_consolidating=False)
    errors = check_narrative(narrative, metrics)
    assert errors == [], f"Verifier flagged numbers in narrative: {errors}\nNarrative: {narrative!r}"


def test_narrative_no_numbers_consolidating() -> None:
    """Consolidation variant narrative must also pass Verifier."""
    ind = _all_bullish_indicators()
    ind["bb_width_pct"] = 0.02  # triggers consolidation
    metrics: dict[str, Any] = dict(ind)
    narrative = _build_narrative(ind, Signal.NEUTRAL, is_consolidating=True)
    errors = check_narrative(narrative, metrics)
    assert errors == [], f"Verifier flagged numbers: {errors}\nNarrative: {narrative!r}"


# ─── Full AgentSignal schema test ─────────────────────────────────────────────

def test_build_signal_schema() -> None:
    """
    FakePriceAdapter → run_technical_agent → valid AgentSignal.

    Checks:
    - agent == AgentType.TECHNICAL
    - time_horizon == TimeHorizon.SHORT
    - hard_constraints == []
    - 0 < confidence <= 1
    - all key_evidence items have source and asof
    - metrics contains indicator keys
    """
    adapter = FakePriceAdapter()
    result = run_technical_agent(
        symbol="TEST",
        market="TW",
        price_adapter=adapter,  # type: ignore[arg-type]
        asof=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )

    assert result.agent == AgentType.TECHNICAL
    assert result.time_horizon == TimeHorizon.SHORT
    assert result.hard_constraints == []
    assert 0.0 < result.confidence <= 1.0
    assert result.signal in (Signal.BULLISH, Signal.BEARISH, Signal.NEUTRAL)

    # All evidence items must carry source and asof
    for ev in result.key_evidence:
        assert ev.source != "", f"Evidence missing source: {ev}"
        assert ev.asof is not None, f"Evidence missing asof: {ev}"

    # Metrics must include key indicator keys
    for key in ("close", "rsi", "macd_hist", "sma20", "bb_width_pct"):
        assert key in result.metrics, f"Missing metric key: {key}"

    # DataQuality fields are within valid bounds
    assert 0.0 <= result.data_quality.completeness <= 1.0
    assert result.data_quality.staleness_sec >= 0.0
    assert 0.0 <= result.data_quality.confidence <= 1.0

    # No pipeline errors with fake adapter
    assert result.errors == [], f"Unexpected pipeline errors: {result.errors}"


def test_build_signal_uptrend_is_bullish() -> None:
    """
    A strong steady uptrend should produce a BULLISH signal.
    (100 bars, strictly increasing price, no noise → all MA indicators align bullish)
    """
    adapter = FakePriceAdapter()
    result = run_technical_agent(
        symbol="TEST",
        market="TW",
        price_adapter=adapter,  # type: ignore[arg-type]
    )
    assert result.signal == Signal.BULLISH, (
        f"Expected BULLISH for uptrend, got {result.signal}. "
        f"Metrics: {result.metrics}"
    )


# ─── FinMind price adapter pure-function tests ────────────────────────────────

def test_strip_tw_suffix_variants() -> None:
    """_strip_tw_suffix must remove .TW and .TWO but leave bare codes alone."""
    assert _strip_tw_suffix("2330.TW")  == "2330"
    assert _strip_tw_suffix("6505.TWO") == "6505"
    assert _strip_tw_suffix("2330")     == "2330"
    assert _strip_tw_suffix("AAPL")     == "AAPL"   # non-TW ticker unchanged


def _make_finmind_rows(
    closes: Sequence[float],
    *,
    high_offset: float = 5.0,
    low_offset: float = 5.0,
    volume: float = 10_000.0,
) -> list[dict[str, Any]]:
    """Build synthetic FinMind TaiwanStockPrice rows for testing."""
    rows = []
    for i, c in enumerate(closes):
        rows.append({
            "date":           f"2024-{(i // 20) + 1:02d}-{(i % 20) + 1:02d}",
            "stock_id":       "2330",
            "open":           c - 1.0,
            "max":            c + high_offset,
            "min":            c - low_offset,
            "close":          c,
            "Trading_Volume": volume,
            "Trading_money":  c * volume,
            "spread":         1.0,
            "Trading_turnover": 100,
        })
    return rows


def test_parse_tw_price_rows_basic() -> None:
    """_parse_tw_price_rows: field mapping and array shapes are correct."""
    closes = [100.0, 102.0, 98.0, 105.0]
    rows   = _make_finmind_rows(closes, high_offset=3.0, low_offset=2.0)
    data   = _parse_tw_price_rows(rows, "2330.TW")

    assert data.symbol == "2330.TW"
    assert data.market == "TW"
    assert len(data.close)  == 4
    assert len(data.dates)  == 4
    np.testing.assert_array_almost_equal(data.close,  closes)
    np.testing.assert_array_almost_equal(data.high,   [c + 3.0 for c in closes])
    np.testing.assert_array_almost_equal(data.low,    [c - 2.0 for c in closes])
    np.testing.assert_array_almost_equal(data.volume, [10_000.0] * 4)


def test_parse_tw_price_rows_sorting() -> None:
    """_parse_tw_price_rows must sort ascending by date even if rows arrive out of order."""
    rows = [
        {"date": "2024-01-03", "stock_id": "2330", "open": 99.0, "max": 105.0,
         "min": 97.0, "close": 103.0, "Trading_Volume": 1000.0,
         "Trading_money": 103000.0, "spread": 1.0, "Trading_turnover": 10},
        {"date": "2024-01-01", "stock_id": "2330", "open": 98.0, "max": 102.0,
         "min": 96.0, "close": 100.0, "Trading_Volume": 1000.0,
         "Trading_money": 100000.0, "spread": 1.0, "Trading_turnover": 10},
        {"date": "2024-01-02", "stock_id": "2330", "open": 100.0, "max": 104.0,
         "min": 98.0, "close": 101.0, "Trading_Volume": 1000.0,
         "Trading_money": 101000.0, "spread": 1.0, "Trading_turnover": 10},
    ]
    data = _parse_tw_price_rows(rows, "2330")
    # dates must be ascending
    assert data.dates[0] < data.dates[1] < data.dates[2]
    # close values must follow sorted date order
    np.testing.assert_array_almost_equal(data.close, [100.0, 101.0, 103.0])


def test_finmind_adapter_fetch_uses_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    FinMindPriceAdapter.fetch() must call _fetch_raw and return correct OHLCVData.

    Monkeypatches _fetch_raw so no real network call is made.
    """
    from adapters.price_adapter import FinMindPriceAdapter

    closes = list(np.linspace(500.0, 550.0, 10))
    fake_rows = _make_finmind_rows(closes)

    adapter = FinMindPriceAdapter(api_token="test_token")
    monkeypatch.setattr(adapter, "_fetch_raw", lambda *_: fake_rows)

    sourced = adapter.fetch(symbol="2330.TW", period="3mo")
    ohlcv: OHLCVData = sourced.payload

    assert sourced.source == "finmind_taiwan_stock_price"
    assert len(ohlcv.close) == 10
    assert ohlcv.close[-1] == pytest.approx(550.0)
    assert sourced.asof == ohlcv.dates[-1]


def test_finmind_adapter_empty_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """FinMindPriceAdapter.fetch() must raise ValueError when no rows returned."""
    from adapters.price_adapter import FinMindPriceAdapter

    adapter = FinMindPriceAdapter()
    monkeypatch.setattr(adapter, "_fetch_raw", lambda *_: [])

    with pytest.raises(ValueError, match="no data"):
        adapter.fetch(symbol="9999.TW")
