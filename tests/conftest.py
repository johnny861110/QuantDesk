"""
Shared pytest fixtures.

fixture_store — builds a real SQLiteStore (via FinancialReports editable install)
in a tmp_path with 3 representative filings:

  Case A │ 2330_2024Q1 │ XBRL source,  confidence=1.0,  quality_score=0.95 (normal)
  Case B │ 3711_2024Q1 │ pdf_table,    confidence=0.75,  quality_score=0.62 (low confidence)
  Case C │ 2454_2024Q1 │ restated,     confidence=0.80,  quality_score=0.78 (restated)

Used only by @pytest.mark.integration tests; unit tests do not import this fixture.
"""
from __future__ import annotations

from typing import Any

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Integration fixture — skipped automatically if FinancialReports not installed
# ─────────────────────────────────────────────────────────────────────────────
sqlite_store_mod = pytest.importorskip(
    "src.storage.sqlite_store",
    reason="FinancialReports editable install not found; skipping integration fixtures",
)
SQLiteStore = sqlite_store_mod.SQLiteStore

domain_identity_mod = pytest.importorskip(
    "src.domain.identity",
    reason="FinancialReports editable install not found",
)
FilingIdentity = domain_identity_mod.FilingIdentity

def _insert_filing(store: Any, stock_code: str, name_zh: str, year: int, quarter: str) -> int:
    """Insert company + filing, return filing_id."""
    company_id = store.upsert_company(stock_code, name_zh=name_zh)
    identity = FilingIdentity(stock_code=stock_code, year=year, quarter=quarter)
    return store.upsert_filing(identity, company_id)


def _insert_fact(
    store: Any,
    filing_id: int,
    field: str,
    value: float,
    source_type: str,
    confidence: float,
) -> None:
    """Insert a fact row directly via SQL (bypasses Fact model's filing_key requirement)."""
    from sqlalchemy import text
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
    from sqlalchemy import text
    with store.conn() as c:
        c.execute(
            text("UPDATE filings SET quality_score=:s, status='completed' WHERE id=:id"),
            {"s": score, "id": filing_id},
        )


@pytest.fixture(scope="session")
def fixture_store(tmp_path_factory: pytest.TempPathFactory):
    """
    Session-scoped SQLiteStore populated with 3 filing cases.
    tmp_path_factory gives a directory that persists for the whole test session.
    """
    db_path = tmp_path_factory.mktemp("fr_fixture") / "test_financial.db"
    store = SQLiteStore(str(db_path))

    # ── Case A: 2330_2024Q1 — XBRL, confidence=1.0, quality_score=0.95 ──────
    fid_a = _insert_filing(store, "2330", "台積電", 2024, "Q1")
    for field, value in [
        ("net_revenue", 625_763_000.0),
        ("gross_profit", 265_457_000.0),
        ("operating_income", 249_519_000.0),
        ("net_income", 225_491_000.0),
        ("eps_basic", 8.70),
        ("total_assets", 6_580_023_000.0),
        ("total_liabilities", 2_790_000_000.0),
        ("equity", 3_790_023_000.0),
        ("operating_cash_flow", 381_232_000.0),
        ("current_assets", 1_200_000_000.0),
        ("current_liabilities", 800_000_000.0),
    ]:
        _insert_fact(store, fid_a, field, value, "xbrl", 1.0)
    _set_quality_score(store, fid_a, 0.95)

    # ── Case B: 3711_2024Q1 — pdf_table, confidence=0.75, quality_score=0.62 ─
    fid_b = _insert_filing(store, "3711", "日月光投控", 2024, "Q1")
    for field, value in [
        ("net_revenue", 58_200_000.0),
        ("gross_profit", 9_800_000.0),
        ("operating_income", 5_100_000.0),
        ("net_income", 3_700_000.0),
        ("eps_basic", 1.30),
        ("total_assets", 420_000_000.0),
        ("total_liabilities", 280_000_000.0),
        ("equity", 140_000_000.0),
    ]:
        _insert_fact(store, fid_b, field, value, "pdf_table", 0.75)
    _set_quality_score(store, fid_b, 0.62)

    # ── Case C: 2454_2024Q1 — restated, confidence=0.80, quality_score=0.78 ──
    fid_c = _insert_filing(store, "2454", "聯發科", 2024, "Q1")
    for field, value in [
        ("net_revenue", 46_798_000.0),
        ("gross_profit", 21_800_000.0),
        ("operating_income", 10_100_000.0),
        ("net_income", 14_300_000.0),
        ("eps_basic", 9.10),
        ("total_assets", 310_000_000.0),
        ("total_liabilities", 110_000_000.0),
        ("equity", 200_000_000.0),
        ("operating_cash_flow", 12_000_000.0),
    ]:
        _insert_fact(store, fid_c, field, value, "restated", 0.80)
    _set_quality_score(store, fid_c, 0.78)

    return store
