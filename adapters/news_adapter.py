"""
新聞 Adapter — 三層來源分工

Confidence tier (machine-readable, used by news_agent for weighting):
  1 = 公開資訊觀測站 (MOPS)  — 公司自發公告，最高可信度
  2 = RSS (鉅亨/工商時報)    — 財經媒體報導，中等可信度
  3 = Tavily 搜尋            — 泛用搜尋結果，最低可信度

Design
------
每個 adapter 繼承 NewsAdapter(base.py)，輸出 SourcedData(payload=NewsResult, ...)。
網路 I/O 全部隔離在 _fetch_raw() / _search_raw() 中；測試 monkeypatch 這些方法，
不打真實 API。

API 金鑰一律從環境變數讀取（python-dotenv），找不到時明確 raise RuntimeError，
不靜默使用空字串繼續跑。

MOPS endpoint
-------------
公開資訊觀測站重大訊息查詢 API（POST form data）：
  https://mops.twse.com.tw/mops/web/ajax_t05st01
欄位：co_id（股票代碼，不含 .TW 後綴）、begin_date/end_date（YYYYMMDD）。
傳回 HTML table；_parse_mops_html() 解析成 list[dict]。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from adapters.base import NewsAdapter, SourcedData

# ─── Confidence tier constants ────────────────────────────────────────────────

TIER_MOPS: int = 1    # 公開資訊觀測站
TIER_RSS: int = 2     # RSS 財經媒體
TIER_TAVILY: int = 3  # Tavily 搜尋

# ─── MOPS constants ───────────────────────────────────────────────────────────

MOPS_URL: str = "https://mops.twse.com.tw/mops/web/ajax_t05st01"
MOPS_TIMEOUT_SEC: int = 15

# ─── RSS defaults ─────────────────────────────────────────────────────────────

DEFAULT_RSS_FEEDS: dict[str, str] = {
    "cnyes":  "https://feeds.cnyes.com/rss/news/tw.xml",
    "ctee":   "https://www.ctee.com.tw/feed",
}
RSS_MAX_ITEMS: int = 20   # per feed, to avoid bloat

# ─── Data structures ──────────────────────────────────────────────────────────


@dataclass
class NewsItem:
    """
    一則新聞的標準化表示。

    confidence_tier : 1 (MOPS) / 2 (RSS) / 3 (Tavily) — 越低越可信
    is_official     : True = 公司正式公告（MOPS），False = 媒體報導 / 搜尋結果
    """
    title: str
    summary: str
    url: str
    published_at: datetime
    source_name: str
    confidence_tier: int
    is_official: bool


@dataclass
class NewsResult:
    """Payload returned by all NewsAdapter implementations."""
    items: list[NewsItem] = field(default_factory=list)
    symbol: str = ""
    query_terms: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ─── MOPS Adapter ─────────────────────────────────────────────────────────────


class MopsNewsAdapter(NewsAdapter):
    """
    公開資訊觀測站重大訊息 adapter。

    以 POST 請求查詢 MOPS ajax_t05st01，解析 HTML table 取得重大訊息列表。
    _fetch_raw() 是唯一的 network I/O 點，測試可 monkeypatch 覆蓋。

    Usage
    -----
        adapter = MopsNewsAdapter()
        data    = adapter.fetch(symbol="2330.TW", days_back=7)
        items   = data.payload.items   # list[NewsItem]
    """

    @property
    def source_name(self) -> str:  # type: ignore[override]
        return "mops_major_disclosure"

    def fetch(  # type: ignore[override]
        self,
        symbol: str,
        days_back: int = 7,
        **kwargs: Any,
    ) -> SourcedData:
        stock_id = _strip_tw_suffix(symbol)
        end_dt = datetime.now(UTC)
        start_dt = end_dt - timedelta(days=days_back)

        raw_html = self._fetch_raw(stock_id, start_dt, end_dt)
        items = _parse_mops_html(raw_html, symbol)

        payload = NewsResult(
            items=items,
            symbol=symbol,
            query_terms=[stock_id],
            fetched_at=end_dt,
        )
        return SourcedData(payload=payload, source=self.source_name, asof=end_dt)

    def _fetch_raw(
        self, stock_id: str, start_date: datetime, end_date: datetime
    ) -> str:
        """POST to MOPS and return raw HTML string. Override in tests."""
        import requests  # lazy import — network only

        form_data = {
            "encodeURIComponent": "1",
            "step": "1",
            "firstin": "1",
            "off": "1",
            "co_id": stock_id,
            "begin_date": start_date.strftime("%Y%m%d"),
            "end_date": end_date.strftime("%Y%m%d"),
        }
        resp = requests.post(MOPS_URL, data=form_data, timeout=MOPS_TIMEOUT_SEC)
        resp.raise_for_status()
        return resp.text


# ─── RSS Adapter ──────────────────────────────────────────────────────────────


class RSSNewsAdapter(NewsAdapter):
    """
    RSS 財經媒體 adapter（鉅亨網、工商時報等）。

    依 feed_urls 逐一拉取 RSS，過濾含 query_terms 的標題，取最新 max_items 則。
    _fetch_raw() 是唯一的 network I/O 點。

    Usage
    -----
        adapter = RSSNewsAdapter()
        data    = adapter.fetch(symbol="2330.TW", query_terms=["台積電", "TSMC"])
    """

    def __init__(
        self,
        feed_urls: dict[str, str] | None = None,
        max_items: int = RSS_MAX_ITEMS,
    ) -> None:
        self._feed_urls = feed_urls if feed_urls is not None else DEFAULT_RSS_FEEDS
        self._max_items = max_items

    @property
    def source_name(self) -> str:  # type: ignore[override]
        return "rss_financial_media"

    def fetch(  # type: ignore[override]
        self,
        symbol: str,
        query_terms: list[str] | None = None,
        days_back: int = 7,
        **kwargs: Any,
    ) -> SourcedData:
        terms = query_terms or [_strip_tw_suffix(symbol)]
        cutoff = datetime.now(UTC) - timedelta(days=days_back)
        fetched_at = datetime.now(UTC)

        all_items: list[NewsItem] = []
        for feed_name, feed_url in self._feed_urls.items():
            raw_feed = self._fetch_raw(feed_url)
            items = _parse_rss_feed(
                raw_feed, feed_name, terms, cutoff, self._max_items
            )
            all_items.extend(items)

        payload = NewsResult(
            items=all_items,
            symbol=symbol,
            query_terms=terms,
            fetched_at=fetched_at,
        )
        return SourcedData(payload=payload, source=self.source_name, asof=fetched_at)

    def _fetch_raw(self, feed_url: str) -> Any:
        """Parse RSS feed URL and return feedparser result. Override in tests."""
        import feedparser  # type: ignore[import-untyped]

        return feedparser.parse(feed_url)


# ─── Tavily Adapter ───────────────────────────────────────────────────────────


class TavilyNewsAdapter(NewsAdapter):
    """
    Tavily 搜尋 API adapter — 最低可信度，用於前兩者覆蓋不到的補充查詢。

    API 金鑰從環境變數 TAVILY_API_KEY 讀取（搭配 python-dotenv）。
    找不到 key 時明確 raise RuntimeError，不靜默繼續。
    _search_raw() 是唯一的 network I/O 點。

    Usage
    -----
        from dotenv import load_dotenv
        load_dotenv()
        adapter = TavilyNewsAdapter()
        data    = adapter.fetch(symbol="AAPL", query_terms=["Apple earnings"])
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key  # None = read from env at call time

    @property
    def source_name(self) -> str:  # type: ignore[override]
        return "tavily_search"

    def fetch(  # type: ignore[override]
        self,
        symbol: str,
        query_terms: list[str] | None = None,
        max_results: int = 5,
        **kwargs: Any,
    ) -> SourcedData:
        key = self._api_key or _require_env("TAVILY_API_KEY")
        terms = query_terms or [symbol]
        fetched_at = datetime.now(UTC)

        query = " ".join(terms)
        raw_results = self._search_raw(query, max_results, key)
        items = _parse_tavily_results(raw_results, symbol)

        payload = NewsResult(
            items=items,
            symbol=symbol,
            query_terms=terms,
            fetched_at=fetched_at,
        )
        return SourcedData(payload=payload, source=self.source_name, asof=fetched_at)

    def _search_raw(
        self, query: str, max_results: int, api_key: str
    ) -> list[dict[str, Any]]:
        """Call Tavily search and return raw result list. Override in tests."""
        from tavily import TavilyClient  # type: ignore[import-untyped]

        client = TavilyClient(api_key=api_key)
        response = client.search(
            query=query,
            max_results=max_results,
            search_depth="basic",
        )
        return list(response.get("results", []))  # type: ignore[no-any-return]


