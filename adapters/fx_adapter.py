"""
FX rate adapter — Yahoo Finance implementation via yfinance.

Interface
---------
YFinanceFXAdapter.fetch(pair="USDTWD") → SourcedData(payload=FXRate, ...)

The payload is an FXRate dataclass (pair + rate).  Downstream callers
(aggregation.py) read payload.rate and snapshot the source+asof for
provenance tracking.

tech-debt
---------
- Yahoo Finance FX data has a 15-min+ delay and is not tick-precise.
  Replace with a Bloomberg / broker rate feed when available.
- Only USDTWD is currently tested; other pairs follow the same =X suffix
  convention but are untested.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from adapters.base import FXAdapter, SourcedData

# Yahoo Finance appends this suffix to currency pair tickers
_YF_FX_SUFFIX: str = "=X"

# Look back 5 days to tolerate weekends / holidays with no session
_YF_HISTORY_PERIOD: str = "5d"


@dataclass
class FXRate:
    """Spot FX rate for one currency pair."""
    pair: str    # e.g. "USDTWD" — base/quote
    rate: float  # units of quote currency per 1 unit of base currency


class YFinanceFXAdapter(FXAdapter):
    """
    FX rate adapter backed by Yahoo Finance (yfinance).

    Fetches the most recent daily close for the requested currency pair.
    yfinance is imported lazily so the rest of the codebase does not need
    it installed unless a real network call is made.

    Usage
    -----
        adapter = YFinanceFXAdapter()
        data    = adapter.fetch(pair="USDTWD")
        rate    = data.payload.rate   # e.g. 32.5
        asof    = data.asof           # datetime of the last session close
    """

    @property
    def source_name(self) -> str:
        return "yfinance_fx"

    def fetch(self, pair: str = "USDTWD", **kwargs: Any) -> SourcedData:
        """
        Fetch the most recent daily close for *pair*.

        Parameters
        ----------
        pair : "{base}{quote}" without a separator, e.g. "USDTWD", "EURUSD".
               The Yahoo Finance ticker is formed as f"{pair}=X".

        Returns
        -------
        SourcedData with:
            payload : FXRate(pair, rate)
            source  : "yfinance_fx"
            asof    : datetime of the last session whose close was used
        """
        import yfinance as yf  # lazy — only needed for real calls

        ticker_symbol = f"{pair}{_YF_FX_SUFFIX}"
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period=_YF_HISTORY_PERIOD)

        if hist.empty:
            raise ValueError(
                f"yfinance returned no data for {ticker_symbol!r}. "
                "Check that the pair is valid and the market is open."
            )

        rate = float(hist["Close"].iloc[-1])
        asof = hist.index[-1].to_pydatetime()
        if asof.tzinfo is not None:
            asof = asof.replace(tzinfo=None)   # strip tz for consistent datetime handling

        return SourcedData(
            payload=FXRate(pair=pair, rate=rate),
            source=self.source_name,
            asof=asof,
        )
