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
    """
    所有資料源 adapter 的抽象基類。

    Substitutability contract
    -------------------------
    fetch(**kwargs) 是最小化的介面簽名，目的是讓 container/registry 能持有
    DataSourceAdapter 型別的參照（dependency inversion），不是 Liskov 意義上的
    完全多型替換。

    具體實作（如 FinMindOptionsAdapter）應定義具名參數簽名以保留靜態型別安全；
    此時 mypy 會回報 [override] 錯誤，應加 # type: ignore[override] 並附上說明。

    為何不改成 **kwargs？
    採用 **kwargs 看似能消除 mypy 錯誤，但會把「必要參數不完整」
    從編譯期錯誤推遲到 runtime KeyError，實際上降低了安全性。
    更重要的是：不同 adapter 的必要參數本來就完全不同
    （FinMind 需要 stock_id/as_of/spot_price；
      未來券商 API 可能只需要 symbol 和帳號 token），
    換實作時呼叫端本來就必須修改，**kwargs 只是假裝可以不改。

    因此本框架選擇：
    - 具名參數 + type: ignore[override]（呼叫端有靜態型別保護）
    - 而非 **kwargs（呼叫端失去靜態型別保護，換來虛假的替換性）
    """

    @property
    @abstractmethod
    def source_name(self) -> str:
        ...

    @abstractmethod
    def fetch(self, **kwargs: Any) -> SourcedData:
        """
        回傳帶 source + asof 的標準化資料。

        子類應以具名參數覆寫此方法以獲得靜態型別安全；
        覆寫時加上 # type: ignore[override]，並說明具體參數意義。
        詳見類別 docstring 中的 Substitutability contract 說明。
        """
        ...


# 各 domain 的 adapter 介面（Phase 1-4 各自實作）
class PriceAdapter(DataSourceAdapter):
    """行情：yfinance / 券商API / 公開行情。"""


class OptionsAdapter(DataSourceAdapter):
    """
    選擇權鏈與 IV：券商API (TXO) / yfinance options chain。Greeks 引擎的輸入。

    Substitutability note
    ---------------------
    具體實作的 fetch() 參數因資料源而異，呼叫端換實作時必須同步更新。
    FinMindOptionsAdapter: fetch(stock_id, as_of, spot_price, r, q)
    未來國泰期貨 API 實作: 參數待定（可能只需要 symbol 和帳號設定）
    換實作屬於 DI 替換，不要求 call site 零修改。
    """


class FundamentalAdapter(DataSourceAdapter):
    """財報結構化數字：FinancialReports 的 SQLite/pg。"""


class NewsAdapter(DataSourceAdapter):
    """財經新聞：NewsAPI / RSS / 公開資訊觀測站。"""


class MacroAdapter(DataSourceAdapter):
    """總經數據：Trading Economics API / Investing 日曆。要含 consensus 值做 surprise 計算。"""


class CrossMarketAdapter(DataSourceAdapter):
    """跨市場：yfinance（美股指數）/ 台指期源。"""


class FXAdapter(DataSourceAdapter):
    """外匯匯率：yfinance / 中央銀行公告 / 券商報價。"""
