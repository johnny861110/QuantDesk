"""
Tests for agents/risk/scenario.py

Fixture portfolio
-----------------
Three positions, all TWD, all index-linked (symbols in INDEX_DERIVATIVE_SYMBOLS):

  idx 0: "TXFF" futures  qty=-2,  mult=200,  spot_twd=22000
           → degenerate: delta=1, gamma=vega=theta=0

  idx 1: "TXO"  short call  qty=-5,  mult=50,   spot_twd=22000
           → delta=0.55, gamma=0.0002, vega=30, theta=-8

  idx 2: "TXO"  long put    qty=+5,  mult=50,   spot_twd=22000
           → delta=-0.45, gamma=0.0001, vega=25, theta=-6

KEY:  The scenario P&L only covers index-linked positions (TXFF, TXO).
      Individual stocks (2330.TW, AAPL) and stock options (AAPL call) are
      excluded because their TAIEX beta is unknown — applying index_shock×1
      would embed a silent beta=1 assumption contradicting aggregation.py's
      unmapped_single_name_exposure design.

Δt convention
-------------
days_held=1 → Δt = 1/365 (calendar days per year, NOT 252).
"""
from __future__ import annotations

import pytest

from agents.risk.position_loader import Position
from agents.risk.pricing_router import GreeksResult
from agents.risk.scenario import (
    BETA_NOT_ESTIMATED_NOTE,
    CALENDAR_DAYS_PER_YEAR,
    INDEX_SHOCKS,
    IV_SHOCKS,
    ScenarioResult,
    ScenarioRow,
    run_scenarios,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

SPOT_TWD = 22_000.0
MOCK_USDTWD = 32.5  # not used in TWD-only tests, kept for reference


def _futures(symbol: str, qty: float, mult: float) -> Position:
    return Position(
        symbol=symbol, instrument_type="futures",
        quantity=qty, currency="TWD", multiplier=mult,
    )


def _option(
    symbol: str, qty: float, mult: float,
    strike: float = 22000.0,
    option_type: str = "call",
    style: str = "european",
    expiry: str = "2026-09-16",
    currency: str = "TWD",
) -> Position:
    return Position(
        symbol=symbol, instrument_type="option",
        quantity=qty, currency=currency, multiplier=mult,
        strike=strike, expiry=expiry, option_type=option_type, style=style,
    )


def _stock(symbol: str, qty: float, mult: float = 1.0, currency: str = "TWD") -> Position:
    return Position(
        symbol=symbol, instrument_type="stock",
        quantity=qty, currency=currency, multiplier=mult,
    )


def _greeks(
    delta: float, gamma: float = 0.0, vega: float = 0.0, theta: float = 0.0
) -> GreeksResult:
    return GreeksResult(
        price=0.0, delta=delta, gamma=gamma, vega=vega, theta=theta, rho=0.0,
        model="mock", iv=0.20,
    )


# Synthetic Greeks for the convexity-demo portfolio
# (mimicking a realistic ATM-ish position at index ~22000)
SHORT_CALL_GREEKS = _greeks(delta=0.55,  gamma=0.0002, vega=30.0, theta=-8.0)
LONG_PUT_GREEKS   = _greeks(delta=-0.45, gamma=0.0001, vega=25.0, theta=-6.0)


@pytest.fixture
def convexity_positions() -> list[Position]:
    """Short TXFF + short TXO call + long TXO put — typical hedged portfolio."""
    return [
        _futures("TXFF", qty=-2, mult=200),          # idx 0
        _option("TXO",   qty=-5, mult=50,            # idx 1: short call
                strike=22500.0, option_type="call"),
        _option("TXO",   qty=+5, mult=50,            # idx 2: long put
                strike=21000.0, option_type="put"),
    ]


@pytest.fixture
def convexity_greeks_map() -> dict[int, GreeksResult]:
    return {
        1: SHORT_CALL_GREEKS,   # short call
        2: LONG_PUT_GREEKS,     # long put
    }


@pytest.fixture
def twd_spot_map() -> dict[str, float]:
    return {"TXFF": SPOT_TWD, "TXO": SPOT_TWD}


@pytest.fixture
def scenario_result(
    convexity_positions: list[Position],
    convexity_greeks_map: dict[int, GreeksResult],
    twd_spot_map: dict[str, float],
) -> ScenarioResult:
    return run_scenarios(
        convexity_positions,
        convexity_greeks_map,
        twd_spot_map,
        days_held=1.0,
    )


# ─── Basic structure tests ────────────────────────────────────────────────────

class TestScenarioShape:
    def test_scenario_count(self, scenario_result: ScenarioResult) -> None:
        """30 scenarios = 6 index shocks × 5 IV shocks."""
        assert len(scenario_result.scenarios) == len(INDEX_SHOCKS) * len(IV_SHOCKS)

    def test_days_held_stored(self, scenario_result: ScenarioResult) -> None:
        assert scenario_result.days_held == 1.0

    def test_shocks_stored(self, scenario_result: ScenarioResult) -> None:
        assert scenario_result.index_shocks == INDEX_SHOCKS
        assert scenario_result.iv_shocks == IV_SHOCKS

    def test_each_row_has_three_legs(self, scenario_result: ScenarioResult) -> None:
        """Three valid positions → three legs per scenario row."""
        for row in scenario_result.scenarios:
            assert len(row.legs) == 3

    def test_agg_equals_sum_of_legs(self, scenario_result: ScenarioResult) -> None:
        """Aggregate P&L must equal the sum of per-leg totals."""
        for row in scenario_result.scenarios:
            expected_total = sum(leg.total_pnl for leg in row.legs)
            assert row.agg_total_pnl == pytest.approx(expected_total, rel=1e-10)

    def test_agg_components_sum_to_total(self, scenario_result: ScenarioResult) -> None:
        """agg_delta + agg_gamma + agg_vega + agg_theta = agg_total."""
        for row in scenario_result.scenarios:
            components = (
                row.agg_delta_pnl + row.agg_gamma_pnl
                + row.agg_vega_pnl + row.agg_theta_pnl
            )
            assert components == pytest.approx(row.agg_total_pnl, rel=1e-10)

    def test_zero_shock_row_exists(self, scenario_result: ScenarioResult) -> None:
        """iv_shock=0.0 is in IV_SHOCKS — verify one row per index shock."""
        zero_iv_rows = [r for r in scenario_result.scenarios if r.iv_shock == 0.0]
        assert len(zero_iv_rows) == len(INDEX_SHOCKS)


# ─── Delta P&L formula validation ─────────────────────────────────────────────

class TestDeltaPnLFormula:
    def test_futures_delta_pnl_at_plus3pct(
        self, scenario_result: ScenarioResult
    ) -> None:
        """
        TXFF futures (idx 0): delta=1, qty=-2, mult=200, spot=22000.
        At +3%: ΔS = 0.03 × 22000 = 660
        delta_pnl = 1 × 660 × (-2) × 200 = -264 000 TWD
        """
        row = _find_row(scenario_result, index_shock=0.03, iv_shock=0.0)
        leg = row.legs[0]
        assert leg.symbol == "TXFF"
        assert leg.instrument_type == "futures"
        assert leg.delta_pnl == pytest.approx(-264_000.0, rel=1e-6)

    def test_futures_gamma_pnl_is_zero(
        self, scenario_result: ScenarioResult
    ) -> None:
        """Futures have degenerate Greeks → gamma=vega=theta=0."""
        row = _find_row(scenario_result, index_shock=0.03, iv_shock=0.10)
        leg = row.legs[0]
        assert leg.gamma_pnl == 0.0
        assert leg.vega_pnl  == 0.0
        assert leg.theta_pnl == 0.0

    def test_short_call_delta_pnl_at_plus3pct(
        self, scenario_result: ScenarioResult
    ) -> None:
        """
        TXO short call (idx 1): delta=0.55, qty=-5, mult=50, spot=22000.
        At +3%: ΔS = 660
        delta_pnl = 0.55 × 660 × (-5) × 50 = -90 750 TWD
        """
        row = _find_row(scenario_result, index_shock=0.03, iv_shock=0.0)
        leg = row.legs[1]
        assert leg.symbol == "TXO"
        assert leg.delta_pnl == pytest.approx(-90_750.0, rel=1e-6)

    def test_long_put_delta_pnl_at_plus3pct(
        self, scenario_result: ScenarioResult
    ) -> None:
        """
        TXO long put (idx 2): delta=-0.45, qty=+5, mult=50, spot=22000.
        At +3%: ΔS = 660
        delta_pnl = -0.45 × 660 × (+5) × 50 = -74 250 TWD
        """
        row = _find_row(scenario_result, index_shock=0.03, iv_shock=0.0)
        leg = row.legs[2]
        assert leg.delta_pnl == pytest.approx(-74_250.0, rel=1e-6)


# ─── Gamma P&L (convexity) formula validation ─────────────────────────────────

class TestGammaPnLFormula:
    def test_short_call_gamma_pnl_negative_at_plus3pct(
        self, scenario_result: ScenarioResult
    ) -> None:
        """
        Short call (idx 1): gamma=0.0002, qty=-5, mult=50, spot=22000.
        At +3%: ΔS=660
        gamma_pnl = ½ × 0.0002 × 660² × (-5) × 50 = -10 890 TWD
        Must be negative — this is the cost of short gamma.
        """
        row = _find_row(scenario_result, index_shock=0.03, iv_shock=0.0)
        leg = row.legs[1]
        expected = 0.5 * 0.0002 * (660.0**2) * (-5) * 50
        assert leg.gamma_pnl == pytest.approx(expected, rel=1e-6)
        assert leg.gamma_pnl < 0.0

    def test_short_call_gamma_pnl_more_negative_at_plus5pct(
        self, scenario_result: ScenarioResult
    ) -> None:
        """
        Gamma P&L ∝ ΔS²: at +5% the loss must be more than at +3%.
        (5/3)² ≈ 2.78 × more negative.
        """
        row3 = _find_row(scenario_result, index_shock=0.03, iv_shock=0.0)
        row5 = _find_row(scenario_result, index_shock=0.05, iv_shock=0.0)
        gamma_3pct = row3.legs[1].gamma_pnl
        gamma_5pct = row5.legs[1].gamma_pnl
        assert gamma_5pct < gamma_3pct  # more negative at larger shock

    def test_gamma_pnl_quadratic_scaling(
        self, scenario_result: ScenarioResult
    ) -> None:
        """
        For the short call leg: gamma_pnl(5%) / gamma_pnl(3%) = (5/3)² ≈ 2.778.
        """
        row3 = _find_row(scenario_result, index_shock=0.03, iv_shock=0.0)
        row5 = _find_row(scenario_result, index_shock=0.05, iv_shock=0.0)
        ratio = row5.legs[1].gamma_pnl / row3.legs[1].gamma_pnl
        expected_ratio = (0.05 / 0.03) ** 2
        assert ratio == pytest.approx(expected_ratio, rel=1e-6)

    def test_long_put_gamma_pnl_positive(
        self, scenario_result: ScenarioResult
    ) -> None:
        """
        Long put (idx 2): gamma=0.0001, qty=+5, mult=50.
        Long gamma → gamma_pnl is always positive (gains from convexity).
        At +3%: ½ × 0.0001 × 660² × 5 × 50 = +5 445 TWD
        """
        row = _find_row(scenario_result, index_shock=0.03, iv_shock=0.0)
        expected = 0.5 * 0.0001 * (660.0**2) * 5 * 50
        assert row.legs[2].gamma_pnl == pytest.approx(expected, rel=1e-6)
        assert row.legs[2].gamma_pnl > 0.0


# ─── Negative convexity: portfolio-level ──────────────────────────────────────

class TestNegativeConvexity:
    """
    Verify that the short-gamma portfolio exhibits negative convexity on large
    upside shocks.  At +3% and +5% the portfolio should lose money, with the
    loss accelerating non-linearly due to short gamma.
    """

    def test_portfolio_negative_at_plus3pct_zero_iv(
        self, scenario_result: ScenarioResult
    ) -> None:
        """Net portfolio P&L at +3%/0 IV should be negative (short gamma dominates)."""
        row = _find_row(scenario_result, index_shock=0.03, iv_shock=0.0)
        # All three legs lose on upside move:
        # TXFF short delta: -264 000
        # TXO short call: delta -90 750 + gamma -10 890 = -101 640
        # TXO long put: delta -74 250 + gamma +5 445 = -68 805
        assert row.agg_total_pnl < 0.0

    def test_portfolio_negative_at_plus5pct_zero_iv(
        self, scenario_result: ScenarioResult
    ) -> None:
        """Net portfolio P&L at +5%/0 IV should be negative."""
        row = _find_row(scenario_result, index_shock=0.05, iv_shock=0.0)
        assert row.agg_total_pnl < 0.0

    def test_loss_accelerates_from_3pct_to_5pct(
        self, scenario_result: ScenarioResult
    ) -> None:
        """
        If loss were purely linear, |P&L(5%)| / |P&L(3%)| = 5/3 ≈ 1.667.
        With negative convexity (short gamma) the ratio must EXCEED 5/3.
        """
        row3 = _find_row(scenario_result, index_shock=0.03, iv_shock=0.0)
        row5 = _find_row(scenario_result, index_shock=0.05, iv_shock=0.0)
        loss_3 = abs(row3.agg_total_pnl)
        loss_5 = abs(row5.agg_total_pnl)
        ratio = loss_5 / loss_3
        linear_ratio = 5.0 / 3.0
        assert ratio > linear_ratio, (
            f"Expected super-linear loss (ratio={ratio:.4f} > {linear_ratio:.4f}) "
            "but loss appears linear or sub-linear — short gamma not captured"
        )

    def test_agg_gamma_pnl_negative_on_upside_shocks(
        self, scenario_result: ScenarioResult
    ) -> None:
        """Portfolio net gamma P&L must be negative at all positive shocks."""
        upside_rows = [r for r in scenario_result.scenarios if r.index_shock > 0]
        for row in upside_rows:
            assert row.agg_gamma_pnl < 0.0, (
                f"Expected negative portfolio gamma_pnl at +{row.index_shock:.0%} "
                f"but got {row.agg_gamma_pnl:.2f}"
            )

    def test_gamma_pnl_symmetric_downside_also_negative(
        self, scenario_result: ScenarioResult
    ) -> None:
        """
        Short gamma loses on BOTH up and down moves (ΔS² is always positive).
        Net gamma P&L should also be negative on downside shocks.
        """
        downside_rows = [r for r in scenario_result.scenarios if r.index_shock < 0]
        for row in downside_rows:
            assert row.agg_gamma_pnl < 0.0, (
                f"Expected negative portfolio gamma_pnl at {row.index_shock:.0%} "
                f"but got {row.agg_gamma_pnl:.2f}"
            )


# ─── Vega P&L tests ───────────────────────────────────────────────────────────

class TestVegaPnL:
    def test_short_call_vega_pnl_at_plus10pp_iv(
        self, scenario_result: ScenarioResult
    ) -> None:
        """
        Short call (idx 1): vega=30, qty=-5, mult=50, Δσ=+0.10.
        vega_pnl = 30 × 0.10 × (-5) × 50 = -750 TWD
        Short vega loses when IV rises.
        Use index_shock=+1% — small enough that delta/gamma noise is irrelevant.
        """
        row = _find_row(scenario_result, index_shock=0.01, iv_shock=0.10)
        leg = row.legs[1]
        # vega_pnl should be -750 regardless of index shock
        assert leg.vega_pnl == pytest.approx(-750.0, rel=1e-6)

    def test_long_put_vega_pnl_positive_at_plus10pp_iv(
        self, scenario_result: ScenarioResult
    ) -> None:
        """
        Long put (idx 2): vega=25, qty=+5, mult=50, Δσ=+0.10.
        vega_pnl = 25 × 0.10 × (+5) × 50 = +625 TWD
        """
        row = _find_row(scenario_result, index_shock=0.01, iv_shock=0.10)
        leg = row.legs[2]
        assert leg.vega_pnl == pytest.approx(625.0, rel=1e-6)


# ─── Theta P&L tests (day-count convention) ───────────────────────────────────

class TestThetaPnL:
    def test_theta_uses_calendar_day_convention(
        self, scenario_result: ScenarioResult
    ) -> None:
        """
        theta is already in $/calendar-day.
        Δt = 1 / 365 for days_held=1.

        Short call (idx 1): theta=-8, qty=-5, mult=50.
        theta_pnl = (-8) × (1/365) × (-5) × 50 = +5.479... TWD
        (short option gains a little as theta decay works for seller — sign correct)
        """
        delta_t = 1.0 / CALENDAR_DAYS_PER_YEAR
        row = _find_row(scenario_result, index_shock=0.01, iv_shock=0.0)
        leg = row.legs[1]
        expected = (-8.0) * delta_t * (-5) * 50
        assert leg.theta_pnl == pytest.approx(expected, rel=1e-6)
        assert leg.theta_pnl > 0.0  # short option: theta works in seller's favour

    def test_theta_not_252_convention(
        self,
        convexity_positions: list[Position],
        convexity_greeks_map: dict[int, GreeksResult],
        twd_spot_map: dict[str, float],
    ) -> None:
        """
        Verify that theta uses / 365 (calendar) NOT / 252 (trading day).
        If 252 were used, theta_pnl would be larger by factor 365/252 ≈ 1.448.
        """
        result = run_scenarios(
            convexity_positions, convexity_greeks_map, twd_spot_map, days_held=1.0
        )
        row = _find_row(result, index_shock=0.01, iv_shock=0.0)
        leg = row.legs[1]  # short call

        # Correct (calendar 365): theta_pnl = -8 × (1/365) × -5 × 50
        correct_theta = (-8.0) * (1.0 / 365) * (-5) * 50
        # Wrong (trading 252): would give larger magnitude
        wrong_theta = (-8.0) * (1.0 / 252) * (-5) * 50

        assert leg.theta_pnl == pytest.approx(correct_theta, rel=1e-6)
        assert abs(leg.theta_pnl) != pytest.approx(abs(wrong_theta), rel=1e-3)


# ─── Edge cases ───────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_invalid_position_skipped(
        self,
        convexity_greeks_map: dict[int, GreeksResult],
        twd_spot_map: dict[str, float],
    ) -> None:
        """Invalid positions (pos.is_valid=False) should be silently skipped."""
        bad_pos = Position(
            symbol="BAD", instrument_type="option",
            quantity=1, currency="TWD", multiplier=50,
            # missing strike/expiry/option_type/style → invalid
        )
        positions = [bad_pos]
        result = run_scenarios(positions, convexity_greeks_map, twd_spot_map)
        for row in result.scenarios:
            assert len(row.legs) == 0

    def test_position_missing_from_spot_map_skipped(
        self,
        convexity_positions: list[Position],
        convexity_greeks_map: dict[int, GreeksResult],
    ) -> None:
        """Positions whose symbol is absent from twd_spot_map are skipped."""
        empty_spot_map: dict[str, float] = {}
        result = run_scenarios(
            convexity_positions, convexity_greeks_map, empty_spot_map
        )
        for row in result.scenarios:
            assert len(row.legs) == 0

    def test_option_missing_from_greeks_map_skipped(
        self,
        convexity_positions: list[Position],
        twd_spot_map: dict[str, float],
    ) -> None:
        """Options without a GreeksResult entry are skipped (only futures remain)."""
        result = run_scenarios(
            convexity_positions, greeks_map={}, twd_spot_map=twd_spot_map
        )
        for row in result.scenarios:
            assert len(row.legs) == 1  # only TXFF futures
            assert row.legs[0].instrument_type == "futures"

    def test_custom_shock_grid(
        self,
        convexity_positions: list[Position],
        convexity_greeks_map: dict[int, GreeksResult],
        twd_spot_map: dict[str, float],
    ) -> None:
        """Custom shock grids produce the expected number of scenarios."""
        result = run_scenarios(
            convexity_positions, convexity_greeks_map, twd_spot_map,
            index_shocks=(0.01, 0.02),
            iv_shocks=(0.05,),
        )
        assert len(result.scenarios) == 2  # 2 × 1 = 2

    def test_stock_excluded_beta_not_estimated(
        self,
        convexity_greeks_map: dict[int, GreeksResult],
    ) -> None:
        """
        Individual stocks (2330.TW) are NOT index-linked → excluded from P&L.
        Applying index_shock × spot × delta would embed beta=1 silently,
        contradicting aggregation.py's unmapped_single_name_exposure design.
        The symbol is tracked in ScenarioResult.unmapped_symbols instead.
        """
        stock = Position(
            symbol="2330.TW", instrument_type="stock",
            quantity=100, currency="TWD", multiplier=1.0,
        )
        spot_map = {"2330.TW": 850.0}
        result = run_scenarios([stock], {}, spot_map, days_held=1.0)

        # Symbol must appear in unmapped_symbols with beta-not-estimated note
        assert "2330.TW" in result.unmapped_symbols

        # No legs: the stock is excluded from all scenario P&L
        for row in result.scenarios:
            assert len(row.legs) == 0
            assert row.agg_total_pnl == 0.0


