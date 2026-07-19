"""
Tests for agents/risk/position_loader.py

Coverage
--------
- Happy-path: parse the default positions.yaml (6 rows, all valid)
- Field-level spot-checks for each instrument type (stock / futures / option)
- Validation error collection (missing fields, wrong values)
- Edge cases: negative quantity, zero multiplier guard, expiry date parsing
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agents.risk.position_loader import (
    Position,
    load_positions,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def all_positions() -> list[Position]:
    return load_positions()


def _load_yaml(text: str, tmp_path: Path) -> list[Position]:
    """Write a YAML string to a temp file and load it."""
    p = tmp_path / "positions.yaml"
    p.write_text(text, encoding="utf-8")
    return load_positions(p)


# ─── Happy-path: default positions.yaml ───────────────────────────────────────

class TestDefaultPositionsFile:
    def test_loads_without_error(self, all_positions):
        assert len(all_positions) > 0

    def test_has_six_positions(self, all_positions):
        assert len(all_positions) == 6

    def test_all_positions_valid(self, all_positions):
        invalid = [p for p in all_positions if not p.is_valid]
        assert invalid == [], f"Invalid positions: {[p.errors for p in invalid]}"

    def test_counts_by_type(self, all_positions):
        types = [p.instrument_type for p in all_positions]
        assert types.count("stock")   == 2
        assert types.count("futures") == 1
        assert types.count("option")  == 3


# ─── Stock positions ──────────────────────────────────────────────────────────

class TestStockPositions:
    @pytest.fixture
    def tsmc(self, all_positions) -> Position:
        return next(p for p in all_positions if p.symbol == "2330.TW")

    @pytest.fixture
    def aapl_stock(self, all_positions) -> Position:
        return next(
            p for p in all_positions
            if p.symbol == "AAPL" and p.instrument_type == "stock"
        )

    def test_tsmc_quantity(self, tsmc):
        assert tsmc.quantity == 1000.0

    def test_tsmc_currency(self, tsmc):
        assert tsmc.currency == "TWD"

    def test_tsmc_multiplier(self, tsmc):
        assert tsmc.multiplier == 1.0

    def test_tsmc_entry_price(self, tsmc):
        assert tsmc.entry_price == pytest.approx(850.0)

    def test_stock_has_no_option_fields(self, tsmc):
        assert tsmc.strike is None
        assert tsmc.expiry is None
        assert tsmc.option_type is None
        assert tsmc.style is None

    def test_aapl_stock_currency_usd(self, aapl_stock):
        assert aapl_stock.currency == "USD"

    def test_is_option_false_for_stock(self, tsmc):
        assert tsmc.is_option is False


# ─── Futures positions ────────────────────────────────────────────────────────

class TestFuturesPositions:
    @pytest.fixture
    def txff(self, all_positions) -> Position:
        return next(p for p in all_positions if p.symbol == "TXFF")

    def test_futures_quantity_negative(self, txff):
        # Short 2 contracts → negative quantity
        assert txff.quantity == -2.0

    def test_futures_multiplier(self, txff):
        # TXFF: 每點 200 元
        assert txff.multiplier == 200.0

    def test_futures_currency(self, txff):
        assert txff.currency == "TWD"

    def test_futures_no_option_fields(self, txff):
        assert txff.strike is None
        assert txff.expiry is None

    def test_futures_is_option_false(self, txff):
        assert txff.is_option is False


# ─── Option positions ─────────────────────────────────────────────────────────

class TestOptionPositions:
    @pytest.fixture
    def txo_call(self, all_positions) -> Position:
        return next(
            p for p in all_positions
            if p.symbol == "TXO" and p.option_type == "call"
        )

    @pytest.fixture
    def txo_put(self, all_positions) -> Position:
        return next(
            p for p in all_positions
            if p.symbol == "TXO" and p.option_type == "put"
        )

    @pytest.fixture
    def aapl_option(self, all_positions) -> Position:
        return next(
            p for p in all_positions
            if p.symbol == "AAPL" and p.instrument_type == "option"
        )

    def test_txo_call_strike(self, txo_call):
        assert txo_call.strike == pytest.approx(22500.0)

    def test_txo_call_expiry(self, txo_call):
        assert txo_call.expiry == "2026-09-16"

    def test_txo_call_expiry_date_property(self, txo_call):
        from datetime import date
        assert txo_call.expiry_date == date(2026, 9, 16)

    def test_txo_call_style_european(self, txo_call):
        assert txo_call.style == "european"

    def test_txo_call_quantity_negative(self, txo_call):
        # Short 5 contracts
        assert txo_call.quantity == -5.0

    def test_txo_call_multiplier(self, txo_call):
        # TXO: 每點 50 元
        assert txo_call.multiplier == 50.0

    def test_txo_put_option_type(self, txo_put):
        assert txo_put.option_type == "put"

    def test_txo_put_quantity_positive(self, txo_put):
        assert txo_put.quantity == 5.0

    def test_aapl_option_style_american(self, aapl_option):
        assert aapl_option.style == "american"

    def test_aapl_option_currency_usd(self, aapl_option):
        assert aapl_option.currency == "USD"

    def test_aapl_option_multiplier(self, aapl_option):
        assert aapl_option.multiplier == 100.0

    def test_option_is_option_true(self, txo_call):
        assert txo_call.is_option is True

    def test_all_options_have_required_fields(self, all_positions):
        options = [p for p in all_positions if p.instrument_type == "option"]
        for opt in options:
            assert opt.strike is not None,      f"{opt.symbol}: strike missing"
            assert opt.expiry is not None,      f"{opt.symbol}: expiry missing"
            assert opt.option_type is not None, f"{opt.symbol}: option_type missing"
            assert opt.style is not None,       f"{opt.symbol}: style missing"


# ─── Validation error collection ──────────────────────────────────────────────

class TestValidationErrors:
    def test_missing_symbol_produces_error(self, tmp_path):
        text = """
