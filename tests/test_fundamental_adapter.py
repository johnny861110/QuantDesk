"""
Unit tests for adapters/fundamental_adapter.py — facts_to_snapshot().

NO SQLiteStore, NO filesystem, NO environment variables.
All inputs are hand-crafted dicts that mimic SQLiteStore.get_facts() output.

Each dict has: field, value, source_type, confidence
(plus optional unit, period_start, etc. which the function ignores)

Edge-case matrix:
  ① normal_xbrl        — XBRL source, confidence=1.0, all key fields present
  ② low_confidence_pdf — pdf_table source, confidence=0.75
  ③ restated           — source_type='restated', confidence=0.80; is_restated must be True
  ④ source_priority    — same field in both xbrl and pdf_table; xbrl must win
  ⑤ eps_alias          — eps_basic and eps_diluted both present; eps_basic must win
  ⑥ missing_field      — net_income absent; snapshot field must be None
  ⑦ to_data_quality    — DataQuality completeness / confidence / staleness conversion
  ⑧ quality_score_pass — quality_score flows through to snapshot and DataQuality
  ⑨ empty_facts        — empty list; all numeric fields None, is_restated False
"""

import pytest

from adapters.fundamental_adapter import FinancialSnapshotWithMeta, facts_to_snapshot


# ─── helpers ──────────────────────────────────────────────────────────────────

def _fact(field: str, value: float, source_type: str = "xbrl", confidence: float = 1.0) -> dict:
    return {"field": field, "value": value, "source_type": source_type, "confidence": confidence}


def _base_xbrl_facts() -> list[dict]:
    """Minimal XBRL fact set covering all key FinancialSnapshot fields."""
    return [
        _fact("net_revenue", 100_000.0),
        _fact("gross_profit", 40_000.0),
        _fact("operating_income", 20_000.0),
        _fact("net_income", 15_000.0),
        _fact("eps_basic", 8.5),
        _fact("total_assets", 500_000.0),
        _fact("total_liabilities", 200_000.0),
        _fact("equity", 300_000.0),
        _fact("operating_cash_flow", 25_000.0),
    ]


# ─── ① normal XBRL ───────────────────────────────────────────────────────────

def test_normal_xbrl_values():
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", _base_xbrl_facts())
    assert snap.stock_code == "2330"
    assert snap.company_name == "台積電"
    assert snap.report_year == 2024
    assert snap.report_season == 1
    assert snap.report_period == "2024Q1"
    assert snap.filing_key == "2330_2024Q1"
    assert snap.net_revenue == 100_000.0
    assert snap.eps == 8.5
    assert snap.total_assets == 500_000.0
    assert snap.is_restated is False


def test_normal_xbrl_confidence_is_one():
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", _base_xbrl_facts())
    assert all(c == 1.0 for c in snap.per_field_confidence.values())
    assert all(s == "xbrl" for s in snap.per_field_source.values())


def test_normal_xbrl_weighted_confidence():
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", _base_xbrl_facts())
    assert snap.weighted_confidence() == pytest.approx(1.0)
    assert snap.min_confidence() == pytest.approx(1.0)


# ─── ② low-confidence PDF source ─────────────────────────────────────────────

def test_pdf_source_confidence_propagates():
    facts = [
        _fact("net_revenue", 80_000.0, "pdf_table", 0.75),
        _fact("net_income", 5_000.0, "pdf_table", 0.75),
        _fact("total_assets", 400_000.0, "pdf_table", 0.75),
    ]
    snap = facts_to_snapshot("3711", 2024, "Q1", "日月光", facts)
    assert snap.net_revenue == 80_000.0
    assert snap.per_field_confidence["net_revenue"] == pytest.approx(0.75)
    assert snap.per_field_source["net_revenue"] == "pdf_table"
    assert snap.weighted_confidence() == pytest.approx(0.75)
    assert snap.min_confidence() == pytest.approx(0.75)


def test_pdf_source_to_data_quality():
    facts = [_fact("net_revenue", 80_000.0, "pdf_table", 0.75)]
    snap = facts_to_snapshot("3711", 2024, "Q1", "日月光", facts, quality_score=0.62)
    dq = snap.to_data_quality(staleness_sec=3600.0)
    assert dq.completeness == pytest.approx(0.62)   # quality_score takes precedence
    assert dq.confidence == pytest.approx(0.75)
    assert dq.staleness_sec == pytest.approx(3600.0)


# ─── ③ restated source ───────────────────────────────────────────────────────

def test_restated_sets_flag():
    facts = [
        _fact("net_revenue", 90_000.0, "restated", 0.80),
        _fact("net_income", 10_000.0, "restated", 0.80),
    ]
    snap = facts_to_snapshot("2454", 2024, "Q1", "聯發科", facts)
    assert snap.is_restated is True


def test_restated_confidence_not_capped_in_snapshot():
    """facts_to_snapshot stores raw confidence; cap only applies in to_data_quality."""
    facts = [_fact("net_revenue", 90_000.0, "restated", 0.80)]
    snap = facts_to_snapshot("2454", 2024, "Q1", "聯發科", facts)
    assert snap.per_field_confidence["net_revenue"] == pytest.approx(0.80)


def test_restated_to_data_quality_passes_raw_confidence():
    """
    No confidence cap for restated data — is_restated flag is the signal;
    cap responsibility belongs to the Verifier (Phase 1c), not the adapter.
    """
    facts = [_fact("net_revenue", 90_000.0, "restated", 0.90)]
    snap = facts_to_snapshot("2454", 2024, "Q1", "聯發科", facts, quality_score=0.78)
    dq = snap.to_data_quality()
    assert dq.confidence == pytest.approx(0.90)   # raw, NOT capped
    assert dq.completeness == pytest.approx(0.78)


