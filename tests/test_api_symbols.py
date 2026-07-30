"""Tests for GET /api/symbols/search."""
from __future__ import annotations

import httpx
import pytest

from adapters.stock_info_adapter import StockInfo, StockInfoAdapter
from api.main import app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_search_symbols_returns_matches(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_search(self: StockInfoAdapter, query: str, limit: int = 20) -> list[StockInfo]:
        return [StockInfo(stock_id="2330", stock_name="台積電", industry_category="半導體", type="twse")]

    monkeypatch.setattr(StockInfoAdapter, "search", fake_search)

    resp = await client.get("/api/symbols/search", params={"q": "台積電"})

    assert resp.status_code == 200
    assert resp.json() == [{"symbol": "2330", "name": "台積電"}]


async def test_search_symbols_empty_query_returns_empty_list(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/api/symbols/search", params={"q": "   "})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_search_symbols_adapter_failure_returns_empty_list(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_search(self: StockInfoAdapter, query: str, limit: int = 20) -> list[StockInfo]:
        raise RuntimeError("FinMind unavailable")

    monkeypatch.setattr(StockInfoAdapter, "search", fake_search)

    resp = await client.get("/api/symbols/search", params={"q": "2330"})

    assert resp.status_code == 200
    assert resp.json() == []