positions:
  - instrument_type: stock
    quantity: 100
    currency: TWD
    multiplier: 1.0
"""
        positions = _load_yaml(text, tmp_path)
        assert len(positions) == 1
        assert not positions[0].is_valid
        assert any("symbol" in e for e in positions[0].errors)

    def test_missing_quantity_produces_error(self, tmp_path):
        text = """
positions:
  - symbol: "2330.TW"
    instrument_type: stock
    currency: TWD
"""
        positions = _load_yaml(text, tmp_path)
        assert not positions[0].is_valid
        assert any("quantity" in e for e in positions[0].errors)

    def test_invalid_instrument_type_produces_error(self, tmp_path):
        text = """
positions:
  - symbol: "2330.TW"
    instrument_type: warrant
    quantity: 100
    currency: TWD
"""
        positions = _load_yaml(text, tmp_path)
        assert not positions[0].is_valid
        assert any("instrument_type" in e for e in positions[0].errors)

    def test_option_missing_strike_produces_error(self, tmp_path):
        text = """
positions:
  - symbol: "TXO"
    instrument_type: option
    quantity: -5
    expiry: "2025-03-19"
    option_type: call
    style: european
    currency: TWD
    multiplier: 50.0
"""
        positions = _load_yaml(text, tmp_path)
        assert not positions[0].is_valid
        assert any("strike" in e for e in positions[0].errors)

    def test_option_invalid_option_type(self, tmp_path):
        text = """
positions:
  - symbol: "TXO"
    instrument_type: option
    quantity: -5
    strike: 22500.0
    expiry: "2025-03-19"
    option_type: straddle
    style: european
    currency: TWD
    multiplier: 50.0
"""
        positions = _load_yaml(text, tmp_path)
        assert not positions[0].is_valid
        assert any("option_type" in e for e in positions[0].errors)

    def test_option_invalid_style(self, tmp_path):
        text = """
positions:
  - symbol: "TXO"
    instrument_type: option
    quantity: -5
    strike: 22500.0
    expiry: "2025-03-19"
    option_type: call
    style: bermudan
    currency: TWD
    multiplier: 50.0
"""
        positions = _load_yaml(text, tmp_path)
        assert not positions[0].is_valid
        assert any("style" in e for e in positions[0].errors)

    def test_option_bad_expiry_date_format(self, tmp_path):
        text = """
positions:
  - symbol: "TXO"
    instrument_type: option
    quantity: 5
    strike: 21000.0
    expiry: "19-03-2025"
    option_type: put
    style: european
    currency: TWD
    multiplier: 50.0
"""
        positions = _load_yaml(text, tmp_path)
        assert not positions[0].is_valid
        assert any("expiry" in e for e in positions[0].errors)

    def test_stock_with_option_fields_produces_error(self, tmp_path):
        text = """
positions:
  - symbol: "2330.TW"
    instrument_type: stock
    quantity: 1000
    currency: TWD
    multiplier: 1.0
    strike: 850.0
"""
        positions = _load_yaml(text, tmp_path)
        assert not positions[0].is_valid
        assert any("strike" in e for e in positions[0].errors)

    def test_invalid_currency_produces_error(self, tmp_path):
        text = """
positions:
  - symbol: "2330.TW"
    instrument_type: stock
    quantity: 1000
    currency: GBP
    multiplier: 1.0
"""
        positions = _load_yaml(text, tmp_path)
        assert not positions[0].is_valid
        assert any("currency" in e for e in positions[0].errors)

    def test_multiple_errors_collected_in_one_row(self, tmp_path):
        """A single bad option row can accumulate multiple errors."""
        text = """
positions:
  - symbol: "TXO"
    instrument_type: option
    quantity: bad_value
    currency: TWD
    multiplier: 50.0
"""
        positions = _load_yaml(text, tmp_path)
        assert not positions[0].is_valid
        # Both quantity and missing option fields should be reported
        assert len(positions[0].errors) >= 2

    def test_expired_option_produces_error(self, tmp_path):
        """An option whose expiry is in the past (T ≤ 0) must be flagged at load time."""
        text = """
positions:
  - symbol: "TXO"
    instrument_type: option
    quantity: -5
    strike: 22500.0
    expiry: "2020-01-15"
    option_type: call
    style: european
    currency: TWD
    multiplier: 50.0
"""
        positions = _load_yaml(text, tmp_path)
        assert len(positions) == 1
        assert not positions[0].is_valid
        assert any("T ≤ 0" in e for e in positions[0].errors), \
            f"Expected T≤0 error, got: {positions[0].errors}"

    def test_expired_stock_has_no_expiry_check(self, tmp_path):
        """Stock positions have no expiry field — the T≤0 check must not fire."""
        text = """
positions:
  - symbol: "2330.TW"
    instrument_type: stock
    quantity: 1000
    currency: TWD
    multiplier: 1.0
"""
        positions = _load_yaml(text, tmp_path)
        assert positions[0].is_valid

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_positions(tmp_path / "nonexistent.yaml")
