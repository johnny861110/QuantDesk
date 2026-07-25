"""
E2E tests for api/main.py SSE streaming endpoint.

Uses httpx.AsyncClient with ASGITransport to test the full FastAPI app
without network I/O. All domain agents are monkeypatched to return
synthetic signals so tests are fast and deterministic.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from api.main import app
from schemas.agent_signal import (
    AgentSignal,
    AgentType,
    DataQuality,
    Evidence,
    Signal,
    Target,
    TimeHorizon,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_signal(agent: AgentType, sig: Signal = Signal.NEUTRAL) -> AgentSignal:
    """Build a minimal valid AgentSignal for testing."""
    now = datetime.now(tz=UTC)
    return AgentSignal(
        agent=agent,
        target=Target(symbol="2330", market="TW", asof=now),
        signal=sig,
        confidence=0.75,
        time_horizon=TimeHorizon.SHORT,
        key_evidence=[
            Evidence(claim="test", value=1.0, source="test", asof=now),
        ],
        hard_constraints=[],
        metrics={"test_metric": 42},
        narrative="Test narrative.",
        data_quality=DataQuality(completeness=0.8, staleness_sec=0.0, confidence=0.75),
        errors=[],
    )


def _parse_sse_events(body: str) -> list[dict[str, Any]]:
    """Parse SSE body text into list of {type, payload} dicts."""
    events = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


# ─── Monkeypatch targets ─────────────────────────────────────────────────────

# These patch paths correspond to the lazy imports inside _stream_analysis()
_AGENT_PATCHES = {
    "agents.technical_agent.run_technical_agent": _make_signal(AgentType.TECHNICAL, Signal.BULLISH),
    "agents.chip_agent.run_chip_agent": None,  # returns DomainReport, handled separately
    "agents.macro_agent.run_macro_agent": _make_signal(AgentType.MACRO),
    "agents.news_agent.run_news_agent": _make_signal(AgentType.NEWS),
    "agents.cross_market_agent.run_cross_market_agent": _make_signal(AgentType.CROSS_MARKET),
}


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ─── Tests ───────────────────────────────────────────────────────────────────


async def test_health_endpoint(client: httpx.AsyncClient) -> None:
    """GET /health returns 200 with status ok."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


async def test_sse_stream_event_schema(client: httpx.AsyncClient) -> None:
    """Every SSE event must have 'type' and 'payload' keys."""
    # Patch router to return quickly
    mock_router = type("R", (), {
        "scenario": "single_stock",
        "targets": ["2330"],
        "market": "TW",
        "depth": "standard",
    })()

    with (
        patch("router.intent_router.route", return_value=mock_router),
        patch("agents.technical_agent.run_technical_agent",
              return_value=_make_signal(AgentType.TECHNICAL)),
        patch("agents.chip_agent.run_chip_agent",
              side_effect=Exception("test skip")),
        patch("agents.macro_agent.run_macro_agent",
              return_value=_make_signal(AgentType.MACRO)),
        patch("agents.news_agent.run_news_agent",
              return_value=_make_signal(AgentType.NEWS)),
        patch("agents.cross_market_agent.run_cross_market_agent",
              return_value=_make_signal(AgentType.CROSS_MARKET)),
        patch("agents.fundamental_agent.FundamentalAgent",
              side_effect=Exception("test skip")),
        patch("agents.risk_agent.run_risk_agent",
              return_value=_make_signal(AgentType.RISK)),
        patch("supervisor.graph.Supervisor.aggregate_debate",
              side_effect=Exception("test skip debate")),
    ):
        resp = await client.get("/api/analyze/stream?query=test")
        assert resp.status_code == 200

        events = _parse_sse_events(resp.text)
        assert len(events) > 0

        valid_types = {
            "router", "agent_start", "agent_done", "agent_error",
            "debate_start", "debate_bull", "debate_bear", "debate_pm",
            "supervisor", "done", "error",
        }
        for evt in events:
            assert "type" in evt, f"Event missing 'type': {evt}"
            assert "payload" in evt, f"Event missing 'payload': {evt}"
            assert evt["type"] in valid_types, f"Unknown event type: {evt['type']}"


