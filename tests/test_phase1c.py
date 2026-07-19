"""
Phase 1c unit tests — Verifier and signal-determination logic.

NO LLM calls, NO SQLiteStore, NO environment variables.

Test matrix:
  Verifier.check_narrative
    ① correct number → passes (no errors)
    ② wrong number   → rejected with error message
    ③ empty narrative → passes trivially
    ④ year numbers (2024, 2025) → NOT flagged (excluded by regex)
    ⑤ percentage variant (8.70%) → passes when 8.70 is in metrics

  Verifier.check_is_restated
    ⑥ is_restated=False → no warning
    ⑦ is_restated=True  → warning emitted with field list
    ⑧ Verifier.run() aggregates warnings + errors correctly

  _determine_signal
    ⑨ EWS critical → BEARISH regardless of ROIC
    ⑩ ROIC > WACC + EQ good → BULLISH
    ⑪ default / no data → NEUTRAL

  _build_hard_constraints
    ⑫ critical EWS signal → HardConstraint breached=True
    ⑬ no critical EWS → empty list

  FundamentalAgent integration (requires financial-agent editable install + fixture_store)
    ⑭ @integration: normal filing → AgentSignal, correct fields
    ⑮ @integration: restated filing → is_restated warning in signal.errors
    ⑯ @integration: missing filing → AgentSignal with empty metrics
"""
from __future__ import annotations

import pytest

from adapters.fundamental_adapter import FinancialSnapshotWithMeta, facts_to_snapshot
from agents.fundamental_agent import (
    Verifier,
    _determine_signal,
    _build_hard_constraints,
)
from schemas.agent_signal import Signal


# ─── helpers ──────────────────────────────────────────────────────────────────

def _snap(is_restated: bool = False, per_field_source: dict | None = None) -> FinancialSnapshotWithMeta:
    facts = [
        {"field": "net_revenue", "value": 100_000.0, "source_type": "restated" if is_restated else "xbrl", "confidence": 0.80 if is_restated else 1.0},
        {"field": "net_income",  "value": 15_000.0,  "source_type": "restated" if is_restated else "xbrl", "confidence": 0.80 if is_restated else 1.0},
    ]
    return facts_to_snapshot("2330", 2024, "Q1", "台積電", facts)


def _metrics_base() -> dict:
    return {
        "roic": 8.70,
        "wacc": 6.50,
        "value_creation_gap": 2.20,
        "is_value_creating": True,
        "eq_total": 72.5,
        "ews_warning_level": "none",
        "ews_signal_count": 0,
        "ews_signals": [],
    }


# ─── ① Verifier: correct number passes ───────────────────────────────────────

def test_correct_number_in_narrative_passes():
    metrics = _metrics_base()
    narrative = "ROIC 為 8.70%，超越 WACC 的 6.50%，差距 2.20 個百分點。"
    errors = Verifier.check_narrative(narrative, metrics)
    assert errors == [], f"Expected no errors but got: {errors}"


# ─── ② Verifier: wrong number is rejected ────────────────────────────────────

def test_wrong_number_in_narrative_is_rejected():
    """
    LLM hallucinated ROIC=15.0 when the actual metric is 8.70.
    Verifier must catch and reject it.
    """
    metrics = _metrics_base()
    narrative = "ROIC 達到 15.0%，遠超 WACC。"
    errors = Verifier.check_narrative(narrative, metrics)
    assert len(errors) >= 1
    assert "15.0" in errors[0] or "未經工具驗證" in errors[0]


def test_wrong_number_partial_match_still_rejected():
    """9.99 is not close enough to any metric value (closest is 8.70)."""
    metrics = _metrics_base()
    narrative = "盈餘品質分數為 9.99 分。"
    errors = Verifier.check_narrative(narrative, metrics)
    assert len(errors) >= 1


# ─── ③ Empty narrative passes trivially ──────────────────────────────────────

def test_empty_narrative_passes():
    assert Verifier.check_narrative("", _metrics_base()) == []


# ─── ④ Year numbers are not flagged ──────────────────────────────────────────

def test_year_numbers_excluded_from_check():
    """2024 and 2025 should NOT be flagged even though they aren't in metrics."""
    metrics = _metrics_base()
    narrative = "2024 年第一季財報顯示，2025 年展望審慎樂觀。"
    errors = Verifier.check_narrative(narrative, metrics)
    assert errors == [], f"Year numbers should be excluded: {errors}"