# ─── Unmapped symbols (beta not estimated) ────────────────────────────────────

class TestUnmappedSymbols:
    """
    Verify that non-index positions are excluded from scenario P&L and
    tracked in ScenarioResult.unmapped_symbols.

    Design contract (mirrors aggregation.py):
    - Symbol in INDEX_DERIVATIVE_SYMBOLS → included in legs
    - All other symbols → excluded; appear in unmapped_symbols
    Reason: applying index_shock × spot to individual stocks silently assumes
    beta=1, contradicting aggregation.py's unmapped_single_name_exposure.
    """

    def test_index_positions_have_no_unmapped_symbols(
        self, scenario_result: ScenarioResult
    ) -> None:
        """Pure index fixture (TXFF + TXO): unmapped_symbols must be empty."""
        assert scenario_result.unmapped_symbols == []

    def test_aapl_stock_goes_to_unmapped(
        self,
        convexity_greeks_map: dict[int, GreeksResult],
    ) -> None:
        """AAPL (individual stock) is not index-linked → unmapped."""
        aapl = _stock("AAPL", qty=100, currency="USD")
        spot_map = {"AAPL": 195.0 * 32.5}   # pre-converted TWD
        result = run_scenarios([aapl], {}, spot_map)
        assert "AAPL" in result.unmapped_symbols
        for row in result.scenarios:
            assert len(row.legs) == 0

    def test_mixed_portfolio_unmapped_excludes_stocks_keeps_index(
        self,
        convexity_greeks_map: dict[int, GreeksResult],
        twd_spot_map: dict[str, float],
    ) -> None:
        """
        Portfolio: TXFF (index) + 2330.TW (stock).
        Expected: TXFF appears in legs; 2330.TW appears in unmapped_symbols.
        """
        txff   = _futures("TXFF", qty=-2, mult=200)
        tsmc   = _stock("2330.TW", qty=1000)
        spot   = {**twd_spot_map, "2330.TW": 850.0}

        result = run_scenarios(
            [txff, tsmc], greeks_map={}, twd_spot_map=spot
        )

        assert "2330.TW" in result.unmapped_symbols
        assert "TXFF" not in result.unmapped_symbols
        for row in result.scenarios:
            assert len(row.legs) == 1
            assert row.legs[0].symbol == "TXFF"

    def test_duplicate_symbols_appear_once_in_unmapped(
        self,
        twd_spot_map: dict[str, float],
    ) -> None:
        """Two AAPL positions: symbol appears only once in unmapped_symbols."""
        aapl1 = _stock("AAPL", qty=100, currency="USD")
        aapl2 = _stock("AAPL", qty=50, currency="USD")
        spot  = {"AAPL": 195.0 * 32.5}
        result = run_scenarios([aapl1, aapl2], greeks_map={}, twd_spot_map=spot)
        assert result.unmapped_symbols.count("AAPL") == 1

    def test_beta_not_estimated_note_is_defined(self) -> None:
        """BETA_NOT_ESTIMATED_NOTE must be a non-empty string."""
        assert isinstance(BETA_NOT_ESTIMATED_NOTE, str)
        assert len(BETA_NOT_ESTIMATED_NOTE) > 0


