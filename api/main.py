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
import platform
from collections.abc import AsyncGenerator, Coroutine
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

load_dotenv()

# Maximum concurrent SSE analysis requests — prevents thread-pool exhaustion
# under high load (each analysis spawns ~7 threads).
_ANALYSIS_SEMAPHORE = asyncio.Semaphore(5)

# Per-agent wall-clock timeout in seconds.
# News agent with 2 RSS feeds × 10s HTTP timeout + LLM = ~25s worst case.
_AGENT_TIMEOUT: float = 45.0

app = FastAPI(title="QuantDesk API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


# ─── Serializers ──────────────────────────────────────────────────────────────


def _safe_val(v: Any) -> Any:
    """把 key_findings 裡的值轉成 JSON 安全型別。含 numpy 純量處理。"""
    if v is None:
        return v
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, str)):
        return v
    # numpy scalar (np.float64, np.int64 …) — convert to Python native
    try:
        f = float(v)
        return int(f) if f == int(f) and abs(f) < 2**53 else f
    except (TypeError, ValueError):
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
    # Serialize individual hard constraint details for frontend display.
    # Deduplicate: a constraint can appear in both hard_constraint_breaches
    # and unverifiable_constraints — show it only once.
    hc_details = []
    _seen_hc: set[tuple[str, str]] = set()
    for agent, hc in output.hard_constraint_breaches + output.unverifiable_constraints:
        key = (agent.value, hc.type)
        if key in _seen_hc:
            continue
        _seen_hc.add(key)
        hc_details.append({
            "agent": agent.value,
            "type": hc.type,
            "current": hc.current,
            "limit": hc.limit,
            "breached": hc.breached,
            "verifiable": hc.verifiable,
            "detail": hc.detail,
        })

    return {
        "signal": output.overall_recommendation.value,
        "confidence": output.confidence,
        "risk_override": output.risk_override,
        "requires_human_review": output.requires_human_review,
        "narrative": output.overall_narrative,
        "mandatory_warnings": output.mandatory_warnings,
        "review_reasons": output.review_reasons,
        "horizon_breakdown": horizon,
        "hard_constraint_details": hc_details,
    }


def _sse(event_type: str, payload: dict[str, Any]) -> str:
    return f"data: {json.dumps({'type': event_type, 'payload': payload}, ensure_ascii=False)}\n\n"


# ─── SSE Stream Generator ─────────────────────────────────────────────────────