# ─── ⑤ Percentage variant passes when value in metrics ───────────────────────

def test_percentage_value_matches_metric():
    """'8.70%' should match metric value 8.70."""
    metrics = {"roic": 8.70}
    errors = Verifier.check_narrative("ROIC 為 8.70%。", metrics)
    assert errors == []


# ─── ⑥ check_is_restated: no warning when not restated ───────────────────────

def test_no_restated_warning_for_xbrl():
    snap = _snap(is_restated=False)
    assert snap.is_restated is False
    warnings = Verifier.check_is_restated(snap)
    assert warnings == []


# ─── ⑦ check_is_restated: warning when restated ──────────────────────────────

def test_restated_warning_emitted():
    snap = _snap(is_restated=True)
    assert snap.is_restated is True
    warnings = Verifier.check_is_restated(snap)
    assert len(warnings) == 1
    assert "重編" in warnings[0]


def test_restated_warning_mentions_fields():
    """Warning should mention which fields are restated."""
    snap = _snap(is_restated=True)
    warnings = Verifier.check_is_restated(snap)
    # At least one of the restated fields should appear in the warning
    assert any(f in warnings[0] for f in snap.per_field_source)


# ─── ⑧ Verifier.run() aggregates correctly ───────────────────────────────────

def test_verifier_run_restated_no_bad_narrative():
    snap = _snap(is_restated=True)
    metrics = _metrics_base()
    # Narrative with only correct numbers → errors=[] but warnings=[restated]
    narrative = "ROIC 為 8.70%，WACC 為 6.50%。"
    warnings, errors = Verifier.run(snap, metrics, narrative)
    assert len(warnings) >= 1          # restated warning
    assert errors == []                # narrative is clean


def test_verifier_run_bad_narrative_adds_error():
    snap = _snap(is_restated=False)
    metrics = _metrics_base()
    narrative = "ROIC 達 99.9%。"      # 99.9 not in metrics
    warnings, errors = Verifier.run(snap, metrics, narrative)
    assert len(errors) >= 1
    assert "99.9" in errors[0] or "未經工具驗證" in errors[0]


def test_verifier_run_clean_returns_empty_both():
    snap = _snap(is_restated=False)
    metrics = _metrics_base()
    warnings, errors = Verifier.run(snap, metrics, "")
    assert warnings == []
    assert errors == []


# ─── ⑨ _determine_signal: EWS critical → BEARISH ────────────────────────────

def test_ews_critical_forces_bearish():
    metrics = {**_metrics_base(), "ews_warning_level": "critical", "is_value_creating": True}
    assert _determine_signal(metrics) == Signal.BEARISH


def test_ews_high_forces_bearish():
    metrics = {**_metrics_base(), "ews_warning_level": "high"}
    assert _determine_signal(metrics) == Signal.BEARISH


# ─── ⑩ _determine_signal: ROIC>WACC + EQ good → BULLISH ─────────────────────

def test_value_creating_good_eq_is_bullish():
    metrics = {**_metrics_base(), "ews_warning_level": "none", "is_value_creating": True, "eq_total": 70.0}
    assert _determine_signal(metrics) == Signal.BULLISH


def test_value_creating_no_eq_score_is_bullish():
    """When EQ data missing, ROIC>WACC alone is enough for BULLISH."""
    metrics = {"ews_warning_level": "low", "is_value_creating": True}
    assert _determine_signal(metrics) == Signal.BULLISH


# ─── ⑪ _determine_signal: default → NEUTRAL ──────────────────────────────────

def test_no_data_is_neutral():
    assert _determine_signal({}) == Signal.NEUTRAL


def test_not_value_creating_is_neutral():
    metrics = {**_metrics_base(), "is_value_creating": False, "ews_warning_level": "none"}
    assert _determine_signal(metrics) == Signal.NEUTRAL


def test_low_eq_plus_value_creating_is_neutral():
    metrics = {**_metrics_base(), "is_value_creating": True, "eq_total": 45.0, "ews_warning_level": "none"}
    assert _determine_signal(metrics) == Signal.NEUTRAL


# ─── ⑫ _build_hard_constraints: critical EWS → HardConstraint breached ───────

