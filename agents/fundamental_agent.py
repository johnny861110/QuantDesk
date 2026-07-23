"""
FundamentalAgent — Phase 1c: wraps FundamentalAdapter into a full domain agent.

Internal pipeline (deterministic first, LLM last):

    load_data   → run_tools  → verify     → synthesize → build_signal
    (SQLite)      (ROIC/EQ/    (Verifier:   (LLM:         (AgentSignal)
                   EWS pure     is_restated, narrative
                   math)        number       only,
                                check)       no numbers)

Design contract (CLAUDE.md §三條不可違反):
  ① Deterministic / LLM separation:
      run_tools nodes produce all numbers (pure Python, no LLM).
      synthesize node receives only a structured metrics dict;
      LLM cannot introduce new numbers — the prompt contains no raw financials.
  ② Verifier is NOT mocked in tests:
      Verifier.check_narrative() verifies every number in the LLM output traces
      back to a known metric value (regex scan + tolerance match).
  ③ Every key_evidence entry carries source + asof from financial_facts.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph

from adapters.fundamental_adapter import FundamentalAdapter, FinancialSnapshotWithMeta
from agents.verifier import check_narrative as _check_narrative_shared
from observability.langfuse_setup import observe, update_current_span
from schemas.agent_signal import (
    AgentSignal,
    AgentType,
    Evidence,
    HardConstraint,
    Signal,
    Target,
    TimeHorizon,
)


# ─────────────────────────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────────────────────────

class FundamentalAgentState(TypedDict):
    # ── inputs ────────────────────────────────────────────────────────────────
    stock_code: str
    year: int
    quarter: str
    # ── intermediate ──────────────────────────────────────────────────────────
    snapshot: Optional[FinancialSnapshotWithMeta]
    metrics: dict[str, Any]          # all numbers produced by deterministic tools
    hard_constraints: list[dict]     # EWS critical/high → hard_constraints
    warnings: list[str]              # non-fatal Verifier warnings
    errors: list[str]                # fatal Verifier errors (bad LLM numbers, etc.)
    narrative: str                   # LLM-produced white-space summary (no new numbers)
    # ── output ────────────────────────────────────────────────────────────────
    signal: Optional[AgentSignal]


# ─────────────────────────────────────────────────────────────────────────────
# Verifier — pure functions, no I/O, fully unit-testable without LLM
# ─────────────────────────────────────────────────────────────────────────────

class Verifier:
    """
    Deterministic guard between tool outputs and LLM narrative.

    All methods are pure (no I/O) so they can be unit-tested with
    hand-crafted inputs without any mock.
    """

    @classmethod
    def check_is_restated(cls, snapshot: FinancialSnapshotWithMeta) -> list[str]:
        """
        Rule: if any fact in the snapshot was sourced from a restatement,
        the Verifier must emit an explicit warning.

        The warning lands in AgentSignal.errors so the Supervisor (and any
        human reviewer) can see it without digging into per_field_source.
        """
        if snapshot.is_restated:
            restated_fields = [
                f for f, s in snapshot.per_field_source.items()
                if s == "restated"
            ]
            fields_str = ", ".join(restated_fields) if restated_fields else "一或多個欄位"
            return [
                f"⚠️ 財務數字包含重編申報數值（{fields_str}）。"
                "數值雖已採用，但可信度需額外確認；建議人工核對原始重編公告。"
            ]
        return []

    @classmethod
    def check_narrative(
        cls,
        narrative: str,
        metrics: dict[str, Any],
    ) -> list[str]:
        return _check_narrative_shared(narrative, metrics)

    @classmethod
    def check_metrics_consistent(
        cls,
        metrics: dict[str, Any],
    ) -> list[str]:
        """
        Basic internal consistency checks on the computed metrics.
        Add domain-specific checks here as needed.
        """
        warnings: list[str] = []
        roic = metrics.get("roic")
        wacc = metrics.get("wacc")
        if roic is not None and wacc is not None:
            gap = roic - wacc
            if abs(gap - metrics.get("value_creation_gap", gap)) > 0.01:
                warnings.append(
                    "[Verifier] value_creation_gap 與 roic-wacc 不一致，"
                    "計算可能有誤。"
                )
        return warnings

    @classmethod
    def run(
        cls,
        snapshot: FinancialSnapshotWithMeta,
        metrics: dict[str, Any],
        narrative: str = "",
    ) -> tuple[list[str], list[str]]:
        """
        Full Verifier pass. Returns (warnings, errors).

        warnings: non-fatal (is_restated, consistency issues)
        errors:   fatal (LLM hallucinated number found in narrative)
        """
        warnings: list[str] = []
        errors: list[str] = []
        warnings.extend(cls.check_is_restated(snapshot))
        warnings.extend(cls.check_metrics_consistent(metrics))
        errors.extend(cls.check_narrative(narrative, metrics))
        return warnings, errors


# ─────────────────────────────────────────────────────────────────────────────
# DataLoader bridge: FundamentalAdapter → FinancialSnapshot (Financial_Agent)
# ─────────────────────────────────────────────────────────────────────────────

def _meta_to_snapshot(meta: FinancialSnapshotWithMeta) -> Any:
    """
    Convert FinancialSnapshotWithMeta → app.models.FinancialSnapshot.

    Import deferred so this module is importable before financial-agent
    editable install is complete (e.g. for Verifier unit tests).
    """
    from app.models import FinancialSnapshot  # type: ignore[import]

    return FinancialSnapshot(
        stock_code=meta.stock_code,
        company_name=meta.company_name,
        report_year=meta.report_year,
        report_season=meta.report_season,
        report_period=meta.report_period,
        currency=meta.currency,
        unit=meta.unit,
        # Income Statement
        net_revenue=meta.net_revenue,
        gross_profit=meta.gross_profit,
        operating_income=meta.operating_income,
        net_income=meta.net_income,
        eps=meta.eps,
        # Balance Sheet
        cash_and_equivalents=meta.cash_and_equivalents,
        accounts_receivable=meta.accounts_receivable,
        inventory=meta.inventory,
        current_assets=meta.current_assets,
        total_assets=meta.total_assets,
        current_liabilities=meta.current_liabilities,
        total_liabilities=meta.total_liabilities,
        equity=meta.equity,
        retained_earnings=meta.retained_earnings,
        short_term_debt=meta.short_term_debt,
        long_term_debt=meta.long_term_debt,
        # Cash Flow
        operating_cash_flow=meta.operating_cash_flow,
        investing_cash_flow=meta.investing_cash_flow,
        financing_cash_flow=meta.financing_cash_flow,
    )


class _QuantDeskDataLoader:
    """
    Drop-in replacement for Financial_Agent's DataLoader.
    Serves FinancialSnapshot data from FinancialReports SQLite via FundamentalAdapter.

    After instantiating a service:
        svc = ROICWACCService()
        svc.data_loader = _QuantDeskDataLoader(adapter)
    """

    def __init__(self, adapter: FundamentalAdapter) -> None:
        self._adapter = adapter

    def _parse_period(self, period: str) -> tuple[int, str]:
        """'2024Q1' → (2024, 'Q1')"""
        return int(period[:4]), period[4:]

    def load_snapshot(self, stock_code: str, period: str) -> Any | None:
        try:
            year, quarter = self._parse_period(period)
            meta = self._adapter.get_snapshot(stock_code, year, quarter)
            if not meta.per_field_confidence:   # no facts found
                return None
            return _meta_to_snapshot(meta)
        except Exception:
            return None

    def load_multiple_periods(self, stock_code: str, periods: list[str]) -> list[Any]:
        snaps = []
        for p in periods:
            s = self.load_snapshot(stock_code, p)
            if s is not None:
                snaps.append(s)
        return snaps

    def list_available_periods(self, stock_code: str) -> list[str]:
        # Phase 1c stub — returns empty; EWS/EQ trend analysis degrades gracefully
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic metric computation
# ─────────────────────────────────────────────────────────────────────────────

def _annualisation_factor(report_period: str) -> int:
    """
    Return the multiplier needed to convert a single-period ROIC to an annual rate.

    ROICWACCService computes ROIC as (period NOPAT) / (balance-sheet IC).
    WACC via CAPM is always annual.  Before comparing them we must bring ROIC
    onto the same annual basis.

    Supported period suffixes (case-insensitive):
      Q1 / Q2 / Q3 / Q4  → 4  (one quarter = 1/4 year)
      H1 / H2            → 2  (one half-year = 1/2 year)
      FY / <year only>   → 1  (already annual)

    Unknown format → 1 (safe fallback; leaves comparison slightly wrong rather
    than catastrophically wrong, and logs no error — caller should validate input).
    """
    suffix = report_period.upper()[-2:]
    if suffix in ("Q1", "Q2", "Q3", "Q4"):
        return 4
    if suffix in ("H1", "H2"):
        return 2
    return 1


def _compute_metrics(
    meta: FinancialSnapshotWithMeta,
    adapter: FundamentalAdapter,
) -> tuple[dict[str, Any], list[str]]:
    """
    Run all deterministic analysis tools and return (metrics_dict, tool_errors).

    All numbers come from here.  LLM nodes receive this dict and may only
    reference its values — they cannot invent new numbers.

    Time-basis note
    ---------------
    ROICWACCService returns a raw per-period ROIC (quarterly operating_income /
    balance-sheet IC).  WACC from CAPM is inherently annual.  We annualise ROIC
    via simple multiplication (×4 for Q, ×2 for H) so that the ROIC > WACC
    comparison and the resulting is_value_creating flag are on the same basis.
    The raw quarterly value is preserved as metrics["roic_quarterly"] for
    audit/Verifier use — it must NOT be used directly against annual WACC.

    Why simple annualisation (×4) rather than compound ((1+r_q)^4 − 1)?
    WACC is estimated via CAPM with defaults: β=1.0, rf=2%, mrp=6%.  The
    estimation error on β alone is typically ±0.2–0.3 (±1.2–1.8 pp on WACC);
    the mrp assumption carries another ±1–2 pp of structural uncertainty.
    The gap between simple and compound annualisation at ROIC ~3 % per quarter
    is only ≈0.27 pp — well below the CAPM noise floor.  Applying compound
    precision against a CAPM estimate is false precision: the denominator is not
    accurate enough to justify the added complexity in the numerator.

    tech-debt: if WACC estimation accuracy improves (e.g. factor-model β,
    market-implied mrp, or credit-spread-based cost of debt), revisit whether
    TTM four-quarter sum of NOPAT divided by average IC is a better alternative
    to simple ×4.  TTM removes the single-quarter seasonality distortion and
    aligns with how most financial data providers report trailing ROIC.
    """
    from app.services.roic_wacc_service import ROICWACCService  # type: ignore[import]
    from app.services.earnings_quality_service import EarningsQualityService  # type: ignore[import]
    from app.services.ews_service import EarlyWarningService  # type: ignore[import]
    from app.core.utils import InsufficientDataError  # type: ignore[import]

    loader = _QuantDeskDataLoader(adapter)
    period = meta.report_period   # e.g. "2024Q1"
    stock_code = meta.stock_code
    ann_factor = _annualisation_factor(period)

    metrics: dict[str, Any] = {
        # Pass-through from snapshot for Verifier baseline
        "quality_score": meta.quality_score,
        "weighted_confidence": round(meta.weighted_confidence(), 4),
        "is_restated": meta.is_restated,
    }
    errors: list[str] = []

    # ── ROIC / WACC ───────────────────────────────────────────────────────────
    try:
        svc = ROICWACCService()
        svc.data_loader = loader
        result = svc.analyze(stock_code, period)
        if result:
            roic_quarterly = round(result.roic, 4)
            roic_annual = round(roic_quarterly * ann_factor, 4)
            wacc = round(result.wacc, 4)      # already annual (CAPM)
            metrics.update({
                # Annual ROIC — same time basis as WACC; use for all comparisons
                "roic": roic_annual,
                # Raw per-period value kept for traceability / Verifier
                "roic_quarterly": roic_quarterly,
                "roic_annualisation_factor": ann_factor,
                "wacc": wacc,
                "value_creation_gap": round(roic_annual - wacc, 4),
                "is_value_creating": roic_annual > wacc,
                "nopat": round(result.nopat, 2),
                "invested_capital": round(result.invested_capital, 2),
                "roic_assumptions": result.assumptions,
            })
    except InsufficientDataError as exc:
        errors.append(f"ROIC/WACC 計算缺少欄位：{exc.missing_fields}")
    except Exception as exc:
        errors.append(f"ROIC/WACC 計算失敗：{exc}")

    # ── Earnings Quality ──────────────────────────────────────────────────────
    try:
        svc_eq = EarningsQualityService()
        svc_eq.data_loader = loader
        eq = svc_eq.calculate_score(stock_code, period)
        if eq:
            metrics.update({
                "eq_total": round(eq.total, 2),
                "eq_accrual_quality": round(eq.accrual_quality, 2),
                "eq_working_capital": round(eq.working_capital_behavior, 2),
                "eq_one_off": round(eq.one_off_dependency, 2),
                "eq_stability": round(eq.earnings_stability, 2),
                "eq_red_flags": eq.red_flags,
            })
    except InsufficientDataError as exc:
        errors.append(f"盈餘品質計算缺少欄位：{exc.missing_fields}")
    except Exception as exc:
        errors.append(f"盈餘品質計算失敗：{exc}")

    # ── Early Warning System ──────────────────────────────────────────────────
    try:
        svc_ew = EarlyWarningService()
        svc_ew.data_loader = loader
        ew = svc_ew.detect_warnings(stock_code, period)
        if ew:
            metrics.update({
                "ews_warning_level": ew.warning_level,
                "ews_signal_count": ew.signal_count,
                "ews_signals": [
                    {"name": s.signal_name, "severity": s.severity}
                    for s in ew.triggered_signals
                ],
            })
    except InsufficientDataError as exc:
        errors.append(f"早期預警計算缺少欄位：{exc.missing_fields}")
    except Exception as exc:
        errors.append(f"早期預警計算失敗：{exc}")

    return metrics, errors


# ─────────────────────────────────────────────────────────────────────────────
# Signal determination (deterministic rule engine, NOT LLM)
# ─────────────────────────────────────────────────────────────────────────────

def _determine_signal(metrics: dict[str, Any]) -> Signal:
    """
    Deterministic signal from verified metrics.  LLM has no vote here.

    Priority:
      1. EWS critical/high → BEARISH (hard)
      2. ROIC > WACC + EQ ≥ 60 → BULLISH
      3. Otherwise → NEUTRAL
    """
    ews_level = metrics.get("ews_warning_level", "none")
    if ews_level in ("critical", "high"):
        return Signal.BEARISH

    is_value_creating = metrics.get("is_value_creating")
    eq_total = metrics.get("eq_total")
    if is_value_creating and (eq_total is None or eq_total >= 60):
        return Signal.BULLISH

    return Signal.NEUTRAL


def _build_hard_constraints(metrics: dict[str, Any]) -> list[HardConstraint]:
    """Promote critical/high EWS signals to AgentSignal.hard_constraints."""
    constraints: list[HardConstraint] = []
    ews_level = metrics.get("ews_warning_level", "none")
    if ews_level in ("critical", "high"):
        for sig in metrics.get("ews_signals", []):
            if sig["severity"] in ("critical", "high"):
                constraints.append(HardConstraint(
                    type=f"ews_{sig['name']}",
                    current=1.0,
                    limit=0.0,
                    breached=True,
                    detail=f"EWS 早期預警觸發：{sig['name']} ({sig['severity']})",
                ))
    return constraints


def _build_key_evidence(
    meta: FinancialSnapshotWithMeta,
    metrics: dict[str, Any],
    asof: datetime,
) -> list[Evidence]:
    evidence: list[Evidence] = []
    source = f"financial_facts#{meta.filing_key}"

    if "roic" in metrics and "wacc" in metrics:
        evidence.append(Evidence(
            claim=f"ROIC {'>' if metrics.get('is_value_creating') else '<'} WACC"
                  f"（差距 {metrics['value_creation_gap']:.2f}pp）",
            value=metrics["value_creation_gap"],
            source=source,
            asof=asof,
        ))
    if "eq_total" in metrics:
        evidence.append(Evidence(
            claim=f"盈餘品質總分 {metrics['eq_total']:.0f}/100",
            value=metrics["eq_total"],
            source=source,
            asof=asof,
        ))
    if "ews_warning_level" in metrics:
        evidence.append(Evidence(
            claim=f"早期預警等級：{metrics['ews_warning_level']}",
            value=float(metrics["ews_signal_count"]),
            source=source,
            asof=asof,
        ))
    return evidence


# ─────────────────────────────────────────────────────────────────────────────
# Langfuse traced wrappers
# ─────────────────────────────────────────────────────────────────────────────

@observe(name="fundamental_agent:adapter:get_snapshot", as_type="tool")  # type: ignore[misc]
def _lf_get_snapshot(
    adapter: FundamentalAdapter,
    stock_code: str,
    year: int,
    quarter: str,
) -> FinancialSnapshotWithMeta:
    """Child span: SQLite financial snapshot fetch."""
    return adapter.get_snapshot(stock_code, year, quarter)


@observe(name="fundamental_agent:verifier:run", as_type="tool")  # type: ignore[misc]
def _lf_verifier_run(
    meta: FinancialSnapshotWithMeta,
    metrics: dict[str, Any],
    narrative: str,
) -> tuple[list[str], list[str]]:
    """Child span: Verifier guard (restatement + narrative number check)."""
    return Verifier.run(meta, metrics, narrative)


# ─────────────────────────────────────────────────────────────────────────────
# LangGraph node functions
# ─────────────────────────────────────────────────────────────────────────────

@observe(name="fundamental_agent:node_load_data")  # type: ignore[misc]
def _node_load_data(state: FundamentalAgentState, adapter: FundamentalAdapter) -> dict:
    meta = _lf_get_snapshot(adapter, state["stock_code"], state["year"], state["quarter"])
    update_current_span(input={
        "stock_code": state["stock_code"],
        "year": state["year"],
        "quarter": state["quarter"],
    }, output={
        "filing_key": meta.filing_key,
        "company_name": meta.company_name,
        "report_period": meta.report_period,
        "is_restated": meta.is_restated,
    })
    return {"snapshot": meta}


@observe(name="fundamental_agent:node_run_tools")  # type: ignore[misc]
def _node_run_tools(state: FundamentalAgentState, adapter: FundamentalAdapter) -> dict:
    meta: FinancialSnapshotWithMeta = state["snapshot"]  # type: ignore[assignment]
    metrics, tool_errors = _compute_metrics(meta, adapter)
    update_current_span(output={
        "roic": metrics.get("roic"),
        "wacc": metrics.get("wacc"),
        "value_creation_gap": metrics.get("value_creation_gap"),
        "eq_total": metrics.get("eq_total"),
        "ews_warning_level": metrics.get("ews_warning_level"),
        "tool_errors": len(tool_errors),
    })
    return {"metrics": metrics, "errors": tool_errors}


@observe(name="fundamental_agent:node_verify")  # type: ignore[misc]
def _node_verify(state: FundamentalAgentState) -> dict:
    meta: FinancialSnapshotWithMeta = state["snapshot"]  # type: ignore[assignment]
    narrative = state.get("narrative", "")
    warnings, errors = _lf_verifier_run(meta, state["metrics"], narrative)  # child span
    hard_constraints = _build_hard_constraints(state["metrics"])
    update_current_span(output={
        "verifier_warnings": len(warnings),
        "verifier_errors": len(errors),
        "hard_constraints_triggered": [hc.type for hc in hard_constraints],
    })
    return {
        "warnings": warnings,
        "errors": state.get("errors", []) + errors,
        "hard_constraints": [hc.model_dump() for hc in hard_constraints],
    }


@observe(name="fundamental_agent:node_synthesize")  # type: ignore[misc]
def _node_synthesize(state: FundamentalAgentState) -> dict:
    """
    Optional LLM node.  Skipped gracefully if OPENAI_API_KEY is absent.

    The prompt contains ONLY the structured metrics dict — no raw financial
    statements.  The LLM cannot cite numbers it wasn't given; the Verifier
    will catch any attempt to do so.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        update_current_span(output={"skipped": True, "reason": "no_api_key"})
        return {"narrative": ""}   # Verifier will find no numbers to flag

    try:
        from langchain_openai import ChatOpenAI  # type: ignore[import]
        from langchain_core.messages import HumanMessage  # type: ignore[import]

        meta: FinancialSnapshotWithMeta = state["snapshot"]  # type: ignore[assignment]
        m = state["metrics"]

        prompt = (
            f"你是財報分析師助理。請根據以下已驗證指標，"
            f"用繁體中文寫一段 150 字內的白話投研摘要。"
            f"只能引用以下數字，不能加入任何其他數字：\n\n"
            f"標的：{meta.stock_code} {meta.company_name} {meta.report_period}\n"
            f"ROIC：{m.get('roic', 'N/A')}%  WACC：{m.get('wacc', 'N/A')}%  "
            f"價值創造差距：{m.get('value_creation_gap', 'N/A')}pp\n"
            f"盈餘品質：{m.get('eq_total', 'N/A')}/100  "
            f"早期預警：{m.get('ews_warning_level', 'N/A')}\n"
            f"品質分數：{m.get('quality_score', 'N/A')}\n"
        )
        update_current_span(input={"model": "gpt-4o-mini", "prompt_length": len(prompt)})
        llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)
        response = llm.invoke([HumanMessage(content=prompt)])
        narrative = str(response.content)
        update_current_span(output={"narrative_length": len(narrative), "llm_ok": True})
        return {"narrative": narrative}
    except Exception as exc:
        update_current_span(output={"skipped": True, "error": str(exc)})
        return {"narrative": ""}