# ─── Pure parsing helpers (unit-testable, no I/O) ────────────────────────────


def _strip_tw_suffix(symbol: str) -> str:
    """Remove .TW / .TWO suffix for MOPS stock_id lookup."""
    for suffix in (".TWO", ".TW"):
        if symbol.upper().endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol


def _parse_mops_html(html: str, symbol: str) -> list[NewsItem]:
    """
    Parse MOPS ajax_t05st01 HTML response into NewsItem list.

    MOPS table columns (approximate): 序號 | 日期 | 時間 | 公司名稱 | 主旨 | 詳細
    We look for <tr> rows containing <td> cells with disclosure info.
    Returns empty list on parse failure (caller handles gracefully).
    """
    if not html:
        return []

    items: list[NewsItem] = []
    # Regex to pull table rows (simplified; real MOPS HTML is irregular)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE)
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL | re.IGNORECASE)
        clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if len(clean) < 5:
            continue
        # Expected layout: [seq, date, time, company, subject, ...]
        date_str = clean[1] if len(clean) > 1 else ""
        subject = clean[4] if len(clean) > 4 else ""
        if not subject or not date_str:
            continue
        try:
            pub_dt = datetime.strptime(date_str, "%Y/%m/%d")
        except ValueError:
            try:
                pub_dt = datetime.strptime(date_str, "%Y%m%d")
            except ValueError:
                continue
        items.append(
            NewsItem(
                title=subject,
                summary=subject,
                url=MOPS_URL,
                published_at=pub_dt,
                source_name="mops",
                confidence_tier=TIER_MOPS,
                is_official=True,
            )
        )
    return items


