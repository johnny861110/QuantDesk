"""
FRED (Federal Reserve Economic Data) Adapter — 替換 TradingEconomics

資料源：美聯儲聖路易分部免費 CSV 端點，不需 API Key。
端點：https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}

涵蓋指標
--------
PAYEMS    美國非農就業人數（千人，月頻率）
CPIAUCSL  美國消費者物價指數（月頻率）
UNRATE    美國失業率（%，月頻率）
DGS10     美國 10Y 國債殖利率（%，日頻率）
FEDFUNDS  聯邦基金有效利率（%，月頻率）

補充：FinMind 台灣資料（需 FINMIND_KEY）
InterestRate          台灣央行利率
TaiwanExchangeRate    USD/TWD 匯率
GovernmentBondsYield  台灣公債殖利率

回傳介面
--------
與 TradingEconomicsAdapter 相同：SourcedData(payload=MacroResult)
MacroResult.events = list[MacroEvent]，每個 MacroEvent 帶
  category, country, actual, previous, unit, importance, release_date

向後相容
--------
本模組新增 FREDAdapter 類別，同時保留 MacroEvent / MacroResult dataclass
（與 macro_adapter.py 共用定義，直接 import 而非重新定義）。
"""
from __future__ import annotations

import csv
import io
import os
from datetime import UTC, date, datetime, timedelta
from typing import Any

from adapters.base import MacroAdapter, SourcedData
from adapters.macro_adapter import MacroEvent, MacroResult

# ─── FRED 系列代碼地圖 ────────────────────────────────────────────────────────

FRED_BASE_URL: str = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_TIMEOUT_SEC: int = 20

# category_label → (series_id, unit, importance, frequency)
# category 名稱刻意對齊 agents/macro_agent.py 的 CATEGORY_DIRECTION 表，
# 使 compute_macro_score() 能正確辨識 surprise 方向。
FRED_SERIES: dict[str, tuple[str, str, int, str]] = {
    "Non Farm Payrolls": ("PAYEMS",   "K",  3, "monthly"),
    "CPI":               ("CPIAUCSL", "%",  3, "monthly"),
    "Unemployment Rate": ("UNRATE",   "%",  3, "monthly"),
    "Fed Funds Rate":    ("FEDFUNDS", "%",  3, "monthly"),
    "US 10Y Treasury Yield": ("DGS10", "%", 2, "daily"),    # 無對應 CATEGORY_DIRECTION，importance=2
}

# ─── FinMind 台灣資料地圖 ─────────────────────────────────────────────────────

FINMIND_API_URL: str = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TIMEOUT_SEC: int = 30

# category_label → (dataset, data_id, unit, importance)
FINMIND_TW_SERIES: dict[str, tuple[str, str, str, int]] = {
    "TW Central Bank Rate":  ("InterestRate",        "central_bank_rate", "%", 3),
    "USD/TWD Exchange Rate": ("TaiwanExchangeRate",  "USD",               "TWD", 2),
}


# ─── FREDAdapter ─────────────────────────────────────────────────────────────


