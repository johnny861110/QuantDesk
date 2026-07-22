"""
Price (OHLCV) adapters — FinMind (台股優先) + Yahoo Finance (美股後備).

Implementations
---------------
FinMindPriceAdapter   台股日線 — FinMind TaiwanStockPrice（優先）
YFinancePriceAdapter  通用日線 — Yahoo Finance yfinance（美股 / 非台股）

Both return SourcedData(payload=OHLCVData, ...).
OHLCVData holds numpy arrays (close, high, low, open_, volume) and a parallel
list of datetime objects (dates), all sorted chronologically oldest → newest.

Design
------
FinMindPriceAdapter follows the same _fetch_raw / _parse_rows split as
FinMindOptionsAdapter: _fetch_raw() is the only method that performs network
I/O and can be subclassed or monkeypatched in tests.

tech-debt
---------
- FinMind free tier is rate-limited; replace with broker API when available.
- YFinancePriceAdapter has 15-min+ delay; not for production tick data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from adapters.base import PriceAdapter, SourcedData

# ─── FinMind constants (shared with options_adapter) ─────────────────────────

FINMIND_API_URL: str = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TIMEOUT_SEC: int = 30
FINMIND_PRICE_DATASET: str = "TaiwanStockPrice"

# Period string → calendar days look-back
_PERIOD_DAYS: dict[str, int] = {
    "1mo":  30,
    "3mo":  90,
    "6mo": 180,
    "1y":  365,
    "2y":  730,
}


@dataclass
class OHLCVData:
    """Standardised OHLCV container for one symbol over a date range."""

    symbol: str
    market: str
    close: np.ndarray    # shape (N,), float64, chronological oldest → newest
    high: np.ndarray
    low: np.ndarray
    open_: np.ndarray    # 'open' is a Python built-in; renamed open_
    volume: np.ndarray
    dates: list[datetime]  # len N, timezone-stripped


class YFinancePriceAdapter(PriceAdapter):
    """
    OHLCV adapter backed by Yahoo Finance (yfinance).

    Fetches daily bars and returns them as an OHLCVData payload.
    yfinance is imported lazily so unit tests that do not need network access
    can import this module without triggering a network call.

    Usage
    -----
        adapter = YFinancePriceAdapter()
        data    = adapter.fetch(symbol="2330.TW", period="6mo")
        ohlcv   = data.payload          # OHLCVData
        close   = ohlcv.close           # np.ndarray, newest value at [-1]
        asof    = data.asof             # datetime of the most-recent bar
    """

    @property
    def source_name(self) -> str:  # type: ignore[override]
        return "yfinance_price"

    def fetch(  # type: ignore[override]
        self,
        symbol: str,
        period: str = "6mo",
        interval: str = "1d",
        **kwargs: Any,
    ) -> SourcedData:
        """
        Fetch OHLCV bars for *symbol*.

        Parameters
        ----------
        symbol   : Ticker symbol recognised by Yahoo Finance, e.g. "2330.TW", "AAPL".
        period   : History window, e.g. "6mo", "1y", "2y".
        interval : Bar size, e.g. "1d", "1wk".

        Returns
        -------
        SourcedData with:
            payload : OHLCVData
            source  : "yfinance_price"
            asof    : datetime of the last bar (timezone-stripped)

        Raises
        ------
        ValueError : if yfinance returns an empty DataFrame.
        """
        import yfinance as yf  # type: ignore[import-untyped]  # no stubs for yfinance

        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period, interval=interval)

        if hist.empty:
            raise ValueError(
                f"yfinance returned no data for {symbol!r}. "
                "Check that the ticker is valid and the market is open."
            )

        # yfinance >= 0.2 may return a MultiIndex DataFrame when multiple tickers
        # are fetched via yf.download(); single-ticker .history() normally returns
        # flat columns, but guard both cases.
        if isinstance(hist.columns, __import__("pandas").MultiIndex):
            # MultiIndex: level 0 = field, level 1 = symbol (ticker)
            # Normalise symbol capitalisation used by yfinance (e.g. "2330.TW")
            try:
                hist = hist.xs(symbol, axis=1, level=1)
            except KeyError:
                # Fall back: try with first level-1 value present
                first_ticker = hist.columns.get_level_values(1)[0]
                hist = hist.xs(first_ticker, axis=1, level=1)

        # Column names from yfinance .history() are capitalised: Open, High, Low, Close, Volume
        close  = hist["Close"].to_numpy(dtype=np.float64)
        high   = hist["High"].to_numpy(dtype=np.float64)
        low    = hist["Low"].to_numpy(dtype=np.float64)
        open_  = hist["Open"].to_numpy(dtype=np.float64)
        volume = hist["Volume"].to_numpy(dtype=np.float64)

        # Convert DatetimeIndex to timezone-stripped datetime list
        dates: list[datetime] = []
        for ts in hist.index:
            dt = ts.to_pydatetime()
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            dates.append(dt)

        asof = dates[-1]  # most-recent bar timestamp

        payload = OHLCVData(
            symbol=symbol,
            market=kwargs.get("market", ""),
            close=close,
            high=high,
            low=low,
            open_=open_,
            volume=volume,
            dates=dates,
        )

        return SourcedData(
            payload=payload,
            source=self.source_name,
            asof=asof,
        )


class FinMindPriceAdapter(PriceAdapter):
    """
    台股日線行情 adapter — FinMind TaiwanStockPrice。

    台股優先選擇此 adapter（symbol 含 .TW / .TWO 結尾，或純四位數字代碼）。
    FinMind 欄位對照：
        max             → high
        min             → low
        open            → open_
        close           → close
        Trading_Volume  → volume

    Design: _fetch_raw() isolates network I/O so tests can subclass and
    override it without touching the real FinMind API — same pattern as
    FinMindOptionsAdapter.

    Usage
    -----
        adapter = FinMindPriceAdapter(api_token="YOUR_TOKEN")
        data    = adapter.fetch(symbol="2330.TW", period="6mo")
        ohlcv   = data.payload   # OHLCVData
        close   = ohlcv.close    # np.ndarray, newest value at [-1]
    """

    def __init__(self, api_token: str = "") -> None:
        self._token = api_token

    @property
    def source_name(self) -> str:  # type: ignore[override]
        return "finmind_taiwan_stock_price"

    def fetch(  # type: ignore[override]
        self,
        symbol: str,
        period: str = "6mo",
        interval: str = "1d",
        **kwargs: Any,
    ) -> SourcedData:
        """
        Fetch OHLCV bars for *symbol* from FinMind TaiwanStockPrice.

        Parameters
        ----------
        symbol   : Taiwan ticker with or without suffix, e.g. "2330.TW" or "2330".
        period   : History window — "1mo", "3mo", "6mo" (default), "1y", "2y".
                   Converted to (start_date, end_date) internally.
        interval : Accepted but ignored — FinMind only provides daily bars.

        Returns
        -------
        SourcedData with:
            payload : OHLCVData (arrays sorted oldest → newest)
            source  : "finmind_taiwan_stock_price"
            asof    : datetime of the last available bar

        Raises
        ------
        ValueError : if FinMind returns no rows for the requested period.
        requests.HTTPError : on non-2xx HTTP response.
        """
        stock_id = _strip_tw_suffix(symbol)
        end_dt   = date.today()
        days     = _PERIOD_DAYS.get(period, 180)
        start_dt = end_dt - timedelta(days=days)

        rows = self._fetch_raw(stock_id, start_dt, end_dt)
        if not rows:
            raise ValueError(
                f"FinMind TaiwanStockPrice returned no data for {stock_id!r} "
                f"({start_dt} → {end_dt}).  Check the stock_id and date range."
            )

        payload = _parse_tw_price_rows(rows, symbol)
        return SourcedData(
            payload=payload,
            source=self.source_name,
            asof=payload.dates[-1],
        )

    def _fetch_raw(
        self,
        stock_id: str,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        """
        Call FinMind TaiwanStockPrice and return raw row list.

        This is the **only** method that performs network I/O.
        Override or monkeypatch this in tests instead of hitting the real API.
        """
        import requests  # lazy import — only needed for real network calls

        params: dict[str, Any] = {
            "dataset":    FINMIND_PRICE_DATASET,
            "data_id":    stock_id,
            "start_date": start_date.isoformat(),
            "end_date":   end_date.isoformat(),
            "token":      self._token,
        }
        resp = requests.get(FINMIND_API_URL, params=params, timeout=FINMIND_TIMEOUT_SEC)
        resp.raise_for_status()
        return resp.json().get("data", [])  # type: ignore[no-any-return]


# ─── Pure helpers (unit-testable, no I/O) ────────────────────────────────────

def _strip_tw_suffix(symbol: str) -> str:
    """Remove .TW / .TWO suffix to obtain FinMind stock_id."""
    for suffix in (".TWO", ".TW"):
        if symbol.upper().endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol


def _parse_tw_price_rows(rows: list[dict[str, Any]], original_symbol: str) -> OHLCVData:
    """
    Convert raw FinMind TaiwanStockPrice rows → OHLCVData.

    Pure function — no network I/O.  Input rows must be non-empty.
    FinMind field names: date, open, max (high), min (low), close, Trading_Volume.
    Rows are sorted ascending by date (FinMind usually delivers in order, but
    we sort explicitly for safety).
    """
    rows_sorted = sorted(rows, key=lambda r: r["date"])

    dates:  list[datetime] = [datetime.fromisoformat(r["date"]) for r in rows_sorted]
    close  = np.array([float(r["close"])           for r in rows_sorted], dtype=np.float64)
    high   = np.array([float(r["max"])             for r in rows_sorted], dtype=np.float64)
    low    = np.array([float(r["min"])             for r in rows_sorted], dtype=np.float64)
    open_  = np.array([float(r["open"])            for r in rows_sorted], dtype=np.float64)
    volume = np.array([float(r["Trading_Volume"])  for r in rows_sorted], dtype=np.float64)

    return OHLCVData(
        symbol=original_symbol,
        market="TW",
        close=close,
        high=high,
        low=low,
        open_=open_,
        volume=volume,
        dates=dates,
    )