def test_critical_ews_signal_produces_hard_constraint():
    metrics = {
        "ews_warning_level": "critical",
        "ews_signals": [{"name": "receivables_spike", "severity": "critical"}],
    }
    constraints = _build_hard_constraints(metrics)
    assert len(constraints) == 1
    assert constraints[0].breached is True
    assert "receivables_spike" in constraints[0].type


# ─── ⑬ _build_hard_constraints: no critical → empty ─────────────────────────

def test_no_critical_ews_empty_constraints():
    metrics = {**_metrics_base(), "ews_warning_level": "low",
               "ews_signals": [{"name": "margin_compression", "severity": "medium"}]}
    constraints = _build_hard_constraints(metrics)
    assert constraints == []


def test_none_ews_empty_constraints():
    assert _build_hard_constraints(_metrics_base()) == []


# ─── ⑭–⑯ Integration: FundamentalAgent end-to-end ──────────────────────────

@pytest.mark.integration
class TestFundamentalAgentIntegration:
    @pytest.fixture
    def agent(self, fixture_store):
        from agents.fundamental_agent import FundamentalAgent
        return FundamentalAgent(str(fixture_store.db_path))

    def test_normal_filing_returns_agent_signal(self, agent):
        from schemas.agent_signal import AgentSignal, AgentType, TimeHorizon
        signal = agent.run("2330", 2024, "Q1")
        assert isinstance(signal, AgentSignal)
        assert signal.agent == AgentType.FUNDAMENTAL
        assert signal.target.symbol == "2330"
        assert signal.time_horizon == TimeHorizon.MEDIUM
        assert signal.confidence > 0
        assert signal.data_quality.completeness > 0

    def test_normal_filing_no_restated_warning(self, agent):
        signal = agent.run("2330", 2024, "Q1")
        restated_warnings = [e for e in signal.errors if "重編" in e]
        assert restated_warnings == []

    def test_restated_filing_has_warning_in_errors(self, agent):
        """
        Completion criterion from docs/tasks/phase_1.md §1c:
        Verifier must check is_restated and emit an explicit warning.
        """
        signal = agent.run("2454", 2024, "Q1")   # Case C: restated source_type
        restated_warnings = [e for e in signal.errors if "重編" in e]
        assert len(restated_warnings) >= 1, (
            "Verifier must emit a restated warning for is_restated=True snapshots"
        )

    def test_missing_filing_returns_valid_signal(self, agent):
        """Agent must not crash on an unknown filing; returns NEUTRAL with empty metrics."""
        signal = agent.run("9999", 2024, "Q1")
        assert signal is not None
        assert signal.signal == Signal.NEUTRAL

    def test_signal_metrics_contains_no_llm_numbers(self, agent):
        """
        Without OPENAI_API_KEY the narrative is empty — Verifier finds no numbers
        to flag, so errors should contain only possible is_restated warnings.
        """
        import os
        if os.environ.get("OPENAI_API_KEY"):
            pytest.skip("Test only valid without OPENAI_API_KEY (no LLM call)")
        signal = agent.run("2330", 2024, "Q1")
        verifier_number_errors = [e for e in signal.errors if "未經工具驗證" in e]
        assert verifier_number_errors == []

    # ── ⑰ run_tools node: real ROICWACCService produces roic/wacc ─────────────

    def test_run_tools_produces_real_roic_wacc(self, agent):
        """
        Verifies _node_run_tools actually invokes ROICWACCService via the
        monkey-patched _QuantDeskDataLoader — not a mock.

        Case A (2330) fixture has operating_income, equity, total_liabilities,
        which are the three fields ROICWACCService.analyze() requires.
        If the service ran, signal.metrics must contain 'roic' and 'wacc'.
        """
        signal = agent.run("2330", 2024, "Q1")
        tool_errors = [e for e in signal.errors if "ROIC/WACC" in e]
        assert tool_errors == [], (
            f"ROICWACCService raised an error instead of computing: {tool_errors}"
        )
        assert "roic" in signal.metrics, (
            "run_tools node did not populate 'roic' — ROICWACCService may not have run"
        )
        assert "wacc" in signal.metrics
        assert "value_creation_gap" in signal.metrics

    def test_run_tools_roic_wacc_values_are_finite_and_positive(self, agent):
        """
        Spot-checks that roic and wacc in signal.metrics are finite positive floats.
        """
        import math
        signal = agent.run("2330", 2024, "Q1")
        roic = signal.metrics.get("roic")
        wacc = signal.metrics.get("wacc")
        assert roic is not None and math.isfinite(roic), f"roic={roic} is not finite"
        assert wacc is not None and math.isfinite(wacc), f"wacc={wacc} is not finite"
        assert roic > 0, f"TSMC should have positive ROIC, got {roic}"
        assert wacc > 0, f"WACC should be positive, got {wacc}"

    # ── ⑱ Time-basis: ROIC must be annualized to match annual WACC ────────────

    def test_roic_and_wacc_share_annual_basis(self, agent):
        """
        ROICWACCService._calculate_nopat() uses a single quarter's operating_income
        divided by balance-sheet invested_capital → raw result is a **quarterly** rate.
        WACC via CAPM (rf + β·mrp) is **annual** by definition.

        Comparing quarterly ROIC against annual WACC produces a false conclusion:
          - Case A quarterly ROIC ≈ 3.0 %  vs  WACC ≈ 6.3 %/yr → "value destruction" (wrong)
          - Annualised ROIC ≈ 12.1 %  vs  WACC ≈ 6.3 %/yr → "value creation"  (correct)

        _compute_metrics must annualise ROIC (×4 simple for Q data) **before**
        storing metrics["roic"] and evaluating is_value_creating.

        Verification: we reconstruct the raw quarterly ROIC from fixture constants
        and assert that metrics["roic"] is NOT the raw quarterly value — the
        difference must be close to 3× the quarterly ROIC (the ×4 multiple less
        one raw copy), which is only possible if annualisation happened.
        """
        # ── Known Case A fixture values (TWD thousands) ───────────────────────
        OPERATING_INCOME = 249_519_000.0
        EQUITY            = 3_790_023_000.0
        TOTAL_LIABILITIES = 2_790_000_000.0
        DEFAULT_TAX_RATE  = 0.20   # Financial_Agent default

        nopat          = OPERATING_INCOME * (1 - DEFAULT_TAX_RATE)
        ic             = EQUITY + TOTAL_LIABILITIES
        roic_quarterly = nopat / ic * 100          # ≈ 3.03 %
        roic_annualized = roic_quarterly * 4       # ≈ 12.11 %  (simple annualisation)

        signal = agent.run("2330", 2024, "Q1")
        roic_in_metrics = signal.metrics["roic"]
        wacc_in_metrics = signal.metrics["wacc"]

        # WACC from CAPM is annual by definition; must be well above a quarterly ROIC
        assert wacc_in_metrics > 1.0, (
            f"WACC={wacc_in_metrics:.2f} looks implausibly low; expected annual rate"
        )

        # The metrics["roic"] must NOT equal the raw quarterly rate.
        # If annualisation was skipped, roic_in_metrics ≈ 3.03, which would differ
        # from roic_annualized (≈12.11) by more than 8 pp — caught below.
        assert abs(roic_in_metrics - roic_quarterly) > 1.0, (
            f"metrics['roic']={roic_in_metrics:.4f} matches the raw quarterly rate "
            f"({roic_quarterly:.4f}). ROIC must be annualised (×4) before comparing "
            f"with the annual WACC ({wacc_in_metrics:.4f})."
        )

        # Must be close to the simple ×4 annualised value (within 0.5 pp tolerance)
        assert abs(roic_in_metrics - roic_annualized) < 0.5, (
            f"Expected annualised ROIC ≈ {roic_annualized:.4f} (quarterly {roic_quarterly:.4f} ×4), "
            f"got {roic_in_metrics:.4f}"
        )

        # is_value_creating and value_creation_gap must be internally consistent
        assert signal.metrics["is_value_creating"] == (roic_in_metrics > wacc_in_metrics), (
            "is_value_creating does not match roic > wacc comparison"
        )
        expected_gap = round(roic_in_metrics - wacc_in_metrics, 4)
        assert abs(signal.metrics["value_creation_gap"] - expected_gap) < 0.01, (
            f"value_creation_gap {signal.metrics['value_creation_gap']} "
            f"inconsistent with roic({roic_in_metrics}) - wacc({wacc_in_metrics})"
        )
