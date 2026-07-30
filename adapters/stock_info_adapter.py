"""
Stock Info Adapter — FinMind 全市場股票代碼/名稱清單，供 UI 標的搜尋使用。

用途
----
Dashboard 側欄的單一 agent 搜尋框需要把使用者輸入的公司名稱或代碼解析成
正確的 stock_id，這支 adapter 提供全市場（TWSE + TPEx）股票清單，讓
api/main.py 的 /api/symbols/search 端點做前綴／子字串比對。取代原本
「查詢框沒有 4 位數代碼就默默 fallback 成寫死的 2330」的行為。

設計原則
--------
- _fetch_raw() 是唯一 network I/O 點，測試可 monkeypatch（與 chip_adapter 一致）
- 全市場清單一天內幾乎不變，用 adapters.cache.ttl_cached 快取 24 小時
- api_token 若未傳入，自動從 FINMIND_KEY 環境變數讀取（與 chip_adapter 一致）
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests

from adapters.cache import ttl_cached

FINMIND_API_URL: str = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TIMEOUT_SEC: int = 30
STOCK_INFO_TTL_SEC: int = 24 * 60 * 60  # 24h


@dataclass(frozen=True)
class StockInfo:
    stock_id: str
    stock_name: str
    industry_category: str
    type: str  # "twse" | "tpex"


def _fetch_raw(token: str) -> list[dict[str, Any]]:
    """Call FinMind TaiwanStockInfo dataset. Sole network I/O point."""
    params = {"dataset": "TaiwanStockInfo", "token": token}
    resp = requests.get(FINMIND_API_URL, params=params, timeout=FINMIND_TIMEOUT_SEC)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return list(data)


@ttl_cached(ttl=STOCK_INFO_TTL_SEC, maxsize=4)
def _fetch_all_cached(token: str) -> list[StockInfo]:
    rows = _fetch_raw(token)
    # FinMind lists some stock_ids under more than one industry_category —
    # keep the first row per stock_id so search results don't show dupes.
    seen: set[str] = set()
    result: list[StockInfo] = []
    for row in rows:
        stock_id = str(row.get("stock_id", ""))
        if not stock_id or stock_id in seen:
            continue
        seen.add(stock_id)
        result.append(
            StockInfo(
                stock_id=stock_id,
                stock_name=str(row.get("stock_name", "")),
                industry_category=str(row.get("industry_category", "")),
                type=str(row.get("type", "")),
            )
        )
    return result


class StockInfoAdapter:
    """FinMind TaiwanStockInfo — 全市場股票代碼/名稱清單搜尋。"""

    def __init__(self, api_token: str = "") -> None:
        self._token = api_token or os.environ.get("FINMIND_KEY", "")

    def fetch_all(self) -> list[StockInfo]:
        """回傳全市場股票清單（快取 24 小時）。"""
        return _fetch_all_cached(self._token)

    def search(self, query: str, limit: int = 20) -> list[StockInfo]:
        """
        依代碼前綴或名稱子字串（不分大小寫）搜尋股票。

        代碼前綴命中排在名稱子字串命中之前，代碼本身再依字典序排序，
        讓「2330」這類精確代碼查詢穩定排在最前面。
        """
        q = query.strip().lower()
        if not q:
            return []
        matches = [
            s for s in self.fetch_all()
            if q in s.stock_id.lower() or q in s.stock_name.lower()
        ]
        matches.sort(key=lambda s: (not s.stock_id.lower().startswith(q), s.stock_id))
        return matches[:limit]
