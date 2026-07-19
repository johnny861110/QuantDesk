"""
FinMind options adapter — fetches TXO trade prices and backs out IV.

Data flow
---------
1. Call FinMind TaiwanOptionDaily API → list of option trade rows.
2. For each row: compute T = (expiry - as_of) / 365 (calendar days).
3. Call price_option_with_iv_solve(trade_price, spec) → GreeksResult.
4. Tag iv_source = IV_SOURCE_BACKED_OUT or IV_SOURCE_UNAVAILABLE.
5. Return SourcedData(payload=list[OptionRecord], ...).

IV degradation policy
---------------------
Backing-out IV is best-effort because FinMind trade prices can be stale,
zero-volume, or outside no-arbitrage bounds.  Failures are collected in
OptionRecord.errors and OptionRecord.iv is set to NaN.  Downstream
(aggregation.py) checks math.isnan(record.iv) to detect missing IV and
reports "無法評估凸性風險" rather than silently propagating zero.

Separation of concerns
-----------------------
_fetch_raw()      : HTTP call to FinMind — override or mock in tests.
_parse_records()  : pure IV computation + record building — unit-testable.

tech-debt
---------
- Replace _fetch_raw() with broker API (國泰期貨) when available;
  only this method changes, upper layers are unaffected.
- TXO settlement calendar uses a pure-math third-Wednesday approximation;
  actual exchange holidays are not accounted for.
- Spot price (S) must be supplied by the caller from a PriceAdapter —
  FinMind option data does not include the underlying price.
- iv_source="finmind_backed_out" must stay visible to downstream so risk
  reports can flag that this is not a directly quoted IV.
"""
from __future__ import annotations

import math
from calendar import WEDNESDAY, monthcalendar
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from adapters.base import OptionsAdapter, SourcedData
from agents.risk.pricing_router import (
    DEFAULT_DIVIDEND_YIELD,
    DEFAULT_RISK_FREE_RATE,
    GreeksResult,
    OptionSpec,
    price_option_with_iv_solve,
)

# ─── Named constants ──────────────────────────────────────────────────────────

# IV source tags — propagated to downstream risk reports for traceability
IV_SOURCE_BACKED_OUT  = "finmind_backed_out"   # IV inferred from trade price
IV_SOURCE_UNAVAILABLE = "iv_unavailable"        # no usable trade price / solve failed

# TXO (台指選擇權) is European-style (settled on cash basis, no early exercise)
OPTION_STYLE_TXO: str = "european"

# FinMind TaiwanOptionDaily dataset identifier
FINMIND_DATASET: str = "TaiwanOptionDaily"
FINMIND_API_URL: str = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TIMEOUT_SEC: int = 30


# ─── Output type ──────────────────────────────────────────────────────────────

@dataclass
class OptionRecord:
    """
    One option contract row after FinMind fetch + IV backing-out.

    Fields
    ------
    symbol      : underlying symbol (e.g. "TXO")
    strike      : strike price
    expiry      : settlement date (third Wednesday of the expiry month)
    option_type : "call" | "put"
    style       : "european" | "american"
    trade_price : last trade price from FinMind (raw, before IV solve)
    iv          : backed-out implied volatility (decimal), or NaN if unavailable
    iv_source   : IV_SOURCE_BACKED_OUT | IV_SOURCE_UNAVAILABLE
    greeks      : full Greeks at solved IV; all zeros if IV unavailable
    errors      : list of human-readable error messages (empty = success)
    """
    symbol: str
    strike: float
    expiry: date
    option_type: str
    style: str
    trade_price: float
    iv: float
    iv_source: str
    greeks: GreeksResult
    errors: list[str] = field(default_factory=list)

    @property
    def iv_available(self) -> bool:
        return not math.isnan(self.iv)


# ─── Adapter ──────────────────────────────────────────────────────────────────

