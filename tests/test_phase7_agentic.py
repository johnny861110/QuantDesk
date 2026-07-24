"""
Phase 7 Agentic Architecture Tests

Coverage
--------
schemas/domain_report.py   : ReasoningStep, DomainReport, RouterOutput,
                             domain_report_to_agent_signal()
adapters/chip_adapter.py   : ChipDataAdapter (subclassed, no network),
                             dataclasses, parsing helpers
adapters/fred_adapter.py   : FREDAdapter (subclassed, no network),
                             _parse_fred_rows()
router/intent_router.py    : _regex_fallback(), route() with mocked LLM
agents/chip_agent.py       : pure scoring functions, _determine_chip_signal(),
                             run_chip_agent() with mocked adapter
supervisor/synthesis.py    : _deterministic_fallback(), _build_reports_context(),
                             synthesize_reports() with mocked LLM

Design rules (CLAUDE.md)
------------------------
- NO network calls — everything mocked or subclassed
- LLM calls always mocked (monkeypatch / patch)
- Deterministic functions tested without mocking
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import patch

import pytest

from adapters.chip_adapter import (
    ChipDataAdapter,
    FuturesInstResult,
    InstitutionalResult,
    MarginResult,
    ShareholdingResult,
)
from adapters.fred_adapter import FREDAdapter, _parse_fred_rows
from adapters.macro_adapter import MacroEvent, MacroResult
from agents.chip_agent import (
    _compute_futures_signal,
    _compute_institutional_score,
    _compute_margin_pressure,
    _compute_shareholding_signal,
    _determine_chip_signal,
    run_chip_agent,
)
from router.intent_router import _regex_fallback, route
from schemas.agent_signal import AgentType, HardConstraint, Signal, TimeHorizon
from schemas.domain_report import (
    DomainReport,
    ReasoningStep,
    RouterOutput,
    domain_report_to_agent_signal,
)
from supervisor.synthesis import (
    SynthesisOutput,
    _build_reports_context,
    _deterministic_fallback,
    synthesize_reports,
)


# ─── Shared test helpers ──────────────────────────────────────────────────────


_NOW = datetime(2026, 7, 23, 9, 0, 0, tzinfo=UTC)


def _make_domain_report(
    agent: AgentType = AgentType.CHIP,
    signal: Signal = Signal.BULLISH,
    confidence: float = 0.70,
    symbol: str = "2330",
    market: str = "TW",
    key_findings: dict[str, Any] | None = None,
    hard_constraints: list[HardConstraint] | None = None,
    narrative_summary: str = "test narrative",
    data_completeness: float = 1.0,
    errors: list[str] | None = None,
) -> DomainReport:
    return DomainReport(
        agent=agent,
        symbol=symbol,
        market=market,
        asof=_NOW,
        signal=signal,
        confidence=confidence,
        time_horizon=TimeHorizon.SHORT,
        key_findings=key_findings or {},
        hard_constraints=hard_constraints or [],
        narrative_summary=narrative_summary,
        data_completeness=data_completeness,
        errors=errors or [],
    )


# ─── schemas/domain_report.py ────────────────────────────────────────────────


class TestReasoningStep:
    def test_instantiation(self) -> None:
        step = ReasoningStep(
            thought="check RSI",
            action="compute_rsi",
            action_input={"period": 14},
            observation="RSI=72.3",
        )
        assert step.thought == "check RSI"
        assert step.action == "compute_rsi"
        assert step.action_input == {"period": 14}
        assert step.observation == "RSI=72.3"

    def test_arbitrary_action_input(self) -> None:
        step = ReasoningStep(
            thought="t",
            action="a",
            action_input={"nested": {"key": [1, 2, 3]}},
            observation="obs",
        )
        assert step.action_input["nested"]["key"] == [1, 2, 3]


class TestDomainReport:
    def test_defaults(self) -> None:
        report = DomainReport(
            agent=AgentType.TECHNICAL,
            symbol="2330",
            market="TW",
            asof=_NOW,
            signal=Signal.NEUTRAL,
            confidence=0.5,
            time_horizon=TimeHorizon.SHORT,
        )
        assert report.hard_constraints == []
        assert report.reasoning_steps == []
        assert report.key_findings == {}
        assert report.narrative_summary == ""
        assert report.data_completeness == 1.0
        assert report.errors == []

    def test_has_breach_false_when_no_constraints(self) -> None:
        report = _make_domain_report()
        assert report.has_breach() is False

    def test_has_breach_false_when_all_not_breached(self) -> None:
        hc = HardConstraint(
            type="net_delta_pct_nav",
            current=0.05,
            limit=0.20,
            breached=False,
            detail="within limit",
        )
        report = _make_domain_report(hard_constraints=[hc])
        assert report.has_breach() is False

    def test_has_breach_true_when_any_breached(self) -> None:
        hc_ok = HardConstraint(
            type="gamma_limit",
            current=0.01,
            limit=0.05,
            breached=False,
            detail="ok",
        )
        hc_breach = HardConstraint(
            type="net_delta_pct_nav",
            current=0.35,
            limit=0.20,
            breached=True,
            detail="breached",
        )
        report = _make_domain_report(hard_constraints=[hc_ok, hc_breach])
        assert report.has_breach() is True

    def test_has_breach_true_with_single_breached_constraint(self) -> None:
        hc = HardConstraint(
            type="vega_limit",
            current=999.0,
            limit=100.0,
            breached=True,
            detail="way over limit",
        )
        report = _make_domain_report(hard_constraints=[hc])
        assert report.has_breach() is True

    def test_key_findings_stored(self) -> None:
        report = _make_domain_report(key_findings={"rsi": 72.3, "foreign_net_buy_3d": 12_000_000})
        assert report.key_findings["rsi"] == pytest.approx(72.3)
        assert report.key_findings["foreign_net_buy_3d"] == 12_000_000

    def test_reasoning_steps_can_be_added(self) -> None:
        step = ReasoningStep(thought="t", action="a", action_input={}, observation="o")
        report = _make_domain_report()
        report.reasoning_steps.append(step)
        assert len(report.reasoning_steps) == 1


class TestRouterOutput:
    def test_single_stock_instantiation(self) -> None:
        ro = RouterOutput(
            scenario="single_stock",
            targets=["2330"],
            market="TW",
        )
        assert ro.scenario == "single_stock"
        assert ro.targets == ["2330"]
        assert ro.market == "TW"
        assert ro.depth == "standard"  # default
        assert ro.original_query == ""
        assert ro.extra_context == {}

    def test_portfolio_risk_with_context(self) -> None:
        ro = RouterOutput(
            scenario="portfolio_risk",
            targets=["PORTFOLIO"],
            market="TW",
            depth="deep",
            original_query="我有 10 口 TXO Call",
            extra_context={"positions": [{"symbol": "TXO", "qty": 10}]},
        )
        assert ro.scenario == "portfolio_risk"
        assert ro.depth == "deep"
        assert ro.extra_context["positions"][0]["qty"] == 10

    def test_multi_stock_scan_defaults(self) -> None:
        ro = RouterOutput(
            scenario="multi_stock_scan",
            targets=["2882", "2881"],
            market="TW",
        )
        assert ro.scenario == "multi_stock_scan"
        assert len(ro.targets) == 2


class TestDomainReportToAgentSignal:
    def test_basic_conversion(self) -> None:
        report = _make_domain_report(
            agent=AgentType.CHIP,
            signal=Signal.BULLISH,
            confidence=0.72,
            key_findings={"consecutive_days": 5.0, "foreign_net_shares": 1_000_000.0},
        )
        sig = domain_report_to_agent_signal(report)
        assert sig.agent == AgentType.CHIP
        assert sig.signal == Signal.BULLISH
        assert sig.confidence == pytest.approx(0.72)

    def test_key_findings_numeric_in_key_evidence(self) -> None:
        report = _make_domain_report(
            key_findings={"rsi": 72.3, "label": "oversold"},
        )
        sig = domain_report_to_agent_signal(report)
        # Only numeric values become Evidence items
        evidence_claims = {e.claim for e in sig.key_evidence}
        assert "rsi" in evidence_claims
        assert "label" not in evidence_claims  # string value excluded

    def test_key_findings_in_metrics(self) -> None:
        report = _make_domain_report(
            key_findings={"foreign_net_shares": 500_000.0, "label": "strong"},
        )
        sig = domain_report_to_agent_signal(report)
        assert sig.metrics["foreign_net_shares"] == 500_000.0
        assert sig.metrics["label"] == "strong"

    def test_hard_constraints_preserved(self) -> None:
        hc = HardConstraint(
            type="delta_limit",
            current=0.40,
            limit=0.20,
            breached=True,
            detail="over delta limit",
        )
        report = _make_domain_report(hard_constraints=[hc])
        sig = domain_report_to_agent_signal(report)
        assert len(sig.hard_constraints) == 1
        assert sig.hard_constraints[0].breached is True

    def test_target_symbol_and_market_preserved(self) -> None:
        report = _make_domain_report(symbol="0050", market="TW")
        sig = domain_report_to_agent_signal(report)
        assert sig.target.symbol == "0050"
        assert sig.target.market == "TW"

    def test_narrative_preserved(self) -> None:
        report = _make_domain_report(narrative_summary="外資連5日買超，籌碼偏多。")
        sig = domain_report_to_agent_signal(report)
        assert sig.narrative == "外資連5日買超，籌碼偏多。"

    def test_data_quality_completeness(self) -> None:
        report = _make_domain_report(data_completeness=0.75)
        sig = domain_report_to_agent_signal(report)
        assert sig.data_quality.completeness == pytest.approx(0.75)

    def test_evidence_has_source_and_asof(self) -> None:
        report = _make_domain_report(key_findings={"score": 0.8})
        sig = domain_report_to_agent_signal(report)
        for ev in sig.key_evidence:
            assert ev.source != ""
            assert ev.asof is not None

    def test_empty_key_findings_gives_empty_evidence(self) -> None:
        report = _make_domain_report(key_findings={})
        sig = domain_report_to_agent_signal(report)
        assert sig.key_evidence == []


# ─── adapters/chip_adapter.py ────────────────────────────────────────────────


class _MockChipAdapter(ChipDataAdapter):
    """Subclass that overrides _fetch_raw() to avoid network I/O."""

    def __init__(self, data_map: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__(api_token="test-token")
        self._data_map = data_map

    def _fetch_raw(
        self,
        dataset: str,
        data_id: str,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        return self._data_map.get(dataset, [])


def _inst_rows(dates_and_values: list[tuple[str, float, float, float]]) -> list[dict[str, Any]]:
    """Build synthetic TaiwanStockInstitutionalInvestorsBuySell rows.

    Each tuple: (date_str, foreign_net, trust_net, dealer_net)
    Uses real FinMind field format: English name + buy/sell columns.
    """
    rows = []
    for d, f, t, dealer in dates_and_values:
        rows.append({"date": d, "name": "Foreign_Investor",  "buy": max(f, 0.0), "sell": max(-f, 0.0)})
        rows.append({"date": d, "name": "Investment_Trust",  "buy": max(t, 0.0), "sell": max(-t, 0.0)})
        rows.append({"date": d, "name": "Dealer_self",       "buy": max(dealer, 0.0), "sell": max(-dealer, 0.0)})
    return rows


def _margin_rows(dates_and_values: list[tuple[str, float, float]]) -> list[dict[str, Any]]:
    """Build synthetic TaiwanStockMarginPurchaseShortSale rows.

    Each tuple: (date_str, margin_today, short_today)
    Uses real FinMind field names: MarginPurchaseTodayBalance, ShortSaleTodayBalance.
    """
    return [
        {
            "date": d,
            "MarginPurchaseBuy": 100.0,
            "MarginPurchaseCashRepayment": 0.0,
            "MarginPurchaseTodayBalance": m,
            "ShortSaleSell": 10.0,
            "ShortSaleBuy": 0.0,
            "ShortSaleTodayBalance": s,
            "OffsetLoanAndShort": 0.0,
        }
        for d, m, s in dates_and_values
    ]


def _shareholding_rows(dates_and_ratios: list[tuple[str, float]]) -> list[dict[str, Any]]:
    """Build synthetic TaiwanStockShareholding rows.

    Uses real FinMind field name: ForeignInvestmentSharesRatio.
    """
    return [
        {
            "date": d,
            "ForeignInvestmentSharesRatio": r,
            "ForeignInvestmentShares": 1_000_000.0,
            "NumberOfSharesIssued": 10_000_000.0,
        }
        for d, r in dates_and_ratios
    ]


def _futures_rows(date_str: str, foreign_net: float) -> list[dict[str, Any]]:
    """Build synthetic TaiwanFuturesInstitutionalInvestors rows for latest date."""
    return [
        {
            "date": date_str,
            "name": "外資及陸資",
            "long_open_interest": max(0.0, foreign_net),
            "short_open_interest": max(0.0, -foreign_net),
            "net_open_interest": foreign_net,
        },
    ]


class TestChipDataAdapterInstitutional:
    def test_fetch_institutional_builds_result(self) -> None:
        rows = _inst_rows([
            ("2026-07-18", 1000.0, 200.0, -100.0),
            ("2026-07-21", 2000.0, 300.0, 50.0),
            ("2026-07-22", 1500.0, -100.0, 0.0),
        ])
        adapter = _MockChipAdapter({"TaiwanStockInstitutionalInvestorsBuySell": rows})
        sourced = adapter.fetch_institutional("2330", days=10)
        result: InstitutionalResult = sourced.payload

        assert isinstance(result, InstitutionalResult)
        assert result.symbol == "2330"
        assert len(result.records) == 3
        # foreign net shares should be 1000+2000+1500
        assert result.foreign_net_shares == pytest.approx(4500.0)

    def test_consecutive_foreign_buy_positive(self) -> None:
        rows = _inst_rows([
            ("2026-07-20", 1000.0, 0.0, 0.0),
            ("2026-07-21", 2000.0, 0.0, 0.0),
            ("2026-07-22", 1500.0, 0.0, 0.0),
        ])
        adapter = _MockChipAdapter({"TaiwanStockInstitutionalInvestorsBuySell": rows})
        sourced = adapter.fetch_institutional("2330", days=5)
        result: InstitutionalResult = sourced.payload
        assert result.consecutive_foreign_buy == 3

    def test_consecutive_foreign_sell_negative(self) -> None:
        rows = _inst_rows([
            ("2026-07-20", -500.0, 0.0, 0.0),
            ("2026-07-21", -800.0, 0.0, 0.0),
            ("2026-07-22", -1000.0, 0.0, 0.0),
        ])
        adapter = _MockChipAdapter({"TaiwanStockInstitutionalInvestorsBuySell": rows})
        sourced = adapter.fetch_institutional("2330", days=5)
        result: InstitutionalResult = sourced.payload
        assert result.consecutive_foreign_buy == -3

    def test_empty_data_gives_zero_result(self) -> None:
        adapter = _MockChipAdapter({"TaiwanStockInstitutionalInvestorsBuySell": []})
        sourced = adapter.fetch_institutional("2330")
        result: InstitutionalResult = sourced.payload
        assert result.records == []
        assert result.foreign_net_shares == 0.0
        assert result.consecutive_foreign_buy == 0

    def test_source_name_in_sourced_data(self) -> None:
        adapter = _MockChipAdapter({"TaiwanStockInstitutionalInvestorsBuySell": []})
        sourced = adapter.fetch_institutional("2330")
        assert sourced.source == "finmind_institutional_investors"

    def test_tw_suffix_stripped(self) -> None:
        """Symbol "2330.TW" should resolve to the same data as "2330"."""
        rows = _inst_rows([("2026-07-22", 1000.0, 0.0, 0.0)])
        adapter = _MockChipAdapter({"TaiwanStockInstitutionalInvestorsBuySell": rows})
        sourced = adapter.fetch_institutional("2330.TW", days=5)
        result: InstitutionalResult = sourced.payload
        assert result.symbol == "2330"   # suffix stripped


class TestChipDataAdapterMargin:
    def test_fetch_margin_builds_result(self) -> None:
        # 7 rows so we have >= 6 for 5-day delta
        dates_vals = [
            ("2026-07-14", 1000.0, 50.0),
            ("2026-07-15", 1050.0, 55.0),
            ("2026-07-16", 1100.0, 60.0),
            ("2026-07-17", 1150.0, 65.0),
            ("2026-07-18", 1200.0, 70.0),
            ("2026-07-21", 1250.0, 75.0),
            ("2026-07-22", 1300.0, 80.0),
        ]
        rows = _margin_rows(dates_vals)
        adapter = _MockChipAdapter({"TaiwanStockMarginPurchaseShortSale": rows})
        sourced = adapter.fetch_margin("2330")
        result: MarginResult = sourced.payload

        assert isinstance(result, MarginResult)
        assert result.margin_balance == pytest.approx(1300.0)
        assert result.short_balance == pytest.approx(80.0)

    def test_margin_change_5d_computed(self) -> None:
        # records[-1].margin_balance - records[-6].margin_balance
        # index -1 = 1300, index -6 = 1050 → change = 250
        dates_vals = [
            ("2026-07-14", 1000.0, 50.0),
            ("2026-07-15", 1050.0, 55.0),
            ("2026-07-16", 1100.0, 60.0),
            ("2026-07-17", 1150.0, 65.0),
            ("2026-07-18", 1200.0, 70.0),
            ("2026-07-21", 1250.0, 75.0),
            ("2026-07-22", 1300.0, 80.0),
        ]
        rows = _margin_rows(dates_vals)
        adapter = _MockChipAdapter({"TaiwanStockMarginPurchaseShortSale": rows})
        sourced = adapter.fetch_margin("2330")
        result: MarginResult = sourced.payload
        # records[-6] (index 1) = 1050; records[-1] (index 6) = 1300 → 250
        assert result.margin_change_5d == pytest.approx(250.0)

    def test_empty_margin_gives_zeros(self) -> None:
        adapter = _MockChipAdapter({"TaiwanStockMarginPurchaseShortSale": []})
        sourced = adapter.fetch_margin("2330")
        result: MarginResult = sourced.payload
        assert result.margin_balance == 0.0
        assert result.margin_change_5d == 0.0


class TestChipDataAdapterShareholding:
    def test_fetch_shareholding_computes_change_30d(self) -> None:
        rows = _shareholding_rows([
            ("2026-06-22", 42.5),  # oldest → records[0]
            ("2026-07-12", 43.0),
            ("2026-07-22", 43.8),  # newest → records[-1]
        ])
        adapter = _MockChipAdapter({"TaiwanStockShareholding": rows})
        sourced = adapter.fetch_shareholding("2330")
        result: ShareholdingResult = sourced.payload

        assert isinstance(result, ShareholdingResult)
        assert result.latest_ratio == pytest.approx(43.8)
        # change = 43.8 - 42.5 = 1.3
        assert result.change_30d == pytest.approx(1.3, abs=0.001)

    def test_empty_shareholding_gives_zeros(self) -> None:
        adapter = _MockChipAdapter({"TaiwanStockShareholding": []})
        sourced = adapter.fetch_shareholding("2330")
        result: ShareholdingResult = sourced.payload
        assert result.latest_ratio == 0.0
        assert result.change_30d == 0.0


class TestChipDataAdapterFutures:
    def test_fetch_futures_inst_builds_result(self) -> None:
        rows = _futures_rows("2026-07-22", 10000.0)
        adapter = _MockChipAdapter({"TaiwanFuturesInstitutionalInvestors": rows})
        sourced = adapter.fetch_futures_inst("TXF")
        result: FuturesInstResult = sourced.payload

        assert isinstance(result, FuturesInstResult)
        assert result.foreign_net_position == pytest.approx(10000.0)

    def test_empty_futures_gives_zero_positions(self) -> None:
        adapter = _MockChipAdapter({"TaiwanFuturesInstitutionalInvestors": []})
        sourced = adapter.fetch_futures_inst("TXF")
        result: FuturesInstResult = sourced.payload
        assert result.foreign_net_position == 0.0


# ─── adapters/fred_adapter.py ────────────────────────────────────────────────


class _MockFREDAdapter(FREDAdapter):
    """Subclass overriding _fetch_fred_csv() to return synthetic rows."""

    def __init__(self, data_map: dict[str, list[dict[str, str]]]) -> None:
        super().__init__(finmind_token="")
        self._data_map = data_map

    def _fetch_fred_csv(
        self,
        series_id: str,
        periods: int = 3,
    ) -> list[dict[str, str]]:
        rows = self._data_map.get(series_id, [])
        return rows[-max(periods, 1):]

    def _fetch_taiwan_series(self, periods: int = 3) -> list[MacroEvent]:
        # suppress Taiwan FinMind calls in tests
        return []


class TestParseFredRows:
    def test_basic_parsing(self) -> None:
        rows = [
            {"DATE": "2024-01-01", "PAYEMS": "155000.0"},
            {"DATE": "2024-02-01", "PAYEMS": "155500.0"},
        ]
        events = _parse_fred_rows(rows, "Non Farm Payrolls", "United States", "K", 3, "PAYEMS")
        assert len(events) == 2
        assert events[0].actual == pytest.approx(155000.0)
        assert events[1].actual == pytest.approx(155500.0)

    def test_previous_field_chains_correctly(self) -> None:
        rows = [
            {"DATE": "2024-01-01", "UNRATE": "3.9"},
            {"DATE": "2024-02-01", "UNRATE": "3.7"},
            {"DATE": "2024-03-01", "UNRATE": "3.8"},
        ]
        events = _parse_fred_rows(rows, "Unemployment Rate", "United States", "%", 3, "UNRATE")
        assert events[0].previous is None          # first has no previous
        assert events[1].previous == pytest.approx(3.9)
        assert events[2].previous == pytest.approx(3.7)

    def test_category_and_country_set(self) -> None:
        rows = [{"DATE": "2024-01-01", "DGS10": "4.25"}]
        events = _parse_fred_rows(rows, "US 10Y Treasury Yield", "United States", "%", 2, "DGS10")
        assert events[0].category == "US 10Y Treasury Yield"
        assert events[0].country == "United States"
        assert events[0].importance == 2

    def test_source_name_is_fred(self) -> None:
        rows = [{"DATE": "2024-01-01", "FEDFUNDS": "5.33"}]
        events = _parse_fred_rows(rows, "Fed Funds Rate", "United States", "%", 3, "FEDFUNDS")
        assert events[0].source_name == "fred"

    def test_consensus_is_none(self) -> None:
        """FRED does not provide consensus forecasts."""
        rows = [{"DATE": "2024-01-01", "PAYEMS": "155000.0"}]
        events = _parse_fred_rows(rows, "Non Farm Payrolls", "United States", "K", 3, "PAYEMS")
        assert events[0].consensus is None

    def test_empty_rows_returns_empty_list(self) -> None:
        events = _parse_fred_rows([], "Non Farm Payrolls", "United States", "K", 3, "PAYEMS")
        assert events == []

    def test_invalid_value_row_skipped(self) -> None:
        rows = [
            {"DATE": "2024-01-01", "PAYEMS": "not_a_number"},
            {"DATE": "2024-02-01", "PAYEMS": "155000.0"},
        ]
        events = _parse_fred_rows(rows, "Non Farm Payrolls", "United States", "K", 3, "PAYEMS")
        assert len(events) == 1
        assert events[0].actual == pytest.approx(155000.0)

    def test_missing_series_key_row_skipped(self) -> None:
        """If the series_id column is absent, row is skipped."""
        rows = [{"DATE": "2024-01-01", "OTHER": "999.0"}]
        events = _parse_fred_rows(rows, "Non Farm Payrolls", "United States", "K", 3, "PAYEMS")
        assert events == []

    def test_release_date_parsed(self) -> None:
        rows = [{"DATE": "2024-09-01", "CPIAUCSL": "315.0"}]
        events = _parse_fred_rows(rows, "CPI", "United States", "%", 3, "CPIAUCSL")
        assert events[0].release_date.year == 2024
        assert events[0].release_date.month == 9


class TestFREDAdapterFetch:
    def test_fetch_returns_sourced_data(self) -> None:
        data_map = {
            "PAYEMS": [{"DATE": "2024-01-01", "PAYEMS": "155000.0"}],
            "CPIAUCSL": [{"DATE": "2024-01-01", "CPIAUCSL": "315.0"}],
            "UNRATE": [{"DATE": "2024-01-01", "UNRATE": "3.9"}],
            "FEDFUNDS": [{"DATE": "2024-01-01", "FEDFUNDS": "5.33"}],
        }
        adapter = _MockFREDAdapter(data_map)
        sourced = adapter.fetch(include_taiwan=False)
        assert sourced.source == "fred_free"
        payload = sourced.payload
        assert isinstance(payload, MacroResult)

    def test_fetch_produces_macro_events(self) -> None:
        data_map = {
            "PAYEMS": [{"DATE": "2024-01-01", "PAYEMS": "155000.0"}],
        }
        adapter = _MockFREDAdapter(data_map)
        sourced = adapter.fetch(series=["Non Farm Payrolls"], include_taiwan=False)
        assert len(sourced.payload.events) >= 1

    def test_unknown_series_label_silently_skipped(self) -> None:
        """An unknown label not in FRED_SERIES should produce zero events."""
        adapter = _MockFREDAdapter({})
        sourced = adapter.fetch(series=["Unknown Indicator XYZ"], include_taiwan=False)
        assert sourced.payload.events == []

    def test_empty_csv_gives_empty_events(self) -> None:
        adapter = _MockFREDAdapter({})
        sourced = adapter.fetch(include_taiwan=False)
        assert sourced.payload.events == []

    def test_fetch_countries_includes_united_states(self) -> None:
        adapter = _MockFREDAdapter({})
        sourced = adapter.fetch(include_taiwan=False)
        assert "United States" in sourced.payload.countries

    def test_source_name_property(self) -> None:
        adapter = _MockFREDAdapter({})
        assert adapter.source_name == "fred_free"


# ─── agents/chip_agent.py — pure scoring functions ───────────────────────────


def _make_inst_result(
    consecutive: int = 0,
    foreign_net: float = 0.0,
    trust_net: float = 0.0,
    records_count: int = 1,
) -> InstitutionalResult:
    from adapters.chip_adapter import InstitutionalFlow

    records = [
        InstitutionalFlow(
            date=date(2026, 7, 22),
            foreign_buy_sell=foreign_net / max(records_count, 1),
            foreign_amount=0.0,
            investment_trust_buy_sell=trust_net / max(records_count, 1),
            investment_trust_amount=0.0,
            dealer_buy_sell=0.0,
            dealer_amount=0.0,
            total_buy_sell=(foreign_net + trust_net) / max(records_count, 1),
        )
        for _ in range(records_count)
    ]
    return InstitutionalResult(
        symbol="2330",
        records=records,
        foreign_net_shares=foreign_net,
        foreign_net_amount=0.0,
        trust_net_shares=trust_net,
        dealer_net_shares=0.0,
        total_net_shares=foreign_net + trust_net,
        consecutive_foreign_buy=consecutive,
    )


def _make_margin_result(
    margin_balance: float = 1000.0,
    short_balance: float = 50.0,
    margin_change_5d: float = 0.0,
) -> MarginResult:
    from adapters.chip_adapter import MarginRecord

    record = MarginRecord(
        date=date(2026, 7, 22),
        margin_purchase=100.0,
        margin_redemption=0.0,
        margin_balance=margin_balance,
        short_sale=10.0,
        short_cover=0.0,
        short_balance=short_balance,
        offset=0.0,
    )
    return MarginResult(
        symbol="2330",
        records=[record],
        margin_balance=margin_balance,
        short_balance=short_balance,
        margin_change_5d=margin_change_5d,
    )


def _make_shareholding_result(
    latest_ratio: float = 42.0,
    change_30d: float = 0.0,
) -> ShareholdingResult:
    from adapters.chip_adapter import ShareholdingRecord

    record = ShareholdingRecord(
        date=date(2026, 7, 22),
        foreign_ownership_ratio=latest_ratio,
        foreign_share_count=1_000_000.0,
        listed_shares=10_000_000.0,
    )
    return ShareholdingResult(
        symbol="2330",
        records=[record],
        latest_ratio=latest_ratio,
        change_30d=change_30d,
    )


def _make_futures_result(
    foreign_net_position: float = 0.0,
    with_record: bool = True,
) -> FuturesInstResult:
    from adapters.chip_adapter import FuturesInstRecord

    records = []
    if with_record:
        records.append(FuturesInstRecord(
            date=date(2026, 7, 22),
            name="外資及陸資",
            long_open_interest=max(0.0, foreign_net_position),
            short_open_interest=max(0.0, -foreign_net_position),
            net_open_interest=foreign_net_position,
        ))
    return FuturesInstResult(
        symbol="TXF",
        records=records,
        foreign_net_position=foreign_net_position,
        trust_net_position=0.0,
        dealer_net_position=0.0,
    )


class TestComputeInstitutionalScore:
    def test_consecutive_5_days_buy_bullish(self) -> None:
        result = _make_inst_result(consecutive=5, foreign_net=5000.0, trust_net=100.0)
        scores = _compute_institutional_score(result)
        assert scores["institutional_score"] >= 0.5
        assert scores["foreign_score"] > 0

    def test_consecutive_minus_5_days_bearish(self) -> None:
        result = _make_inst_result(consecutive=-5, foreign_net=-5000.0, trust_net=-100.0)
        scores = _compute_institutional_score(result)
        assert scores["institutional_score"] <= -0.5
        assert scores["foreign_score"] < 0

    def test_empty_records_gives_zeros(self) -> None:
        result = InstitutionalResult(symbol="2330")
        scores = _compute_institutional_score(result)
        assert scores["institutional_score"] == 0.0
        assert scores["foreign_score"] == 0.0
        assert scores["trust_score"] == 0.0

    def test_small_consecutive_positive(self) -> None:
        result = _make_inst_result(consecutive=1, foreign_net=500.0, trust_net=0.0)
        scores = _compute_institutional_score(result)
        assert scores["foreign_score"] == pytest.approx(0.2)

    def test_small_consecutive_negative(self) -> None:
        result = _make_inst_result(consecutive=-1, foreign_net=-500.0, trust_net=0.0)
        scores = _compute_institutional_score(result)
        assert scores["foreign_score"] == pytest.approx(-0.2)

    def test_trust_positive_adds_score(self) -> None:
        result = _make_inst_result(consecutive=0, foreign_net=0.0, trust_net=300.0)
        scores = _compute_institutional_score(result)
        assert scores["trust_score"] == pytest.approx(0.3)

    def test_trust_negative_subtracts_score(self) -> None:
        result = _make_inst_result(consecutive=0, foreign_net=0.0, trust_net=-300.0)
        scores = _compute_institutional_score(result)
        assert scores["trust_score"] == pytest.approx(-0.3)

    def test_score_includes_consecutive_days_key(self) -> None:
        result = _make_inst_result(consecutive=3, foreign_net=1000.0)
        scores = _compute_institutional_score(result)
        assert "consecutive_days" in scores
        assert scores["consecutive_days"] == 3.0

    def test_score_includes_foreign_net_shares_key(self) -> None:
        result = _make_inst_result(consecutive=2, foreign_net=12345.0)
        scores = _compute_institutional_score(result)
        assert "foreign_net_shares" in scores


class TestComputeMarginPressure:
    def test_margin_increase_negative_score(self) -> None:
        result = _make_margin_result(margin_balance=1000.0, short_balance=50.0, margin_change_5d=100.0)
        scores = _compute_margin_pressure(result)
        assert scores["margin_pressure_score"] < 0

    def test_margin_decrease_positive_score(self) -> None:
        result = _make_margin_result(margin_balance=1000.0, short_balance=50.0, margin_change_5d=-100.0)
        scores = _compute_margin_pressure(result)
        # When margin decreases, score is positive (chips lighter)
        assert scores["margin_pressure_score"] > 0

    def test_high_short_ratio_adds_positive(self) -> None:
        # short_balance/margin_balance > 0.1 → short_score = 0.15
        result = _make_margin_result(margin_balance=1000.0, short_balance=200.0, margin_change_5d=0.0)
        scores = _compute_margin_pressure(result)
        # margin_change=0 → margin_score=0; short_ratio=0.2>0.1 → short_score=0.15
        assert scores["margin_pressure_score"] == pytest.approx(0.15)

    def test_empty_records_gives_zeros(self) -> None:
        result = MarginResult(symbol="2330")
        scores = _compute_margin_pressure(result)
        assert scores["margin_pressure_score"] == 0.0
        assert scores["margin_balance"] == 0.0

    def test_score_clamped_within_minus_one_one(self) -> None:
        result = _make_margin_result(margin_balance=1.0, short_balance=0.0, margin_change_5d=9999.0)
        scores = _compute_margin_pressure(result)
        assert -1.0 <= scores["margin_pressure_score"] <= 1.0


class TestComputeShareholdingSignal:
    def test_large_positive_change_positive_score(self) -> None:
        result = _make_shareholding_result(latest_ratio=42.0, change_30d=0.8)
        scores = _compute_shareholding_signal(result)
        assert scores["shareholding_score"] == pytest.approx(0.4)

    def test_large_negative_change_negative_score(self) -> None:
        result = _make_shareholding_result(latest_ratio=42.0, change_30d=-0.8)
        scores = _compute_shareholding_signal(result)
        assert scores["shareholding_score"] == pytest.approx(-0.4)

    def test_high_ratio_reduces_score(self) -> None:
        # change_30d=0.8 normally → 0.4, but ratio>45 → -0.1 reduction
        result = _make_shareholding_result(latest_ratio=46.0, change_30d=0.8)
        scores = _compute_shareholding_signal(result)
        assert scores["shareholding_score"] == pytest.approx(0.3)

    def test_empty_records_gives_zeros(self) -> None:
        result = ShareholdingResult(symbol="2330")
        scores = _compute_shareholding_signal(result)
        assert scores["shareholding_score"] == 0.0
        assert scores["foreign_ownership_ratio"] == 0.0

    def test_small_positive_change_moderate_score(self) -> None:
        result = _make_shareholding_result(latest_ratio=42.0, change_30d=0.3)
        scores = _compute_shareholding_signal(result)
        assert scores["shareholding_score"] == pytest.approx(0.2)

    def test_small_negative_change_moderate_negative_score(self) -> None:
        result = _make_shareholding_result(latest_ratio=42.0, change_30d=-0.3)
        scores = _compute_shareholding_signal(result)
        assert scores["shareholding_score"] == pytest.approx(-0.2)


class TestComputeFuturesSignal:
    def test_large_net_long_positive_score(self) -> None:
        result = _make_futures_result(foreign_net_position=10000.0)
        scores = _compute_futures_signal(result)
        assert scores["futures_signal_score"] == pytest.approx(0.5)

    def test_large_net_short_negative_score(self) -> None:
        result = _make_futures_result(foreign_net_position=-10000.0)
        scores = _compute_futures_signal(result)
        assert scores["futures_signal_score"] == pytest.approx(-0.5)

    def test_small_positive_gives_0_2(self) -> None:
        result = _make_futures_result(foreign_net_position=1000.0)
        scores = _compute_futures_signal(result)
        assert scores["futures_signal_score"] == pytest.approx(0.2)

    def test_small_negative_gives_minus_0_2(self) -> None:
        result = _make_futures_result(foreign_net_position=-1000.0)
        scores = _compute_futures_signal(result)
        assert scores["futures_signal_score"] == pytest.approx(-0.2)

    def test_empty_records_gives_zeros(self) -> None:
        result = FuturesInstResult(symbol="TXF")  # no records
        scores = _compute_futures_signal(result)
        assert scores["futures_signal_score"] == 0.0
        assert scores["foreign_net_position"] == 0.0

    def test_score_capped_at_0_5(self) -> None:
        result = _make_futures_result(foreign_net_position=100_000.0)
        scores = _compute_futures_signal(result)
        assert scores["futures_signal_score"] <= 0.5

    def test_score_capped_at_minus_0_5(self) -> None:
        result = _make_futures_result(foreign_net_position=-100_000.0)
        scores = _compute_futures_signal(result)
        assert scores["futures_signal_score"] >= -0.5


class TestDetermineChipSignal:
    def test_all_positive_gives_bullish(self) -> None:
        scores = {
            "institutional_score": 0.8,
            "shareholding_score": 0.4,
            "futures_signal_score": 0.5,
            "margin_pressure_score": 0.15,
        }
        signal, confidence = _determine_chip_signal(scores)
        assert signal == Signal.BULLISH

    def test_all_negative_gives_bearish(self) -> None:
        scores = {
            "institutional_score": -0.8,
            "shareholding_score": -0.4,
            "futures_signal_score": -0.5,
            "margin_pressure_score": -0.15,
        }
        signal, confidence = _determine_chip_signal(scores)
        assert signal == Signal.BEARISH

    def test_mixed_gives_neutral(self) -> None:
        scores = {
            "institutional_score": 0.1,
            "shareholding_score": -0.1,
            "futures_signal_score": 0.0,
            "margin_pressure_score": 0.0,
        }
        signal, confidence = _determine_chip_signal(scores)
        assert signal == Signal.NEUTRAL

    def test_confidence_in_valid_range(self) -> None:
        for scores in [
            {"institutional_score": 0.9, "shareholding_score": 0.4, "futures_signal_score": 0.5, "margin_pressure_score": 0.15},
            {"institutional_score": -0.9, "shareholding_score": -0.4, "futures_signal_score": -0.5, "margin_pressure_score": -0.15},
            {"institutional_score": 0.0, "shareholding_score": 0.0, "futures_signal_score": 0.0, "margin_pressure_score": 0.0},
        ]:
            _, confidence = _determine_chip_signal(scores)
            assert 0.15 <= confidence <= 0.90

    def test_empty_scores_gives_neutral(self) -> None:
        signal, confidence = _determine_chip_signal({})
        assert signal == Signal.NEUTRAL
        assert 0.15 <= confidence <= 0.90


class TestRunChipAgent:
    """Tests for run_chip_agent() using a mocked ChipDataAdapter."""

    def _make_adapter(
        self,
        inst_rows: list[dict[str, Any]] | None = None,
        margin_rows_data: list[dict[str, Any]] | None = None,
        shareholding_rows_data: list[dict[str, Any]] | None = None,
        futures_rows_data: list[dict[str, Any]] | None = None,
    ) -> _MockChipAdapter:
        data_map: dict[str, list[dict[str, Any]]] = {
            "TaiwanStockInstitutionalInvestorsBuySell": inst_rows or [],
            "TaiwanStockMarginPurchaseShortSale": margin_rows_data or [],
            "TaiwanStockShareholding": shareholding_rows_data or [],
            "TaiwanFuturesInstitutionalInvestors": futures_rows_data or [],
        }
        return _MockChipAdapter(data_map)

    def test_bullish_scenario(self) -> None:
        # 5 consecutive buys → bullish institutional signal
        inst = _inst_rows([
            ("2026-07-16", 2000.0, 100.0, 0.0),
            ("2026-07-17", 2500.0, 100.0, 0.0),
            ("2026-07-18", 1800.0, 200.0, 0.0),
            ("2026-07-21", 3000.0, 100.0, 0.0),
            ("2026-07-22", 2000.0, 100.0, 0.0),
        ])
        share = _shareholding_rows([("2026-06-22", 41.0), ("2026-07-22", 43.0)])
        fut = _futures_rows("2026-07-22", 8000.0)
        adapter = self._make_adapter(
            inst_rows=inst,
            shareholding_rows_data=share,
            futures_rows_data=fut,
        )
        with patch("agents.chip_agent._llm_synthesize_chip", return_value="bullish narrative"):
            report = run_chip_agent(symbol="2330", adapter=adapter, asof=_NOW)

        assert isinstance(report, DomainReport)
        assert report.signal == Signal.BULLISH
        assert report.agent == AgentType.CHIP
        assert 0.15 <= report.confidence <= 0.90

    def test_no_data_gives_neutral_report(self) -> None:
        """Empty adapter data → all scores zero → weighted sum near 0 → NEUTRAL."""
        adapter = self._make_adapter()
        with patch("agents.chip_agent._llm_synthesize_chip", return_value="no data"):
            report = run_chip_agent(symbol="2330", adapter=adapter, asof=_NOW)

        # All chip sources empty → scores all zero → NEUTRAL signal
        assert report.signal == Signal.NEUTRAL

    def test_report_has_key_findings_when_data_present(self) -> None:
        inst = _inst_rows([("2026-07-22", 1000.0, 0.0, 0.0)])
        adapter = self._make_adapter(inst_rows=inst)
        with patch("agents.chip_agent._llm_synthesize_chip", return_value=""):
            report = run_chip_agent(symbol="2330", adapter=adapter, asof=_NOW)
        assert "institutional_score" in report.key_findings

    def test_report_time_horizon_is_short(self) -> None:
        adapter = self._make_adapter()
        with patch("agents.chip_agent._llm_synthesize_chip", return_value=""):
            report = run_chip_agent(symbol="2330", adapter=adapter, asof=_NOW)
        assert report.time_horizon == TimeHorizon.SHORT

    def test_report_has_no_hard_constraints(self) -> None:
        """Chip agent never emits hard_constraints (per CLAUDE.md)."""
        inst = _inst_rows([("2026-07-22", 1000.0, 0.0, 0.0)])
        adapter = self._make_adapter(inst_rows=inst)
        with patch("agents.chip_agent._llm_synthesize_chip", return_value=""):
            report = run_chip_agent(symbol="2330", adapter=adapter, asof=_NOW)
        assert report.hard_constraints == []

    def test_llm_failure_falls_back_to_template(self) -> None:
        """When the OpenAI client raises inside _llm_synthesize_chip,
        the internal try/except catches it and returns a deterministic fallback
        narrative — run_chip_agent must not crash."""
        inst = _inst_rows([("2026-07-22", 2000.0, 100.0, 0.0)])
        adapter = self._make_adapter(inst_rows=inst)
        # Patch OpenAI at the import point used by chip_agent to trigger the
        # internal fallback path (the try/except inside _llm_synthesize_chip).
        with patch("agents.chip_agent.os.environ.get", return_value=""):
            with patch("agents.chip_agent._llm_synthesize_chip", return_value="[fallback narrative]"):
                report = run_chip_agent(symbol="2330", adapter=adapter, asof=_NOW)
        assert isinstance(report, DomainReport)
        assert report.narrative_summary == "[fallback narrative]"


# ─── router/intent_router.py ─────────────────────────────────────────────────


class TestRegexFallback:
    def test_single_stock_from_ticker(self) -> None:
        result = _regex_fallback("2330 現在怎樣")
        assert result.scenario == "single_stock"
        assert "2330" in result.targets

    def test_portfolio_risk_from_options_keyword(self) -> None:
        result = _regex_fallback("我有 10 口 TXO Call 850 9月到期")
        assert result.scenario == "portfolio_risk"

    def test_portfolio_risk_chinese_options_keyword(self) -> None:
        result = _regex_fallback("我的選擇權組合要怎麼對沖")
        assert result.scenario == "portfolio_risk"

    def test_multi_stock_scan_from_scan_keyword(self) -> None:
        result = _regex_fallback("掃金融股找機會")
        assert result.scenario == "multi_stock_scan"

    def test_multi_stock_scan_from_two_tickers(self) -> None:
        result = _regex_fallback("2882 2881 比較")
        assert result.scenario == "multi_stock_scan"
        assert "2882" in result.targets
        assert "2881" in result.targets

    def test_no_match_defaults_to_single_stock(self) -> None:
        result = _regex_fallback("幫我分析")
        assert result.scenario == "single_stock"
        assert result.targets == []

    def test_original_query_preserved(self) -> None:
        query = "2330 現在怎樣"
        result = _regex_fallback(query)
        assert result.original_query == query

    def test_market_default_is_tw(self) -> None:
        result = _regex_fallback("2330 技術面")
        assert result.market == "TW"

    def test_depth_default_is_standard(self) -> None:
        result = _regex_fallback("2330 技術面")
        assert result.depth == "standard"

    def test_returns_router_output_instance(self) -> None:
        result = _regex_fallback("0050 報酬率")
        assert isinstance(result, RouterOutput)

    def test_portfolio_targets_fallback_to_portfolio_when_no_ticker(self) -> None:
        result = _regex_fallback("我的 delta 曝險多少")
        assert result.scenario == "portfolio_risk"
        assert "PORTFOLIO" in result.targets


class TestRoute:
    def test_llm_failure_falls_back_to_regex(self) -> None:
        with patch("router.intent_router._llm_classify", side_effect=RuntimeError("no API key")):
            result = route("2330 現在怎樣")
        assert isinstance(result, RouterOutput)
        assert result.scenario == "single_stock"
        assert "2330" in result.targets

    def test_llm_success_returns_valid_router_output(self) -> None:
        fake_llm_response = {
            "scenario": "single_stock",
            "targets": ["2330"],
            "market": "TW",
            "depth": "standard",
            "extra_context": {},
        }
        with patch("router.intent_router._llm_classify", return_value=fake_llm_response):
            result = route("2330 現在怎樣")
        assert isinstance(result, RouterOutput)
        assert result.scenario == "single_stock"
        assert result.targets == ["2330"]
        assert result.market == "TW"

    def test_llm_portfolio_scenario_returned(self) -> None:
        fake_llm_response = {
            "scenario": "portfolio_risk",
            "targets": ["TXO"],
            "market": "TW",
            "depth": "deep",
            "extra_context": {"positions": "10 口 TXO Call"},
        }
        with patch("router.intent_router._llm_classify", return_value=fake_llm_response):
            result = route("我有 10 口 TXO Call")
        assert result.scenario == "portfolio_risk"
        assert result.depth == "deep"

    def test_llm_returns_unknown_scenario_defaults_gracefully(self) -> None:
        """LLM returning garbage scenario still produces a RouterOutput."""
        fake_llm_response = {
            "scenario": "single_stock",  # valid fallback default
            "targets": [],
            "market": "TW",
            "depth": "standard",
            "extra_context": {},
        }
        with patch("router.intent_router._llm_classify", return_value=fake_llm_response):
            result = route("totally ambiguous query")
        assert isinstance(result, RouterOutput)


# ─── supervisor/synthesis.py ─────────────────────────────────────────────────


class TestBuildReportsContext:
    def test_contains_agent_name(self) -> None:
        report = _make_domain_report(agent=AgentType.CHIP, signal=Signal.BULLISH)
        context = _build_reports_context([report], "2330", "single_stock")
        assert "chip" in context.lower()

    def test_contains_signal_label(self) -> None:
        report = _make_domain_report(signal=Signal.BULLISH, confidence=0.72)
        context = _build_reports_context([report], "2330", "single_stock")
        # Signal label in Chinese: 偏多 ↑
        assert "偏多" in context

    def test_contains_confidence(self) -> None:
        report = _make_domain_report(confidence=0.72)
        context = _build_reports_context([report], "2330", "single_stock")
        assert "72%" in context

    def test_contains_symbol(self) -> None:
        context = _build_reports_context([], "2330", "single_stock")
        assert "2330" in context

    def test_contains_scenario(self) -> None:
        context = _build_reports_context([], "2330", "single_stock")
        assert "single_stock" in context

    def test_multiple_reports_all_present(self) -> None:
        r1 = _make_domain_report(agent=AgentType.CHIP)
        r2 = _make_domain_report(agent=AgentType.TECHNICAL)
        context = _build_reports_context([r1, r2], "2330", "single_stock")
        assert "chip" in context.lower()
        assert "technical" in context.lower()

    def test_key_findings_in_context(self) -> None:
        report = _make_domain_report(key_findings={"rsi": 72.3})
        context = _build_reports_context([report], "2330", "single_stock")
        assert "rsi" in context.lower()

    def test_bearish_signal_shown(self) -> None:
        report = _make_domain_report(signal=Signal.BEARISH)
        context = _build_reports_context([report], "2330", "single_stock")
        assert "偏空" in context

    def test_neutral_signal_shown(self) -> None:
        report = _make_domain_report(signal=Signal.NEUTRAL)
        context = _build_reports_context([report], "2330", "single_stock")
        assert "中性" in context


class TestDeterministicFallback:
    def test_empty_reports_gives_neutral(self) -> None:
        result = _deterministic_fallback([])
        assert result.signal == Signal.NEUTRAL
        assert result.method == "fallback"
        assert result.confidence < 0.20

    def test_empty_reports_low_confidence(self) -> None:
        result = _deterministic_fallback([])
        assert result.confidence == pytest.approx(0.10)

    def test_single_bullish_gives_bullish(self) -> None:
        report = _make_domain_report(signal=Signal.BULLISH, confidence=0.8, data_completeness=1.0)
        result = _deterministic_fallback([report])
        assert result.signal == Signal.BULLISH

    def test_single_bearish_gives_bearish(self) -> None:
        report = _make_domain_report(signal=Signal.BEARISH, confidence=0.8, data_completeness=1.0)
        result = _deterministic_fallback([report])
        assert result.signal == Signal.BEARISH

    def test_bullish_and_bearish_conflict_detected(self) -> None:
        bull = _make_domain_report(
            agent=AgentType.CHIP,
            signal=Signal.BULLISH,
            confidence=0.8,
            data_completeness=1.0,
        )
        bear = _make_domain_report(
            agent=AgentType.TECHNICAL,
            signal=Signal.BEARISH,
            confidence=0.8,
            data_completeness=1.0,
        )
        result = _deterministic_fallback([bull, bear])
        assert len(result.conflicts) > 0
        # Conflict message should mention both sides
        assert any("偏多" in c or "偏空" in c for c in result.conflicts)

    def test_two_bullish_gives_bullish(self) -> None:
        b1 = _make_domain_report(agent=AgentType.CHIP, signal=Signal.BULLISH, confidence=0.7)
        b2 = _make_domain_report(agent=AgentType.TECHNICAL, signal=Signal.BULLISH, confidence=0.7)
        result = _deterministic_fallback([b1, b2])
        assert result.signal == Signal.BULLISH

    def test_method_is_fallback(self) -> None:
        result = _deterministic_fallback([_make_domain_report()])
        assert result.method == "fallback"

    def test_domain_consensus_populated(self) -> None:
        report = _make_domain_report(agent=AgentType.CHIP, signal=Signal.BULLISH)
        result = _deterministic_fallback([report])
        assert "chip" in result.domain_consensus
        assert result.domain_consensus["chip"] == "bullish"

    def test_confidence_in_valid_range(self) -> None:
        reports = [
            _make_domain_report(signal=Signal.BULLISH, confidence=0.9, data_completeness=1.0)
            for _ in range(3)
        ]
        result = _deterministic_fallback(reports)
        assert 0.0 <= result.confidence <= 1.0


class TestSynthesizeReports:
    def test_empty_reports_gives_neutral_fallback(self) -> None:
        result = synthesize_reports(reports=[])
        assert result.signal == Signal.NEUTRAL
        assert result.method == "fallback"

    def test_llm_failure_uses_fallback(self) -> None:
        reports = [_make_domain_report(signal=Signal.BULLISH, confidence=0.8)]
        with patch("supervisor.synthesis._call_synthesis_llm", side_effect=RuntimeError("no key")):
            result = synthesize_reports(reports=reports, symbol="2330")
        assert isinstance(result, SynthesisOutput)
        assert result.method == "fallback"
        assert result.error != ""

    def test_llm_success_gives_llm_method(self) -> None:
        reports = [_make_domain_report(signal=Signal.BULLISH)]
        fake_llm_response = {
            "signal": "bullish",
            "confidence": 0.78,
            "narrative": "外資法人連買，技術面確認，偏多看待。",
            "key_drivers": ["外資連5買超", "RSI強勢"],
            "key_risks": ["融資增加"],
            "domain_consensus": {"chip": "bullish"},
            "conflicts": [],
        }
        with patch("supervisor.synthesis._call_synthesis_llm", return_value=fake_llm_response):
            result = synthesize_reports(reports=reports, symbol="2330")
        assert result.method == "llm"
        assert result.signal == Signal.BULLISH
        assert result.confidence == pytest.approx(0.78)

    def test_llm_bearish_signal_parsed(self) -> None:
        reports = [_make_domain_report()]
        fake_llm_response = {
            "signal": "bearish",
            "confidence": 0.65,
            "narrative": "外資持續賣超，技術破底，偏空。",
            "key_drivers": ["外資賣超"],
            "key_risks": ["跌破支撐"],
            "domain_consensus": {},
            "conflicts": [],
        }
        with patch("supervisor.synthesis._call_synthesis_llm", return_value=fake_llm_response):
            result = synthesize_reports(reports=reports)
        assert result.signal == Signal.BEARISH

    def test_llm_neutral_signal_parsed(self) -> None:
        reports = [_make_domain_report()]
        fake_llm_response = {
            "signal": "neutral",
            "confidence": 0.50,
            "narrative": "各指標分歧，暫時中性觀望。",
            "key_drivers": [],
            "key_risks": [],
            "domain_consensus": {},
            "conflicts": ["技術偏多但籌碼外資賣超"],
        }
        with patch("supervisor.synthesis._call_synthesis_llm", return_value=fake_llm_response):
            result = synthesize_reports(reports=reports)
        assert result.signal == Signal.NEUTRAL
        assert len(result.conflicts) == 1

    def test_confidence_clamped_between_0_and_1(self) -> None:
        reports = [_make_domain_report()]
        fake_llm_response = {
            "signal": "bullish",
            "confidence": 1.5,  # out of range
            "narrative": "",
            "key_drivers": [],
            "key_risks": [],
            "domain_consensus": {},
            "conflicts": [],
        }
        with patch("supervisor.synthesis._call_synthesis_llm", return_value=fake_llm_response):
            result = synthesize_reports(reports=reports)
        assert 0.0 <= result.confidence <= 1.0

    def test_synthesis_output_is_synthesis_output_instance(self) -> None:
        with patch("supervisor.synthesis._call_synthesis_llm", side_effect=RuntimeError("no")):
            result = synthesize_reports(reports=[_make_domain_report()])
        assert isinstance(result, SynthesisOutput)

    def test_key_drivers_and_risks_from_llm(self) -> None:
        reports = [_make_domain_report()]
        fake_llm_response = {
            "signal": "bullish",
            "confidence": 0.70,
            "narrative": "ok",
            "key_drivers": ["外資連5買超", "費半強勢"],
            "key_risks": ["RSI進入超買區"],
            "domain_consensus": {},
            "conflicts": [],
        }
        with patch("supervisor.synthesis._call_synthesis_llm", return_value=fake_llm_response):
            result = synthesize_reports(reports=reports)
        assert "外資連5買超" in result.key_drivers
        assert "RSI進入超買區" in result.key_risks
