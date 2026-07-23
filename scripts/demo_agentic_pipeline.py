"""
QuantDesk Agentic Pipeline Demo

展示重構後的真正 Agentic 架構：
  Router LLM → Domain Agents（含真實 FinMind 資料）→ Synthesis LLM → 最終報告

三個情境：
  1. 單標的分析：「2330 現在怎樣」
  2. 籌碼深度分析：「2330 籌碼面怎樣，外資在買嗎」
  3. 總經環境掃描：「現在美國總經環境對台股有何影響」

執行需求：
  .env 設定好以下任一組合即可執行（功能隨可用 key 自動降級）：
  - FINMIND_KEY      → 台股資料（price / chip）
  - OPENAI_API_KEY   → Router LLM + Synthesis LLM
  - LANGFUSE_*       → 可觀測性（非必要）

執行方式：
  uv run python scripts/demo_agentic_pipeline.py
"""
from __future__ import annotations

import os
import textwrap
from datetime import UTC, datetime

# ─── Load .env ────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ─── Langfuse setup ───────────────────────────────────────────────────────────
from observability.langfuse_setup import observe  # noqa: E402


def _divider(title: str = "", width: int = 70) -> None:
    if title:
        pad = (width - len(title) - 2) // 2
        print("\n" + "─" * pad + f" {title} " + "─" * pad)
    else:
        print("\n" + "─" * width)


def _wrap(text: str, width: int = 68, indent: str = "  ") -> str:
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent)


# ─── Scenario 1: 單標的分析 ──────────────────────────────────────────────────