class FREDAdapter(MacroAdapter):
    """
    美聯儲 FRED 免費資料 Adapter。

    不需 API Key。資料以日頻或月頻 CSV 形式從 FRED 網站下載。
    _fetch_fred_csv() 是唯一的 network I/O 點，測試可 monkeypatch。

    Usage
    -----
        adapter = FREDAdapter()
        data    = adapter.fetch(series=["US CPI", "US 10Y Treasury Yield"])
        events  = data.payload.events   # list[MacroEvent]
    """

    def __init__(self, finmind_token: str = "") -> None:
        self._finmind_token = finmind_token or os.environ.get("FINMIND_KEY", "")

    @property
    def source_name(self) -> str:  # type: ignore[override]
        return "fred_free"

    def fetch(  # type: ignore[override]
        self,
        series: list[str] | None = None,
        include_taiwan: bool = True,
        periods: int = 3,
        **kwargs: Any,
    ) -> SourcedData:
        """
        取 FRED 總經指標（+ 可選 FinMind 台灣補充）。

        Parameters
        ----------
        series         : 要拉的 FRED 指標 label list，None = 全部預設指標
        include_taiwan : 是否也從 FinMind 拉台灣央行利率 / 匯率
        periods        : 回溯期數（月頻 = 3 個月，日頻 = 最近 3 個有效點）

        Returns
        -------
        SourcedData, payload = MacroResult
        """
        fetched_at = datetime.now(UTC)
        # 預設只拉有 consensus 意義的 4 個指標（排除 10Y 殖利率，它沒有 CATEGORY_DIRECTION 對應）
        default_series = [s for s in FRED_SERIES if s != "US 10Y Treasury Yield"]
        target_series = series or default_series

        all_events: list[MacroEvent] = []

        # FRED 指標
        for label in target_series:
            if label not in FRED_SERIES:
                continue
            series_id, unit, importance, freq = FRED_SERIES[label]
            rows = self._fetch_fred_csv(series_id, periods=periods)
            events = _parse_fred_rows(rows, label, "United States", unit, importance, series_id)
            all_events.extend(events)

        # FinMind 台灣補充
        if include_taiwan and self._finmind_token:
            tw_events = self._fetch_taiwan_series(periods=periods)
            all_events.extend(tw_events)

        payload = MacroResult(
            events=all_events,
            countries=["United States", "Taiwan"] if include_taiwan else ["United States"],
            fetched_at=fetched_at,
        )
        return SourcedData(payload=payload, source=self.source_name, asof=fetched_at)

    # ── FRED network layer ─────────────────────────────────────────────────────

    def _fetch_fred_csv(
        self,
        series_id: str,
        periods: int = 3,
    ) -> list[dict[str, str]]:
        """
        Download FRED CSV and return last *periods* rows as dicts.
        Columns: {"DATE": "YYYY-MM-DD", series_id: "value or ."}

        Tests: subclass or monkeypatch this method.
        """
        import requests  # lazy import

        # 拉最近 2 年資料，然後取最後 N 個有效點
        resp = requests.get(FRED_BASE_URL, params={"id": series_id}, timeout=FRED_TIMEOUT_SEC)
        resp.raise_for_status()

        reader = csv.DictReader(io.StringIO(resp.text))
        rows = [r for r in reader if r.get(series_id, ".") != "."]   # "." = missing

        # 最後 N 筆（最近期）
        return rows[-max(periods, 1):]

    # ── FinMind Taiwan supplemental ───────────────────────────────────────────

    def _fetch_taiwan_series(self, periods: int = 3) -> list[MacroEvent]:
        """
        從 FinMind 拉台灣央行利率 / 匯率（只在有 FINMIND_KEY 時執行）。
        Returns list[MacroEvent] or [] on failure.
        """
        import requests  # lazy import

        end_dt = date.today()
        start_dt = end_dt - timedelta(days=365)

        events: list[MacroEvent] = []
        for label, (dataset, data_id, unit, importance) in FINMIND_TW_SERIES.items():
            try:
                params: dict[str, Any] = {
                    "dataset":    dataset,
                    "data_id":    data_id,
                    "start_date": start_dt.isoformat(),
                    "end_date":   end_dt.isoformat(),
                    "token":      self._finmind_token,
                }
                resp = requests.get(FINMIND_API_URL, params=params, timeout=FINMIND_TIMEOUT_SEC)
                resp.raise_for_status()
                data = resp.json()
                rows = data.get("data", []) if data.get("status") == 200 else []
                if not rows:
                    continue

                # 最後 N 筆
                recent = sorted(rows, key=lambda r: str(r.get("date", "")))[-periods:]
                for row in recent:
                    try:
                        actual = float(str(row.get("value", row.get("close", 0))))
                        dt_str = str(row.get("date", ""))
                        dt = datetime.fromisoformat(dt_str) if dt_str else datetime.now(UTC)
                        events.append(MacroEvent(
                            category=label,
                            country="Taiwan",
                            actual=actual,
                            consensus=None,
                            previous=None,
                            unit=unit,
                            importance=importance,
                            release_date=dt,
                            source_name="finmind",
                        ))
                    except (ValueError, TypeError):
                        continue
            except Exception:  # noqa: BLE001
                # 任何錯誤（timeout, 無 key 等）靜默跳過，不影響 FRED 資料
                continue

        return events


# ─── Pure helpers ─────────────────────────────────────────────────────────────


def _parse_fred_rows(
    rows: list[dict[str, str]],
    category: str,
    country: str,
    unit: str,
    importance: int,
    series_id: str,
) -> list[MacroEvent]:
    """
    Convert FRED CSV rows → MacroEvent list.

    Pure function — no network I/O.

    FRED CSV format: {"DATE": "YYYY-MM-DD", <series_id>: "<float value>"}
    Missing/future values appear as "." and are already filtered by _fetch_fred_csv.
    """
    events: list[MacroEvent] = []
    prev_value: float | None = None

    for row in rows:
        try:
            actual = float(row[series_id])
            date_str = str(row.get("DATE", ""))
            release_date = datetime.fromisoformat(date_str) if date_str else datetime.now(UTC)
        except (KeyError, ValueError, TypeError):
            continue

        events.append(MacroEvent(
            category=category,
            country=country,
            actual=actual,
            consensus=None,           # FRED 不提供共識預測
            previous=prev_value,
            unit=unit,
            importance=importance,
            release_date=release_date,
            source_name="fred",
        ))
        prev_value = actual

    return events
