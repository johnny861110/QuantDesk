"""
QuantDesk FastAPI Backend — SSE Streaming Analysis

啟動方式：
  uv run uvicorn api.main:app --reload --port 8000

Endpoints:
  GET  /health                          → 健康檢查
  GET  /api/analyze/stream?query=...    → SSE 分析事件串流

SSE Event Schema（全部 JSON，data: {...}\\n\\n 格式）：
  {"type": "router",       "payload": RouterPayload}
  {"type": "agent_start",  "payload": {"agent": str}}
  {"type": "agent_done",   "payload": AgentDonePayload}
  {"type": "agent_error",  "payload": {"agent": str, "error": str}}
  {"type": "debate_start", "payload": {}}
  {"type": "debate_bull",  "payload": DebatePartyPayload}
  {"type": "debate_bear",  "payload": DebatePartyPayload}
  {"type": "debate_pm",    "payload": DebatePMPayload}
  {"type": "supervisor",   "payload": SupervisorPayload}
  {"type": "done",         "payload": {}}
  {"type": "error",        "payload": {"message": str}}
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

load_dotenv()

app = FastAPI(title="QuantDesk API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


# ─── Serializers ──────────────────────────────────────────────────────────────


def _safe_val(v: Any) -> Any:
    """把 key_findings 裡的值轉成 JSON 安全型別。"""
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def _serialize_agent(report: Any) -> dict[str, Any]:
    return {
        "agent": report.agent.value,
        "signal": report.signal.value,
        "confidence": report.confidence,
        "time_horizon": report.time_horizon.value,
        "data_completeness": report.data_completeness,
        "key_findings": {k: _safe_val(v) for k, v in report.key_findings.items()},
        "narrative_summary": report.narrative_summary or "",
        "errors": report.errors[:2] if report.errors else [],
    }


def _serialize_supervisor(output: Any) -> dict[str, Any]:
    horizon: dict[str, Any] = {}
    for key, result in output.horizon_breakdown.items():
        horizon[key] = {
            "direction": result.direction.value,
            "evidence_confidence": result.evidence_confidence,
            "agents": [a.value for a, _, _ in result.contributing_agents],
        }
    return {
        "signal": output.overall_recommendation.value,
        "confidence": output.confidence,
        "risk_override": output.risk_override,
        "requires_human_review": output.requires_human_review,
        "narrative": output.overall_narrative,
        "mandatory_warnings": output.mandatory_warnings,
        "review_reasons": output.review_reasons,
        "horizon_breakdown": horizon,
    }


def _sse(event_type: str, payload: dict[str, Any]) -> str:
    return f"data: {json.dumps({'type': event_type, 'payload': payload}, ensure_ascii=False)}\n\n"


# ─── SSE Stream Generator ─────────────────────────────────────────────────────


_FUNDAMENTAL_DB_PATH = "/mnt/c/Users/johnn/GITHUB_REPO/FinancialReports/data/financial.db"


def _current_year_quarter() -> tuple[int, str]:
    """Return the most recently *completed* quarter as (year, 'Q1'|'Q2'|'Q3'|'Q4')."""
    now = datetime.now(tz=UTC)
    # Q1 ends Mar, Q2 ends Jun, Q3 ends Sep, Q4 ends Dec
    # Use the quarter that ended at least one month ago (data lag)
    month = now.month - 1  # shift back 1 month for data availability
    if month <= 0:
        month += 12
        year = now.year - 1
    else:
        year = now.year
    quarter = f"Q{(month - 1) // 3 + 1}"
    return year, quarter


async def _stream_analysis(query: str) -> AsyncGenerator[str, None]:  # noqa: C901, PLR0912, PLR0915
    """
    全流程 SSE 生成器：
      Router → Technical + Chip + Macro + News + CrossMarket + Fundamental → aggregate_debate()
    """
    try:
        asof = datetime.now(tz=UTC)

        # ── Step 1: Router ────────────────────────────────────────────────────
        try:
            from router.intent_router import route  # noqa: PLC0415
            router_out = await asyncio.to_thread(route, query)
            yield _sse("router", {
                "scenario": router_out.scenario,
                "targets": router_out.targets,
                "market": router_out.market,
                "depth": router_out.depth,
                "method": "llm",
            })
        except Exception as exc:  # noqa: BLE001
            yield _sse("router", {
                "scenario": "single_stock",
                "targets": ["2330"],
                "market": "TW",
                "depth": "standard",
                "method": "fallback",
                "error": str(exc)[:80],
            })
            router_out = type("R", (), {  # type: ignore[assignment]
                "scenario": "single_stock",
                "targets": ["2330"],
                "market": "TW",
                "depth": "standard",
            })()

        symbol: str = router_out.targets[0] if router_out.targets else "2330"
        market: str = getattr(router_out, "market", "TW")
        reports: list[Any] = []

        # ── Step 2: Domain Agents ─────────────────────────────────────────────

        # Technical
        yield _sse("agent_start", {"agent": "technical"})
        try:
            from agents.technical_agent import run_technical_agent  # noqa: PLC0415
            from schemas.agent_signal import AgentType, TimeHorizon  # noqa: PLC0415
            from schemas.domain_report import DomainReport  # noqa: PLC0415

            tech_signal = await asyncio.to_thread(
                run_technical_agent, symbol=symbol, market=market, asof=asof
            )
            tech_report = DomainReport(
                agent=AgentType.TECHNICAL,
                symbol=symbol,
                market=market,
                asof=asof,
                signal=tech_signal.signal,
                confidence=tech_signal.confidence,
                time_horizon=TimeHorizon.SHORT,
                key_findings=tech_signal.metrics,
                narrative_summary=tech_signal.narrative,
                data_completeness=tech_signal.data_quality.completeness,
            )
            reports.append(tech_report)
            yield _sse("agent_done", _serialize_agent(tech_report))
        except Exception as exc:  # noqa: BLE001
            yield _sse("agent_error", {"agent": "technical", "error": str(exc)[:120]})

        # Chip
        yield _sse("agent_start", {"agent": "chip"})
        try:
            from agents.chip_agent import run_chip_agent  # noqa: PLC0415

            chip_report = await asyncio.to_thread(
                run_chip_agent, symbol=symbol, market=market, asof=asof
            )
            reports.append(chip_report)
            yield _sse("agent_done", _serialize_agent(chip_report))
        except Exception as exc:  # noqa: BLE001
            yield _sse("agent_error", {"agent": "chip", "error": str(exc)[:120]})

        # Macro
        yield _sse("agent_start", {"agent": "macro"})
        try:
            from agents.macro_agent import run_macro_agent  # noqa: PLC0415
            from schemas.agent_signal import AgentType, TimeHorizon  # noqa: PLC0415
            from schemas.domain_report import DomainReport  # noqa: PLC0415

            macro_signal = await asyncio.to_thread(
                run_macro_agent, symbol=symbol, market=market, asof=asof
            )
            macro_report = DomainReport(
                agent=AgentType.MACRO,
                symbol=symbol,
                market=market,
                asof=asof,
                signal=macro_signal.signal,
                confidence=macro_signal.confidence,
                time_horizon=TimeHorizon.MEDIUM,
                key_findings=macro_signal.metrics,
                narrative_summary=macro_signal.narrative,
                data_completeness=macro_signal.data_quality.completeness,
            )
            reports.append(macro_report)
            yield _sse("agent_done", _serialize_agent(macro_report))
        except Exception as exc:  # noqa: BLE001
            yield _sse("agent_error", {"agent": "macro", "error": str(exc)[:120]})

        # News
        yield _sse("agent_start", {"agent": "news"})
        try:
            from agents.news_agent import run_news_agent  # noqa: PLC0415
            from schemas.agent_signal import AgentType, TimeHorizon  # noqa: PLC0415
            from schemas.domain_report import DomainReport  # noqa: PLC0415

            news_signal = await asyncio.to_thread(
                run_news_agent, symbol=symbol, market=market, asof=asof
            )
            news_report = DomainReport(
                agent=AgentType.NEWS,
                symbol=symbol,
                market=market,
                asof=asof,
                signal=news_signal.signal,
                confidence=news_signal.confidence,
                time_horizon=TimeHorizon.SHORT,
                key_findings=news_signal.metrics,
                narrative_summary=news_signal.narrative,
                data_completeness=news_signal.data_quality.completeness,
            )
            reports.append(news_report)
            yield _sse("agent_done", _serialize_agent(news_report))
        except Exception as exc:  # noqa: BLE001
            yield _sse("agent_error", {"agent": "news", "error": str(exc)[:120]})

        # Cross Market (no symbol param — analyzes market-wide cross-asset signals)
        yield _sse("agent_start", {"agent": "cross_market"})
        try:
            from agents.cross_market_agent import run_cross_market_agent  # noqa: PLC0415
            from schemas.agent_signal import AgentType, TimeHorizon  # noqa: PLC0415
            from schemas.domain_report import DomainReport  # noqa: PLC0415

            cm_signal = await asyncio.to_thread(
                run_cross_market_agent, market=market, asof=asof
            )
            cm_report = DomainReport(
                agent=AgentType.CROSS_MARKET,
                symbol=symbol,
                market=market,
                asof=asof,
                signal=cm_signal.signal,
                confidence=cm_signal.confidence,
                time_horizon=TimeHorizon.MEDIUM,
                key_findings=cm_signal.metrics,
                narrative_summary=cm_signal.narrative,
                data_completeness=cm_signal.data_quality.completeness,
            )
            reports.append(cm_report)
            yield _sse("agent_done", _serialize_agent(cm_report))
        except Exception as exc:  # noqa: BLE001
            yield _sse("agent_error", {"agent": "cross_market", "error": str(exc)[:120]})

        # Fundamental (requires FinancialReports SQLite DB)
        yield _sse("agent_start", {"agent": "fundamental"})
        try:
            import os  # noqa: PLC0415
            from agents.fundamental_agent import FundamentalAgent  # noqa: PLC0415
            from schemas.agent_signal import AgentType, TimeHorizon  # noqa: PLC0415
            from schemas.domain_report import DomainReport  # noqa: PLC0415

            if os.path.exists(_FUNDAMENTAL_DB_PATH):
                year, quarter = _current_year_quarter()
                fund_signal = await asyncio.to_thread(
                    FundamentalAgent(_FUNDAMENTAL_DB_PATH).run,
                    symbol, year, quarter,
                )
                fund_report = DomainReport(
                    agent=AgentType.FUNDAMENTAL,
                    symbol=symbol,
                    market=market,
                    asof=asof,
                    signal=fund_signal.signal,
                    confidence=fund_signal.confidence,
                    time_horizon=TimeHorizon.LONG,
                    key_findings=fund_signal.metrics,
                    hard_constraints=fund_signal.hard_constraints,
                    narrative_summary=fund_signal.narrative,
                    data_completeness=fund_signal.data_quality.completeness,
                )
                reports.append(fund_report)
                yield _sse("agent_done", _serialize_agent(fund_report))
            else:
                yield _sse("agent_error", {"agent": "fundamental", "error": "DB not found"})
        except Exception as exc:  # noqa: BLE001
            yield _sse("agent_error", {"agent": "fundamental", "error": str(exc)[:120]})

        if not reports:
            yield _sse("error", {"message": "所有 domain agents 均失敗，無法進行仲裁。"})
            yield _sse("done", {})
            return

        # ── Step 3: Debate + Supervisor ───────────────────────────────────────
        yield _sse("debate_start", {})
        try:
            from supervisor.graph import Supervisor  # noqa: PLC0415

            sup_out, debate_out = await Supervisor().aggregate_debate(
                domain_reports=reports,
                symbol=symbol,
                scenario=getattr(router_out, "scenario", "single_stock"),
            )

            yield _sse("debate_bull", {
                "thesis": debate_out.bull.thesis,
                "key_points": debate_out.bull.key_points,
                "confidence": debate_out.bull.confidence,
            })
            yield _sse("debate_bear", {
                "thesis": debate_out.bear.thesis,
                "key_points": debate_out.bear.key_points,
                "confidence": debate_out.bear.confidence,
            })
            yield _sse("debate_pm", {
                "thesis": debate_out.pm_verdict.thesis,
                "key_points": debate_out.pm_verdict.key_points,
                "confidence": debate_out.pm_verdict.confidence,
                "signal": debate_out.final_signal.value,
            })
            yield _sse("supervisor", _serialize_supervisor(sup_out))

        except Exception as exc:  # noqa: BLE001
            yield _sse("error", {"message": f"Debate/Supervisor 失敗: {str(exc)[:120]}"})

        yield _sse("done", {})

    except Exception as exc:  # noqa: BLE001
        yield _sse("error", {"message": str(exc)[:200]})
        yield _sse("done", {})


# ─── Routes ───────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "finmind": "set" if os.environ.get("FINMIND_KEY") else "unset",
        "openai": "set" if os.environ.get("OPENAI_API_KEY") else "unset",
    }


@app.get("/api/analyze/stream")
async def analyze_stream(query: str) -> StreamingResponse:
    """SSE endpoint — 接收自然語言查詢，串流回傳分析事件。"""
    return StreamingResponse(
        _stream_analysis(query),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
