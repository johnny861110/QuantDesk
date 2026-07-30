"""Tests for adapters/stock_info_adapter.py — Taiwan stock symbol search.

Regression coverage for a UI bug: single-agent runs from the dashboard
sidebar had no real way to specify a target symbol and silently fell back to
a hardcoded "2330", so any agent click analyzed TSMC regardless of what the
user actually wanted. This adapter backs a real code/name lookup instead.
"""
from __future__ import annotations

from typing import Any

import pytest

import adapters.stock_info_adapter as sia
from adapters.stock_info_adapter import StockInfoAdapter

_ROWS: list[dict[str, Any]] = [
    {"stock_id": "2330", "stock_name": "台積電", "industry_category": "半導體", "type": "twse"},
    {"stock_id": "2317", "stock_name": "鴻海", "industry_category": "電子", "type": "twse"},
    {"stock_id": "2454", "stock_name": "聯發科", "industry_category": "半導體", "type": "twse"},
    {"stock_id": "3231", "stock_name": "緯創", "industry_category": "電子", "type": "twse"},
]


def _patch_fetch_raw(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]] = _ROWS
) -> list[str]:
    calls: list[str] = []

    def fake_fetch_raw(token: str) -> list[dict[str, Any]]:
        calls.append(token)
        return rows

    monkeypatch.setattr(sia, "_fetch_raw", fake_fetch_raw)
    return calls


def test_search_by_code_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch_raw(monkeypatch)
    adapter = StockInfoAdapter(api_token="test-token-1")
    results = adapter.search("233")
    assert [r.stock_id for r in results] == ["2330"]


def test_search_by_name_substring(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch_raw(monkeypatch)
    adapter = StockInfoAdapter(api_token="test-token-2")
    results = adapter.search("鴻海")
    assert [r.stock_id for r in results] == ["2317"]


def test_search_matches_by_code_returns_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch_raw(monkeypatch)
    adapter = StockInfoAdapter(api_token="test-token-3")
    results = adapter.search("2330")
    assert results[0].stock_name == "台積電"


def test_search_empty_query_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch_raw(monkeypatch)
    adapter = StockInfoAdapter(api_token="test-token-4")
    assert adapter.search("   ") == []


def test_search_respects_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch_raw(monkeypatch)
    adapter = StockInfoAdapter(api_token="test-token-5")
    results = adapter.search("2", limit=1)
    assert len(results) == 1


def test_exact_code_prefix_sorted_before_substring_only_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch_raw(monkeypatch)
    adapter = StockInfoAdapter(api_token="test-token-6")
    results = adapter.search("23")
    ids = [r.stock_id for r in results]
    assert ids[:2] == sorted(["2330", "2317"])
    assert "3231" in ids[2:]


def test_fetch_all_is_cached_per_token(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_fetch_raw(monkeypatch)
    adapter = StockInfoAdapter(api_token="test-token-7")
    adapter.fetch_all()
    adapter.fetch_all()
    assert calls == ["test-token-7"]


def test_fetch_all_deduplicates_stock_id_listed_under_multiple_industries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FinMind's real TaiwanStockInfo dataset lists some stock_ids twice, once
    # per industry_category classification — verified against the live API.
    dup_rows = [
        {"stock_id": "2330", "stock_name": "台積電", "industry_category": "半導體業", "type": "twse"},
        {"stock_id": "2330", "stock_name": "台積電", "industry_category": "電子工業", "type": "twse"},
    ]
    _patch_fetch_raw(monkeypatch, rows=dup_rows)
    adapter = StockInfoAdapter(api_token="test-token-8")
    results = adapter.fetch_all()
    assert [r.stock_id for r in results] == ["2330"]