_FUNDAMENTAL_DB_PATH = os.environ.get(
    "FINANCIAL_DB_PATH",
    r"C:\Users\johnn\GITHUB_REPO\FinancialReports\data\financial.db"
    if platform.system() == "Windows"
    else "/mnt/c/Users/johnn/GITHUB_REPO/FinancialReports/data/financial.db",
)


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
    SSE generator with semaphore, parallel agents, and per-agent timeouts.

    Concurrency design
    ------------------
    All domain agents run in parallel via asyncio tasks + queue.  Each agent is
    wrapped with asyncio.wait_for(_AGENT_TIMEOUT) so a stalled HTTP call (e.g.
    RSS feed with no timeout) cannot block the entire pipeline.  The queue lets
    the SSE stream emit agent_done events in completion order, giving the client
    progressive updates instead of a single batch.

    High-concurrency safety
    -----------------------
    _ANALYSIS_SEMAPHORE limits concurrent analyses to 5, preventing thread-pool
    exhaustion when many users connect simultaneously.
    """

    # ── Queue-based parallel agent runner ─────────────────────────────────────
    # Each agent task puts ("start"|"done"|"error", agent_name, data) into the
    # queue.  The main loop drains the queue and yields SSE events progressively.

    _AgentQueueItem = tuple[str, str, Any]  # (kind, agent_name, data)

    async def _run_agent(
        name: str,
        coro: Coroutine[Any, Any, Any],
        queue: asyncio.Queue[_AgentQueueItem],
    ) -> None:
        await queue.put(("start", name, None))
        try:
            result = await asyncio.wait_for(coro, timeout=_AGENT_TIMEOUT)
            await queue.put(("done", name, result))
        except asyncio.TimeoutError:
            await queue.put(("error", name, f"timeout after {_AGENT_TIMEOUT:.0f}s"))
        except Exception as exc:  # noqa: BLE001
            await queue.put(("error", name, str(exc)[:120]))

    async with _ANALYSIS_SEMAPHORE:
        try:
            asof = datetime.now(tz=UTC)

            # ── Step 1: Router ────────────────────────────────────────────────
            try:
                from router.intent_router import route  # noqa: PLC0415
                router_out = await asyncio.wait_for(
                    asyncio.to_thread(route, query), timeout=30.0
                )
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

            # ── Step 2: Build agent coroutines ────────────────────────────────
            # Lazy imports inside coroutines keep module load fast.

            from schemas.agent_signal import AgentType, TimeHorizon  # noqa: PLC0415
            from schemas.domain_report import DomainReport  # noqa: PLC0415

            async def _technical() -> Any:
                from agents.technical_agent import run_technical_agent  # noqa: PLC0415
                sig = await asyncio.to_thread(
                    run_technical_agent, symbol=symbol, market=market, asof=asof
                )
                return DomainReport(
                    agent=AgentType.TECHNICAL, symbol=symbol, market=market, asof=asof,
                    signal=sig.signal, confidence=sig.confidence,
                    time_horizon=TimeHorizon.SHORT, key_findings=sig.metrics,
                    narrative_summary=sig.narrative,
                    data_completeness=sig.data_quality.completeness,
                )

            async def _chip() -> Any:
                from agents.chip_agent import run_chip_agent  # noqa: PLC0415
                return await asyncio.to_thread(
                    run_chip_agent, symbol=symbol, market=market, asof=asof
                )

            async def _macro() -> Any:
                from agents.macro_agent import run_macro_agent  # noqa: PLC0415
                sig = await asyncio.to_thread(
                    run_macro_agent, symbol=symbol, market=market, asof=asof
                )
                return DomainReport(
                    agent=AgentType.MACRO, symbol=symbol, market=market, asof=asof,
                    signal=sig.signal, confidence=sig.confidence,
                    time_horizon=TimeHorizon.MEDIUM, key_findings=sig.metrics,
                    narrative_summary=sig.narrative,
                    data_completeness=sig.data_quality.completeness,
                )

            async def _news() -> Any:
                from agents.news_agent import run_news_agent  # noqa: PLC0415
                sig = await asyncio.to_thread(
                    run_news_agent, symbol=symbol, market=market, asof=asof
                )
                return DomainReport(
                    agent=AgentType.NEWS, symbol=symbol, market=market, asof=asof,
                    signal=sig.signal, confidence=sig.confidence,
                    time_horizon=TimeHorizon.SHORT, key_findings=sig.metrics,
                    narrative_summary=sig.narrative,
                    data_completeness=sig.data_quality.completeness,
                )

            async def _cross_market() -> Any:
                from agents.cross_market_agent import run_cross_market_agent  # noqa: PLC0415
                sig = await asyncio.to_thread(
                    run_cross_market_agent, market=market, asof=asof
                )
                return DomainReport(
                    agent=AgentType.CROSS_MARKET, symbol=symbol, market=market, asof=asof,
                    signal=sig.signal, confidence=sig.confidence,
                    time_horizon=TimeHorizon.MEDIUM, key_findings=sig.metrics,
                    narrative_summary=sig.narrative,
                    data_completeness=sig.data_quality.completeness,
                )

            async def _fundamental() -> Any:
                from adapters.fundamental_adapter import FundamentalAdapter as _FundamentalAdapter  # noqa: PLC0415
                from agents.fundamental_agent import FundamentalAgent  # noqa: PLC0415
                if not os.path.exists(_FUNDAMENTAL_DB_PATH):
                    raise FileNotFoundError("DB not found")
                # Use the latest insight_ready filing in the DB rather than a
                # calendar-derived quarter — avoids empty results when the current
                # quarter has not been processed yet.
                adapter = _FundamentalAdapter(_FUNDAMENTAL_DB_PATH)
                filing = adapter.get_latest_filing(symbol)
                if filing is None:
                    raise ValueError(f"No financial data found for {symbol}")
                year, quarter = filing
                sig = await asyncio.to_thread(
                    FundamentalAgent(_FUNDAMENTAL_DB_PATH).run,
                    symbol, year, quarter,
                )
                return DomainReport(
                    agent=AgentType.FUNDAMENTAL, symbol=symbol, market=market, asof=asof,
                    signal=sig.signal, confidence=sig.confidence,
                    time_horizon=TimeHorizon.LONG, key_findings=sig.metrics,
                    hard_constraints=sig.hard_constraints,
                    narrative_summary=sig.narrative,
                    data_completeness=sig.data_quality.completeness,
                )

            async def _risk() -> Any:
                from agents.risk_agent import run_risk_agent  # noqa: PLC0415
                sig = await asyncio.to_thread(run_risk_agent, asof=asof)
                return DomainReport(
                    agent=AgentType.RISK, symbol=symbol, market=market, asof=asof,
                    signal=sig.signal, confidence=sig.confidence,
                    time_horizon=TimeHorizon.SHORT,
                    key_findings={k: _safe_val(v) for k, v in sig.metrics.items()},
                    hard_constraints=sig.hard_constraints,
                    narrative_summary=sig.narrative,
                    data_completeness=sig.data_quality.completeness,
                )

            # ── Step 3: Launch all agents in parallel ─────────────────────────
            queue: asyncio.Queue[_AgentQueueItem] = asyncio.Queue()
            agent_defs = [
                ("technical",   _technical()),
                ("chip",        _chip()),
                ("macro",       _macro()),
                ("news",        _news()),
                ("cross_market", _cross_market()),
                ("fundamental", _fundamental()),
                ("risk",        _risk()),
            ]
            n_agents = len(agent_defs)
            tasks = [
                asyncio.create_task(_run_agent(name, coro, queue))
                for name, coro in agent_defs
            ]

            # Drain the queue as agents complete; emit SSE events progressively.
            reports: list[Any] = []
            n_done = 0
            while n_done < n_agents:
                kind, name, data = await queue.get()
                if kind == "start":
                    yield _sse("agent_start", {"agent": name})
                elif kind == "done":
                    n_done += 1
                    reports.append(data)
                    yield _sse("agent_done", _serialize_agent(data))
                else:  # "error"
                    n_done += 1
                    yield _sse("agent_error", {"agent": name, "error": data})

            # Ensure all tasks are properly awaited (they should be done by now).
            await asyncio.gather(*tasks, return_exceptions=True)

            if not reports:
                yield _sse("error", {"message": "所有 domain agents 均失敗，無法進行仲裁。"})
                yield _sse("done", {})
                return

            # ── Step 4: Debate + Supervisor ───────────────────────────────────
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
        "finmind": "set" if os.environ.get("FINMIND_TOKEN") or os.environ.get("FINMIND_KEY") else "unset",
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
