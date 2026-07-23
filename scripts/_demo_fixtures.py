"""
Internal helper — scripts/ only, not imported by tests or agents.

Provides the same SQLiteStore setup helpers as tests/conftest.py but WITHOUT
importing pytest, so this can be used from demo scripts running outside pytest.

Requires FinancialReports editable install (financial-reports in pyproject.toml).
If the install is absent, all exports raise ImportError with a clear message.
"""
from __future__ import annotations

from typing import Any

try:
    from src.storage.sqlite_store import SQLiteStore  # type: ignore[import-untyped]
    from src.domain.identity import FilingIdentity   # type: ignore[import-untyped]
    _FR_AVAILABLE = True
except ImportError:
    _FR_AVAILABLE = False
    SQLiteStore = None   # type: ignore[assignment, misc]
    FilingIdentity = None  # type: ignore[assignment]


def _require_fr() -> None:
    if not _FR_AVAILABLE:
        raise ImportError(
            "FinancialReports editable install not found.\n"
            "Install with: pip install -e /path/to/FinancialReports"
        )


def _insert_filing(
    store: Any, stock_code: str, name_zh: str, year: int, quarter: str
) -> int:
    """Insert company + filing, return filing_id."""
    _require_fr()
    company_id = store.upsert_company(stock_code, name_zh=name_zh)
    identity = FilingIdentity(stock_code=stock_code, year=year, quarter=quarter)
    return store.upsert_filing(identity, company_id)  # type: ignore[no-any-return]


def _insert_fact(
    store: Any,
    filing_id: int,
    field: str,
    value: float,
    source_type: str,
    confidence: float,
) -> None:
    """Insert a fact row directly via SQL."""
    _require_fr()
    from sqlalchemy import text  # type: ignore[import-untyped]
    with store.conn() as c:
        c.execute(
            text(
                "INSERT OR REPLACE INTO financial_facts"
                "(filing_id, field, value, unit, source_type, confidence)"
                " VALUES(:fid, :fld, :val, 'TWD_thousands', :src, :conf)"
            ),
            {"fid": filing_id, "fld": field, "val": value, "src": source_type, "conf": confidence},
        )


def _set_quality_score(store: Any, filing_id: int, score: float) -> None:
    """Set quality_score and status='completed' on a filing."""
    _require_fr()
    from sqlalchemy import text  # type: ignore[import-untyped]
    with store.conn() as c:
        c.execute(
            text("UPDATE filings SET quality_score=:s, status='completed' WHERE id=:id"),
            {"s": score, "id": filing_id},
        )
