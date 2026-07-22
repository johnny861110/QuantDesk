"""
Cross-market adapter — Yahoo Finance implementation via yfinance.

Interface
---------
YFinanceCrossMarketAdapter.fetch(symbols, period, interval) → SourcedData(payload=CrossMarketData, ...)

The payload is a CrossMarketData dataclass containing aligned close-price and
log-return arrays for a set of market indices.  Downstream callers
(cross_market_agent.py) use the arrays for pure rolling-correlation and
lead-lag analysis.

Design
------
- _align_series() is a pure helper (unit-testable, no I/O) that finds the date
  intersection across all symbols and returns aligned numpy arrays.
- yfinance is imported lazily so tests that use FakeCrossMarketAdapter do not
  trigger network calls.
- All datetimes are timezone-stripped before storage (consistent with other
  adapters in this codebase).

tech-debt
---------
- Replace YFinanceCrossMarketAdapter with a Bloomberg / broker feed when
  available; only this module changes, the agent is unaffected.
- yfinance data has a 15-min+ delay and is not tick-precise.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from adapters.base import CrossMarketAdapter, SourcedData

# ─── Default symbol set ───────────────────────────────────────────────────────

DEFAULT_SYMBOLS: dict[str, str] = {
    "^TWII": "TAIEX",
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq",
}


# ─── Payload dataclass ────────────────────────────────────────────────────────

@dataclass
class CrossMarketData:
    """Aligned multi-market close prices and log returns."""

    symbols: list[str]                  # ordered, e.g. ["^TWII", "^GSPC", "^IXIC"]
    labels:  dict[str, str]             # symbol → display name
    close:   dict[str, np.ndarray]      # symbol → aligned close (oldest→newest)
    returns_: dict[str, np.ndarray]     # symbol → log returns (len = n_dates - 1)
    dates:   list[datetime]             # n_dates aligned dates (intersection of all series)


# ─── Pure alignment helper ────────────────────────────────────────────────────

def _align_series(
    raw: dict[str, dict[datetime, float]],
) -> tuple[list[datetime], dict[str, np.ndarray]]:
    """
    Intersect dates across all symbols and return (sorted_dates, aligned_close_dict).

    Parameters
    ----------
    raw : {symbol: {date: close_price}}

    Returns
    -------
    (sorted_dates, {symbol: np.ndarray of aligned close prices})
    Dates are sorted ascending (oldest first).
    """
    if not raw:
        return [], {}

    # Find intersection of all date sets
    date_sets = [set(dates_dict.keys()) for dates_dict in raw.values()]
    common: set[datetime] = date_sets[0]
    for s in date_sets[1:]:
        common &= s

    sorted_dates = sorted(common)

    aligned: dict[str, np.ndarray] = {}
    for sym, date_map in raw.items():
        aligned[sym] = np.array([date_map[d] for d in sorted_dates], dtype=np.float64)

    return sorted_dates, aligned


# ─── YFinance implementation ──────────────────────────────────────────────────

class YFinanceCrossMarketAdapter(CrossMarketAdapter):
    """
    Cross-market adapter backed by Yahoo Finance (yfinance).

    Fetches daily close prices for a set of market index symbols, aligns them
    on their common trading dates, and computes log returns.

    Usage
    -----
        adapter = YFinanceCrossMarketAdapter()
        sourced = adapter.fetch(period="6mo")
        data    = sourced.payload   # CrossMarketData
        tw_ret  = data.returns_["^TWII"]
        us_ret  = data.returns_["^GSPC"]
    """

    @property
    def source_name(self) -> str:
        return "yfinance_cross_market"

    def fetch(  # type: ignore[override]
        self,
        symbols: dict[str, str] | None = None,
        period: str = "6mo",
        interval: str = "1d",
        **kwargs: Any,
    ) -> SourcedData:
        """
        Fetch aligned close prices and log returns for market index symbols.

        Parameters
        ----------
        symbols  : {ticker: label} mapping.  Defaults to DEFAULT_SYMBOLS.
        period   : yfinance history period string, e.g. "6mo", "1y".
        interval : yfinance interval string, e.g. "1d", "1wk".

        Returns
        -------
        SourcedData with:
            payload : CrossMarketData
            source  : "yfinance_cross_market"
            asof    : datetime of the last aligned date
        """
        import yfinance as yf  # type: ignore[import-untyped]

        sym_map: dict[str, str] = symbols if symbols is not None else DEFAULT_SYMBOLS

        # Fetch raw data for each symbol
        raw: dict[str, dict[datetime, float]] = {}
        for sym in sym_map:
            ticker = yf.Ticker(sym)
            hist = ticker.history(period=period, interval=interval)

            if hist.empty:
                raise ValueError(f"no data for {sym!r}")

            # Handle MultiIndex columns (yfinance ≥0.2 can return MultiIndex)
            if isinstance(hist.columns, type(hist.columns)) and hasattr(hist.columns, "levels"):
                # Flatten MultiIndex if present
                if hasattr(hist.columns, "get_level_values"):
                    try:
                        close_col = hist["Close"]
                        if hasattr(close_col, "columns"):
                            # MultiIndex: take first column
                            close_series = close_col.iloc[:, 0]
                        else:
                            close_series = close_col
                    except KeyError:
                        close_series = hist.iloc[:, 0]
                else:
                    close_series = hist["Close"]
            else:
                close_series = hist["Close"]

            # Strip timezone from index
            date_map: dict[datetime, float] = {}
            for idx, val in close_series.items():
                dt = idx
                if hasattr(dt, "to_pydatetime"):
                    dt = dt.to_pydatetime()
                if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                date_map[dt] = float(val)

            raw[sym] = date_map

        # Align on common dates
        sorted_dates, aligned_close = _align_series(raw)

        if not sorted_dates:
            raise ValueError(
                f"no common trading dates found across symbols: {list(sym_map.keys())}"
            )

        # Compute log returns for each symbol
        returns_: dict[str, np.ndarray] = {}
        for sym, close_arr in aligned_close.items():
            returns_[sym] = np.log(close_arr[1:] / close_arr[:-1])

        asof = sorted_dates[-1]

        payload = CrossMarketData(
            symbols=list(sym_map.keys()),
            labels=dict(sym_map),
            close=aligned_close,
            returns_=returns_,
            dates=sorted_dates,
        )

        return SourcedData(
            payload=payload,
            source=self.source_name,
            asof=asof,
        )