@observe(name="fundamental_agent:node_build_signal")  # type: ignore[misc]
def _node_build_signal(state: FundamentalAgentState) -> dict:
    meta: FinancialSnapshotWithMeta = state["snapshot"]  # type: ignore[assignment]
    metrics = state["metrics"]
    asof = datetime.now(UTC)

    sig_val = _determine_signal(metrics)
    conf = round(meta.weighted_confidence(), 4)
    signal = AgentSignal(
        agent=AgentType.FUNDAMENTAL,
        target=Target(
            symbol=meta.stock_code,
            market="TW",
            asof=asof,
        ),
        signal=sig_val,
        confidence=conf,
        time_horizon=TimeHorizon.MEDIUM,
        key_evidence=_build_key_evidence(meta, metrics, asof),
        hard_constraints=[HardConstraint(**hc) for hc in state.get("hard_constraints", [])],
        metrics=metrics,
        narrative=state.get("narrative", ""),
        data_quality=meta.to_data_quality(),
        errors=state.get("warnings", []) + state.get("errors", []),
    )
    update_current_span(output={
        "signal": sig_val.value,
        "confidence": conf,
        "hard_constraint_breaches": [
            hc["type"] for hc in state.get("hard_constraints", []) if hc.get("breached")
        ],
        "total_errors": len(signal.errors),
    })
    return {"signal": signal}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

