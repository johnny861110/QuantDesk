"""
Position loader — reads config/positions.yaml → list[Position].

Design
------
Position is the single source-of-truth for "what we own" before any
pricing or Greeks computation.  The loader validates schema, fills sensible
defaults, and collects errors per-row rather than raising so callers can
decide whether to abort or skip bad rows.

Instrument treatment
--------------------
stock   : degenerate option — delta = ±1 per quantity sign, gamma/vega/theta = 0.
futures : same degenerate treatment as stock (linear delta exposure).
option  : full Greeks via pricing_router.price_option(); requires all option fields.

tech-debt: positions.yaml is temporary.  When a broker API (e.g. 國泰期貨程式化 API)
           is available, replace the YAML source with a PositionAdapter that implements
           the same load_positions() interface.  No upper-layer code should change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml  # pyyaml

# Default path is repo-root / config / positions.yaml
DEFAULT_POSITIONS_PATH = (
    Path(__file__).parent.parent.parent / "config" / "positions.yaml"
)

# ─── Allowed values ───────────────────────────────────────────────────────────

VALID_INSTRUMENT_TYPES = {"stock", "futures", "option"}
VALID_OPTION_TYPES     = {"call", "put"}
VALID_STYLES           = {"european", "american"}
VALID_CURRENCIES       = {"TWD", "USD", "EUR", "JPY"}


# ─── Position dataclass ───────────────────────────────────────────────────────

@dataclass
class Position:
    """
    One position in the portfolio.

    Common fields (all instrument types)
    -------------------------------------
    symbol          : ticker / contract code
    instrument_type : "stock" | "futures" | "option"
    quantity        : positive = long, negative = short
                      (shares for stock; contracts for futures/options)
    currency        : settlement currency
    multiplier      : notional value per 1-unit move in the underlying
                      (stock = 1.0; TXO = 50; TXFF = 200)
    entry_price     : fill price; used for P&L attribution, not Greeks

    Option-specific fields (None for stock/futures)
    -----------------------------------------------
    strike      : option strike price
    expiry      : ISO date string "YYYY-MM-DD"
    option_type : "call" | "put"
    style       : "european" | "american"

    Validation
    ----------
    errors : list of human-readable validation error strings.
             Empty → position is valid; non-empty → skip or alert.
    """
    symbol: str
    instrument_type: str
    quantity: float
    currency: str = "TWD"
    multiplier: float = 1.0
    entry_price: float | None = None

    # Option-only fields — None for stock/futures
    strike: float | None = None
    expiry: str | None = None       # "YYYY-MM-DD"
    option_type: str | None = None  # "call" | "put"
    style: str | None = None        # "european" | "american"

    errors: list[str] = field(default_factory=list)

    # ── Convenience properties ─────────────────────────────────────────────────

    @property
    def is_option(self) -> bool:
        return self.instrument_type == "option"

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    @property
    def expiry_date(self) -> date | None:
        """Return expiry as a date object, or None if not set / not parseable."""
        if self.expiry is None:
            return None
        try:
            return date.fromisoformat(self.expiry)
        except ValueError:
            return None


# ─── Portfolio config ─────────────────────────────────────────────────────────

@dataclass
class PortfolioConfig:
    """
    Full contents of positions.yaml: positions list + portfolio-level settings.

    portfolio_nav  : NAV in nav_currency, used as denominator for
                     net_delta_pct_nav hard constraint in aggregation.py.
    nav_currency   : currency of portfolio_nav (default TWD).
    """
    positions: list[Position]
    portfolio_nav: float
    nav_currency: str = "TWD"


# ─── Public API ───────────────────────────────────────────────────────────────

def load_portfolio(path: Path = DEFAULT_POSITIONS_PATH) -> PortfolioConfig:
    """
    Parse positions.yaml → PortfolioConfig (positions + portfolio_nav).

    Raises
    ------
    FileNotFoundError  : path does not exist
    yaml.YAMLError     : file is not valid YAML
    KeyError           : top-level key "positions" missing
    """
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    rows: list[dict[str, Any]] = raw["positions"]
    positions = [_parse_row(row, idx) for idx, row in enumerate(rows)]

    nav_section  = raw.get("portfolio_nav") or {}
    portfolio_nav = float(nav_section.get("value", 0.0))
    nav_currency  = str(nav_section.get("currency", "TWD"))

    return PortfolioConfig(
        positions=positions,
        portfolio_nav=portfolio_nav,
        nav_currency=nav_currency,
    )


def load_positions(path: Path = DEFAULT_POSITIONS_PATH) -> list[Position]:
    """
    Parse positions.yaml → list[Position].  Backward-compatible wrapper.

    Validation errors per row are stored in Position.errors rather than raised,
    so callers can filter:

        valid = [p for p in load_positions() if p.is_valid]

    Raises
    ------
    FileNotFoundError  : path does not exist
    yaml.YAMLError     : file is not valid YAML
    KeyError           : top-level key "positions" missing
    """
    return load_portfolio(path).positions


# ─── Private helpers ──────────────────────────────────────────────────────────

def _parse_row(row: dict[str, Any], idx: int) -> Position:
    """Parse and validate one YAML row into a Position."""
    errors: list[str] = []

    # Required fields — present in all instrument types
    symbol          = _require_str(row, "symbol", idx, errors)
    instrument_type = _require_str(row, "instrument_type", idx, errors)
    quantity        = _require_number(row, "quantity", idx, errors)

    if instrument_type and instrument_type not in VALID_INSTRUMENT_TYPES:
        errors.append(
            f"[{idx}] instrument_type={instrument_type!r} "
            f"must be one of {sorted(VALID_INSTRUMENT_TYPES)}"
        )

    # Optional common fields
    currency = str(row.get("currency", "TWD"))
    if currency not in VALID_CURRENCIES:
        errors.append(
            f"[{idx}] currency={currency!r} must be one of {sorted(VALID_CURRENCIES)}"
        )

    multiplier   = float(row.get("multiplier", 1.0))
    entry_raw    = row.get("entry_price")
    entry_price  = float(entry_raw) if entry_raw is not None else None

    # Option-specific fields
    strike: float | None      = None
    expiry: str | None        = None
    option_type: str | None   = None
    style: str | None         = None

    if instrument_type == "option":
        strike_raw = row.get("strike")
        if strike_raw is None:
            errors.append(f"[{idx}] option is missing required field: strike")
        else:
            strike = float(strike_raw)

        expiry_raw = row.get("expiry")
        if expiry_raw is None:
            errors.append(f"[{idx}] option is missing required field: expiry")
        else:
            expiry = str(expiry_raw)
            try:
                parsed_expiry = date.fromisoformat(expiry)
                # T ≤ 0 guard: expired options produce degenerate or zero Greeks.
                # Catch this at load time rather than letting it silently flow
                # into the BS formula.  On the expiry date itself T = 0 (same-day
                # settlement) which is also degenerate — flag it.
                if parsed_expiry <= date.today():
                    errors.append(
                        f"[{idx}] option expiry {expiry!r} is today or in the past "
                        "(T ≤ 0) — remove or roll this position"
                    )
            except ValueError:
                errors.append(
                    f"[{idx}] expiry={expiry!r} is not a valid ISO date (YYYY-MM-DD)"
                )

        option_type = row.get("option_type")
        if option_type is None:
            errors.append(f"[{idx}] option is missing required field: option_type")
        elif option_type not in VALID_OPTION_TYPES:
            errors.append(
                f"[{idx}] option_type={option_type!r} "
                f"must be one of {sorted(VALID_OPTION_TYPES)}"
            )

        style = row.get("style")
        if style is None:
            errors.append(f"[{idx}] option is missing required field: style")
        elif style not in VALID_STYLES:
            errors.append(
                f"[{idx}] style={style!r} must be one of {sorted(VALID_STYLES)}"
            )

    elif instrument_type in ("stock", "futures"):
        _warn_unexpected_option_fields(row, instrument_type, idx, errors)

    return Position(
        symbol          = symbol or "",
        instrument_type = instrument_type or "",
        quantity        = quantity if quantity is not None else 0.0,
        currency        = currency,
        multiplier      = multiplier,
        entry_price     = entry_price,
        strike          = strike,
        expiry          = expiry,
        option_type     = option_type,
        style           = style,
        errors          = errors,
    )


def _require_str(
    row: dict[str, Any], key: str, idx: int, errors: list[str]
) -> str | None:
    val = row.get(key)
    if val is None:
        errors.append(f"[{idx}] missing required field: {key}")
        return None
    return str(val)


def _require_number(
    row: dict[str, Any], key: str, idx: int, errors: list[str]
) -> float | None:
    val = row.get(key)
    if val is None:
        errors.append(f"[{idx}] missing required field: {key}")
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        errors.append(f"[{idx}] {key}={val!r} is not a valid number")
        return None


def _warn_unexpected_option_fields(
    row: dict[str, Any], instrument_type: str, idx: int, errors: list[str]
) -> None:
    """Warn if option-specific keys appear on a stock/futures row."""
    option_keys = {"strike", "expiry", "option_type", "style"}
    present = option_keys.intersection(row.keys())
    if present:
        errors.append(
            f"[{idx}] {instrument_type} position has unexpected option fields: "
            f"{sorted(present)} — did you mean instrument_type: option?"
        )