# ─── Numerical spot-check ─────────────────────────────────────────────────────

class TestNumericalValues:
    def test_full_portfolio_pnl_at_plus3pct_zero_iv(
        self, scenario_result: ScenarioResult
    ) -> None:
        """
        Full numerical verification at (+3%, 0 IV, 1 day held).

        TXFF  (idx 0): delta=1,    qty=-2, mult=200, ΔS=660
          delta_pnl = 1×660×(-2)×200 = -264 000
          gamma/vega/theta = 0

        TXO short call (idx 1): delta=0.55, gamma=0.0002, vega=30, theta=-8
          qty=-5, mult=50, ΔS=660, Δσ=0, Δt=1/365
          delta_pnl = 0.55 × 660 × (-5) × 50 =  -90 750
          gamma_pnl = 0.5 × 0.0002 × 660² × (-5) × 50 = -10 890
          vega_pnl  = 0 (Δσ=0)
          theta_pnl = -8 × (1/365) × (-5) × 50 = +5.479...

        TXO long put (idx 2): delta=-0.45, gamma=0.0001, vega=25, theta=-6
          qty=+5, mult=50, ΔS=660, Δσ=0, Δt=1/365
          delta_pnl = -0.45 × 660 × 5 × 50 = -74 250
          gamma_pnl = 0.5 × 0.0001 × 660² × 5 × 50 = +5 445
          vega_pnl  = 0
          theta_pnl = -6 × (1/365) × 5 × 50 = -4.110...

        Aggregate:
          delta = -264 000 + (-90 750) + (-74 250) = -429 000
          gamma = 0 + (-10 890) + 5 445 = -5 445
          vega  = 0
          theta = 5.479... + (-4.110...) = 1.369...
          total = -429 000 + (-5 445) + 0 + 1.369... = -434 443.630...
        """
        row = _find_row(scenario_result, index_shock=0.03, iv_shock=0.0)
        delta_t = 1.0 / 365

        # Verify leg P&Ls individually
        leg0, leg1, leg2 = row.legs

        assert leg0.delta_pnl == pytest.approx(-264_000.0, rel=1e-6)
        assert leg0.gamma_pnl == 0.0

        assert leg1.delta_pnl == pytest.approx(-90_750.0, rel=1e-6)
        assert leg1.gamma_pnl == pytest.approx(-10_890.0, rel=1e-6)
        assert leg1.theta_pnl == pytest.approx((-8.0) * delta_t * (-5) * 50, rel=1e-6)

        assert leg2.delta_pnl == pytest.approx(-74_250.0, rel=1e-6)
        assert leg2.gamma_pnl == pytest.approx(5_445.0, rel=1e-6)
        assert leg2.theta_pnl == pytest.approx((-6.0) * delta_t * 5 * 50, rel=1e-6)

        # Verify aggregates
        assert row.agg_delta_pnl == pytest.approx(-429_000.0, rel=1e-6)
        assert row.agg_gamma_pnl == pytest.approx(-5_445.0, rel=1e-6)
        assert row.agg_vega_pnl  == pytest.approx(0.0, abs=1e-9)

        expected_theta = (
            (-8.0) * delta_t * (-5) * 50
            + (-6.0) * delta_t * 5 * 50
        )
        assert row.agg_theta_pnl == pytest.approx(expected_theta, rel=1e-6)
        assert row.agg_total_pnl == pytest.approx(
            -429_000.0 + (-5_445.0) + 0.0 + expected_theta, rel=1e-6
        )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _find_row(result: ScenarioResult, index_shock: float, iv_shock: float) -> ScenarioRow:
    """Find the ScenarioRow matching the given shocks (exact float match)."""
    for row in result.scenarios:
        if row.index_shock == index_shock and row.iv_shock == iv_shock:
            return row
    raise KeyError(
        f"No scenario row found for index_shock={index_shock}, iv_shock={iv_shock}. "
        f"Available index_shocks={list(result.index_shocks)}, "
        f"iv_shocks={list(result.iv_shocks)}"
    )