class FinMindOptionsAdapter(OptionsAdapter):
    """
    OptionsAdapter backed by FinMind TaiwanOptionDaily.

    Parameters
    ----------
    api_token : FinMind API token (required for real fetch; pass "" in tests
                that mock _fetch_raw).
    """

    def __init__(self, api_token: str) -> None:
        self._token = api_token

    @property
    def source_name(self) -> str:
        return "finmind_taiwan_option_daily"

    def fetch(
        self,
        stock_id: str,
        as_of: date,
        spot_price: float,
        r: float = DEFAULT_RISK_FREE_RATE,
        q: float = DEFAULT_DIVIDEND_YIELD,
    ) -> SourcedData:
        """
        Fetch option chain for stock_id on as_of date and back out IVs.

        Parameters
        ----------
        stock_id    : FinMind data_id (e.g. "TXO" for Taiwan index options)
        as_of       : pricing date — only rows from this date are used
        spot_price  : current underlying price (S); caller must supply this
                      from a separate PriceAdapter fetch
        r, q        : risk-free rate and continuous dividend yield (decimals)

        Returns
        -------
        SourcedData with payload: list[OptionRecord]
            Consumers should check record.iv_available before using IV/Greeks.
        """
        raw_rows = self._fetch_raw(stock_id, as_of)
        records = self._parse_records(raw_rows, spot_price, as_of, r, q)
        return SourcedData(
            payload=records,
            source=self.source_name,
            asof=datetime.combine(as_of, datetime.min.time()),
        )

    # ── Internal: HTTP layer (override in tests) ───────────────────────────────

    def _fetch_raw(self, stock_id: str, as_of: date) -> list[dict[str, Any]]:
        """
        Call FinMind TaiwanOptionDaily and return raw row list.

        This method is the only one that performs network I/O.
        Tests should subclass and override this method (or monkeypatch it)
        rather than hitting the real network.

        tech-debt: replace with broker API endpoint when available.
        """
        import requests  # lazy import — only needed for real network calls

        params = {
            "dataset": FINMIND_DATASET,
            "data_id": stock_id,
            "start_date": as_of.isoformat(),
            "end_date": as_of.isoformat(),
            "token": self._token,
        }
        resp = requests.get(FINMIND_API_URL, params=params, timeout=FINMIND_TIMEOUT_SEC)
        resp.raise_for_status()
        return resp.json().get("data", [])

    # ── Internal: IV computation layer (unit-testable) ─────────────────────────

    def _parse_records(
        self,
        raw_rows: list[dict[str, Any]],
        spot_price: float,
        as_of: date,
        r: float,
        q: float,
    ) -> list[OptionRecord]:
        """
        Convert raw FinMind rows → OptionRecord list with backed-out IVs.

        Rows that cannot be parsed (malformed, expired, unknown call/put)
        are silently skipped — callers should check len(records) against
        expected volume to detect silent drops.
        """
        records: list[OptionRecord] = []
        for row in raw_rows:
            record = self._process_row(row, spot_price, as_of, r, q)
            if record is not None:
                records.append(record)
        return records

    def _process_row(
        self,
        row: dict[str, Any],
        spot_price: float,
        as_of: date,
        r: float,
        q: float,
    ) -> OptionRecord | None:
        """
        Parse one FinMind row and back out IV.

        Returns None if the row should be skipped (expired, unknown type,
        malformed field).  Returns OptionRecord with errors if IV solve failed.
        """
        try:
            strike = float(row["strike_price"])

            # FinMind contract_date format: "YYYY-MM" (expiry month)
            contract_date: str = str(row["contract_date"])
            yr  = int(contract_date[:4])
            mo  = int(contract_date[5:7])
            expiry_date = _third_wednesday(yr, mo)

            call_put_raw = str(row.get("call_put", ""))
            if call_put_raw == "買權":
                option_type = "call"
            elif call_put_raw == "賣權":
                option_type = "put"
            else:
                return None   # unknown option type — skip row

            trade_price = float(row.get("close", 0.0) or 0.0)
            symbol      = str(row.get("stock_id", ""))

        except (KeyError, ValueError, TypeError):
            return None   # malformed row — skip silently

        # Expired or expiring today — skip
        days_to_expiry = (expiry_date - as_of).days
        if days_to_expiry <= 0:
            return None

        T     = days_to_expiry / 365.0   # calendar days, matching bs_theta convention
        style = OPTION_STYLE_TXO

        spec = OptionSpec(
            S=spot_price, K=strike, T=T, r=r, q=q,
            sigma=0.20,                   # initial guess for IV solver
            option_type=option_type,
            style=style,
            spot_currency="TWD",          # TXO is TWD-quoted; caller must supply TWD spot
        )

        # ── IV backing-out ─────────────────────────────────────────────────────

        if trade_price <= 0.0:
            # No trade recorded — cannot back out IV
            err = "trade_price=0: no trades available to back out IV"
            return OptionRecord(
                symbol=symbol, strike=strike, expiry=expiry_date,
                option_type=option_type, style=style,
                trade_price=trade_price,
                iv=float("nan"), iv_source=IV_SOURCE_UNAVAILABLE,
                greeks=_zero_greeks(trade_price, error=err),
                errors=[err],
            )

        greeks = price_option_with_iv_solve(trade_price, spec)

        if greeks.errors:
            # IV solve failed (e.g. price outside no-arbitrage bounds)
            return OptionRecord(
                symbol=symbol, strike=strike, expiry=expiry_date,
                option_type=option_type, style=style,
                trade_price=trade_price,
                iv=float("nan"), iv_source=IV_SOURCE_UNAVAILABLE,
                greeks=greeks,
                errors=greeks.errors,
            )

        return OptionRecord(
            symbol=symbol, strike=strike, expiry=expiry_date,
            option_type=option_type, style=style,
            trade_price=trade_price,
            iv=greeks.iv, iv_source=IV_SOURCE_BACKED_OUT,
            greeks=greeks,
            errors=[],
        )


# ─── Settlement calendar helper ───────────────────────────────────────────────

def _third_wednesday(year: int, month: int) -> date:
    """
    Return the third Wednesday of (year, month).

    TXO (台指選擇權) settles on the third Wednesday of the expiry month.
    This is a pure calendar calculation; it does NOT account for exchange
    holidays (tech-debt: use TWSE holiday calendar for precise settlement dates).

    Example: _third_wednesday(2025, 3) → date(2025, 3, 19)
    """
    cal = monthcalendar(year, month)
    wednesdays = [week[WEDNESDAY] for week in cal if week[WEDNESDAY] != 0]
    return date(year, month, wednesdays[2])   # index 2 = third occurrence


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _zero_greeks(market_price: float, error: str) -> GreeksResult:
    """Return an all-zero GreeksResult with the given error — used for IV-unavailable records."""
    return GreeksResult(
        price=market_price,
        delta=0.0, gamma=0.0, vega=0.0, theta=0.0, rho=0.0,
        model="iv_solve_failed",
        iv=float("nan"),
        errors=[error],
    )