def test_mixed_restated_and_xbrl_is_restated_true():
    """Even one restated fact is enough to set the flag."""
    facts = [
        _fact("net_revenue", 100_000.0, "xbrl", 1.0),
        _fact("operating_income", 20_000.0, "restated", 0.80),
    ]
    snap = facts_to_snapshot("2454", 2024, "Q1", "聯發科", facts)
    assert snap.is_restated is True


# ─── ④ source priority: xbrl beats pdf_table ─────────────────────────────────

def test_xbrl_beats_pdf_table_for_same_field():
    facts = [
        _fact("net_revenue", 99_000.0, "pdf_table", 0.75),  # lower priority
        _fact("net_revenue", 100_000.0, "xbrl", 1.0),       # higher priority
    ]
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", facts)
    assert snap.net_revenue == 100_000.0
    assert snap.per_field_confidence["net_revenue"] == pytest.approx(1.0)
    assert snap.per_field_source["net_revenue"] == "xbrl"


def test_ixbrl_beats_pdf_but_loses_to_xbrl():
    facts = [
        _fact("net_revenue", 80_000.0, "pdf_table", 0.75),
        _fact("net_revenue", 95_000.0, "ixbrl", 0.95),
        _fact("net_revenue", 100_000.0, "xbrl", 1.0),
    ]
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", facts)
    assert snap.net_revenue == 100_000.0
    assert snap.per_field_source["net_revenue"] == "xbrl"


def test_pdf_wins_when_only_source():
    facts = [_fact("net_revenue", 80_000.0, "pdf_table", 0.75)]
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", facts)
    assert snap.net_revenue == 80_000.0
    assert snap.per_field_source["net_revenue"] == "pdf_table"


# ─── ⑤ eps_basic → eps, eps_diluted → eps_diluted (zero-discard) ─────────────

def test_eps_basic_maps_to_eps():
    facts = [_fact("eps_basic", 8.5)]
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", facts)
    assert snap.eps == pytest.approx(8.5)
    assert snap.eps_diluted is None


def test_eps_diluted_maps_to_own_field():
    """eps_diluted is no longer an alias for eps — it has its own field."""
    facts = [_fact("eps_diluted", 8.0)]
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", facts)
    assert snap.eps is None           # eps_basic absent → eps is None
    assert snap.eps_diluted == pytest.approx(8.0)


def test_both_eps_fields_preserved():
    """When both present, both fields are stored independently; no data discarded."""
    facts = [
        _fact("eps_basic", 8.5),
        _fact("eps_diluted", 8.0),
    ]
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", facts)
    assert snap.eps == pytest.approx(8.5)
    assert snap.eps_diluted == pytest.approx(8.0)
    assert snap.per_field_confidence["eps"] == pytest.approx(1.0)
    assert snap.per_field_confidence["eps_diluted"] == pytest.approx(1.0)


# ─── ⑥ missing field is None ─────────────────────────────────────────────────

def test_missing_field_is_none():
    facts = [_fact("net_revenue", 100_000.0)]
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", facts)
    assert snap.net_income is None
    assert snap.total_assets is None
    assert snap.eps is None
    assert "net_income" not in snap.per_field_confidence


# ─── ⑦ DataQuality conversion ────────────────────────────────────────────────

def test_to_data_quality_without_quality_score():
    """When quality_score is None, completeness falls back to weighted_confidence."""
    facts = [_fact("net_revenue", 100_000.0, "xbrl", 1.0)]
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", facts, quality_score=None)
    dq = snap.to_data_quality()
    assert dq.completeness == pytest.approx(1.0)
    assert dq.confidence == pytest.approx(1.0)
    assert dq.staleness_sec == pytest.approx(0.0)


def test_to_data_quality_clamps_to_0_1():
    """Pathological confidence > 1 should be clamped."""
    facts = [_fact("net_revenue", 1.0, "xbrl", 1.0)]
    snap = facts_to_snapshot("X", 2024, "Q1", "X", facts, quality_score=1.5)
    dq = snap.to_data_quality()
    assert dq.completeness == pytest.approx(1.0)


# ─── ⑧ quality_score propagation ─────────────────────────────────────────────

def test_quality_score_stored_on_snapshot():
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", _base_xbrl_facts(), quality_score=0.93)
    assert snap.quality_score == pytest.approx(0.93)


def test_quality_score_used_as_completeness():
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", _base_xbrl_facts(), quality_score=0.87)
    dq = snap.to_data_quality()
    assert dq.completeness == pytest.approx(0.87)


# ─── ⑨ empty facts list ──────────────────────────────────────────────────────

def test_empty_facts_all_none():
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", [])
    assert snap.net_revenue is None
    assert snap.eps is None
    assert snap.total_assets is None
    assert snap.per_field_confidence == {}
    assert snap.per_field_source == {}
    assert snap.is_restated is False


def test_empty_facts_weighted_confidence_is_zero():
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", [])
    assert snap.weighted_confidence() == pytest.approx(0.0)
    assert snap.min_confidence() == pytest.approx(0.0)


# ─── ⑩ serialization round-trip ──────────────────────────────────────────────

def test_snapshot_serializes_and_deserializes():
    snap = facts_to_snapshot("2330", 2024, "Q1", "台積電", _base_xbrl_facts(), quality_score=0.95)
    json_str = snap.model_dump_json()
    restored = FinancialSnapshotWithMeta.model_validate_json(json_str)
    assert restored.stock_code == "2330"
    assert restored.eps == pytest.approx(8.5)
    assert restored.quality_score == pytest.approx(0.95)
    assert restored.per_field_source["net_revenue"] == "xbrl"
    assert restored.is_restated is False
