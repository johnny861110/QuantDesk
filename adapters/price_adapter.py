"""
Price (OHLCV) adapter — Yahoo Finance implementation via yfinance.

Interface
---------
YFinancePriceAdapter.fetch(symbol, period, interval) → SourcedData(payload=OHLCVData, ...)

The payload is an OHLCVData dataclass containing numpy arrays (close, high, low,
open_, volume) and a parallel list of datetime objects (dates), all sorted
chronologically oldest → newest.

tech-debt
---------
- Yahoo Finance data has 15-min+ delay for equities and is not tick-precise.
  Replace with a broker or Bloomberg feed for production.
- Column-level MultiIndex handling covers newer yfinance versions; tested against
  yfinance ≥ 0.2.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from adapters.base import PriceAdapter, SourcedData


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
