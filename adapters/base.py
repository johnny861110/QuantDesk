"""
資料源抽象層。所有外部資料存取都走這裡，agent 內不得直接呼叫外部 API。

核心目的：機構級來源（Bloomberg/Reuters）與可負擔替代源（yfinance/RSS/公開資料）
可互換，不影響上層 agent。未來若拿到機構 feed，只換 adapter，不動 agent。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class SourcedData(BaseModel):
    """所有 adapter 回傳的標準包裝：帶來源與時間戳。"""
    payload: Any
    source: str
    asof: datetime


class DataSourceAdapter(ABC):
    """所有資料源 adapter 的抽象基類。"""

    @property
    @abstractmethod
    def source_name(self) -> str:
        ...

    @abstractmethod
    def fetch(self, **kwargs: Any) -> SourcedData:
        """回傳帶 source + asof 的標準化資料。"""
        ...


# 各 domain 的 adapter 介面（Phase 1-4 各自實作）
class PriceAdapter(DataSourceAdapter):
    """行情：yfinance / 券商API / 公開行情。"""


class OptionsAdapter(DataSourceAdapter):
    """選擇權鏈與 IV：券商API (TXO) / yfinance options chain。Greeks 引擎的輸入。"""


class FundamentalAdapter(DataSourceAdapter):
    """財報結構化數字：FinancialReports 的 SQLite/pg。"""


class NewsAdapter(DataSourceAdapter):
    """財經新聞：NewsAPI / RSS / 公開資訊觀測站。"""


class MacroAdapter(DataSourceAdapter):
    """總經數據：Trading Economics API / Investing 日曆。要含 consensus 值做 surprise 計算。"""


class CrossMarketAdapter(DataSourceAdapter):
    """跨市場：yfinance（美股指數）/ 台指期源。"""
