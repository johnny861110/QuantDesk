"""
Langfuse observability — pure optional.

Philosophy:
  有 Langfuse 就記錄 trace，沒有就跳過，agent 永遠正常執行。
  與 risk/news agent 的 fail-safe 哲學一致：基礎設施問題不影響分析品質。

Exports:
  observe             : @observe() decorator — real or transparent no-op
  update_current_span : thin wrapper for get_client().update_current_span()
                        — safe no-op when tracing is inactive

Environment variables（process env 或 .env，由呼叫端自行 load_dotenv()）:
  LANGFUSE_ENABLED    : "true" 才啟動（預設 false）
  LANGFUSE_PUBLIC_KEY : Langfuse project public key
  LANGFUSE_SECRET_KEY : Langfuse project secret key
  LANGFUSE_HOST       : Langfuse server URL（也支援 LANGFUSE_BASE_URL 作為 fallback）
  LANGFUSE_REQUIRED   : "true" → init 失敗時額外印警告（僅顯示，不影響執行邏輯）

  注意：本模組不自行呼叫 load_dotenv()（library code 不應有 process env 副作用）。
  若需從 .env 讀取，請在應用程式入口點（demo script / CLI）自行呼叫 load_dotenv()。

Usage in agents:
  from observability.langfuse_setup import observe, update_current_span

  @observe(name="risk_agent:run")
  def run_risk_agent(...):
      ...

  @observe(name="risk_agent:node_fetch_fx")
  def _node_fetch_fx(state):
      result = _lf_fetch_fx(adapter, pair="USDTWD")   # child span
      update_current_span(output={"usdtwd": result})
      ...
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Any])


# ─── No-op fallbacks (used when Langfuse is disabled or unavailable) ─────────

def _noop(func: F | None = None, /, **_: Any) -> F | Callable[[F], F]:
    """Transparent no-op: passes through the decorated function unchanged."""
    if func is None:
        return lambda f: f  # type: ignore[return-value]
    return func


def _noop_update(**_: Any) -> None:
    """No-op span metadata update."""


# ─── Module-level exports (mutated by _activate if Langfuse is available) ────

observe: Callable[..., Any] = _noop
update_current_span: Callable[..., None] = _noop_update
TRACING_ACTIVE: bool = False    # read-only for external callers


# ─── Activation (runs once at import time) ────────────────────────────────────

def _activate() -> None:
    global observe, update_current_span, TRACING_ACTIVE  # noqa: PLW0603

    if os.getenv("LANGFUSE_ENABLED", "false").lower() != "true":
        logger.debug("Langfuse disabled (LANGFUSE_ENABLED != 'true')")
        return

    try:
        from langfuse import get_client
        from langfuse import observe as _lf_observe

        observe = _lf_observe
        TRACING_ACTIVE = True

        _client = get_client  # capture import so closure doesn't re-import

        def _real_update(**kwargs: Any) -> None:
            _client().update_current_span(**kwargs)

        update_current_span = _real_update

        host = (
            os.getenv("LANGFUSE_HOST")
            or os.getenv("LANGFUSE_BASE_URL", "http://localhost:3000")
        )
        logger.debug("Langfuse tracing active (host=%s)", host)

    except ImportError:
        logger.debug("langfuse package not installed — tracing skipped")
    except Exception as exc:
        if os.getenv("LANGFUSE_REQUIRED", "false").lower() == "true":
            logger.warning(
                "⚠️ LANGFUSE_REQUIRED已設定但Langfuse連線失敗: %s", exc
            )
        else:
            logger.debug("Langfuse init failed: %s", exc)


_activate()
