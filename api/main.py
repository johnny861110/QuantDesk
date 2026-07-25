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
    """Serialize DomainReport to SSE payload including full intermediate metadata."""
    # Full key_findings (all keys, not truncated)
    full_findings = {k: _safe_val(v) for k, v in report.key_findings.items()}

    # Hard constraints per-agent (if any)
    hc_list = []
    for hc in getattr(report, "hard_constraints", []):
        hc_list.append({
            "type": hc.type,
            "current": hc.current,
            "limit": hc.limit,
            "breached": hc.breached,
            "verifiable": hc.verifiable,
            "detail": hc.detail,
        })

    # asof timestamp
    asof = getattr(report, "asof", None)
    asof_iso = asof.isoformat() if asof else None

    return {
        "agent": report.agent.value,
        "signal": report.signal.value,
        "confidence": report.confidence,
        "time_horizon": report.time_horizon.value,
        "data_completeness": report.data_completeness,
        "key_findings": full_findings,
        "narrative_summary": report.narrative_summary or "",
        "errors": report.errors or [],        # full list, not truncated
        "hard_constraints": hc_list,          # per-agent constraint details
        "asof": asof_iso,                     # data timestamp
        "symbol": getattr(report, "symbol", None),
        "market": getattr(report, "market", None),
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


def _expected_latest_quarter() -> tuple[int, str]:
    """
    Return the latest quarter whose public filing deadline has passed,
    based on Taiwan TWSE disclosure rules:

      Q1 (Jan–Mar)  → deadline May 15   → available from May 16
      Q2 (Apr–Jun)  → deadline Aug 14   → available from Aug 15
      Q3 (Jul–Sep)  → deadline Nov 14   → available from Nov 15
      Q4 (Oct–Dec)  → deadline Mar 31+1 → available from Apr 1 next year

    This is the quarter we should try to crawl when data is missing.
    """
    now = datetime.now(tz=UTC)
    y, m, d = now.year, now.month, now.day

    if (m > 5) or (m == 5 and d >= 16):
        # Q1 is available
        if (m > 8) or (m == 8 and d >= 15):
            # Q2 is available
            if (m > 11) or (m == 11 and d >= 15):
                return y, "Q3"  # Q3 available
            return y, "Q2"
        return y, "Q1"
    else:
        # Before May 16 — Q4 of previous year is the latest
        return y - 1, "Q4"


async def _stream_analysis(query: str) -> AsyncGenerator[str, None]:
    """SSE generator — runs Router LLM then delegates to _stream_analysis_with_router."""
    try:
        from router.intent_router import route, _QUERY_TYPE_AGENTS  # noqa: PLC0415
        from schemas.domain_report import RouterOutput  # noqa: PLC0415
        router_out = await asyncio.wait_for(
            asyncio.to_thread(route, query), timeout=30.0
        )
        yield _sse("router", {
            "scenario": router_out.scenario,
            "targets": router_out.targets,
            "market": router_out.market,
            "depth": router_out.depth,
            "query_type": getattr(router_out, "query_type", "stock_analysis"),
            "agents": getattr(router_out, "agents", []),
            "method": "llm",
        })
    except Exception as exc:  # noqa: BLE001
        from router.intent_router import _QUERY_TYPE_AGENTS  # noqa: PLC0415
        from schemas.domain_report import RouterOutput  # noqa: PLC0415
        yield _sse("router", {
            "scenario": "single_stock", "targets": ["2330"],
            "market": "TW", "depth": "standard",
            "query_type": "stock_analysis",
            "agents": _QUERY_TYPE_AGENTS["stock_analysis"],
            "method": "fallback", "error": str(exc)[:80],
        })
        router_out = RouterOutput(
            scenario="single_stock", targets=["2330"], market="TW",
            query_type="stock_analysis",
            agents=_QUERY_TYPE_AGENTS["stock_analysis"],
            run_supervisor=False, run_debate=False,
        )

    async for event in _stream_analysis_with_router(router_out):
        yield event


# ─── Routes ───────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    finmind_token = os.environ.get("FINMIND_TOKEN") or os.environ.get("FINMIND_KEY") or ""
    openai_key = os.environ.get("OPENAI_API_KEY") or ""

    # Quick FinMind token validation (non-blocking HEAD request)
    finmind_status = "unset"
    if finmind_token:
        try:
            import httpx  # noqa: PLC0415
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    "https://api.finmindtrade.com/api/v4/user_info",
                    headers={"Authorization": f"Bearer {finmind_token}"},
                )
            finmind_status = "valid" if resp.status_code == 200 else "invalid"
        except Exception:  # noqa: BLE001
            finmind_status = "set_but_unreachable"

    return {
        "status": "ok",
        "finmind": finmind_status,
        "openai": "set" if openai_key else "unset",
    }