@observe(name="demo:scenario1_single_stock")
def run_scenario_1(query: str = "2330 現在怎樣") -> None:
    """Scenario 1: 單標的分析，展示 Router → Technical + Chip → Synthesis。"""
    from datetime import UTC, datetime

    _divider("情境 1 — 單標的分析")
    print(f"  使用者查詢：{query!r}")

    # Step 1: Router 意圖分類
    _divider("Step 1: Router 意圖分類")
    try:
        from router.intent_router import route
        router_out = route(query)
        print(f"  場景：{router_out.scenario}")
        print(f"  標的：{router_out.targets}")
        print(f"  市場：{router_out.market}")
        print(f"  深度：{router_out.depth}")
        print("  方法：LLM ✓")
    except Exception as exc:
        print(f"  ⚠ Router 失敗（{exc}）→ 使用規則 fallback")
        class _FallbackRouter:
            scenario = "single_stock"
            targets = ["2330"]
            market = "TW"
            depth = "standard"
        router_out = _FallbackRouter()  # type: ignore

    symbol = router_out.targets[0] if router_out.targets else "2330"
    asof = datetime.now(tz=UTC)

    # Step 2: Technical Agent
    _divider("Step 2: Technical Agent（FinMind OHLCV + 確定性指標）")
    technical_report = None
    try:
        from agents.technical_agent import run_technical_agent
        from schemas.domain_report import DomainReport
        from schemas.agent_signal import AgentType, TimeHorizon

        tech_signal = run_technical_agent(symbol=symbol, market="TW", asof=asof)
        print(f"  訊號：{tech_signal.signal.value.upper()}")
        print(f"  信心：{tech_signal.confidence:.0%}")
        print(f"  RSI：{tech_signal.metrics.get('rsi', 'N/A'):.1f}" if isinstance(tech_signal.metrics.get('rsi'), float) else "  RSI：N/A")
        print(f"  MACD 柱狀圖：{tech_signal.metrics.get('macd_hist', 'N/A')}")
        print(f"  描述：{tech_signal.narrative[:80]}...")

        # 轉換為 DomainReport
        technical_report = DomainReport(
            agent=AgentType.TECHNICAL,
            symbol=symbol,
            market="TW",
            asof=asof,
            signal=tech_signal.signal,
            confidence=tech_signal.confidence,
            time_horizon=TimeHorizon.SHORT,
            key_findings=tech_signal.metrics,
            narrative_summary=tech_signal.narrative,
            data_completeness=tech_signal.data_quality.completeness,
        )
    except Exception as exc:
        print(f"  ⚠ Technical Agent 失敗：{exc}")

    # Step 3: Chip Agent
    _divider("Step 3: Chip Agent（FinMind 三大法人 + 融資融券 + 外資持股）")
    chip_report = None
    try:
        from agents.chip_agent import run_chip_agent
        chip_report = run_chip_agent(symbol=symbol, market="TW", asof=asof)
        print(f"  訊號：{chip_report.signal.value.upper()}")
        print(f"  信心：{chip_report.confidence:.0%}")
        kf = chip_report.key_findings
        if "consecutive_days" in kf:
            days = int(kf["consecutive_days"])
            direction = "買超" if days > 0 else "賣超"
            print(f"  外資連續{direction}：{abs(days)} 日")
        if "foreign_ownership_ratio" in kf:
            print(f"  外資持股比例：{kf['foreign_ownership_ratio']:.2f}%")
        if chip_report.narrative_summary:
            print(f"  LLM 摘要：{chip_report.narrative_summary[:100]}...")
        print(f"  資料完整度：{chip_report.data_completeness:.0%}")
    except Exception as exc:
        print(f"  ⚠ Chip Agent 失敗：{exc}")

    # Step 4: Macro Agent (FRED fallback)
    _divider("Step 4: Macro Agent（FRED 免費總經資料）")
    macro_report = None
    try:
        from agents.macro_agent import run_macro_agent
        from schemas.domain_report import DomainReport
        from schemas.agent_signal import AgentType, TimeHorizon

        macro_signal = run_macro_agent(symbol=symbol, market="TW", asof=asof)
        print(f"  訊號：{macro_signal.signal.value.upper()}")
        print(f"  信心：{macro_signal.confidence:.0%}")
        degraded = macro_signal.metrics.get("degraded", False)
        print(f"  資料源：{'FRED（降級）' if degraded else 'TradingEconomics'}")
        print(f"  可計算事件數：{macro_signal.metrics.get('computable_count', 0)}")

        macro_report = DomainReport(
            agent=AgentType.MACRO,
            symbol=symbol,
            market="TW",
            asof=asof,
            signal=macro_signal.signal,
            confidence=macro_signal.confidence,
            time_horizon=TimeHorizon.MEDIUM,
            key_findings=macro_signal.metrics,
            narrative_summary=macro_signal.narrative,
            data_completeness=macro_signal.data_quality.completeness,
        )
    except Exception as exc:
        print(f"  ⚠ Macro Agent 失敗：{exc}")

    # Step 5: Synthesis LLM
    _divider("Step 5: Synthesis LLM（GPT-4o 跨 domain 仲裁）")
    reports = [r for r in [technical_report, chip_report, macro_report] if r is not None]
    try:
        from supervisor.synthesis import synthesize_reports
        synthesis = synthesize_reports(reports=reports, symbol=symbol, scenario="single_stock")
        print(f"  最終訊號：{synthesis.signal.value.upper()}")
        print(f"  信心：{synthesis.confidence:.0%}")
        print(f"  方法：{'GPT-4o' if synthesis.method == 'llm' else '確定性 fallback'}")
        if synthesis.key_drivers:
            print("  核心根據：")
            for d in synthesis.key_drivers:
                print(f"    • {d}")
        if synthesis.key_risks:
            print("  主要風險：")
            for r in synthesis.key_risks:
                print(f"    ⚠ {r}")
        if synthesis.conflicts:
            print("  訊號矛盾：")
            for c in synthesis.conflicts:
                print(f"    ↕ {c}")
        _divider("最終報告")
        if synthesis.narrative:
            print(_wrap(synthesis.narrative))
        else:
            print("  （無 LLM 摘要）")
    except Exception as exc:
        print(f"  ⚠ Synthesis 失敗：{exc}")
        if reports:
            # 簡單 fallback 報告
            signals = [r.signal.value for r in reports]
            print(f"  Domain signals: {dict(zip(['technical', 'chip', 'macro'], signals))}")


# ─── Scenario 2: 籌碼深度分析 ────────────────────────────────────────────────

