"""
FundamentalAdapter — Phase 1a: bridges FinancialReports SQLite into QuantDesk.

Dependency chain:
    FinancialReports/src/storage/SQLiteStore
        ↓  get_facts(filing_key)  →  list[dict]
             each dict: {field, value, source_type, confidence, unit, ...}
        ↓  quality_score via SQL on filings table
    facts_to_snapshot()   ← pure function, no I/O, fully unit-testable
        ↓
    FinancialSnapshotWithMeta
        (mirrors Financial_Agent's FinancialSnapshot field layout
         + quality metadata: quality_score / per_field_confidence / per_field_source)
        ↓
    FundamentalAdapter.fetch()  →  SourcedData[FinancialSnapshotWithMeta]

Phase 1c will wire Financial_Agent's analysis services
(ROICWACCService, EarningsQualityService, …) on top of this adapter.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import text

from adapters.base import FundamentalAdapter as _BaseFundamentalAdapter, SourcedData
from schemas.agent_signal import DataQuality

# ─────────────────────────────────────────────────────────────────────────────
# Field mapping: FinancialReports canonical field → FinancialSnapshotWithMeta field
#
# All entries are 1:1 — no two FR fields map to the same snapshot field.
# eps_basic  → eps          (primary EPS; Q2 design decision)
# eps_diluted → eps_diluted (preserved separately; zero-discard policy)
# equity vs equity_attributable_to_parent: first-write-wins (equity preferred)
# ─────────────────────────────────────────────────────────────────────────────
_FIELD_MAP: dict[str, str] = {
    # Income Statement
    "net_revenue": "net_revenue",
    "gross_profit": "gross_profit",
    "operating_income": "operating_income",
    "net_income": "net_income",
    "eps_basic": "eps",              # basic EPS → primary eps field
    "eps_diluted": "eps_diluted",    # diluted EPS → own field; NOT an alias for eps
    # Balance Sheet
    "cash_and_equivalents": "cash_and_equivalents",
    "accounts_receivable": "accounts_receivable",
    "inventory": "inventory",
    "current_assets": "current_assets",
    "total_assets": "total_assets",
    "current_liabilities": "current_liabilities",
    "total_liabilities": "total_liabilities",
    "equity": "equity",                                # preferred
    "equity_attributable_to_parent": "equity",         # fallback (first-write-wins)
    "retained_earnings": "retained_earnings",
    "short_term_debt": "short_term_debt",
    "long_term_debt": "long_term_debt",
    # Cash Flow
    "operating_cash_flow": "operating_cash_flow",
    "investing_cash_flow": "investing_cash_flow",
    "financing_cash_flow": "financing_cash_flow",
}

# Lower integer = higher priority when the same field has multiple sources.
# Rationale for restated near the top (rank 2, just after xbrl/ixbrl):
#   A restatement is a company-issued correction to previously published filings.
#   The restated value is the authoritative "latest truth" — more reliable than
#   FinMind API snapshots or PDF-extracted numbers. is_restated=True on the
#   snapshot already signals downstream (Verifier, Supervisor) to add warnings.
#
# Rationale for computed at the bottom (rank 99):
#   computed facts are derived from other facts via deterministic formulas.
#   Their real confidence is inherited from the weakest input fact — but that
#   chain is not tracked here (tech-debt: computed priority should be dynamic,
#   set to min(priority_of_inputs) at pipeline time rather than fixed 99).
_SRC_PRIORITY: dict[str, int] = {
    "xbrl": 0,
    "ixbrl": 1,
    "restated": 2,   # authoritative correction; is_restated flag handles downstream warnings
    "finmind": 3,
    "pdf_table": 4,
    "pdf_text": 5,
    "computed": 99,  # tech-debt: should inherit min(input priorities); fixed fallback for now
}


# ─────────────────────────────────────────────────────────────────────────────
# Extended snapshot model
# ─────────────────────────────────────────────────────────────────────────────
class FinancialSnapshotWithMeta(BaseModel):
    """
    Mirrors Financial_Agent's FinancialSnapshot field layout,
    extended with FinancialReports quality metadata.

    Why not inherit from Financial_Agent's FinancialSnapshot directly?
    Phase 1a installs only FinancialReports as an editable dep to avoid
    pulling in Financial_Agent's heavy transitive deps (FastAPI, Streamlit,
    langchain-openai…) before they are needed.  Phase 1c will wire
    the Financial_Agent analysis services on top of this model.
    """
    # ── Identification ────────────────────────────────────────────────────────
    stock_code: str
    company_name: str
    report_year: int
    report_season: int   # 1–4
    report_period: str   # e.g. "2024Q1"
    filing_key: str      # e.g. "2330_2024Q1"
    currency: str = "TWD"
    unit: str = "thousand"

    # ── Income Statement ──────────────────────────────────────────────────────
    net_revenue: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    eps: float | None = None          # from eps_basic (preferred)
    eps_diluted: float | None = None  # from eps_diluted (preserved separately; zero-discard)

    # ── Balance Sheet ─────────────────────────────────────────────────────────
    cash_and_equivalents: float | None = None
    accounts_receivable: float | None = None
    inventory: float | None = None
    current_assets: float | None = None
    total_assets: float | None = None
    current_liabilities: float | None = None
    total_liabilities: float | None = None
    equity: float | None = None
    retained_earnings: float | None = None
    short_term_debt: float | None = None
    long_term_debt: float | None = None

    # ── Cash Flow ─────────────────────────────────────────────────────────────
    operating_cash_flow: float | None = None
    investing_cash_flow: float | None = None
    financing_cash_flow: float | None = None

    # ── FinancialReports quality metadata (new in QuantDesk) ─────────────────
    quality_score: float | None = None
    """filings.quality_score: four-dim weighted score [0, 1]
    (40% source coverage + 30% completeness + 20% validation + 10% evidence)"""

    per_field_confidence: dict[str, float] = Field(default_factory=dict)
    """snap_field → confidence from financial_facts.confidence
    (xbrl=1.0, ixbrl/finmind=0.95, pdf_table=0.75, restated≤0.80)"""

    per_field_source: dict[str, str] = Field(default_factory=dict)
    """snap_field → source_type (xbrl | ixbrl | finmind | pdf_table | …)"""

    is_restated: bool = False
    """True when any fact in this filing has source_type == 'restated'."""

    # ── Helpers ───────────────────────────────────────────────────────────────
    def weighted_confidence(self) -> float:
        """Mean confidence across all populated numeric fields (0 if no fields)."""
        vals = list(self.per_field_confidence.values())
        return sum(vals) / len(vals) if vals else 0.0

    def min_confidence(self) -> float:
        """Minimum confidence across populated fields (0 if no fields)."""
        vals = list(self.per_field_confidence.values())
        return min(vals) if vals else 0.0

    def to_data_quality(self, staleness_sec: float = 0.0) -> DataQuality:
        """
        Convert to AgentSignal.DataQuality for the Supervisor.

        completeness: quality_score (filing-level, preferred) or weighted_confidence
        confidence:   raw weighted field-level confidence — NOT capped for restated data.

        Why no cap for is_restated?
          The is_restated flag is the signal — it belongs to the Verifier / Supervisor
          layer to decide how to handle it (add warnings, reduce weight, escalate).
          Silently lowering confidence here would hide information from downstream
          consumers and make the behaviour hard to audit. The Verifier (Phase 1c)
          is required to check is_restated and emit an explicit warning.
        """
        completeness = (
            self.quality_score
            if self.quality_score is not None
            else self.weighted_confidence()
        )
        return DataQuality(
            completeness=max(0.0, min(1.0, completeness)),
            staleness_sec=staleness_sec,
            confidence=max(0.0, min(1.0, self.weighted_confidence())),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Pure conversion function — the only piece that matters for unit tests
# ─────────────────────────────────────────────────────────────────────────────
def facts_to_snapshot(
    stock_code: str,
    year: int,
    quarter: str,        # "Q1" | "Q2" | "Q3" | "Q4"
    company_name: str,
    facts: list[dict[str, Any]],
    quality_score: float | None = None,
) -> FinancialSnapshotWithMeta:
    """
    Pure, deterministic function — no I/O, no side effects.

    Convert a list of FinancialReports fact dicts (as returned by
    SQLiteStore.get_facts()) into a FinancialSnapshotWithMeta.

    Each dict in `facts` must have at minimum:
        field:       str   — canonical FinancialReports field name
        value:       float
        source_type: str   — xbrl | ixbrl | finmind | pdf_table | pdf_text
                             | restated | computed
        confidence:  float — per-fact confidence [0, 1]

    Resolution rules:
    1. Per FR field, keep only the highest-priority source
       (xbrl beats ixbrl beats pdf_table, …).
    2. Map resolved FR fields to snapshot fields via _FIELD_MAP.
       First-write-wins for aliases (eps_basic → eps before eps_diluted → eps).
    3. is_restated = True if ANY fact has source_type == 'restated'.
    """
    # Step 1: per FR field, keep best-priority source
    best_per_fr: dict[str, dict[str, Any]] = {}
    is_restated = False

    for fact in facts:
        fr_field = fact["field"]
        src = fact.get("source_type", "computed")
        if src == "restated":
            is_restated = True
        rank = _SRC_PRIORITY.get(src, 99)
        existing_rank = _SRC_PRIORITY.get(
            best_per_fr[fr_field].get("source_type", "computed"), 99
        ) if fr_field in best_per_fr else 100
        if rank < existing_rank:
            best_per_fr[fr_field] = fact

    # Step 2: map to snapshot fields (first-write-wins for aliases)
    snap_values: dict[str, float] = {}
    per_field_confidence: dict[str, float] = {}
    per_field_source: dict[str, str] = {}

    for fr_field, snap_field in _FIELD_MAP.items():
        if fr_field not in best_per_fr:
            continue
        if snap_field in snap_values:   # already filled by a preferred alias
            continue
        fact = best_per_fr[fr_field]
        snap_values[snap_field] = float(fact["value"])
        per_field_confidence[snap_field] = float(fact["confidence"])
        per_field_source[snap_field] = fact.get("source_type", "unknown")

    season = int(quarter[1])  # "Q1" → 1, "Q4" → 4

    return FinancialSnapshotWithMeta.model_validate(
        {
            "stock_code": stock_code,
            "company_name": company_name,
            "report_year": year,
            "report_season": season,
            "report_period": f"{year}{quarter}",
            "filing_key": f"{stock_code}_{year}{quarter}",
            "quality_score": quality_score,
            "per_field_confidence": per_field_confidence,
            "per_field_source": per_field_source,
            "is_restated": is_restated,
            **snap_values,
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
# Adapter (requires FinancialReports editable install)
# ─────────────────────────────────────────────────────────────────────────────
class FundamentalAdapter(_BaseFundamentalAdapter):
    """
    Reads from FinancialReports SQLite and exposes FinancialSnapshotWithMeta.

    The import of SQLiteStore is deferred to __init__ so this module can be
    imported in environments where FinancialReports is not installed
    (unit tests only use facts_to_snapshot() directly).

    Usage:
        adapter = FundamentalAdapter("/path/to/FinancialReports/data/financial.db")
        data = adapter.fetch(stock_code="2330", year=2024, quarter="Q1")
        snapshot: FinancialSnapshotWithMeta = data.payload
    """

    def __init__(self, db_path: str | Path) -> None:
        # Lazy import keeps the module importable without FinancialReports
        from src.storage.sqlite_store import SQLiteStore  # type: ignore[import]

        self._store = SQLiteStore(db_path)
        self._db_path = Path(db_path)

    @property
    def source_name(self) -> str:
        return f"FinancialReports@{self._db_path.name}"

    def fetch(self, **kwargs: Any) -> SourcedData:
        """
        Required kwargs: stock_code (str), year (int), quarter (str "Q1"–"Q4").
        Returns SourcedData whose .payload is FinancialSnapshotWithMeta.
        """
        stock_code: str = kwargs["stock_code"]
        year: int = int(kwargs["year"])
        quarter: str = kwargs["quarter"]
        snapshot = self.get_snapshot(stock_code, year, quarter)
        return SourcedData(
            payload=snapshot,
            source=self.source_name,
            asof=datetime.now(UTC),
        )

    def get_snapshot(
        self,
        stock_code: str,
        year: int,
        quarter: str,
    ) -> FinancialSnapshotWithMeta:
        """High-level convenience: returns a ready-to-use snapshot."""
        filing_key = f"{stock_code}_{year}{quarter}"
        facts = self._store.get_facts(filing_key)
        quality_score = self._get_quality_score(filing_key)
        company_name = self._get_company_name(stock_code)
        return facts_to_snapshot(
            stock_code=stock_code,
            year=year,
            quarter=quarter,
            company_name=company_name,
            facts=facts,
            quality_score=quality_score,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get_quality_score(self, filing_key: str) -> float | None:
        with self._store.conn() as c:
            row = c.execute(
                text("SELECT quality_score FROM filings WHERE filing_key=:fk"),
                {"fk": filing_key},
            ).fetchone()
            return float(row[0]) if row and row[0] is not None else None

    def _get_company_name(self, stock_code: str) -> str:
        with self._store.conn() as c:
            row = c.execute(
                text("SELECT name_zh FROM companies WHERE stock_code=:sc"),
                {"sc": stock_code},
            ).fetchone()
            return row[0] if row else stock_code