def _sse_response(generator: AsyncGenerator[str, None]) -> StreamingResponse:
    """Wrap an async SSE generator in a StreamingResponse with correct headers."""
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/analyze/stream")
async def analyze_stream(query: str) -> StreamingResponse:
    """SSE endpoint — 接收自然語言查詢，Router 自動決定 query_type 與 agent 組合。"""
    return _sse_response(_stream_analysis(query))


# ─── 快捷查詢類型 Endpoints ────────────────────────────────────────────────────


async def _stream_preset(
    query_type: str,
    symbol: str,
    market: str = "TW",
) -> AsyncGenerator[str, None]:
    """
    直接用 query_type preset 跑分析，略過 Router LLM。
    適合前端已知查詢類型時使用（快速、省 LLM token）。
    """
    from router.intent_router import _QUERY_TYPE_AGENTS  # noqa: PLC0415
    from schemas.domain_report import RouterOutput  # noqa: PLC0415

    agents = _QUERY_TYPE_AGENTS.get(query_type, _QUERY_TYPE_AGENTS["stock_analysis"])
    fake_router = RouterOutput(
        scenario="single_stock",
        targets=[symbol],
        market=market,
        depth="standard",
        original_query=f"{symbol} {query_type}",
        query_type=query_type,  # type: ignore[arg-type]
        agents=agents,
        run_supervisor=query_type == "investment_strategy",
        run_debate=query_type == "investment_strategy",
    )

    # Emit synthetic router event so frontend knows what was routed
    yield _sse("router", {
        "scenario": fake_router.scenario,
        "targets": fake_router.targets,
        "market": fake_router.market,
        "depth": fake_router.depth,
        "query_type": fake_router.query_type,
        "agents": fake_router.agents,
        "method": "preset",
    })

    # Reuse _stream_analysis but inject the preset router output.
    # We do this by building a minimal query string that the router would
    # interpret correctly — but since we already have router_out we patch it
    # via a thin wrapper.
    async for event in _stream_analysis_with_router(fake_router):
        yield event


