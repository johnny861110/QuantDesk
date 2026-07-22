"""
Macro (總經) Adapter — Trading Economics API

資料源：Trading Economics REST API (https://api.tradingeconomics.com)
  免費額度：每月 ~100 requests；正式環境需付費方案。
  API 金鑰從環境變數 TE_API_KEY 讀取（搭配 python-dotenv）。
  找不到 key 時明確 raise RuntimeError，不靜默繼續。

Confidence tier：
  來源單一，所有事件都來自 TE API，confidence_tier 固定 = 1（機構彙整數據）。
  consensus 欄位對應 TE API 的 "Forecast"（市場經濟學家預測中位數）。

Design
------
_fetch_raw() 是唯一的 network I/O 點，測試可 monkeypatch 覆蓋。
_parse_te_events() 是純函數，負責欄位對映與型別轉換。

TE API Endpoint
---------------
GET https://api.tradingeconomics.com/calendar/country/{countries}
Params: c={api_key}, d1=YYYY-MM-DD, d2=YYYY-MM-DD, importance={1,2,3}
Response: JSON array with fields below.

Key TE fields → MacroEvent fields:
  Date         → release_date    # 數據公布時間（ISO string）
  Country      → country
  Category     → category        # e.g. "Inflation Rate", "GDP Growth Rate"
  Actual       → actual          # null until data is released
  Forecast     → consensus       # 市場共識預測（可為 null）
  Previous     → previous        # 前值
  Unit         → unit            # "%", "K", "B" 等
  Importance   → importance      # 1–3（3 最重要）

Usage
-----
    from dotenv import load_dotenv
    load_dotenv()
    adapter = TradingEconomicsAdapter()
    data    = adapter.fetch(countries=["united states", "taiwan"], days_back=7)
    events  = data.payload.events  # list[MacroEvent]
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from adapters.base import MacroAdapter, SourcedData

# ─── API constants ────────────────────────────────────────────────────────────

TE_API_BASE: str = "https://api.tradingeconomics.com"
TE_CALENDAR_PATH: str = "/calendar/country/{countries}"
TE_TIMEOUT_SEC: int = 15

# Default countries to monitor for macro events
DEFAULT_COUNTRIES: list[str] = ["united states", "taiwan"]

# Minimum importance level to include (1=low, 2=medium, 3=high)
# Free tier sometimes limits to importance≥2
DEFAULT_MIN_IMPORTANCE: int = 2


# ─── Data structures ──────────────────────────────────────────────────────────


@dataclass
class MacroEvent:
    """
    一個總經數據公布事件的標準化表示。

    surprise_direction 由 macro_agent 計算（不在 adapter 層決定），
    adapter 只負責原始數值的忠實對映。

    consensus 對應 TE API 的 "Forecast"（市場預測中位數）。
    actual 在數據尚未公布時為 None；只有 actual 和 consensus 都非 None
    才能計算 surprise。
    """
    category: str            # e.g. "Inflation Rate", "GDP Growth Rate"
    country: str             # e.g. "United States", "Taiwan"
    actual: float | None     # 實際公布值（未公布則 None）
    consensus: float | None  # 市場共識預測（可為 None）
    previous: float | None   # 前期值
    unit: str                # "%", "K", "B" 等
    importance: int          # 1-3，3 最重要
    release_date: datetime   # 數據公布日期時間
    source_name: str         # e.g. "trading_economics"


@dataclass
class MacroResult:
    """Payload returned by all MacroAdapter implementations."""
    events: list[MacroEvent] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ─── Trading Economics Adapter ────────────────────────────────────────────────


class TradingEconomicsAdapter(MacroAdapter):
    """
    Trading Economics REST API adapter。

    API 金鑰從環境變數 TE_API_KEY 讀取。
    _fetch_raw() 是唯一的 network I/O 點，測試可 monkeypatch 覆蓋。

    Usage
    -----
        from dotenv import load_dotenv
        load_dotenv()
        adapter = TradingEconomicsAdapter()
        data    = adapter.fetch(countries=["united states", "taiwan"], days_back=7)
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key  # None → read from env at call time

    @property
    def source_name(self) -> str:  # type: ignore[override]
        return "trading_economics"

    def fetch(  # type: ignore[override]
        self,
        countries: list[str] | None = None,
        days_back: int = 7,
        min_importance: int = DEFAULT_MIN_IMPORTANCE,
        **kwargs: Any,
    ) -> SourcedData:
        """
        Fetch macro calendar events for the given countries.

        Parameters
        ----------
        countries     : Country names accepted by TE API (lowercase).
                        Defaults to ["united states", "taiwan"].
        days_back     : Look-back window in calendar days.
        min_importance: Minimum importance level (1–3) to include.
        """
        key = self._api_key or _require_te_key()
        country_list = countries or DEFAULT_COUNTRIES
        fetched_at = datetime.now(UTC)

        end_dt = fetched_at
        start_dt = end_dt - timedelta(days=days_back)

        raw = self._fetch_raw(country_list, start_dt, end_dt, key)
        events = _parse_te_events(raw, min_importance=min_importance)

        payload = MacroResult(
            events=events,
            countries=country_list,
            fetched_at=fetched_at,
        )
        return SourcedData(payload=payload, source=self.source_name, asof=fetched_at)

    def _fetch_raw(
        self,
        countries: list[str],
        start_date: datetime,
        end_date: datetime,
        api_key: str,
    ) -> list[dict[str, Any]]:
        """
        GET Trading Economics calendar API and return raw JSON list.

        This is the ONLY method performing network I/O.
        Override or monkeypatch in tests.
        """
        import requests  # lazy import — network only

        countries_str = "/".join(c.lower().replace(" ", "%20") for c in countries)
        url = f"{TE_API_BASE}/calendar/country/{countries_str}"
        params: dict[str, Any] = {
            "c":  api_key,
            "d1": start_date.strftime("%Y-%m-%d"),
            "d2": end_date.strftime("%Y-%m-%d"),
        }
        resp = requests.get(url, params=params, timeout=TE_TIMEOUT_SEC)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data  # type: ignore[no-any-return]
        return []