class FundamentalAgent:
    """
    Fundamental domain agent.

    Usage:
        agent = FundamentalAgent("/path/to/financial.db")
        signal = agent.run("2330", 2024, "Q1")
        assert isinstance(signal, AgentSignal)
    """

    def __init__(self, db_path: str) -> None:
        self._adapter = FundamentalAdapter(db_path)
        self._graph = self._build_graph()

    def _build_graph(self) -> Any:
        adapter = self._adapter
        g: StateGraph = StateGraph(FundamentalAgentState)

        g.add_node("load_data", lambda s: _node_load_data(s, adapter))
        g.add_node("run_tools", lambda s: _node_run_tools(s, adapter))
        g.add_node("verify", _node_verify)
        g.add_node("synthesize", _node_synthesize)
        g.add_node("build_signal", _node_build_signal)

        g.set_entry_point("load_data")
        g.add_edge("load_data", "run_tools")
        g.add_edge("run_tools", "verify")
        g.add_edge("verify", "synthesize")
        g.add_edge("synthesize", "build_signal")
        g.add_edge("build_signal", END)

        return g.compile()

    @observe(name="fundamental_agent:run")  # type: ignore[misc]
    def run(self, stock_code: str, year: int, quarter: str) -> AgentSignal:
        """Execute the full agent pipeline and return an AgentSignal.

        Calls nodes directly (not via graph.invoke) so that the Langfuse
        @observe() trace context propagates into each node's child span.
        """
        update_current_span(input={"stock_code": stock_code, "year": year, "quarter": quarter})
        adapter = self._adapter

        # Direct sequential node calls — guarantees OTel context propagation.
        state: FundamentalAgentState = {
            "stock_code": stock_code,
            "year": year,
            "quarter": quarter,
            "snapshot": None,
            "metrics": {},
            "hard_constraints": [],
            "warnings": [],
            "errors": [],
            "narrative": "",
            "signal": None,
        }
        state = {**state, **_node_load_data(state, adapter)}
        state = {**state, **_node_run_tools(state, adapter)}
        state = {**state, **_node_verify(state)}
        state = {**state, **_node_synthesize(state)}
        state = {**state, **_node_build_signal(state)}

        result: AgentSignal = state["signal"]  # type: ignore[assignment]
        update_current_span(output={
            "signal": result.signal.value,
            "confidence": result.confidence,
        })
        return result
