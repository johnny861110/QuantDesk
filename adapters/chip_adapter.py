"""
Chip (籌碼) Adapter — FinMind 台股籌碼資料整合

資料集對應
----------
fetch_institutional()     TaiwanStockInstitutionalInvestorsBuySell  三大法人買賣超
fetch_margin()            TaiwanStockMarginPurchaseShortSale         融資融券餘額
fetch_shareholding()      TaiwanStockShareholding                    外資持股比例
fetch_futures_inst()      TaiwanFuturesInstitutionalInvestors        期貨三大法人部位

設計原則
--------
- _fetch_raw() 是唯一 network I/O 點，測試可 subclass/monkeypatch
- 所有輸出欄位都是 Python 原生型別（dict / list），不依賴 pandas
- api_token 若未傳入，自動從 FINMIND_KEY 環境變數讀取

使用方式
--------
    adapter = ChipDataAdapter()                    # 自動讀 FINMIND_KEY
    inst    = adapter.fetch_institutional("2330", days=10)
    margin  = adapter.fetch_margin("2330", days=10)
    share   = adapter.fetch_shareholding("2330")
    futures = adapter.fetch_futures_inst("TXF")
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from adapters.base import DataSourceAdapter, SourcedData

# ─── FinMind constants ────────────────────────────────────────────────────────

FINMIND_API_URL: str = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TIMEOUT_SEC: int = 30


# ─── Output data classes ──────────────────────────────────────────────────────


@dataclass
class InstitutionalFlow:
    """
    一日三大法人買賣超資料（單一標的）。

    單位：shares = 股數，amount = 新台幣元（或千元，依 FinMind 欄位）。
    buy_sell 正值 = 淨買超，負值 = 淨賣超。
    """
    date: date
    foreign_buy_sell: float          # 外資買賣超（股數）
    foreign_amount: float            # 外資買賣超金額（元）
    investment_trust_buy_sell: float  # 投信買賣超（股數）
    investment_trust_amount: float
    dealer_buy_sell: float            # 自營商買賣超（股數）
    dealer_amount: float
    total_buy_sell: float             # 三大法人合計（股數）


@dataclass
class InstitutionalResult:
    """fetch_institutional() 的回傳 payload。"""
    symbol: str
    records: list[InstitutionalFlow] = field(default_factory=list)
    # 便利統計（N 日合計）
    foreign_net_shares: float = 0.0      # 外資 N 日合計買賣超（股數）
    foreign_net_amount: float = 0.0      # 外資 N 日合計買賣超金額
    trust_net_shares: float = 0.0
    dealer_net_shares: float = 0.0
    total_net_shares: float = 0.0
    consecutive_foreign_buy: int = 0     # 連續買超天數（正=買，負=賣）


@dataclass
class MarginRecord:
    """一日融資融券資料。"""
    date: date
    margin_purchase: float      # 融資餘額（千股）
    margin_redemption: float    # 融資減少（千股）
    margin_balance: float       # 融資餘額（千股）
    short_sale: float           # 融券賣出（千股）
    short_cover: float          # 融券買進（千股）
    short_balance: float        # 融券餘額（千股）
    offset: float               # 資券相抵（千股）


@dataclass
class MarginResult:
    """fetch_margin() 的回傳 payload。"""
    symbol: str
    records: list[MarginRecord] = field(default_factory=list)
    # 便利統計（最新一日）
    margin_balance: float = 0.0       # 最新融資餘額（千股）
    short_balance: float = 0.0        # 最新融券餘額（千股）
    margin_change_5d: float = 0.0     # 5 日融資變化（千股）
    short_change_5d: float = 0.0      # 5 日融券變化（千股）
    margin_ratio: float = 0.0         # 融資使用率（需搭配可融資張數，先置 0）


@dataclass
class ShareholdingRecord:
    """外資持股比例一日資料。"""
    date: date
    foreign_ownership_ratio: float    # 外資持股比例（%）
    foreign_share_count: float        # 外資持股股數
    listed_shares: float              # 上市股數


@dataclass
class ShareholdingResult:
    """fetch_shareholding() 的回傳 payload。"""
    symbol: str
    records: list[ShareholdingRecord] = field(default_factory=list)
    latest_ratio: float = 0.0         # 最新外資持股比例（%）
    change_30d: float = 0.0           # 近 30 日持股比例變化（ppt）


@dataclass
class FuturesInstRecord:
    """期貨三大法人一日部位。"""
    date: date
    name: str                         # e.g. "自營商", "投信", "外資及陸資"
    long_open_interest: float         # 多方未平倉口數
    short_open_interest: float        # 空方未平倉口數
    net_open_interest: float          # 淨未平倉口數（正=偏多）


@dataclass
class FuturesInstResult:
    """fetch_futures_inst() 的回傳 payload。"""
    symbol: str
    records: list[FuturesInstRecord] = field(default_factory=list)
    # 便利：最新一日法人合計淨多空
    foreign_net_position: float = 0.0   # 外資淨部位（口）
    trust_net_position: float = 0.0
    dealer_net_position: float = 0.0


# ─── Adapter ──────────────────────────────────────────────────────────────────


class ChipDataAdapter(DataSourceAdapter):
    """
    台股籌碼分析 Adapter。

    整合 FinMind 四個籌碼相關資料集，對外提供四個 fetch_* 方法。
    所有 fetch_* 方法都回傳 SourcedData，payload 為對應的 Result dataclass。

    Parameters
    ----------
    api_token : FinMind API token。
                空字串（預設）→ 自動從 FINMIND_KEY 環境變數讀取。
    """

    def __init__(self, api_token: str = "") -> None:
        self._token = api_token or os.environ.get("FINMIND_KEY", "")

    @property
    def source_name(self) -> str:
        return "finmind_chip_data"

    def fetch(self, **kwargs: Any) -> SourcedData:  # type: ignore[override]
        """
        Generic fetch — delegates to fetch_institutional().
        Direct callers should use the typed fetch_* methods instead.
        """
        symbol: str = str(kwargs.get("symbol", ""))
        days: int = int(kwargs.get("days", 10))
        return self.fetch_institutional(symbol, days=days)

    # ── 三大法人買賣超 ─────────────────────────────────────────────────────────

    def fetch_institutional(
        self,
        symbol: str,
        days: int = 10,
    ) -> SourcedData:
        """
        取最近 *days* 個交易日的三大法人買賣超。

        Parameters
        ----------
        symbol : 台股代號，e.g. "2330"（無需 .TW 後綴）
        days   : 回溯天數（日曆天，非交易日）

        Returns
        -------
        SourcedData, payload = InstitutionalResult
        """
        stock_id = _strip_tw_suffix(symbol)
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=days * 2)   # 2× 確保含足夠交易日

        rows = self._fetch_raw("TaiwanStockInstitutionalInvestorsBuySell", stock_id, start_dt, end_dt)
        result = _parse_institutional(stock_id, rows, days)

        return SourcedData(
            payload=result,
            source="finmind_institutional_investors",
            asof=datetime.now(UTC),
        )

    # ── 融資融券 ───────────────────────────────────────────────────────────────

    def fetch_margin(
        self,
        symbol: str,
        days: int = 10,
    ) -> SourcedData:
        """
        取最近 *days* 個交易日的融資融券餘額資料。

        Returns
        -------
        SourcedData, payload = MarginResult
        """
        stock_id = _strip_tw_suffix(symbol)
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=days * 2)

        rows = self._fetch_raw("TaiwanStockMarginPurchaseShortSale", stock_id, start_dt, end_dt)
        result = _parse_margin(stock_id, rows)

        return SourcedData(
            payload=result,
            source="finmind_margin_purchase_short_sale",
            asof=datetime.now(UTC),
        )

    # ── 外資持股比例 ───────────────────────────────────────────────────────────

    def fetch_shareholding(
        self,
        symbol: str,
        days: int = 60,
    ) -> SourcedData:
        """
        取最近 *days* 個日曆天的外資持股比例。

        Returns
        -------
        SourcedData, payload = ShareholdingResult
        """
        stock_id = _strip_tw_suffix(symbol)
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=days)

        rows = self._fetch_raw("TaiwanStockShareholding", stock_id, start_dt, end_dt)
        result = _parse_shareholding(stock_id, rows)

        return SourcedData(
            payload=result,
            source="finmind_shareholding",
            asof=datetime.now(UTC),
        )

    # ── 期貨三大法人 ───────────────────────────────────────────────────────────

    def fetch_futures_inst(
        self,
        symbol: str = "TXF",
    ) -> SourcedData:
        """
        取最新交易日的期貨三大法人未平倉部位。

        Parameters
        ----------
        symbol : 期貨代號，e.g. "TXF"（台指期）

        Returns
        -------
        SourcedData, payload = FuturesInstResult
        """
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=7)   # 最近一周確保拿到最新交易日

        rows = self._fetch_raw("TaiwanFuturesInstitutionalInvestors", symbol, start_dt, end_dt)
        result = _parse_futures_inst(symbol, rows)

        return SourcedData(
            payload=result,
            source="finmind_futures_institutional",
            asof=datetime.now(UTC),
        )

    # ── Network I/O layer（測試 subclass/monkeypatch 此方法）─────────────────

    def _fetch_raw(
        self,
        dataset: str,
        data_id: str,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        """
        Call FinMind /data endpoint and return raw row list.

        This is the ONLY method performing network I/O.
        Override or monkeypatch in tests — do NOT hit real API in unit tests.
        """
        import requests  # lazy import

        params: dict[str, Any] = {
            "dataset":    dataset,
            "data_id":    data_id,
            "start_date": start_date.isoformat(),
            "end_date":   end_date.isoformat(),
            "token":      self._token,
        }
        resp = requests.get(FINMIND_API_URL, params=params, timeout=FINMIND_TIMEOUT_SEC)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != 200:
            # FinMind 回傳 status != 200 代表資料錯誤或無資料
            return []
        return data.get("data", [])  # type: ignore[no-any-return]


# ─── Pure parsing helpers（unit-testable, no I/O）────────────────────────────


def _strip_tw_suffix(symbol: str) -> str:
    """Remove .TW / .TWO suffix to obtain FinMind stock_id."""
    for suffix in (".TWO", ".TW"):
        if symbol.upper().endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol


def _parse_institutional(
    symbol: str,
    rows: list[dict[str, Any]],
    days: int,
) -> InstitutionalResult:
    """
    Convert raw FinMind TaiwanStockInstitutionalInvestorsBuySell rows
    → InstitutionalResult.

    FinMind 欄位：
        date            : "YYYY-MM-DD"
        name            : "外資及陸資" | "投信" | "自營商"（含自行買賣/避險）
        buy             : 買進股數
        sell            : 賣出股數
        buy_sell        : 買賣超股數（= buy - sell）

    每一個交易日有多筆（每個 name 一筆），需先 group by date，再分 name 匯總。
    """
    from collections import defaultdict

    # group: date_str → {name → row}
    by_date: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        d = str(row.get("date", ""))
        name = str(row.get("name", ""))
        by_date[d][name] = row

    # 取最近 N 個交易日（排序後取尾端）
    sorted_dates = sorted(by_date.keys())[-days:]

    records: list[InstitutionalFlow] = []
    for d_str in sorted_dates:
        day_rows = by_date[d_str]
        try:
            dt = date.fromisoformat(d_str)
        except ValueError:
            continue

        def _buy_sell(name_key: str) -> tuple[float, float]:
            """Return (buy_sell_shares, buy_sell_amount) for a name key."""
            for k, v in day_rows.items():
                if name_key in k:
                    bs = float(v.get("buy_sell", 0) or 0)
                    # FinMind 有時有 buy/sell 而無 buy_sell 欄位
                    if bs == 0.0:
                        b = float(v.get("buy", 0) or 0)
                        s = float(v.get("sell", 0) or 0)
                        bs = b - s
                    # amount = buy_price * shares（有些版本有此欄，無則用 0）
                    amt = float(v.get("buy_deal_volume", 0) or v.get("amount", 0) or 0)
                    return bs, amt
            return 0.0, 0.0

        f_shares, f_amt = _buy_sell("外資")
        t_shares, t_amt = _buy_sell("投信")
        d_shares, d_amt = _buy_sell("自營商")

        records.append(InstitutionalFlow(
            date=dt,
            foreign_buy_sell=f_shares,
            foreign_amount=f_amt,
            investment_trust_buy_sell=t_shares,
            investment_trust_amount=t_amt,
            dealer_buy_sell=d_shares,
            dealer_amount=d_amt,
            total_buy_sell=f_shares + t_shares + d_shares,
        ))

    # 彙總統計
    foreign_total = sum(r.foreign_buy_sell for r in records)
    foreign_amt_total = sum(r.foreign_amount for r in records)
    trust_total = sum(r.investment_trust_buy_sell for r in records)
    dealer_total = sum(r.dealer_buy_sell for r in records)
    total_total = sum(r.total_buy_sell for r in records)

    # 連續外資買超天數
    consecutive = 0
    for r in reversed(records):
        if r.foreign_buy_sell > 0:
            if consecutive >= 0:
                consecutive += 1
            else:
                break
        elif r.foreign_buy_sell < 0:
            if consecutive <= 0:
                consecutive -= 1
            else:
                break
        else:
            break

    return InstitutionalResult(
        symbol=symbol,
        records=records,
        foreign_net_shares=foreign_total,
        foreign_net_amount=foreign_amt_total,
        trust_net_shares=trust_total,
        dealer_net_shares=dealer_total,
        total_net_shares=total_total,
        consecutive_foreign_buy=consecutive,
    )


def _parse_margin(
    symbol: str,
    rows: list[dict[str, Any]],
) -> MarginResult:
    """
    Convert raw FinMind TaiwanStockMarginPurchaseShortSale rows
    → MarginResult.

    FinMind 欄位：
        date                     : "YYYY-MM-DD"
        MarginPurchaseBuy        : 融資買進（千股）
        MarginPurchaseSell       : 融資賣出（千股）
        MarginPurchaseRedeem     : 融資現金償還（千股）
        MarginPurchaseToday      : 融資今日餘額（千股）
        ShortSaleBuy             : 融券買進（千股）
        ShortSaleSell            : 融券賣出（千股）
        ShortSaleToday           : 融券今日餘額（千股）
        OffsetLoanAndShort       : 資券相抵（千股）
    """
    sorted_rows = sorted(rows, key=lambda r: str(r.get("date", "")))

    records: list[MarginRecord] = []
    for row in sorted_rows:
        try:
            dt = date.fromisoformat(str(row["date"]))
        except (KeyError, ValueError):
            continue

        records.append(MarginRecord(
            date=dt,
            margin_purchase=float(row.get("MarginPurchaseBuy", 0) or 0),
            margin_redemption=float(row.get("MarginPurchaseRedeem", 0) or 0),
            margin_balance=float(row.get("MarginPurchaseToday", 0) or 0),
            short_sale=float(row.get("ShortSaleSell", 0) or 0),
            short_cover=float(row.get("ShortSaleBuy", 0) or 0),
            short_balance=float(row.get("ShortSaleToday", 0) or 0),
            offset=float(row.get("OffsetLoanAndShort", 0) or 0),
        ))

    latest_margin = records[-1].margin_balance if records else 0.0
    latest_short = records[-1].short_balance if records else 0.0

    # 5 日融資/券變化
    if len(records) >= 5:
        margin_5d = records[-1].margin_balance - records[-6].margin_balance if len(records) >= 6 else 0.0
        short_5d = records[-1].short_balance - records[-6].short_balance if len(records) >= 6 else 0.0
    else:
        margin_5d = 0.0
        short_5d = 0.0

    return MarginResult(
        symbol=symbol,
        records=records,
        margin_balance=latest_margin,
        short_balance=latest_short,
        margin_change_5d=margin_5d,
        short_change_5d=short_5d,
    )


def _parse_shareholding(
    symbol: str,
    rows: list[dict[str, Any]],
) -> ShareholdingResult:
    """
    Convert raw FinMind TaiwanStockShareholding rows → ShareholdingResult.

    FinMind 欄位：
        date                  : "YYYY-MM-DD"
        ForeignInvestmentRatio: 外資持股比例（%）
        ForeignInvestmentShares: 外資持股股數
        NumberOfSharesIssued  : 上市股數
    """
    sorted_rows = sorted(rows, key=lambda r: str(r.get("date", "")))

    records: list[ShareholdingRecord] = []
    for row in sorted_rows:
        try:
            dt = date.fromisoformat(str(row["date"]))
            ratio = float(row.get("ForeignInvestmentRatio", 0) or 0)
        except (KeyError, ValueError):
            continue
        records.append(ShareholdingRecord(
            date=dt,
            foreign_ownership_ratio=ratio,
            foreign_share_count=float(row.get("ForeignInvestmentShares", 0) or 0),
            listed_shares=float(row.get("NumberOfSharesIssued", 0) or 0),
        ))

    latest_ratio = records[-1].foreign_ownership_ratio if records else 0.0

    # 30 日持股比例變化
    change_30d = 0.0
    if len(records) >= 2:
        change_30d = records[-1].foreign_ownership_ratio - records[0].foreign_ownership_ratio

    return ShareholdingResult(
        symbol=symbol,
        records=records,
        latest_ratio=latest_ratio,
        change_30d=change_30d,
    )


def _parse_futures_inst(
    symbol: str,
    rows: list[dict[str, Any]],
) -> FuturesInstResult:
    """
    Convert raw FinMind TaiwanFuturesInstitutionalInvestors rows
    → FuturesInstResult.

    FinMind 欄位：
        date                   : "YYYY-MM-DD"
        name                   : "自營商" | "投信" | "外資及陸資"
        long_open_interest      : 多方未平倉口數
        short_open_interest     : 空方未平倉口數
        net_open_interest       : 淨未平倉口數

    取最近一個有資料的交易日。
    """
    if not rows:
        return FuturesInstResult(symbol=symbol)

    # 找最近一個交易日
    sorted_rows = sorted(rows, key=lambda r: str(r.get("date", "")))
    latest_date = str(sorted_rows[-1].get("date", ""))
    latest_rows = [r for r in sorted_rows if str(r.get("date", "")) == latest_date]

    records: list[FuturesInstRecord] = []
    foreign_net = 0.0
    trust_net = 0.0
    dealer_net = 0.0

    try:
        dt = date.fromisoformat(latest_date)
    except ValueError:
        return FuturesInstResult(symbol=symbol)

    for row in latest_rows:
        name = str(row.get("name", ""))
        long_oi = float(row.get("long_open_interest", 0) or 0)
        short_oi = float(row.get("short_open_interest", 0) or 0)
        net_oi = float(row.get("net_open_interest", long_oi - short_oi) or 0)

        records.append(FuturesInstRecord(
            date=dt,
            name=name,
            long_open_interest=long_oi,
            short_open_interest=short_oi,
            net_open_interest=net_oi,
        ))

        if "外資" in name:
            foreign_net = net_oi
        elif "投信" in name:
            trust_net = net_oi
        elif "自營商" in name:
            dealer_net = net_oi

    return FuturesInstResult(
        symbol=symbol,
        records=records,
        foreign_net_position=foreign_net,
        trust_net_position=trust_net,
        dealer_net_position=dealer_net,
    )