# ─── Pure parsing helpers ─────────────────────────────────────────────────────


def _parse_te_events(
    raw: list[dict[str, Any]],
    min_importance: int = DEFAULT_MIN_IMPORTANCE,
) -> list[MacroEvent]:
    """
    Convert raw TE API calendar rows → MacroEvent list.

    Only rows with importance ≥ min_importance AND actual value present
    are kept.  Events without consensus (Forecast) are kept but will be
    skipped by the surprise computation layer.

    Pure function — no network I/O.
    """
    events: list[MacroEvent] = []
    for row in raw:
        try:
            importance = int(row.get("Importance") or 0)
        except (ValueError, TypeError):
            importance = 0
        if importance < min_importance:
            continue

        actual = _parse_float(row.get("Actual"))
        # Skip rows where actual hasn't been released yet
        if actual is None:
            continue

        consensus = _parse_float(row.get("Forecast"))
        previous = _parse_float(row.get("Previous"))

        date_str: str = str(row.get("Date") or "")
        release_date = _parse_te_date(date_str)
        if release_date is None:
            continue

        events.append(
            MacroEvent(
                category=str(row.get("Category") or "").strip(),
                country=str(row.get("Country") or "").strip(),
                actual=actual,
                consensus=consensus,
                previous=previous,
                unit=str(row.get("Unit") or "").strip(),
                importance=importance,
                release_date=release_date,
                source_name="trading_economics",
            )
        )
    return events


def _parse_float(value: Any) -> float | None:
    """Convert TE API value (string, int, float, or None) to float."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_te_date(date_str: str) -> datetime | None:
    """Parse TE API date string (ISO-8601 variants) to naive datetime."""
    if not date_str:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(date_str[:len(fmt) - 2 + 4], fmt)  # rough slice
        except ValueError:
            pass
    # Fallback: try isoformat parser
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


# ─── Environment key helper ───────────────────────────────────────────────────


def _require_te_key() -> str:
    """
    Read TE_API_KEY from environment.  Raises RuntimeError if missing or empty.
    Callers should load .env via python-dotenv before invoking the adapter.
    """
    value = os.environ.get("TE_API_KEY", "")
    if not value:
        raise RuntimeError(
            "環境變數 'TE_API_KEY' 未設定或為空字串。\n"
            "請在 .env 檔案中設定 Trading Economics API Key，"
            "並在程式入口用 python-dotenv 載入：\n"
            "  from dotenv import load_dotenv; load_dotenv()\n"
            "免費額度申請：https://developer.tradingeconomics.com\n"
            "不得將 API 金鑰硬寫在程式碼中。"
        )
    return value