async def _stream_analysis_with_router(
    router_out: Any,
) -> AsyncGenerator[str, None]:
    """
    Internal: run the agent pipeline with a pre-built RouterOutput.
    Skips the Router LLM step; everything else is identical to _stream_analysis.
    """
    from schemas.agent_signal import AgentType, TimeHorizon  # noqa: PLC0415
    from schemas.domain_report import DomainReport  # noqa: PLC0415
    from router.intent_router import _QUERY_TYPE_AGENTS  # noqa: PLC0415

    _AgentQueueItem = tuple[str, str, Any]

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
            symbol: str = router_out.targets[0] if router_out.targets else "2330"
            market: str = getattr(router_out, "market", "TW")
            active_agents: list[str] = (
                getattr(router_out, "agents", None)
                or _QUERY_TYPE_AGENTS["stock_analysis"]
            )
            run_supervisor: bool = getattr(router_out, "run_supervisor", False)
            run_debate: bool = getattr(router_out, "run_debate", False)

            # Reuse the same coroutine builders from _stream_analysis
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

            # Shared queue for fundamental crawl progress events.
            # The main drain loop below picks these up and emits SSE events.
            _crawl_progress: asyncio.Queue[dict[str, str]] = asyncio.Queue()

            async def _fundamental() -> Any:
                from adapters.fundamental_adapter import FundamentalAdapter as _FA  # noqa: PLC0415
                from agents.fundamental_agent import FundamentalAgent  # noqa: PLC0415
                if not os.path.exists(_FUNDAMENTAL_DB_PATH):
                    raise FileNotFoundError("DB not found")
                adapter = _FA(_FUNDAMENTAL_DB_PATH)
                filing = adapter.get_latest_filing(symbol)
                if filing is None:
                    exp_year, exp_quarter = _expected_latest_quarter()
                    crawl_ok = await asyncio.wait_for(
                        adapter.crawl_if_missing(
                            symbol, exp_year, exp_quarter,
                            progress_queue=_crawl_progress,
                        ),
                        timeout=300.0,
                    )
                    if not crawl_ok:
                        raise ValueError(f"爬取 {symbol} 失敗，無財報資料")
                    filing = adapter.get_latest_filing(symbol)
                if filing is None:
                    raise ValueError(f"No financial data found for {symbol}")
                year, quarter = filing
                sig = await asyncio.to_thread(
                    FundamentalAgent(_FUNDAMENTAL_DB_PATH).run, symbol, year, quarter,
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

            _ALL_BUILDERS: dict[str, Any] = {
                "technical": _technical, "chip": _chip, "macro": _macro,
                "news": _news, "cross_market": _cross_market,
                "fundamental": _fundamental, "risk": _risk,
            }

            queue: asyncio.Queue[_AgentQueueItem] = asyncio.Queue()
            agent_defs = [
                (name, builder())
                for name, builder in _ALL_BUILDERS.items()
                if name in active_agents
            ]
            n_agents = len(agent_defs)
            tasks = [
                asyncio.create_task(_run_agent(name, coro, queue))
                for name, coro in agent_defs
            ]

            reports: list[Any] = []
            n_done = 0
            while n_done < n_agents:
                # Drain crawl progress events (non-blocking) before waiting for agents
                while not _crawl_progress.empty():
                    prog = _crawl_progress.get_nowait()
                    yield _sse("fundamental_crawl", prog)

                kind, name, data = await queue.get()
                if kind == "start":
                    yield _sse("agent_start", {"agent": name})
                elif kind == "done":
                    n_done += 1
                    reports.append(data)
                    yield _sse("agent_done", _serialize_agent(data))
                else:
                    n_done += 1
                    yield _sse("agent_error", {"agent": name, "error": data})

            await asyncio.gather(*tasks, return_exceptions=True)

            if not reports or not run_supervisor:
                yield _sse("done", {})
                return

            yield _sse("debate_start", {})
            try:
                from supervisor.graph import Supervisor  # noqa: PLC0415
                if run_debate:
                    sup_out, debate_out = await Supervisor().aggregate_debate(
                        domain_reports=reports, symbol=symbol,
                        scenario=getattr(router_out, "scenario", "single_stock"),
                    )
                    yield _sse("debate_bull", {"thesis": debate_out.bull.thesis, "key_points": debate_out.bull.key_points, "confidence": debate_out.bull.confidence})
                    yield _sse("debate_bear", {"thesis": debate_out.bear.thesis, "key_points": debate_out.bear.key_points, "confidence": debate_out.bear.confidence})
                    yield _sse("debate_pm", {"thesis": debate_out.pm_verdict.thesis, "key_points": debate_out.pm_verdict.key_points, "confidence": debate_out.pm_verdict.confidence, "signal": debate_out.final_signal.value})
                else:
                    from schemas.domain_report import domain_report_to_agent_signal  # noqa: PLC0415
                    signals = [domain_report_to_agent_signal(r) for r in reports]
                    sup_out = await asyncio.to_thread(Supervisor().aggregate, signals)
                yield _sse("supervisor", _serialize_supervisor(sup_out))
            except Exception as exc:  # noqa: BLE001
                yield _sse("error", {"message": f"Supervisor 失敗: {str(exc)[:120]}"})

            yield _sse("done", {})

        except Exception as exc:  # noqa: BLE001
            yield _sse("error", {"message": str(exc)[:200]})
            yield _sse("done", {})


# ─── 快捷查詢 Endpoints ────────────────────────────────────────────────────────

@app.get("/api/analyze/stock")
async def analyze_stock(symbol: str, market: str = "TW") -> StreamingResponse:
    """個股技術/籌碼/新聞分析（technical + chip + news）。不含 Supervisor。"""
    return _sse_response(_stream_preset("stock_analysis", symbol, market))


@app.get("/api/analyze/strategy")
async def analyze_strategy(query: str) -> StreamingResponse:
    """投資策略（全 agent + Debate + Supervisor）。等同完整分析。"""
    return _sse_response(_stream_analysis(query))


@app.get("/api/analyze/fundamental")
async def analyze_fundamental(symbol: str, market: str = "TW") -> StreamingResponse:
    """財報/基本面分析（fundamental + chip）。"""
    return _sse_response(_stream_preset("fundamental_review", symbol, market))


@app.get("/api/analyze/macro")
async def analyze_macro(market: str = "TW") -> StreamingResponse:
    """總經快照（macro + cross_market）。"""
    return _sse_response(_stream_preset("macro_outlook", "MARKET", market))


@app.get("/api/analyze/risk")
async def analyze_risk() -> StreamingResponse:
    """組合風控快照（risk only，讀 positions.yaml）。"""
    return _sse_response(_stream_preset("portfolio_risk", "PORTFOLIO"))


# ─── Single-agent Endpoint ────────────────────────────────────────────────────

_VALID_AGENTS = {
    "technical", "chip", "macro", "news",
    "cross_market", "fundamental", "risk",
}


async def _stream_single_agent(
    agent_name: str,
    symbol: str,
    market: str,
) -> AsyncGenerator[str, None]:
    """Run exactly one agent and stream its result, no Supervisor."""
    from schemas.domain_report import RouterOutput  # noqa: PLC0415

    router_out = RouterOutput(
        scenario="single_stock",
        targets=[symbol],
        market=market,
        depth="quick",
        original_query=f"{symbol} {agent_name}",
        query_type="stock_analysis",  # type: ignore[arg-type]
        agents=[agent_name],
        run_supervisor=False,
        run_debate=False,
    )
    yield _sse("router", {
        "scenario": router_out.scenario,
        "targets": router_out.targets,
        "market": router_out.market,
        "depth": router_out.depth,
        "query_type": router_out.query_type,
        "agents": router_out.agents,
        "method": "single_agent",
    })
    async for event in _stream_analysis_with_router(router_out):
        yield event


@app.get("/api/agent/{agent_name}")
async def analyze_single_agent(
    agent_name: str,
    symbol: str = "2330",
    market: str = "TW",
) -> StreamingResponse:
    """
    單一 Agent SSE 串流。

    Path param: agent_name ∈ {technical, chip, macro, news, cross_market, fundamental, risk}
    Query params: symbol (default 2330), market (default TW)

    回傳與 /api/analyze/stream 相同的 SSE 格式，但只有單一 agent 的
    agent_start / agent_done（或 agent_error）+ done 事件。
    """
    if agent_name not in _VALID_AGENTS:
        from fastapi.responses import JSONResponse  # noqa: PLC0415
        return JSONResponse(  # type: ignore[return-value]
            status_code=400,
            content={"error": f"Unknown agent: {agent_name!r}. Valid: {sorted(_VALID_AGENTS)}"},
        )
    return _sse_response(_stream_single_agent(agent_name, symbol, market))
