"""
Tests for adapters/options_adapter.py

Strategy
--------
All tests bypass _fetch_raw() by calling _parse_records() directly with
synthetic FinMind-format row dicts.  This keeps tests hermetic (no network)
and fast, while fully exercising the IV backing-out logic and degradation paths.

IV degradation cases covered
-----------------------------
1. trade_price = 0          → iv=NaN, iv_source="iv_unavailable"
2. price outside no-arb bounds → IV solver fails, same degradation
3. expired contract          → row skipped (None returned)
4. unknown call_put value    → row skipped
5. malformed row             → row skipped

Happy path
----------
6. Valid ATM call with reasonable trade price → iv solved, iv_source="finmind_backed_out"
7. Valid ATM put                              → same
8. Greeks are finite and in reasonable range for ATM option
"""
from __future__ import annotations

import math
from datetime import date

import pytest

from adapters.options_adapter import (
    IV_SOURCE_BACKED_OUT,
    IV_SOURCE_UNAVAILABLE,
    FinMindOptionsAdapter,
    OptionRecord,
    _third_wednesday,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def adapter() -> FinMindOptionsAdapter:
    """Adapter with no real token — _fetch_raw is never called in these tests."""
    return FinMindOptionsAdapter(api_token="")


# Pricing date for all tests
AS_OF = date(2025, 1, 15)

# Underlying spot price
SPOT = 22000.0

# Reference rates (match pricing_router defaults)
R = 0.02
Q = 0.00


def _row(
    strike: float,
    contract_date: str,
    call_put: str,  # "買權" | "賣權"
    close: float,
    stock_id: str = "TXO",
) -> dict:
    """Helper: build a minimal FinMind TaiwanOptionDaily row dict."""
    return {
        "stock_id":      stock_id,
        "strike_price":  strike,
        "contract_date": contract_date,
        "call_put":      call_put,
        "close":         close,
    }


# ─── _third_wednesday helper ──────────────────────────────────────────────────

class TestThirdWednesday:
    def test_march_2025(self):
        assert _third_wednesday(2025, 3) == date(2025, 3, 19)

    def test_january_2025(self):
        # Jan 2025: first Wednesday is the 1st → third is the 15th
        assert _third_wednesday(2025, 1) == date(2025, 1, 15)

    def test_december_2024(self):
        assert _third_wednesday(2024, 12) == date(2024, 12, 18)

    def test_february_2025(self):
        assert _third_wednesday(2025, 2) == date(2025, 2, 19)


# ─── Happy-path: IV backing-out succeeds ──────────────────────────────────────

class TestIVBackingOutSuccess:
    """ATM call/put with realistic TXO trade prices → IV should converge."""

    @pytest.fixture
    def atm_call_record(self, adapter) -> OptionRecord:
        # ATM call on TXO expiring 2025-03 (March 19)
        # With S=22000, K=22000, T≈63 days, r=2%, σ≈20%:
        # rough BS call ≈ 900 points → use 900 as trade price
        row = _row(strike=22000.0, contract_date="2025-03",
                   call_put="買權", close=900.0)
        records = adapter._parse_records([row], SPOT, AS_OF, R, Q)
        assert len(records) == 1
        return records[0]

    @pytest.fixture
    def atm_put_record(self, adapter) -> OptionRecord:
        row = _row(strike=22000.0, contract_date="2025-03",
                   call_put="賣權", close=750.0)
        records = adapter._parse_records([row], SPOT, AS_OF, R, Q)
        assert len(records) == 1
        return records[0]

    def test_call_iv_available(self, atm_call_record):
        assert atm_call_record.iv_available

    def test_call_iv_source_is_backed_out(self, atm_call_record):
        assert atm_call_record.iv_source == IV_SOURCE_BACKED_OUT

    def test_call_iv_is_positive(self, atm_call_record):
        assert atm_call_record.iv > 0.0

    def test_call_iv_in_realistic_range(self, atm_call_record):
        # TXO IV typically 15–50 %; smoke-test the solve landed in this band
        assert 0.05 < atm_call_record.iv < 1.0, \
            f"IV={atm_call_record.iv:.4f} outside expected range"

    def test_call_no_errors(self, atm_call_record):
        assert atm_call_record.errors == []

    def test_call_delta_in_range(self, atm_call_record):
        # ATM call delta should be close to 0.5 (0.3–0.7 range)
        assert 0.2 < atm_call_record.greeks.delta < 0.8

    def test_call_gamma_positive(self, atm_call_record):
        assert atm_call_record.greeks.gamma > 0.0

    def test_call_theta_negative(self, atm_call_record):
        # Long option: theta is negative (time decay)
        # Note: record is from adapter perspective (per unit, no quantity applied)
        assert atm_call_record.greeks.theta < 0.0

    def test_put_iv_available(self, atm_put_record):
        assert atm_put_record.iv_available

    def test_put_iv_source(self, atm_put_record):
        assert atm_put_record.iv_source == IV_SOURCE_BACKED_OUT

    def test_put_delta_negative(self, atm_put_record):
        assert atm_put_record.greeks.delta < 0.0

    def test_record_fields_populated(self, atm_call_record):
        assert atm_call_record.symbol == "TXO"
        assert atm_call_record.strike == pytest.approx(22000.0)
        assert atm_call_record.expiry == date(2025, 3, 19)
        assert atm_call_record.option_type == "call"
        assert atm_call_record.style == "european"
        assert atm_call_record.trade_price == pytest.approx(900.0)

    def test_greeks_model_is_black_scholes(self, atm_call_record):
        assert atm_call_record.greeks.model == "black_scholes"


# ─── IV round-trip sanity ─────────────────────────────────────────────────────

class TestIVRoundTrip:
    """Back out IV from a known BS price; the solved IV should match the original."""

    def test_roundtrip_call(self, adapter):
        from agents.risk.black_scholes import bs_price
        S, K, T, r, q, sigma = 22000.0, 22000.0, 63 / 365.0, 0.02, 0.0, 0.20
        market_price = bs_price(S, K, T, r, q, sigma, "call")

        row = _row(strike=K, contract_date="2025-03", call_put="買權",
                   close=market_price)
        records = adapter._parse_records([row], S, AS_OF, r, q)
        assert len(records) == 1
        rec = records[0]

        assert rec.iv_available
        assert abs(rec.iv - sigma) < 1e-3, \
            f"IV round-trip error: expected {sigma:.4f}, got {rec.iv:.4f}"


# ─── Degradation: zero trade price ────────────────────────────────────────────

class TestZeroTradePriceDegradation:
    @pytest.fixture
    def zero_price_record(self, adapter) -> OptionRecord:
        row = _row(strike=22000.0, contract_date="2025-03",
                   call_put="買權", close=0.0)
        records = adapter._parse_records([row], SPOT, AS_OF, R, Q)
        assert len(records) == 1
        return records[0]

    def test_iv_is_nan(self, zero_price_record):
        assert math.isnan(zero_price_record.iv)

    def test_iv_source_is_unavailable(self, zero_price_record):
        assert zero_price_record.iv_source == IV_SOURCE_UNAVAILABLE

    def test_iv_available_is_false(self, zero_price_record):
        assert not zero_price_record.iv_available

    def test_errors_list_not_empty(self, zero_price_record):
        assert len(zero_price_record.errors) > 0

    def test_error_mentions_trade_price(self, zero_price_record):
        assert any("trade_price" in e for e in zero_price_record.errors)

    def test_greeks_price_is_zero(self, zero_price_record):
        # We store the (zero) market price in greeks.price for traceability
        assert zero_price_record.greeks.price == pytest.approx(0.0)


# ─── Degradation: price outside no-arbitrage bounds ──────────────────────────

class TestNoArbBoundsDegradation:
    """A stale/erroneous price that violates no-arb bounds should degrade cleanly."""

    def test_call_price_above_spot_is_degraded(self, adapter):
        # A call price > S is outside no-arb upper bound (call ≤ S * e^{-qT})
        S = 22000.0
        row = _row(strike=22000.0, contract_date="2025-03",
                   call_put="買權", close=S * 2)   # clearly impossible
        records = adapter._parse_records([row], S, AS_OF, R, Q)
        assert len(records) == 1
        rec = records[0]
        assert not rec.iv_available
        assert rec.iv_source == IV_SOURCE_UNAVAILABLE
        assert len(rec.errors) > 0

    def test_put_price_above_strike_discounted(self, adapter):
        # A put price > K * e^{-rT} violates put upper bound
        K = 22000.0
        S = 22000.0
        T = 63 / 365.0
        import math as _math
        hi_bound = K * _math.exp(-R * T)
        row = _row(strike=K, contract_date="2025-03",
                   call_put="賣權", close=hi_bound * 1.5)
        records = adapter._parse_records([row], S, AS_OF, R, Q)
        assert len(records) == 1
        rec = records[0]
        assert not rec.iv_available
        assert rec.iv_source == IV_SOURCE_UNAVAILABLE


# ─── Row skipping ─────────────────────────────────────────────────────────────

class TestRowSkipping:
    def test_expired_row_is_skipped(self, adapter):
        # contract_date earlier than AS_OF → days_to_expiry ≤ 0 → skip
        row = _row(strike=22000.0, contract_date="2024-12",
                   call_put="買權", close=500.0)
        records = adapter._parse_records([row], SPOT, AS_OF, R, Q)
        assert records == []

    def test_unknown_call_put_is_skipped(self, adapter):
        row = _row(strike=22000.0, contract_date="2025-03",
                   call_put="不明", close=500.0)
        records = adapter._parse_records([row], SPOT, AS_OF, R, Q)
        assert records == []

    def test_malformed_row_missing_strike_is_skipped(self, adapter):
        row = {"stock_id": "TXO", "contract_date": "2025-03",
               "call_put": "買權", "close": 500.0}
        records = adapter._parse_records([row], SPOT, AS_OF, R, Q)
        assert records == []

    def test_empty_row_list_returns_empty(self, adapter):
        records = adapter._parse_records([], SPOT, AS_OF, R, Q)
        assert records == []

    def test_mixed_valid_and_invalid_rows(self, adapter):
        rows = [
            _row(22000.0, "2025-03", "買權", 900.0),   # valid
            _row(22000.0, "2024-12", "買權", 500.0),   # expired → skip
            _row(22000.0, "2025-03", "不明", 500.0),   # bad call_put → skip
            _row(22000.0, "2025-03", "賣權", 750.0),   # valid
        ]
        records = adapter._parse_records(rows, SPOT, AS_OF, R, Q)
        assert len(records) == 2


# ─── Adapter metadata ─────────────────────────────────────────────────────────

class TestAdapterMetadata:
    def test_source_name(self, adapter):
        assert adapter.source_name == "finmind_taiwan_option_daily"

    def test_option_record_dataclass_fields(self, adapter):
        """Smoke test: OptionRecord has expected attributes."""
        row = _row(22000.0, "2025-03", "買權", 900.0)
        rec = adapter._parse_records([row], SPOT, AS_OF, R, Q)[0]
        # Verify all expected attributes exist
        _ = rec.symbol
        _ = rec.strike
        _ = rec.expiry
        _ = rec.option_type
        _ = rec.style
        _ = rec.trade_price
        _ = rec.iv
        _ = rec.iv_source
        _ = rec.greeks
        _ = rec.errors
        _ = rec.iv_available
