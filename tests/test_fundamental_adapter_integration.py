"""
Integration tests for FundamentalAdapter — uses a real SQLiteStore.

Run with:  uv run pytest -m integration
(Not in default CI; requires FinancialReports editable install.)

fixture_store is defined in tests/conftest.py and contains 3 filings:
  Case A │ 2330_2024Q1 │ XBRL,     confidence=1.0,  quality_score=0.95
  Case B │ 3711_2024Q1 │ pdf_table, confidence=0.75, quality_score=0.62
  Case C │ 2454_2024Q1 │ restated,  confidence=0.80, quality_score=0.78
"""
from __future__ import annotations

import pytest

from adapters.fundamental_adapter import FundamentalAdapter, FinancialSnapshotWithMeta


pytestmark = pytest.mark.integration


# ─── helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture
def adapter(fixture_store, tmp_path):
    """FundamentalAdapter pointed at the session fixture store."""
    return FundamentalAdapter(fixture_store.db_path)


# ─── Case A: normal XBRL filing ───────────────────────────────────────────────

class TestCaseA_NormalXBRL:
    def test_snapshot_values(self, adapter: FundamentalAdapter):
        snap: FinancialSnapshotWithMeta = adapter.get_snapshot("2330", 2024, "Q1")
        assert snap.stock_code == "2330"
        assert snap.company_name == "台積電"
        assert snap.report_year == 2024
        assert snap.report_season == 1
        assert snap.report_period == "2024Q1"
        assert snap.net_revenue == pytest.approx(625_763_000.0)
        assert snap.eps == pytest.approx(8.70)

    def test_confidence_is_one(self, adapter: FundamentalAdapter):
        snap = adapter.get_snapshot("2330", 2024, "Q1")
        assert all(c == pytest.approx(1.0) for c in snap.per_field_confidence.values())

    def test_all_sources_are_xbrl(self, adapter: FundamentalAdapter):
        snap = adapter.get_snapshot("2330", 2024, "Q1")
        assert all(s == "xbrl" for s in snap.per_field_source.values())

    def test_quality_score_propagated(self, adapter: FundamentalAdapter):
        snap = adapter.get_snapshot("2330", 2024, "Q1")
        assert snap.quality_score == pytest.approx(0.95)

    def test_not_restated(self, adapter: FundamentalAdapter):
        snap = adapter.get_snapshot("2330", 2024, "Q1")
        assert snap.is_restated is False

    def test_data_quality_bridge(self, adapter: FundamentalAdapter):
        snap = adapter.get_snapshot("2330", 2024, "Q1")
        dq = snap.to_data_quality(staleness_sec=60.0)
        assert dq.completeness == pytest.approx(0.95)
        assert dq.confidence == pytest.approx(1.0)
        assert dq.staleness_sec == pytest.approx(60.0)

    def test_fetch_returns_sourced_data(self, adapter: FundamentalAdapter):
        sourced = adapter.fetch(stock_code="2330", year=2024, quarter="Q1")
        assert "FinancialReports" in sourced.source
        assert isinstance(sourced.payload, FinancialSnapshotWithMeta)
        assert sourced.asof is not None


# ─── Case B: low-confidence PDF filing ────────────────────────────────────────

class TestCaseB_LowConfidencePDF:
    def test_snapshot_values(self, adapter: FundamentalAdapter):
        snap = adapter.get_snapshot("3711", 2024, "Q1")
        assert snap.stock_code == "3711"
        assert snap.company_name == "日月光投控"
        assert snap.net_revenue == pytest.approx(58_200_000.0)
        assert snap.eps == pytest.approx(1.30)

    def test_confidence_is_0_75(self, adapter: FundamentalAdapter):
        snap = adapter.get_snapshot("3711", 2024, "Q1")
        assert all(
            c == pytest.approx(0.75) for c in snap.per_field_confidence.values()
        )

    def test_sources_are_pdf_table(self, adapter: FundamentalAdapter):
        snap = adapter.get_snapshot("3711", 2024, "Q1")
        assert all(s == "pdf_table" for s in snap.per_field_source.values())

    def test_quality_score_is_0_62(self, adapter: FundamentalAdapter):
        snap = adapter.get_snapshot("3711", 2024, "Q1")
        assert snap.quality_score == pytest.approx(0.62)

    def test_not_restated(self, adapter: FundamentalAdapter):
        snap = adapter.get_snapshot("3711", 2024, "Q1")
        assert snap.is_restated is False

    def test_data_quality_uses_quality_score_for_completeness(self, adapter: FundamentalAdapter):
        snap = adapter.get_snapshot("3711", 2024, "Q1")
        dq = snap.to_data_quality()
        assert dq.completeness == pytest.approx(0.62)
        assert dq.confidence == pytest.approx(0.75)


# ─── Case C: restated filing ───────────────────────────────────────────────────

class TestCaseC_Restated:
    def test_snapshot_values(self, adapter: FundamentalAdapter):
        snap = adapter.get_snapshot("2454", 2024, "Q1")
        assert snap.stock_code == "2454"
        assert snap.company_name == "聯發科"
        assert snap.net_revenue == pytest.approx(46_798_000.0)
        assert snap.eps == pytest.approx(9.10)

    def test_is_restated_true(self, adapter: FundamentalAdapter):
        snap = adapter.get_snapshot("2454", 2024, "Q1")
        assert snap.is_restated is True

    def test_raw_confidence_stored(self, adapter: FundamentalAdapter):
        """per_field_confidence stores the raw 0.80 value, not the capped value."""
        snap = adapter.get_snapshot("2454", 2024, "Q1")
        assert all(c == pytest.approx(0.80) for c in snap.per_field_confidence.values())

    def test_quality_score_is_0_78(self, adapter: FundamentalAdapter):
        snap = adapter.get_snapshot("2454", 2024, "Q1")
        assert snap.quality_score == pytest.approx(0.78)

    def test_data_quality_passes_raw_confidence(self, adapter: FundamentalAdapter):
        """
        No cap — raw confidence (0.80) passes through unchanged.
        The Verifier (Phase 1c) is responsible for acting on is_restated.
        """
        snap = adapter.get_snapshot("2454", 2024, "Q1")
        dq = snap.to_data_quality()
        assert dq.completeness == pytest.approx(0.78)
        assert dq.confidence == pytest.approx(0.80)   # raw, not capped


# ─── Missing filing (not in fixture) ─────────────────────────────────────────

class TestMissingFiling:
    def test_missing_filing_returns_empty_snapshot(self, adapter: FundamentalAdapter):
        """A filing that doesn't exist returns a snapshot with all None numeric fields."""
        snap = adapter.get_snapshot("9999", 2024, "Q1")
        assert snap.net_revenue is None
        assert snap.total_assets is None
        assert snap.per_field_confidence == {}
        assert snap.quality_score is None
        assert snap.is_restated is False