async def test_sse_stream_starts_with_router(client: httpx.AsyncClient) -> None:
    """First event should be 'router' type."""
    mock_router = type("R", (), {
        "scenario": "single_stock",
        "targets": ["2330"],
        "market": "TW",
        "depth": "standard",
    })()

    with (
        patch("router.intent_router.route", return_value=mock_router),
        patch("agents.technical_agent.run_technical_agent",
              side_effect=Exception("skip")),
        patch("agents.chip_agent.run_chip_agent",
              side_effect=Exception("skip")),
        patch("agents.macro_agent.run_macro_agent",
              side_effect=Exception("skip")),
        patch("agents.news_agent.run_news_agent",
              side_effect=Exception("skip")),
        patch("agents.cross_market_agent.run_cross_market_agent",
              side_effect=Exception("skip")),
        patch("agents.fundamental_agent.FundamentalAgent",
              side_effect=Exception("skip")),
        patch("agents.risk_agent.run_risk_agent",
              side_effect=Exception("skip")),
        patch("supervisor.graph.Supervisor.aggregate_debate",
              side_effect=Exception("skip")),
    ):
        resp = await client.get("/api/analyze/stream?query=test")
        events = _parse_sse_events(resp.text)
        assert events[0]["type"] == "router"


async def test_sse_stream_ends_with_done(client: httpx.AsyncClient) -> None:
    """Last event should be 'done' type."""
    mock_router = type("R", (), {
        "scenario": "single_stock",
        "targets": ["2330"],
        "market": "TW",
        "depth": "standard",
    })()

    with (
        patch("router.intent_router.route", return_value=mock_router),
        patch("agents.technical_agent.run_technical_agent",
              side_effect=Exception("skip")),
        patch("agents.chip_agent.run_chip_agent",
              side_effect=Exception("skip")),
        patch("agents.macro_agent.run_macro_agent",
              side_effect=Exception("skip")),
        patch("agents.news_agent.run_news_agent",
              side_effect=Exception("skip")),
        patch("agents.cross_market_agent.run_cross_market_agent",
              side_effect=Exception("skip")),
        patch("agents.fundamental_agent.FundamentalAgent",
              side_effect=Exception("skip")),
        patch("agents.risk_agent.run_risk_agent",
              side_effect=Exception("skip")),
        patch("supervisor.graph.Supervisor.aggregate_debate",
              side_effect=Exception("skip")),
    ):
        resp = await client.get("/api/analyze/stream?query=test")
        events = _parse_sse_events(resp.text)
        assert events[-1]["type"] == "done"


async def test_sse_stream_all_agents_fail(client: httpx.AsyncClient) -> None:
    """When all agents fail, stream should still emit agent_error events and finish."""
    mock_router = type("R", (), {
        "scenario": "single_stock",
        "targets": ["2330"],
        "market": "TW",
        "depth": "standard",
    })()

    with (
        patch("router.intent_router.route", return_value=mock_router),
        patch("agents.technical_agent.run_technical_agent",
              side_effect=Exception("fail")),
        patch("agents.chip_agent.run_chip_agent",
              side_effect=Exception("fail")),
        patch("agents.macro_agent.run_macro_agent",
              side_effect=Exception("fail")),
        patch("agents.news_agent.run_news_agent",
              side_effect=Exception("fail")),
        patch("agents.cross_market_agent.run_cross_market_agent",
              side_effect=Exception("fail")),
        patch("agents.fundamental_agent.FundamentalAgent",
              side_effect=Exception("fail")),
        patch("agents.risk_agent.run_risk_agent",
              side_effect=Exception("fail")),
    ):
        resp = await client.get("/api/analyze/stream?query=test")
        events = _parse_sse_events(resp.text)

        # Should have agent_error events
        error_events = [e for e in events if e["type"] == "agent_error"]
        assert len(error_events) >= 3  # at least some agents should report errors

        # Should end with done or error
        assert events[-1]["type"] in ("done", "error")
