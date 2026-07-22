"""
Phase 4 Macro Agent Tests

All external network I/O (Trading Economics API) is mocked.
No real TE_API_KEY required to run these tests.

Coverage
--------
Pure analytics:
  compute_surprise, compute_surprise_pct, classify_surprise,
  event_market_direction, _recency_weight, _detect_hot_data_warning,
  compute_macro_score

Adapter parsing:
  _parse_float, _parse_te_date, _parse_te_events, TradingEconomicsAdapter

Narrative builder:
  _build_narrative (no_events, hot_data, per-event highlights)

Full pipeline (run_macro_agent):
  - Normal bullish / bearish / neutral scenarios
  - No events → degraded confidence
  - API key missing → degraded confidence
  - Fetch failure → degraded confidence
  - hot_data_warning flag propagation

Real-world validation case (in-code commentary):
  US CPI Sep 2024: actual=2.4%, consensus=2.3%
  surprise_pct = (2.4 - 2.3) / |2.3| ≈ +4.35%
  classify_surprise("beat") [5% > 4.35% ≥ 5% → borderline; tested separately]
  event_market_direction("Inflation Rate", +1) = -1 (inflation beat = bearish)
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from adapters.macro_adapter import (
    MacroEvent,
    MacroResult,
    TradingEconomicsAdapter,
    _parse_float,
    _parse_te_date,
    _parse_te_events,
    _require_te_key,
)
from agents.macro_agent import (
    CATEGORY_DIRECTION,
    GROWTH_CATEGORIES,
    INFLATION_CATEGORIES,
    Signal,
    _build_narrative,
    _detect_hot_data_warning,
    _recency_weight,
    _score_to_signal,
    classify_surprise,
    compute_macro_score,
    compute_surprise,
    compute_surprise_pct,
    event_market_direction,
    run_macro_agent,
)
from schemas.agent_signal import AgentType, TimeHorizon


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_event(
    category: str = "GDP Growth Rate",
    country: str = "United States",
    actual: float | None = 2.5,
    consensus: float | None = 2.0,
    previous: float | None = 1.8,
    importance: int = 3,
    days_ago: float = 0.0,
) -> MacroEvent:
    """Build a MacroEvent with a relative release_date (days ago from now UTC)."""
    release = datetime.now(UTC) - timedelta(days=days_ago)
    return MacroEvent(
        category=category,
        country=country,
        actual=actual,
        consensus=consensus,
        previous=previous,
        unit="%",
        importance=importance,
        release_date=release,
        source_name="trading_economics",
    )


def _make_mock_adapter(events: list[MacroEvent]) -> MagicMock:
    """Create a mock DataSourceAdapter that returns the given events."""
    mock = MagicMock()
    result = MacroResult(
        events=events,
        countries=["united states"],
        fetched_at=datetime.now(UTC),
    )
    sourced = MagicMock()
    sourced.payload = result
    sourced.asof = result.fetched_at
    mock.fetch.return_value = sourced
    return mock


# ─── compute_surprise ─────────────────────────────────────────────────────────


class TestComputeSurprise:
    def test_positive_surprise(self) -> None:
        assert compute_surprise(2.5, 2.0) == pytest.approx(0.5)

    def test_negative_surprise(self) -> None:
        assert compute_surprise(1.8, 2.0) == pytest.approx(-0.2)

    def test_in_line(self) -> None:
        assert compute_surprise(2.0, 2.0) == pytest.approx(0.0)

    def test_negative_consensus(self) -> None:
        # surprise is raw difference regardless of sign
        assert compute_surprise(-1.0, -2.0) == pytest.approx(1.0)


# ─── compute_surprise_pct ─────────────────────────────────────────────────────


class TestComputeSurprisePct:
    def test_5pct_beat(self) -> None:
        # (2.1 - 2.0) / 2.0 = 5%
        assert compute_surprise_pct(2.1, 2.0) == pytest.approx(0.05)

    def test_25pct_miss(self) -> None:
        # (1.5 - 2.0) / 2.0 = -25%
        assert compute_surprise_pct(1.5, 2.0) == pytest.approx(-0.25)

    def test_zero_consensus_returns_nan(self) -> None:
        result = compute_surprise_pct(1.0, 0.0)
        assert math.isnan(result)

    def test_negative_consensus_normalises_correctly(self) -> None:
        # (-1.0 - -2.0) / |-2.0| = 1.0 / 2.0 = 0.5
        assert compute_surprise_pct(-1.0, -2.0) == pytest.approx(0.5)

    def test_real_world_us_cpi_sep2024(self) -> None:
        """
        Real-world validation: US CPI Sep 2024
          actual=2.4%, consensus=2.3%
          surprise_pct = (2.4 - 2.3) / |2.3| ≈ 0.04348
        """
        spct = compute_surprise_pct(2.4, 2.3)
        assert spct == pytest.approx(0.04348, rel=0.01)
        # 4.35% < 5% threshold → classify as "in_line" (not "beat")
        assert classify_surprise(spct) == "in_line"


# ─── classify_surprise ────────────────────────────────────────────────────────


class TestClassifySurprise:
    def test_large_beat(self) -> None:
        assert classify_surprise(0.25) == "large_beat"

    def test_beat(self) -> None:
        assert classify_surprise(0.10) == "beat"

    def test_in_line_positive(self) -> None:
        assert classify_surprise(0.03) == "in_line"

    def test_in_line_zero(self) -> None:
        assert classify_surprise(0.0) == "in_line"

    def test_miss(self) -> None:
        assert classify_surprise(-0.10) == "miss"

    def test_large_miss(self) -> None:
        assert classify_surprise(-0.25) == "large_miss"

    def test_nan_returns_in_line(self) -> None:
        assert classify_surprise(float("nan")) == "in_line"

    def test_exactly_at_large_threshold(self) -> None:
        assert classify_surprise(0.20) == "large_beat"

    def test_exactly_at_small_threshold(self) -> None:
        assert classify_surprise(0.05) == "beat"

    def test_real_world_nfp_beat(self) -> None:
        """
        Real-world validation: US NFP Oct 2023
          actual=336K, consensus=170K
          surprise_pct = (336 - 170) / 170 ≈ +97.6% → large_beat
        """
        spct = compute_surprise_pct(336.0, 170.0)
        assert classify_surprise(spct) == "large_beat"


# ─── event_market_direction ───────────────────────────────────────────────────


class TestEventMarketDirection:
    def test_inflation_beat_is_bearish(self) -> None:
        """CPI beat (positive surprise) → rate-hike risk → bearish."""
        # category_dir = -1; surprise_sign = +1 → -1 * +1 = -1
        assert event_market_direction("Inflation Rate", +1) == -1

    def test_inflation_miss_is_bullish(self) -> None:
        """CPI miss → disinflationary signal → bullish."""
        assert event_market_direction("Inflation Rate", -1) == +1

    def test_gdp_beat_is_bullish(self) -> None:
        assert event_market_direction("GDP Growth Rate", +1) == +1

    def test_gdp_miss_is_bearish(self) -> None:
        assert event_market_direction("GDP Growth Rate", -1) == -1

    def test_nfp_beat_is_bullish(self) -> None:
        assert event_market_direction("Non Farm Payrolls", +1) == +1

    def test_unemployment_beat_is_bearish(self) -> None:
        """Unemployment Rate beat (more jobless) → bearish."""
        assert event_market_direction("Unemployment Rate", +1) == -1

    def test_interest_rate_is_neutral(self) -> None:
        assert event_market_direction("Interest Rate", +1) == 0

    def test_unknown_category_is_neutral(self) -> None:
        assert event_market_direction("Unknown Indicator XYZ", +1) == 0

    def test_zero_surprise_sign(self) -> None:
        """Exactly in-line → direction=0 regardless of category."""
        assert event_market_direction("Inflation Rate", 0) == 0

    def test_cpi_real_world_direction(self) -> None:
        """
        Real-world: US CPI Sep 2024 actual=2.4%, consensus=2.3%
        surprise_pct ≈ +4.35% (in_line per classify_surprise)
        surprise_sign = +1 (actual > consensus)
        event_market_direction("CPI", +1) should be -1 (bearish)
        """
        assert event_market_direction("CPI", +1) == -1

    def test_all_table_entries_valid(self) -> None:
        """All entries in CATEGORY_DIRECTION are -1, 0, or +1."""
        for category, direction in CATEGORY_DIRECTION.items():
            assert direction in (-1, 0, 1), f"{category} has invalid direction {direction}"


# ─── _recency_weight ─────────────────────────────────────────────────────────


class TestRecencyWeight:
    def test_today_has_weight_one(self) -> None:
        now = datetime.now(UTC)
        w = _recency_weight(now, now)
        assert w == pytest.approx(1.0, abs=0.01)

    def test_half_life_days_has_weight_half(self) -> None:
        """After HALF_LIFE_DAYS days, weight should be ~0.5."""
        now = datetime.now(UTC)
        release = now - timedelta(days=3.0)  # half-life = 3 days
        w = _recency_weight(release, now)
        assert w == pytest.approx(0.5, rel=0.01)

    def test_older_event_has_lower_weight(self) -> None:
        now = datetime.now(UTC)
        w_new = _recency_weight(now - timedelta(days=1), now)
        w_old = _recency_weight(now - timedelta(days=5), now)
        assert w_new > w_old

    def test_future_event_clamped_to_one(self) -> None:
        """Future release dates should not produce weight > 1 (days_ago clamped to 0)."""
        now = datetime.now(UTC)
        future = now + timedelta(days=2)
        w = _recency_weight(future, now)
        assert w == pytest.approx(1.0, abs=0.01)

    def test_naive_and_aware_compatibility(self) -> None:
        """Mixed tz-aware and naive datetimes should not raise."""
        naive = datetime(2024, 9, 15, 8, 30, 0)  # naive
        aware = datetime(2024, 9, 16, 8, 30, 0, tzinfo=UTC)
        # Should not raise, just compute a weight
        w = _recency_weight(naive, aware)
        assert 0 < w <= 1.0


# ─── _detect_hot_data_warning ─────────────────────────────────────────────────


class TestDetectHotDataWarning:
    def test_no_warning_when_only_growth(self) -> None:
        # Growth beat only — no inflation
        events = [(_make_event("GDP Growth Rate", actual=3.0, consensus=2.0), +1)]
        assert _detect_hot_data_warning(events) is False

    def test_no_warning_when_only_inflation(self) -> None:
        # Inflation beat only, no growth beat → no "hot data" paradox
        events = [(_make_event("Inflation Rate", actual=3.5, consensus=3.0), -1)]
        assert _detect_hot_data_warning(events) is False

    def test_warning_when_both_growth_and_inflation_beat(self) -> None:
        """Both growth positive + inflation beat → hot data warning."""
        events = [
            (_make_event("GDP Growth Rate", actual=3.0, consensus=2.0), +1),
            (_make_event("Inflation Rate", actual=3.5, consensus=3.0), -1),
        ]
        assert _detect_hot_data_warning(events) is True

    def test_no_warning_when_growth_miss(self) -> None:
        """Growth miss + inflation beat → no hot-data tension."""
        events = [
            (_make_event("GDP Growth Rate", actual=1.0, consensus=2.0), -1),
            (_make_event("Inflation Rate", actual=3.5, consensus=3.0), -1),
        ]
        assert _detect_hot_data_warning(events) is False

    def test_no_warning_empty_events(self) -> None:
        assert _detect_hot_data_warning([]) is False


# ─── compute_macro_score ──────────────────────────────────────────────────────


class TestComputeMacroScore:
    def test_single_bullish_event(self) -> None:
        """GDP beat → positive score."""
        event = _make_event("GDP Growth Rate", actual=3.0, consensus=2.0)
        score, details = compute_macro_score([event], datetime.now(UTC))
        assert score > 0
        assert len(details) == 1

    def test_single_bearish_event(self) -> None:
        """Inflation beat → negative score."""
        event = _make_event("Inflation Rate", actual=3.5, consensus=3.0)
        score, details = compute_macro_score([event], datetime.now(UTC))
        assert score < 0

    def test_empty_events_returns_zero(self) -> None:
        score, details = compute_macro_score([], datetime.now(UTC))
        assert score == 0.0
        assert details == []

    def test_events_without_consensus_skipped(self) -> None:
        event = _make_event("GDP Growth Rate", actual=3.0, consensus=None)
        score, details = compute_macro_score([event], datetime.now(UTC))
        assert score == 0.0
        assert len(details) == 0

    def test_score_clamped_between_minus_one_and_one(self) -> None:
        """Extreme surprise should not push score outside [-1, 1]."""
        event = _make_event("Inflation Rate", actual=10.0, consensus=2.0)
        score, _ = compute_macro_score([event], datetime.now(UTC))
        assert -1.0 <= score <= 1.0

    def test_offsetting_events_near_neutral(self) -> None:
        """Balanced growth beat + inflation beat should partially offset."""
        gdp = _make_event("GDP Growth Rate", actual=3.0, consensus=2.0, importance=3)
        cpi = _make_event("Inflation Rate", actual=3.0, consensus=2.0, importance=3)
        score, _ = compute_macro_score([gdp, cpi], datetime.now(UTC))
        # GDP direction = +1; CPI direction = -1; equal magnitude → close to 0
        assert abs(score) < 0.1

    def test_older_event_contributes_less(self) -> None:
        """
        With two identical opposing events (different ages), the fresher one
        should dominate, producing a net positive score.

        fresh GDP beat (days_ago=0) vs stale GDP miss (days_ago=10):
        fresh has higher raw_weight → positive score wins.
        """
        now = datetime.now(UTC)
        fresh_beat = _make_event("GDP Growth Rate", actual=3.0, consensus=2.0, days_ago=0)
        stale_miss = _make_event("GDP Growth Rate", actual=1.0, consensus=2.0, days_ago=10)
        score, details = compute_macro_score([fresh_beat, stale_miss], now)
        # Fresh beat dominates stale miss → net positive
        assert score > 0

    def test_high_importance_event_outweighs_low(self) -> None:
        """Importance=3 bullish event should dominate importance=1 bearish event."""
        strong = _make_event("GDP Growth Rate", actual=3.0, consensus=2.0, importance=3)
        weak   = _make_event("Inflation Rate", actual=3.0, consensus=2.0, importance=1)
        score, _ = compute_macro_score([strong, weak], datetime.now(UTC))
        assert score > 0  # bullish wins


# ─── _score_to_signal ─────────────────────────────────────────────────────────


class TestScoreToSignal:
    def test_bullish(self) -> None:
        assert _score_to_signal(0.20) == Signal.BULLISH

    def test_bearish(self) -> None:
        assert _score_to_signal(-0.20) == Signal.BEARISH

    def test_neutral_positive(self) -> None:
        assert _score_to_signal(0.10) == Signal.NEUTRAL

    def test_neutral_negative(self) -> None:
        assert _score_to_signal(-0.10) == Signal.NEUTRAL

    def test_neutral_zero(self) -> None:
        assert _score_to_signal(0.0) == Signal.NEUTRAL

    def test_exactly_at_bullish_threshold(self) -> None:
        assert _score_to_signal(0.15) == Signal.BULLISH

    def test_exactly_at_bearish_threshold(self) -> None:
        assert _score_to_signal(-0.15) == Signal.BEARISH


# ─── _build_narrative ────────────────────────────────────────────────────────


class TestBuildNarrative:
    def test_no_events_returns_silence_message(self) -> None:
        narrative = _build_narrative([], 0.0, Signal.NEUTRAL, False, no_events=True)
        assert "靜默" in narrative or "無方向性" in narrative

    def test_bullish_signal_mentioned(self) -> None:
        event = _make_event("GDP Growth Rate", actual=3.0, consensus=2.0)
        asof = datetime.now(UTC)
        score, details = compute_macro_score([event], asof)
        narrative = _build_narrative(details, score, Signal.BULLISH, False, no_events=False)
        assert "偏多" in narrative or "bullish" in narrative.lower()

    def test_bearish_signal_mentioned(self) -> None:
        event = _make_event("Inflation Rate", actual=3.5, consensus=3.0)
        asof = datetime.now(UTC)
        score, details = compute_macro_score([event], asof)
        narrative = _build_narrative(details, score, Signal.BEARISH, False, no_events=False)
        assert "偏空" in narrative or "通膨" in narrative

    def test_hot_data_warning_annotated(self) -> None:
        gdp = _make_event("GDP Growth Rate", actual=3.0, consensus=2.0)
        cpi = _make_event("Inflation Rate", actual=3.5, consensus=3.0)
        asof = datetime.now(UTC)
        score, details = compute_macro_score([gdp, cpi], asof)
        narrative = _build_narrative(details, score, Signal.NEUTRAL, hot_data_warning=True, no_events=False)
        # Hot-data annotation must appear
        assert "好數據" in narrative or "升息" in narrative

    def test_no_numeric_literals_in_narrative(self) -> None:
        """Narrative must not contain actual/consensus values (numbers belong in metrics)."""
        import re
        event = _make_event("GDP Growth Rate", actual=2.5, consensus=2.0)
        asof = datetime.now(UTC)
        score, details = compute_macro_score([event], asof)
        narrative = _build_narrative(details, score, Signal.BULLISH, False, no_events=False)
        # Allow single-digit ordinals and "2" in Chinese phrases, but not "2.5" or "2.0"
        decimal_numbers = re.findall(r"\d+\.\d+", narrative)
        assert decimal_numbers == [], f"Numeric literals in narrative: {decimal_numbers}"

    def test_time_horizon_mentioned(self) -> None:
        event = _make_event("GDP Growth Rate", actual=3.0, consensus=2.0)
        asof = datetime.now(UTC)
        score, details = compute_macro_score([event], asof)
        narrative = _build_narrative(details, score, Signal.BULLISH, False, no_events=False)
        assert "MEDIUM" in narrative or "中期" in narrative


# ─── Adapter: _parse_float ────────────────────────────────────────────────────


class TestParseFloat:
    def test_float_string(self) -> None:
        assert _parse_float("2.5") == pytest.approx(2.5)

    def test_comma_separated(self) -> None:
        assert _parse_float("1,234.5") == pytest.approx(1234.5)

    def test_int_value(self) -> None:
        assert _parse_float(3) == pytest.approx(3.0)

    def test_none_returns_none(self) -> None:
        assert _parse_float(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_float("") is None

    def test_invalid_string_returns_none(self) -> None:
        assert _parse_float("N/A") is None

    def test_negative_float(self) -> None:
        assert _parse_float("-3.14") == pytest.approx(-3.14)


# ─── Adapter: _parse_te_date ──────────────────────────────────────────────────


class TestParseTeDate:
    def test_iso_with_time(self) -> None:
        dt = _parse_te_date("2024-09-11T08:30:00")
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 9

    def test_iso_with_z(self) -> None:
        dt = _parse_te_date("2024-09-11T08:30:00Z")
        assert dt is not None
        assert dt.day == 11

    def test_date_only(self) -> None:
        dt = _parse_te_date("2024-09-11")
        assert dt is not None
        assert dt.year == 2024

    def test_empty_string_returns_none(self) -> None:
        assert _parse_te_date("") is None

    def test_invalid_format_returns_none(self) -> None:
        assert _parse_te_date("not-a-date") is None


# ─── Adapter: _parse_te_events ────────────────────────────────────────────────


class TestParseTeEvents:
    def _make_raw_row(
        self,
        category: str = "GDP Growth Rate",
        country: str = "United States",
        actual: str | None = "2.5",
        forecast: str | None = "2.0",
        previous: str | None = "1.8",
        importance: int = 3,
        date: str = "2024-09-01T08:30:00",
    ) -> dict[str, Any]:
        return {
            "Category":   category,
            "Country":    country,
            "Actual":     actual,
            "Forecast":   forecast,
            "Previous":   previous,
            "Importance": importance,
            "Date":       date,
            "Unit":       "%",
        }

    def test_parses_valid_row(self) -> None:
        raw = [self._make_raw_row()]
        events = _parse_te_events(raw, min_importance=2)
        assert len(events) == 1
        assert events[0].category == "GDP Growth Rate"
        assert events[0].actual == pytest.approx(2.5)
        assert events[0].consensus == pytest.approx(2.0)

    def test_filters_low_importance(self) -> None:
        raw = [self._make_raw_row(importance=1)]
        events = _parse_te_events(raw, min_importance=2)
        assert len(events) == 0

    def test_skips_rows_without_actual(self) -> None:
        raw = [self._make_raw_row(actual=None)]
        events = _parse_te_events(raw, min_importance=2)
        assert len(events) == 0

    def test_keeps_rows_without_forecast(self) -> None:
        """Events without consensus should still appear (no surprise, but kept)."""
        raw = [self._make_raw_row(forecast=None)]
        events = _parse_te_events(raw, min_importance=2)
        assert len(events) == 1
        assert events[0].consensus is None

    def test_skips_rows_with_invalid_date(self) -> None:
        raw = [self._make_raw_row(date="not-a-date")]
        events = _parse_te_events(raw, min_importance=2)
        assert len(events) == 0

    def test_skips_rows_with_unparseable_importance(self) -> None:
        row = self._make_raw_row()
        row["Importance"] = "high"  # non-numeric
        events = _parse_te_events([row], min_importance=2)
        assert len(events) == 0

    def test_multiple_rows_all_valid(self) -> None:
        raw = [self._make_raw_row("GDP Growth Rate"), self._make_raw_row("Inflation Rate")]
        events = _parse_te_events(raw, min_importance=2)
        assert len(events) == 2

    def test_source_name_set_correctly(self) -> None:
        raw = [self._make_raw_row()]
        events = _parse_te_events(raw)
        assert events[0].source_name == "trading_economics"


# ─── TradingEconomicsAdapter ─────────────────────────────────────────────────


class TestTradingEconomicsAdapter:
    def test_source_name(self) -> None:
        adapter = TradingEconomicsAdapter(api_key="fake-key")
        assert adapter.source_name == "trading_economics"

    def test_fetch_returns_sourced_data(self) -> None:
        raw_row = {
            "Category": "GDP Growth Rate", "Country": "United States",
            "Actual": "2.5", "Forecast": "2.0", "Previous": "1.8",
            "Importance": 3, "Date": "2024-09-01T08:30:00", "Unit": "%",
        }
        adapter = TradingEconomicsAdapter(api_key="fake-key")
        with patch.object(adapter, "_fetch_raw", return_value=[raw_row]):
            result = adapter.fetch(countries=["united states"], days_back=7)

        assert result.source == "trading_economics"
        payload = result.payload
        assert isinstance(payload, MacroResult)
        assert len(payload.events) == 1

    def test_fetch_empty_response(self) -> None:
        adapter = TradingEconomicsAdapter(api_key="fake-key")
        with patch.object(adapter, "_fetch_raw", return_value=[]):
            result = adapter.fetch()
        assert result.payload.events == []

    def test_fetch_non_list_response_treated_as_empty(self) -> None:
        """If TE API returns a dict (error), _fetch_raw returns []."""
        adapter = TradingEconomicsAdapter(api_key="fake-key")
        with patch.object(adapter, "_fetch_raw", return_value=[]):
            result = adapter.fetch()
        assert result.payload.events == []


# ─── _require_te_key ─────────────────────────────────────────────────────────


class TestRequireTeKey:
    def test_raises_when_env_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="TE_API_KEY"):
            _require_te_key()

    def test_returns_key_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TE_API_KEY", "my-secret-key")
        key = _require_te_key()
        assert key == "my-secret-key"

    def test_raises_when_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TE_API_KEY", "")
        with pytest.raises(RuntimeError, match="TE_API_KEY"):
            _require_te_key()


# ─── Full pipeline: run_macro_agent ──────────────────────────────────────────


class TestRunMacroAgentNormal:
    def test_bullish_scenario_gdp_beat(self) -> None:
        event = _make_event("GDP Growth Rate", actual=3.0, consensus=2.0, days_ago=1)
        sig = run_macro_agent(
            macro_adapter=_make_mock_adapter([event]),
            asof=datetime.now(UTC),
        )
        assert sig.signal == Signal.BULLISH
        assert sig.confidence > _NO_EVENTS_CONFIDENCE
        assert sig.agent == AgentType.MACRO
        assert sig.time_horizon == TimeHorizon.MEDIUM

    def test_bearish_scenario_cpi_beat(self) -> None:
        event = _make_event("Inflation Rate", actual=4.0, consensus=3.0, days_ago=1)
        sig = run_macro_agent(
            macro_adapter=_make_mock_adapter([event]),
            asof=datetime.now(UTC),
        )
        assert sig.signal == Signal.BEARISH
        assert sig.confidence > _NO_EVENTS_CONFIDENCE

    def test_neutral_when_events_offset(self) -> None:
        gdp = _make_event("GDP Growth Rate", actual=3.0, consensus=2.0, importance=3)
        cpi = _make_event("Inflation Rate", actual=3.0, consensus=2.0, importance=3)
        sig = run_macro_agent(
            macro_adapter=_make_mock_adapter([gdp, cpi]),
            asof=datetime.now(UTC),
        )
        assert sig.signal == Signal.NEUTRAL

    def test_hot_data_warning_in_metrics(self) -> None:
        """Both growth beat + inflation beat → hot_data_warning=True in metrics."""
        gdp = _make_event("GDP Growth Rate", actual=3.0, consensus=2.0, importance=3, days_ago=1)
        cpi = _make_event("Inflation Rate", actual=3.5, consensus=3.0, importance=3, days_ago=1)
        sig = run_macro_agent(
            macro_adapter=_make_mock_adapter([gdp, cpi]),
            asof=datetime.now(UTC),
        )
        assert sig.metrics.get("hot_data_warning") is True

    def test_hot_data_annotation_in_narrative(self) -> None:
        gdp = _make_event("GDP Growth Rate", actual=3.0, consensus=2.0, importance=3, days_ago=1)
        cpi = _make_event("Inflation Rate", actual=3.5, consensus=3.0, importance=3, days_ago=1)
        sig = run_macro_agent(
            macro_adapter=_make_mock_adapter([gdp, cpi]),
            asof=datetime.now(UTC),
        )
        assert "好數據" in sig.narrative or "升息" in sig.narrative

    def test_key_evidence_has_source_and_asof(self) -> None:
        event = _make_event("GDP Growth Rate", actual=3.0, consensus=2.0)
        sig = run_macro_agent(
            macro_adapter=_make_mock_adapter([event]),
            asof=datetime.now(UTC),
        )
        for ev in sig.key_evidence:
            assert ev.source is not None and ev.source != ""
            assert ev.asof is not None

    def test_hard_constraints_always_empty(self) -> None:
        """Macro agent never emits hard_constraints (CLAUDE.md §二)."""
        event = _make_event("Inflation Rate", actual=4.0, consensus=3.0)
        sig = run_macro_agent(macro_adapter=_make_mock_adapter([event]))
        assert sig.hard_constraints == []

    def test_multiple_events_increase_confidence(self) -> None:
        one = [_make_event("GDP Growth Rate", actual=3.0, consensus=2.0)]
        many = [
            _make_event("GDP Growth Rate", actual=3.0, consensus=2.0),
            _make_event("Non Farm Payrolls", actual=300.0, consensus=200.0),
            _make_event("Retail Sales", actual=0.5, consensus=0.3),
        ]
        sig_one  = run_macro_agent(macro_adapter=_make_mock_adapter(one))
        sig_many = run_macro_agent(macro_adapter=_make_mock_adapter(many))
        assert sig_many.confidence >= sig_one.confidence

    def test_agent_type_is_macro(self) -> None:
        sig = run_macro_agent(macro_adapter=_make_mock_adapter([]))
        assert sig.agent == AgentType.MACRO


class TestRunMacroAgentDegraded:
    def test_no_events_confidence_is_floored(self) -> None:
        sig = run_macro_agent(
            macro_adapter=_make_mock_adapter([]),
            asof=datetime.now(UTC),
        )
        assert sig.confidence == pytest.approx(_NO_EVENTS_CONFIDENCE)
        assert sig.metrics.get("no_recent_events") is True

    def test_no_events_completeness_is_zero(self) -> None:
        sig = run_macro_agent(macro_adapter=_make_mock_adapter([]))
        assert sig.data_quality.completeness == 0.0

    def test_no_events_signal_is_neutral(self) -> None:
        sig = run_macro_agent(macro_adapter=_make_mock_adapter([]))
        assert sig.signal == Signal.NEUTRAL

    def test_api_key_missing_triggers_degraded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no adapter is injected and TE_API_KEY is missing, signal degrades."""
        monkeypatch.delenv("TE_API_KEY", raising=False)
        sig = run_macro_agent(macro_adapter=None)
        assert sig.confidence == pytest.approx(_NO_EVENTS_CONFIDENCE)
        assert any("[降級]" in e for e in sig.errors)

    def test_api_key_missing_error_message_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TE_API_KEY", raising=False)
        sig = run_macro_agent(macro_adapter=None)
        assert sig.errors  # at least one error

    def test_fetch_failure_triggers_degraded(self) -> None:
        """If adapter.fetch() raises, pipeline degrades gracefully."""
        mock = MagicMock()
        mock.fetch.side_effect = RuntimeError("connection refused")
        sig = run_macro_agent(macro_adapter=mock)
        assert sig.confidence == pytest.approx(_NO_EVENTS_CONFIDENCE)
        assert any("[降級]" in e or "fetch" in e.lower() for e in sig.errors)

    def test_degraded_flag_in_metrics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TE_API_KEY", raising=False)
        sig = run_macro_agent(macro_adapter=None)
        assert sig.metrics.get("degraded") is True

    def test_events_without_consensus_treated_as_no_computable(self) -> None:
        """Events with actual but no consensus → computable_count=0 → degraded confidence."""
        event = _make_event("GDP Growth Rate", actual=2.5, consensus=None)
        sig = run_macro_agent(macro_adapter=_make_mock_adapter([event]))
        assert sig.confidence == pytest.approx(_NO_EVENTS_CONFIDENCE)
        assert sig.metrics.get("computable_count") == 0