def _parse_rss_feed(
    feed: Any,
    feed_name: str,
    query_terms: list[str],
    cutoff: datetime,
    max_items: int,
) -> list[NewsItem]:
    """
    Parse a feedparser result, filter by query_terms and recency.

    Pure function (feed is already-parsed feedparser object).
    Returns at most max_items items, newest first.
    """
    items: list[NewsItem] = []
    entries = getattr(feed, "entries", []) or []
    for entry in entries:
        title: str = getattr(entry, "title", "") or ""
        summary: str = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
        url: str = getattr(entry, "link", "") or ""

        # Filter: at least one query term must appear in title or summary
        combined = (title + " " + summary).lower()
        if not any(t.lower() in combined for t in query_terms):
            continue

        # Parse publication date (always naive UTC for consistent comparison)
        pub_dt: datetime | None = None
        published_parsed = getattr(entry, "published_parsed", None)
        if published_parsed is not None:
            try:
                pub_dt = datetime(*published_parsed[:6])  # naive UTC from struct_time
            except (TypeError, ValueError):
                pub_dt = None
        if pub_dt is None:
            pub_dt = datetime.now(UTC).replace(tzinfo=None)

        # cutoff may be tz-aware; compare as naive
        cutoff_naive = cutoff.replace(tzinfo=None) if cutoff.tzinfo else cutoff
        if pub_dt < cutoff_naive:
            continue

        items.append(
            NewsItem(
                title=title,
                summary=summary[:500],  # cap summary length
                url=url,
                published_at=pub_dt,
                source_name=feed_name,
                confidence_tier=TIER_RSS,
                is_official=False,
            )
        )
        if len(items) >= max_items:
            break

    return items


def _parse_tavily_results(
    results: list[dict[str, Any]], symbol: str
) -> list[NewsItem]:
    """
    Convert raw Tavily search result dicts into NewsItem list.

    Tavily result fields: title, url, content, score, published_date (optional).
    """
    items: list[NewsItem] = []
    for r in results:
        title: str = str(r.get("title", "")).strip()
        content: str = str(r.get("content", "")).strip()
        url: str = str(r.get("url", "")).strip()
        if not title:
            continue

        pub_dt: datetime | None = None
        pub_str = r.get("published_date") or r.get("published") or ""
        if pub_str:
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
                try:
                    pub_dt = datetime.strptime(pub_str[:19], fmt)
                    break
                except ValueError:
                    continue
        if pub_dt is None:
            pub_dt = datetime.now(UTC)

        items.append(
            NewsItem(
                title=title,
                summary=content[:500],
                url=url,
                published_at=pub_dt,
                source_name="tavily",
                confidence_tier=TIER_TAVILY,
                is_official=False,
            )
        )
    return items


def _require_env(key: str) -> str:
    """
    Read API key from environment.  Raises RuntimeError if missing or empty.
    Callers should load .env via python-dotenv before invoking adapters.
    """
    value = os.environ.get(key, "")
    if not value:
        raise RuntimeError(
            f"環境變數 {key!r} 未設定或為空字串。"
            f"請在 .env 檔案中設定，並在程式入口用 python-dotenv 載入：\n"
            f"  from dotenv import load_dotenv; load_dotenv()\n"
            f"不得將 API 金鑰硬寫在程式碼中。"
        )
    return value