@observe(name="demo:scenario2_chip_deep")
def run_scenario_2(symbol: str = "2330") -> None:
    """Scenario 2: 籌碼深度分析，展示 Chip Agent 的完整推理步驟。"""
    _divider(f"情境 2 — 籌碼深度分析（{symbol}）")

    from datetime import UTC, datetime
    asof = datetime.now(tz=UTC)

    try:
        from agents.chip_agent import run_chip_agent
        chip_report = run_chip_agent(symbol=symbol, market="TW", asof=asof)

        print(f"\n  最終訊號：{chip_report.signal.value.upper()}")
        print(f"  信心：{chip_report.confidence:.0%}")
        print(f"  時間框架：{chip_report.time_horizon.value}")

        if chip_report.reasoning_steps:
            print("\n  推理步驟（ReAct trace）：")
            for i, step in enumerate(chip_report.reasoning_steps, 1):
                print(f"\n    [{i}] 思考：{step.thought}")
                print(f"        工具：{step.action}")
                print(f"        觀察：{step.observation}")

        print("\n  結構化發現：")
        for k, v in chip_report.key_findings.items():
            if isinstance(v, float):
                print(f"    {k}: {v:.4f}")
            else:
                print(f"    {k}: {v}")

        print("\n  LLM 摘要：")
        print(_wrap(chip_report.narrative_summary or "（無）"))

        if chip_report.errors:
            print(f"\n  ⚠ 管線警告：{'; '.join(chip_report.errors[:3])}")

    except Exception as exc:
        print(f"  ⚠ Chip Agent 失敗：{exc}")


# ─── Scenario 3: 總經環境掃描 ────────────────────────────────────────────────

@observe(name="demo:scenario3_macro_scan")
def run_scenario_3() -> None:
    """Scenario 3: 總經環境掃描，展示 FRED adapter 取代 TradingEconomics。"""
    _divider("情境 3 — 總經環境掃描（FRED 免費資料）")

    from datetime import UTC, datetime
    asof = datetime.now(tz=UTC)

    # 直接呼叫 FREDAdapter 展示取回的真實數據
    _divider("FRED 總經資料擷取")
    try:
        from adapters.fred_adapter import FREDAdapter
        adapter = FREDAdapter()
        sourced = adapter.fetch(series=["Non Farm Payrolls", "CPI", "Unemployment Rate", "Fed Funds Rate"])
        events = sourced.payload.events

        print(f"  成功取得 {len(events)} 筆總經資料：")
        for ev in events[-6:]:  # 最近 6 筆
            actual_str = f"{ev.actual}" if ev.actual is not None else "N/A"
            print(f"    {ev.release_date.strftime('%Y-%m')}  {ev.category:30s}  實際={actual_str} {ev.unit}")
    except Exception as exc:
        print(f"  ⚠ FRED 資料擷取失敗：{exc}")
        events = []

    # 跑 Macro Agent
    _divider("Macro Agent（FRED fallback 自動啟動）")
    try:
        from agents.macro_agent import run_macro_agent
        macro_signal = run_macro_agent(asof=asof)

        print(f"  訊號：{macro_signal.signal.value.upper()}")
        print(f"  信心：{macro_signal.confidence:.0%}")
        print(f"  降級標記：{macro_signal.metrics.get('degraded', False)}")
        print(f"  事件數：{macro_signal.metrics.get('event_count', 0)}")
        print(f"  可計算 surprise 數：{macro_signal.metrics.get('computable_count', 0)}")

        if macro_signal.errors:
            print("\n  系統訊息：")
            for e in macro_signal.errors[:3]:
                print(f"    {e}")

        print(f"\n  總結：{macro_signal.narrative}")
    except Exception as exc:
        print(f"  ⚠ Macro Agent 失敗：{exc}")


# ─── Main ─────────────────────────────────────────────────────────────────────

@observe(name="demo_agentic_pipeline:main")
def main() -> None:
    print("=" * 70)
    print("  QuantDesk Agentic Pipeline Demo")
    print(f"  時間：{datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 70)

    # 顯示可用的 API keys
    print("\n  API Keys 狀態：")
    print(f"  FINMIND_KEY   : {'✓ 已設定' if os.environ.get('FINMIND_KEY') else '✗ 未設定（chip/price 使用 mock）'}")
    print(f"  OPENAI_API_KEY: {'✓ 已設定' if os.environ.get('OPENAI_API_KEY') else '✗ 未設定（Router/Synthesis 使用 fallback）'}")
    print("  FRED          : 免費，無需 Key ✓")

    run_scenario_1("2330 現在怎樣")
    run_scenario_2("2330")
    run_scenario_3()

    _divider("Demo 完成")
    print("  所有情境執行完畢。")
    print("  Langfuse traces：http://localhost:3000（若已啟動）")


if __name__ == "__main__":
    main()
