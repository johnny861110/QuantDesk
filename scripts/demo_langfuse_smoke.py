"""
Langfuse smoke test — 六個 agent 各跑一次，確認全部 trace 出現在 localhost:3000

此腳本需先 load .env 並驗證 LANGFUSE_ENABLED/TRACING_ACTIVE，才能安全
import 後續 agent/observability 模組，因此 import 故意不在檔案最上方。
E402 noqa 均為刻意設計，非偷懶。

用法：
  uv run python scripts/demo_langfuse_smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock

import numpy as np
from dotenv import load_dotenv

load_dotenv()

if os.getenv("LANGFUSE_ENABLED", "false").lower() != "true":
    print("⚠️  LANGFUSE_ENABLED 未設為 true，請確認 .env 內容")
    sys.exit(1)

from observability.langfuse_setup import TRACING_ACTIVE  # noqa: E402

if not TRACING_ACTIVE:
    print("⚠️  Langfuse 未成功啟動（可能 PUBLIC_KEY/SECRET_KEY 有誤或 host 連不到）")
    sys.exit(1)

print("✅ Langfuse tracing active")

# ─── shared imports ───────────────────────────────────────────────────────────
from adapters.base import DataSourceAdapter, SourcedData  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# 1. risk_agent
# ─────────────────────────────────────────────────────────────────────────────
print("\n▶ [1/6] Running risk_agent ...")

from adapters.fx_adapter import FXRate  # noqa: E402
from agents.risk_agent import run_risk_agent  # noqa: E402


class _MockFXAdapter(DataSourceAdapter):
    @property
    def source_name(self) -> str:
        return "mock_fx"

    def fetch(self, pair: str = "USDTWD", **kwargs: object) -> SourcedData:
        return SourcedData(
            payload=FXRate(pair=pair, rate=32.5),
            source=self.source_name,
            asof=datetime(2026, 7, 23, 9, 0),
        )


risk_sig = run_risk_agent(fx_adapter=_MockFXAdapter())
print(f"   signal={risk_sig.signal.value}  confidence={risk_sig.confidence:.2f}")
print(f"   errors={risk_sig.errors[:1] if risk_sig.errors else []}")

# ─────────────────────────────────────────────────────────────────────────────
# 2. fundamental_agent
# ─────────────────────────────────────────────────────────────────────────────
print("\n▶ [2/6] Running fundamental_agent ...")

from agents.fundamental_agent import FundamentalAgent  # noqa: E402

# _demo_fixtures lives in the same scripts/ directory
from _demo_fixtures import (  # noqa: E402
    SQLiteStore,
    _insert_fact,
    _insert_filing,
    _set_quality_score,
)

try:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/smoke.db"
        store = SQLiteStore(db_path)
        fid = _insert_filing(store, "2330", "台積電", 2024, "Q1")
        for field, value in [
            ("net_revenue", 625_763_000.0), ("gross_profit", 265_457_000.0),
            ("operating_income", 249_519_000.0), ("net_income", 225_491_000.0),
            ("eps_basic", 8.70), ("total_assets", 6_580_023_000.0),
            ("total_liabilities", 2_790_000_000.0), ("equity", 3_790_023_000.0),
            ("operating_cash_flow", 381_232_000.0),
            ("current_assets", 1_200_000_000.0), ("current_liabilities", 800_000_000.0),
        ]:
            _insert_fact(store, fid, field, value, "xbrl", 1.0)
        _set_quality_score(store, fid, 0.95)

        fund_sig = FundamentalAgent(db_path).run("2330", 2024, "Q1")
        print(f"   signal={fund_sig.signal.value}  confidence={fund_sig.confidence:.2f}")
        print(f"   errors={fund_sig.errors[:1] if fund_sig.errors else []}")
except Exception as exc:
    print(f"   ⚠️  fundamental_agent 跑失敗: {exc}")

# ─────────────────────────────────────────────────────────────────────────────
# 3. technical_agent
# ─────────────────────────────────────────────────────────────────────────────
print("\n▶ [3/6] Running technical_agent ...")

from adapters.price_adapter import OHLCVData  # noqa: E402
from agents.technical_agent import run_technical_agent  # noqa: E402


class _FakePriceAdapter(DataSourceAdapter):
    @property
    def source_name(self) -> str:
        return "fake_price"

    def fetch(self, symbol: str = "2330.TW", **kwargs: object) -> SourcedData:
        n = 100
        close = np.linspace(100.0, 120.0, n)
        dates = [datetime(2024, 1, 1, tzinfo=timezone.utc)] * n
        payload = OHLCVData(
            symbol=symbol, market="TW",
            close=close, high=close + 2.0,
            low=close - 2.0, open_=close - 1.0,
            volume=np.ones(n) * 1000.0,
            dates=dates,
        )
        return SourcedData(payload=payload, source="fake_price", asof=dates[-1])


tech_sig = run_technical_agent(symbol="2330.TW", price_adapter=_FakePriceAdapter())
print(f"   signal={tech_sig.signal.value}  confidence={tech_sig.confidence:.2f}")
print(f"   errors={tech_sig.errors[:1] if tech_sig.errors else []}")

# ─────────────────────────────────────────────────────────────────────────────
# 4. cross_market_agent
# ─────────────────────────────────────────────────────────────────────────────
print("\n▶ [4/6] Running cross_market_agent ...")

from adapters.cross_market_adapter import CrossMarketData  # noqa: E402
from agents.cross_market_agent import run_cross_market_agent  # noqa: E402


class _FakeCrossAdapter(DataSourceAdapter):
    @property
    def source_name(self) -> str:
        return "fake_cross"

    def fetch(self, **kwargs: object) -> SourcedData:
        n = 120
        rng = np.random.default_rng(42)
        base = np.cumsum(rng.normal(0, 1, n))
        tw_ret = np.diff(base + rng.normal(0, 0.3, n))
        us_ret = np.diff(base + rng.normal(0, 0.3, n))
        tw_close = np.exp(base[:n-1]) * 20000
        us_close = np.exp(base[:n-1]) * 5000
        dates = [datetime(2024, 1, 1) + timedelta(days=i) for i in range(n - 1)]
        payload = CrossMarketData(
            symbols=["^TWII", "^GSPC"],
            labels={"^TWII": "TAIEX", "^GSPC": "S&P 500"},
            close={"^TWII": tw_close, "^GSPC": us_close},
            returns_={"^TWII": tw_ret, "^GSPC": us_ret},
            dates=dates,
        )
        return SourcedData(payload=payload, source="fake_cross", asof=dates[-1])


cm_sig = run_cross_market_agent(cross_adapter=_FakeCrossAdapter())
print(f"   signal={cm_sig.signal.value}  confidence={cm_sig.confidence:.2f}")
print(f"   errors={cm_sig.errors[:1] if cm_sig.errors else []}")

# ─────────────────────────────────────────────────────────────────────────────
# 5. macro_agent
# ─────────────────────────────────────────────────────────────────────────────
print("\n▶ [5/6] Running macro_agent ...")

from adapters.macro_adapter import MacroEvent, MacroResult  # noqa: E402
from agents.macro_agent import run_macro_agent  # noqa: E402

_macro_event = MacroEvent(
    category="GDP Growth Rate",
    country="United States",
    actual=2.5,
    consensus=2.0,
    previous=1.8,
    unit="%",
    importance=3,
    release_date=datetime.now(UTC) - timedelta(days=1),
    source_name="trading_economics",
)
_macro_mock = MagicMock()
_macro_sourced = MagicMock()
_macro_sourced.payload = MacroResult(
    events=[_macro_event],
    countries=["united states"],
    fetched_at=datetime.now(UTC),
)
_macro_sourced.asof = _macro_sourced.payload.fetched_at
_macro_mock.fetch.return_value = _macro_sourced

macro_sig = run_macro_agent(macro_adapter=_macro_mock)
print(f"   signal={macro_sig.signal.value}  confidence={macro_sig.confidence:.2f}")
print(f"   errors={macro_sig.errors[:1] if macro_sig.errors else []}")

# ─────────────────────────────────────────────────────────────────────────────
# 6. news_agent
# ─────────────────────────────────────────────────────────────────────────────
print("\n▶ [6/6] Running news_agent ...")

from adapters.news_adapter import NewsItem, NewsResult, TIER_MOPS, TIER_RSS  # noqa: E402
from agents.news_agent import run_news_agent  # noqa: E402

_NEWS_NOW = datetime(2026, 7, 23, 9, 0, 0)


class _FakeMopsAdapter(DataSourceAdapter):
    source_name = "fake_mops"

    def fetch(self, **kwargs: object) -> SourcedData:
        items = [NewsItem(
            title="重大訊息：本公司第二季獲利優於預期",
            summary="台積電第二季 EPS 創新高，優於法人預估",
            url="https://mops.twse.com.tw",
            published_at=_NEWS_NOW,
            source_name="mops",
            confidence_tier=TIER_MOPS,
            is_official=True,
        )]
        return SourcedData(
            payload=NewsResult(items=items, symbol="2330.TW", fetched_at=_NEWS_NOW),
            source="fake_mops", asof=_NEWS_NOW,
        )


class _FakeRSSAdapter(DataSourceAdapter):
    source_name = "fake_rss"

    def fetch(self, **kwargs: object) -> SourcedData:
        items = [NewsItem(
            title="台積電法說第二季獲利超越市場預期",
            summary="台積電 Q2 業績優於分析師預估",
            url="https://cnyes.com/1",
            published_at=_NEWS_NOW,
            source_name="cnyes",
            confidence_tier=TIER_RSS,
            is_official=False,
        )]
        return SourcedData(
            payload=NewsResult(items=items, symbol="2330.TW", fetched_at=_NEWS_NOW),
            source="fake_rss", asof=_NEWS_NOW,
        )


def _make_mock_openai() -> MagicMock:
    response_body = {
        "articles": [
            {"index": 1, "implication": "positive_surprise",
             "already_priced_in": False, "is_fact": True,
             "financial_context": "EPS 超越預期"},
            {"index": 2, "implication": "positive_surprise",
             "already_priced_in": False, "is_fact": True,
             "financial_context": "法說會確認優於預期"},
        ],
        "overall_summary": "整體新聞面偏多，無數字。",
    }
    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = json.dumps(response_body, ensure_ascii=False)
    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])
    return mock_client


news_sig = run_news_agent(
    symbol="2330.TW",
    market="TW",
    query_terms=["台積電"],
    mops_adapter=_FakeMopsAdapter(),   # type: ignore[arg-type]
    rss_adapter=_FakeRSSAdapter(),     # type: ignore[arg-type]
    openai_client=_make_mock_openai(),
    asof=_NEWS_NOW,
)
print(f"   signal={news_sig.signal.value}  confidence={news_sig.confidence:.2f}")
print(f"   errors={news_sig.errors[:1] if news_sig.errors else []}")

# ─── flush ────────────────────────────────────────────────────────────────────
print("\n⏳ Flushing Langfuse spans ...")
from langfuse import get_client  # noqa: E402

get_client().flush()
print("✅ Done — 請開 localhost:3000 確認六個 agent 的 trace")
