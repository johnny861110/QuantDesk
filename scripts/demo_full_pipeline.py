#!/usr/bin/env python3
"""
scripts/demo_full_pipeline.py

End-to-end QuantDesk pipeline demo：六個 domain agent → Supervisor 仲裁。

執行方式：
    uv run python scripts/demo_full_pipeline.py

設計原則：
  - 所有 mock adapter 內嵌（不 import tests.*），可在任何環境下執行
  - Fundamental agent 使用真實 SQLiteStore + 已驗證過的 2330/2024Q1 財務數據
  - News agent：有 OPENAI_API_KEY 時走真實 LLM；無 key 時走既有降級路徑
  - 所有 agent 執行掛 Langfuse trace（LANGFUSE_ENABLED=true 時生效）

⚠ spot price / IV 均為示意值，非當日市場報價。
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── dotenv first — before any observability imports ──────────────────────────
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

# ── repo root on path ─────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))  # for _demo_fixtures

from observability.langfuse_setup import TRACING_ACTIVE, observe, update_current_span  # noqa: E402

# ── agent imports ─────────────────────────────────────────────────────────────
from adapters.base import SourcedData  # noqa: E402
from adapters.fx_adapter import FXRate  # noqa: E402
from adapters.macro_adapter import MacroEvent, MacroResult  # noqa: E402
from adapters.news_adapter import (  # noqa: E402
    NewsItem,
    NewsResult,
    TIER_MOPS,
    TIER_RSS,
)
from adapters.price_adapter import OHLCVData  # noqa: E402
from adapters.cross_market_adapter import CrossMarketData  # noqa: E402
from agents.fundamental_agent import FundamentalAgent  # noqa: E402
from agents.news_agent import run_news_agent  # noqa: E402
from agents.macro_agent import run_macro_agent  # noqa: E402
from agents.risk_agent import run_risk_agent  # noqa: E402
from agents.technical_agent import run_technical_agent  # noqa: E402
from agents.cross_market_agent import run_cross_market_agent  # noqa: E402
from schemas.agent_signal import AgentSignal  # noqa: E402
from supervisor.graph import Supervisor  # noqa: E402
from supervisor.signal import SupervisorOutput  # noqa: E402
from _demo_fixtures import (  # noqa: E402
    SQLiteStore,
    _insert_filing,
    _insert_fact,
    _set_quality_score,
)

import numpy as np  # noqa: E402

UTC = timezone.utc
DEMO_ASOF = datetime(2026, 7, 23, 9, 30, 0, tzinfo=UTC)

W = 68


# ─── Print helpers ────────────────────────────────────────────────────────────

def _bar(char: str = "─") -> None:
    print(char * W)

def _section(title: str) -> None:
    print()
    _bar("═")
    print(f"  {title}")
    _bar("═")

def _ok(msg: str)   -> None: print(f"  ✓  {msg}")
def _warn(msg: str) -> None: print(f"  ⚠  {msg}")
def _err(msg: str)  -> None: print(f"  ✗  {msg}")

def _sig_header(agent_name: str) -> None:
    _bar("─")
    print(f"  [{agent_name}]")
    _bar("─")


# ─── Mock adapters (no tests.* import) ───────────────────────────────────────

class _MockFXAdapter:
    """USDTWD = 31.50 (fixed rate for demo)."""
    RATE = 31.50

    @property
    def source_name(self) -> str:
        return "demo_mock_fx"

    def fetch(self, pair: str = "USDTWD", **kwargs: Any) -> SourcedData:
        return SourcedData(
            payload=FXRate(pair=pair, rate=self.RATE),
            source=self.source_name,
            asof=DEMO_ASOF,
        )


class _FakePriceAdapter:
    """Synthetic uptrend 100→120, RSI in bullish territory."""

    @property
    def source_name(self) -> str:
        return "demo_fake_price"

    def fetch(self, symbol: str, period: str = "6mo", interval: str = "1d", **kwargs: Any) -> SourcedData:
        n = 100
        close = np.linspace(100.0, 120.0, n)
        high   = close + 2.0
        low    = close - 2.0
        open_  = close - 1.0
        volume = np.ones(n) * 1_000_000.0
        dates  = [datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i) for i in range(n)]
        payload = OHLCVData(
            symbol=symbol,
            market=kwargs.get("market", "TW"),
            close=close,
            high=high,
            low=low,
            open_=open_,
            volume=volume,
            dates=dates,
        )
        return SourcedData(payload=payload, source=self.source_name, asof=dates[-1])


class _FakeCrossMarketAdapter:
    """Correlated TW/US return series for cross-market correlation."""

    @property
    def source_name(self) -> str:
        return "demo_fake_cross"

    def fetch(self, symbols: dict[str, str] | None = None,
              period: str = "6mo", interval: str = "1d", **kwargs: Any) -> SourcedData:
        n = 120
        rng0 = np.random.default_rng(42)
        rng1 = np.random.default_rng(43)
        rng2 = np.random.default_rng(44)

        base  = np.cumsum(rng0.normal(0, 1, n))
        tw    = base + rng1.normal(0, 0.3, n)
        us    = base + rng2.normal(0, 0.3, n)
        dates = [datetime(2025, 10, 1) + timedelta(days=i) for i in range(n)]

        tw_ret = np.log(np.exp(tw[1:]) / np.exp(tw[:-1]))
        us_ret = np.log(np.exp(us[1:]) / np.exp(us[:-1]))
        tw_close = np.exp(tw) * 20_000
        us_close = np.exp(us) * 5_000

        payload = CrossMarketData(
            symbols=["^TWII", "^GSPC"],
            labels={"^TWII": "TAIEX", "^GSPC": "S&P 500"},
            close={"^TWII": tw_close, "^GSPC": us_close},
            returns_={"^TWII": tw_ret, "^GSPC": us_ret},
            dates=dates,
        )
        return SourcedData(payload=payload, source=self.source_name, asof=dates[-1])


class _DemoMacroAdapter:
    """US NFP (大幅超預期) + CPI (輕微超預期) — 複製自 2024 Q3 實際值。"""

    @property
    def source_name(self) -> str:
        return "demo_macro"

    def fetch(self, **kwargs: Any) -> SourcedData:
        now = datetime.now(UTC)
        events = [
            MacroEvent(
                category="Non Farm Payrolls",
                country="united states",
                actual=261.0,
                consensus=200.0,
                previous=216.0,
                unit="K",
                importance=3,       # 3 = high
                release_date=now - timedelta(days=3),
                source_name="trading_economics",
            ),
            MacroEvent(
                category="CPI YoY",
                country="united states",
                actual=2.4,
                consensus=2.3,
                previous=2.5,
                unit="%",
                importance=3,       # 3 = high
                release_date=now - timedelta(days=5),
                source_name="trading_economics",
            ),
        ]
        result = MacroResult(
            events=events,
            countries=["united states", "taiwan"],
            fetched_at=now,
        )
        return SourcedData(payload=result, source=self.source_name, asof=now)


class _DemoMopsAdapter:
    """MOPS 官方重訊：台積電第二季財報公告。"""
    source_name = "demo_mops"

    def fetch(self, **kwargs: Any) -> SourcedData:
        items = [
            NewsItem(
                title="重大訊息：台積電公告 2026Q2 合併財務報告",
                summary="台積電 2026 年第二季 EPS 達 15.20 元，優於法人預估約 12%。",
                url="https://mops.twse.com.tw/mops/web/t100sb10",
                published_at=DEMO_ASOF - timedelta(days=2),
                source_name="mops",
                confidence_tier=TIER_MOPS,
                is_official=True,
            )
        ]
        return SourcedData(
            payload=NewsResult(items=items, symbol="2330.TW", fetched_at=DEMO_ASOF),
            source=self.source_name,
            asof=DEMO_ASOF,
        )


class _DemoRSSAdapter:
    """財經媒體 RSS：法說會相關報導。"""
    source_name = "demo_rss"

    def fetch(self, **kwargs: Any) -> SourcedData:
        items = [
            NewsItem(
                title="台積電法說：下半年 AI 需求強勁，CoWoS 產能持續滿載",
                summary="台積電 CEO 法說會表示，AI 訓練及推論需求驅動先進封裝需求，H2 outlook 優於預期。",
                url="https://cnyes.com/news/2026072301",
                published_at=DEMO_ASOF - timedelta(days=1),
                source_name="cnyes",
                confidence_tier=TIER_RSS,
                is_official=False,
            ),
        ]
        return SourcedData(
            payload=NewsResult(items=items, symbol="2330.TW", fetched_at=DEMO_ASOF),
            source=self.source_name,
            asof=DEMO_ASOF,
        )


# ─── Fundamental DB setup ─────────────────────────────────────────────────────

def _build_fundamental_db() -> str:
    """Build a temp SQLite DB with 2330/2024Q1 XBRL data (same as conftest)."""
    tmp = tempfile.mkdtemp(prefix="quantdesk_demo_")
    db_path = os.path.join(tmp, "demo_fundamental.db")
    store = SQLiteStore(db_path)

    fid = _insert_filing(store, "2330", "台積電", 2024, "Q1")
    for field, value in [
        ("net_revenue",          625_763_000.0),
        ("gross_profit",         265_457_000.0),
        ("operating_income",     249_519_000.0),
        ("net_income",           225_491_000.0),
        ("eps_basic",                      8.70),
        ("total_assets",       6_580_023_000.0),
        ("total_liabilities",  2_790_000_000.0),
        ("equity",             3_790_023_000.0),
        ("operating_cash_flow",  381_232_000.0),
        ("current_assets",     1_200_000_000.0),
        ("current_liabilities",  800_000_000.0),
    ]:
        _insert_fact(store, fid, field, value, "xbrl", 1.0)
    _set_quality_score(store, fid, 0.95)
    return db_path


# ─── Print helpers for output sections ───────────────────────────────────────

def _print_agent_summary(sig: AgentSignal) -> None:
    """Print one-line summary for an AgentSignal."""
    hc_info = ""
    breaches = [hc for hc in sig.hard_constraints if hc.breached]
    unverif  = [hc for hc in sig.hard_constraints if not hc.verifiable]
    if breaches:
        hc_info += f"  ⛔ BREACH: {', '.join(h.type for h in breaches)}"
    if unverif:
        hc_info += f"  ⚠ UNVERIFIABLE: {', '.join(h.type for h in unverif)}"
    errors_note = f"  [{len(sig.errors)} errors]" if sig.errors else ""

    print(f"  agent      : {sig.agent.value}")
    print(f"  signal     : {sig.signal.value:<10}  confidence={sig.confidence:.2f}"
          f"  horizon={sig.time_horizon.value}{errors_note}")
    print(f"  narrative  : {sig.narrative[:80]}")
    if hc_info:
        print(f"  constraints:{hc_info}")
    if sig.errors:
        for e in sig.errors[:2]:   # show at most 2 errors inline
            _warn(f"  error      : {e[:90]}")


def _print_layer1(out: SupervisorOutput) -> None:
    _section("Supervisor Layer 1 — 硬約束掃描")
    if not out.hard_constraint_breaches and not out.unverifiable_constraints:
        _ok("無任何 hard_constraint breach / unverifiable")
    for agent, hc in out.hard_constraint_breaches:
        print(f"  ⛔ BREACH   [{agent.value}] {hc.type}")
        print(f"              current={hc.current}  limit={hc.limit}")
    for agent, hc in out.unverifiable_constraints:
        print(f"  ⚠ UNVERIF  [{agent.value}] {hc.type}")
        print(f"              current={hc.current}  limit={hc.limit}  verifiable=False")
    print(f"  risk_override      : {out.risk_override}")
    print(f"  mandatory_warnings : {out.mandatory_warnings}")


def _print_layer2(out: SupervisorOutput) -> None:
    _section("Supervisor Layer 2 — Horizon Breakdown")
    if not out.horizon_breakdown:
        _warn("無任何 horizon 結果")
        return
    hdr = f"  {'Horizon':<10}  {'Direction':<10}  {'consensus_share':>16}  {'evidence_conf':>14}"
    print(hdr)
    _bar("─")
    for key, hr in out.horizon_breakdown.items():
        print(f"  {key:<10}  {hr.direction.value:<10}  {hr.consensus_share:>16.2%}  "
              f"{hr.evidence_confidence:>14.2f}")
        agents_str = ", ".join(
            f"{a.value}({s.value},{w:.2f})" for a, s, w in hr.contributing_agents
        )
        print(f"  {'':10}  contributing : {agents_str}")
        if hr.excluded_agents:
            excl_str = ", ".join(a.value for a in hr.excluded_agents)
            print(f"  {'':10}  excluded     : {excl_str}")
    print()
    print("  ┌ consensus_share   = 勝方向加權份額 ÷ 全部有效權重（方向一致度）")
    print("  └ evidence_conf     = Σ(conf×compl×rel) ÷ Σ(rel)（底層信心，compl 不在分母）")


def _print_layer3(out: SupervisorOutput) -> None:
    _section("Supervisor Layer 3 — 信心加權 + 排除清單")
    print(f"  directional_vote_pool : {[a.value for a in out.directional_vote_pool]}")
    if out.excluded_from_voting:
        print(f"  excluded_from_voting  : {[a.value for a in out.excluded_from_voting]}")
        for agent, reason in out.exclusion_reasons.items():
            _warn(f"    {agent.value}: {reason}")
    else:
        _ok("無 agent 被排除於投票")
    if out.background_context:
        ctx_agents = [s.agent.value for s in out.background_context]
        print(f"  background_context    : {ctx_agents}（不參與方向投票）")


def _print_hitl(out: SupervisorOutput) -> None:
    _section("HITL Gate — 人工複核標記")
    icon = "🔴" if out.requires_human_review else "🟢"
    print(f"  requires_human_review : {icon}  {out.requires_human_review}")
    if out.review_reasons:
        print("  review_reasons        :")
        for r in out.review_reasons:
            print(f"    \"{r}\"")
    else:
        _ok("無觸發原因")


def _print_final(out: SupervisorOutput) -> None:
    _section("最終 SupervisorOutput")
    print(f"  overall_recommendation : {out.overall_recommendation.value}")
    print(f"  confidence             : {out.confidence:.2f}")
    print(f"  asof                   : {out.asof}")
    print()
    print("  overall_narrative:")
    _bar("─")
    print(out.overall_narrative)
    _bar("─")
    print()
    print(f"  disclaimer : {out.disclaimer[:90]}")


# ─── Main pipeline ────────────────────────────────────────────────────────────

@observe(name="demo_full_pipeline:run")  # type: ignore[misc]
def run_pipeline() -> SupervisorOutput:
    """Execute all 6 agents + Supervisor and return the final output."""
    update_current_span(input={"symbol": "2330.TW", "demo": True})

    signals: list[AgentSignal] = []

    # ── 1. Risk agent ─────────────────────────────────────────────────────────
    _section("Step 1 — Risk Agent（positions.yaml + MockFX USDTWD=31.50）")
    risk_sig = run_risk_agent(
        fx_adapter=_MockFXAdapter(),
        asof=DEMO_ASOF,
    )
    signals.append(risk_sig)
    _sig_header("risk")
    _print_agent_summary(risk_sig)

    # ── 2. Fundamental agent ──────────────────────────────────────────────────
    _section("Step 2 — Fundamental Agent（2330/2024Q1 XBRL，quality_score=0.95）")
    db_path = _build_fundamental_db()
    fa = FundamentalAgent(db_path)
    fund_sig = fa.run("2330", 2024, "Q1")
    signals.append(fund_sig)
    _sig_header("fundamental")
    _print_agent_summary(fund_sig)

    # ── 3. Technical agent ────────────────────────────────────────────────────
    _section("Step 3 — Technical Agent（2330.TW，synthetic uptrend close 100→120）")
    tech_sig = run_technical_agent(
        symbol="2330.TW",
        market="TW",
        price_adapter=_FakePriceAdapter(),
        asof=DEMO_ASOF,
    )
    signals.append(tech_sig)
    _sig_header("technical")
    _print_agent_summary(tech_sig)

    # ── 4. Cross-market agent ─────────────────────────────────────────────────
    _section("Step 4 — Cross-Market Agent（TAIEX vs S&P 500，correlated synthetic）")
    cm_sig = run_cross_market_agent(
        market="TW",
        cross_adapter=_FakeCrossMarketAdapter(),
        asof=DEMO_ASOF,
    )
    signals.append(cm_sig)
    _sig_header("cross_market")
    _print_agent_summary(cm_sig)

    # ── 5. Macro agent ────────────────────────────────────────────────────────
    _section("Step 5 — Macro Agent（US NFP 261K vs 200K；CPI YoY 2.4% vs 2.3%）")
    macro_sig = run_macro_agent(
        macro_adapter=_DemoMacroAdapter(),  # type: ignore[arg-type]
        asof=DEMO_ASOF,
    )
    signals.append(macro_sig)
    _sig_header("macro")
    _print_agent_summary(macro_sig)

    # ── 6. News agent ─────────────────────────────────────────────────────────
    _section("Step 6 — News Agent（台積電 MOPS + RSS；OpenAI key 存在時走 LLM）")
    has_openai = bool(os.getenv("OPENAI_API_KEY"))
    if has_openai:
        _ok("OPENAI_API_KEY 已設定，news agent 使用真實 LLM 分析")
    else:
        _warn("OPENAI_API_KEY 未設定，news agent 走 LLM 降級路徑（既有 fail-safe）")

    news_sig = run_news_agent(
        symbol="2330.TW",
        market="TW",
        query_terms=["台積電", "TSMC"],
        mops_adapter=_DemoMopsAdapter(),   # type: ignore[arg-type]
        rss_adapter=_DemoRSSAdapter(),     # type: ignore[arg-type]
        tavily_adapter=None,               # skip Tavily（無 key）
        openai_client=None,                # None → agent 自行從 env 讀取 or 降級
        asof=DEMO_ASOF,
    )
    signals.append(news_sig)
    _sig_header("news")
    _print_agent_summary(news_sig)

    # ── Supervisor ────────────────────────────────────────────────────────────
    _section("Supervisor.aggregate() — 六個 AgentSignal 仲裁")
    print(f"  輸入信號數 : {len(signals)}")
    for sig in signals:
        print(f"    {sig.agent.value:<15}  {sig.signal.value:<10}  "
              f"conf={sig.confidence:.2f}  horizon={sig.time_horizon.value}")

    supervisor = Supervisor()
    output = supervisor.aggregate(signals)

    _print_layer1(output)
    _print_layer2(output)
    _print_layer3(output)
    _print_hitl(output)
    _print_final(output)

    update_current_span(output={
        "overall_recommendation": output.overall_recommendation.value,
        "confidence": output.confidence,
        "requires_human_review": output.requires_human_review,
        "review_reasons": output.review_reasons,
    })
    return output


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    print()
    _bar("═")
    print("  QuantDesk — Full Pipeline Demo（六 Agent + Supervisor）")
    print("  Symbol  : 2330.TW  台積電")
    print(f"  Demo as of : {DEMO_ASOF.isoformat()}")
    print(f"  Langfuse tracing : {'ACTIVE' if TRACING_ACTIVE else 'inactive (set LANGFUSE_ENABLED=true)'}")
    _bar("═")

    run_pipeline()

    # flush Langfuse if active
    if TRACING_ACTIVE:
        try:
            from langfuse import get_client
            get_client().flush()
            print()
            _ok("完整 trace 已記錄，可於 Langfuse UI 查看")
        except Exception:
            pass

    print()
    _bar("═")
    print("  Demo 完成。")
    _bar("═")
    print()


if __name__ == "__main__":
    main()