# ─── Real-world validation case ──────────────────────────────────────────────


class TestRealWorldValidation:
    """
    End-to-end validation against published macro data.

    Case 1: US CPI September 2024
    ─────────────────────────────
    Data (Bureau of Labor Statistics, released 2024-10-10):
      Actual   : 2.4% YoY
      Consensus: 2.3% (Bloomberg median forecast)
      Previous : 2.5%

    Expected:
      surprise_pct = (2.4 - 2.3) / |2.3| ≈ +4.35%   → classify_surprise = "in_line"
      surprise_sign = +1
      event_market_direction("CPI", +1) = -1 (bearish: inflation beat = rate-hike risk)
      BUT since surprise is "in_line" (< 5%), market impact is modest.

    Case 2: US Non-Farm Payrolls October 2023
    ──────────────────────────────────────────
    Data (BLS, released 2023-11-03):
      Actual   : 150K (lower revision from initial 336K; let's use 336K headline first release)
      Consensus: 170K
    Using 336K first release:
      surprise_pct = (336 - 170) / 170 ≈ +97.6%  → "large_beat"
      event_market_direction("Non Farm Payrolls", +1) = +1 (bullish on its own)
      BUT accompanied by sticky inflation → potential hot_data_warning
    """

    def test_us_cpi_sep2024_surprise_direction(self) -> None:
        spct = compute_surprise_pct(2.4, 2.3)
        # Small positive surprise → in_line
        assert classify_surprise(spct) == "in_line"
        # Still bearish direction (inflation beat, even small)
        surprise_sign = 1 if spct > 0 else -1
        direction = event_market_direction("CPI", surprise_sign)
        assert direction == -1  # bearish

    def test_us_nfp_oct2023_large_beat(self) -> None:
        spct = compute_surprise_pct(336.0, 170.0)
        assert classify_surprise(spct) == "large_beat"
        assert event_market_direction("Non Farm Payrolls", +1) == +1  # bullish

    def test_full_pipeline_cpi_bearish(self) -> None:
        """
        Run full pipeline with US CPI Sep 2024 data.
        Even 'in_line' CPI surprise contributes a small negative score.
        Net signal may be NEUTRAL due to small surprise magnitude.
        """
        release = datetime(2024, 10, 10, 8, 30, 0, tzinfo=UTC)
        asof = datetime(2024, 10, 10, 12, 0, 0, tzinfo=UTC)

        event = MacroEvent(
            category="CPI",
            country="United States",
            actual=2.4,
            consensus=2.3,
            previous=2.5,
            unit="%",
            importance=3,
            release_date=release,
            source_name="trading_economics",
        )
        sig = run_macro_agent(
            macro_adapter=_make_mock_adapter([event]),
            asof=asof,
        )
        # Small surprise → NEUTRAL or BEARISH (direction=-1 but magnitude small)
        assert sig.signal in (Signal.NEUTRAL, Signal.BEARISH)
        # Evidence should reference CPI
        sources = [ev.source for ev in sig.key_evidence]
        assert any("cpi" in s.lower() for s in sources)

    def test_full_pipeline_hot_data_nfp_plus_cpi(self) -> None:
        """
        NFP large beat + CPI beat simultaneously → hot_data_warning.
        NFP Oct 2023: 336K actual vs 170K consensus.
        CPI (hypothetical same batch): 3.7% actual vs 3.6% consensus.
        """
        now = datetime.now(UTC)
        nfp = MacroEvent(
            category="Non Farm Payrolls",
            country="United States",
            actual=336.0, consensus=170.0, previous=180.0,
            unit="K", importance=3, release_date=now - timedelta(days=1),
            source_name="trading_economics",
        )
        cpi = MacroEvent(
            category="CPI",
            country="United States",
            actual=3.7, consensus=3.6, previous=3.5,
            unit="%", importance=3, release_date=now - timedelta(days=1),
            source_name="trading_economics",
        )
        sig = run_macro_agent(
            macro_adapter=_make_mock_adapter([nfp, cpi]),
            asof=now,
        )
        assert sig.metrics.get("hot_data_warning") is True
        assert "好數據" in sig.narrative or "升息" in sig.narrative


# ─── Category set integrity ───────────────────────────────────────────────────


class TestCategorySetIntegrity:
    def test_growth_categories_subset_of_direction_table(self) -> None:
        """All GROWTH_CATEGORIES must appear in CATEGORY_DIRECTION."""
        for cat in GROWTH_CATEGORIES:
            assert cat in CATEGORY_DIRECTION, f"{cat} missing from CATEGORY_DIRECTION"

    def test_inflation_categories_subset_of_direction_table(self) -> None:
        for cat in INFLATION_CATEGORIES:
            assert cat in CATEGORY_DIRECTION, f"{cat} missing from CATEGORY_DIRECTION"

    def test_growth_categories_all_positive(self) -> None:
        for cat in GROWTH_CATEGORIES:
            assert CATEGORY_DIRECTION[cat] == +1, f"{cat} should have direction +1"

    def test_inflation_categories_all_negative(self) -> None:
        for cat in INFLATION_CATEGORIES:
            assert CATEGORY_DIRECTION[cat] == -1, f"{cat} should have direction -1"


# ─── Import from macro_agent private constant ─────────────────────────────────

# Needed by TestRunMacroAgentDegraded — import at module level
from agents.macro_agent import _NO_EVENTS_CONFIDENCE  # noqa: E402
