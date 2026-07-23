"""
DomainReport — Agentic 架構的 domain agent 輸出格式。

背景
----
原有的 AgentSignal 為確定性管線設計，欄位偏窄：
  - 無推理過程記錄
  - narrative 是模板字串，不帶 LLM 思考步驟
  - 無 scenario 路由資訊

DomainReport 為 ReAct 架構設計，記錄推理步驟和結構化發現，
供 Synthesis LLM 閱讀後做跨 domain 整合判斷。

向後相容
--------
AgentSignal 保留不動，DomainReport 新增。
domain_report_to_agent_signal() 提供橋接轉換，
讓 Supervisor 既有邏輯（hard_constraint 規則引擎）仍可運作。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from schemas.agent_signal import AgentType, HardConstraint, Signal, TimeHorizon

if TYPE_CHECKING:
    from schemas.agent_signal import AgentSignal


# ─── ReAct 推理步驟 ───────────────────────────────────────────────────────────


@dataclass
class ReasoningStep:
    """
    ReAct loop 中一個 Thought-Action-Observation 循環的紀錄。

    Fields
    ------
    thought     : LLM 的推理文字（分析為何要呼叫此工具）
    action      : 呼叫的工具名稱
    action_input: 工具的輸入參數（dict）
    observation : 工具回傳的結果（摘要文字，不含原始大 payload）
    """
    thought: str
    action: str
    action_input: dict[str, Any]
    observation: str


# ─── DomainReport ─────────────────────────────────────────────────────────────


@dataclass
class DomainReport:
    """
    Domain Agent 的完整輸出，包含推理過程。

    Synthesis LLM 讀所有 DomainReport 後產出最終仲裁報告。
    規則引擎（hard_constraints / HITL Gate）仍由確定性代碼處理，不由 LLM 裁量。

    Fields
    ------
    agent           : 產出此報告的 agent 類型
    symbol          : 分析標的（e.g. "2330"）；組合層級使用 "PORTFOLIO"
    market          : 市場代碼（e.g. "TW", "US"）
    asof            : 報告產出時間戳

    signal          : 機器可讀的方向性結論（BULLISH / BEARISH / NEUTRAL）
    confidence      : 0-1，信心水準
    time_horizon    : SHORT / MEDIUM / LONG / INTRADAY
    hard_constraints: 硬約束（規則引擎，不由 LLM 裁量）

    reasoning_steps : ReAct loop 的推理步驟（供可觀測性 / 審計使用）
    key_findings    : 結構化數值發現（供 Synthesis LLM 讀取，不含原始資料 payload）
                      例：{"rsi": 72.3, "foreign_net_buy_3d": 12_000_000}
    narrative_summary: LLM 生成的白話摘要（供 Synthesis LLM 跨 domain 整合用）

    data_completeness: 0-1，資料完整度（0 = 無資料，1 = 完整）
    errors          : 管線錯誤訊息列表
    """
    agent: AgentType
    symbol: str
    market: str
    asof: datetime

    # 機器可讀結論
    signal: Signal
    confidence: float
    time_horizon: TimeHorizon
    hard_constraints: list[HardConstraint] = field(default_factory=list)

    # 推理過程（供 Langfuse 追蹤 + Synthesis LLM）
    reasoning_steps: list[ReasoningStep] = field(default_factory=list)

    # 結構化發現（純數值，不含原始 payload）
    key_findings: dict[str, Any] = field(default_factory=dict)

    # 白話摘要（LLM 生成）
    narrative_summary: str = ""

    # 資料品質
    data_completeness: float = 1.0
    errors: list[str] = field(default_factory=list)

    def has_breach(self) -> bool:
        """True if any hard constraint is breached."""
        return any(c.breached for c in self.hard_constraints)


# ─── 場景路由輸出 ─────────────────────────────────────────────────────────────


@dataclass
class RouterOutput:
    """
    Router LLM 的意圖分類輸出。

    scenario 決定後續呼叫哪些 agent、以何種模式執行。
    """
    scenario: Literal["single_stock", "portfolio_risk", "multi_stock_scan"]
    targets: list[str]           # e.g. ["2330"] / ["PORTFOLIO"] / ["2882", "2881"]
    market: str                  # "TW" / "US" / "MIXED"
    depth: Literal["quick", "standard", "deep"] = "standard"
    original_query: str = ""
    extra_context: dict[str, Any] = field(default_factory=dict)
    # extra_context 用途：
    #   portfolio_risk → {"positions": [...]}（使用者提供的部位 JSON）
    #   multi_stock_scan → {"sector": "financial", "criteria": "..."}
    #   single_stock → {} 通常為空


# ─── 橋接函數 ─────────────────────────────────────────────────────────────────


def domain_report_to_agent_signal(
    report: DomainReport,
) -> "AgentSignal":  # type: ignore[name-defined]
    """
    把 DomainReport 轉換成 AgentSignal，供現有 Supervisor 規則引擎使用。

    這是 Agentic 架構過渡期的向後相容橋接函數。
    當 Supervisor 完全遷移到 Synthesis LLM 後，此函數可移除。
    """

    from schemas.agent_signal import (
        AgentSignal,
        DataQuality,
        Evidence,
        Target,
    )

    return AgentSignal(
        agent=report.agent,
        target=Target(symbol=report.symbol, market=report.market, asof=report.asof),
        signal=report.signal,
        confidence=report.confidence,
        time_horizon=report.time_horizon,
        key_evidence=[
            Evidence(
                claim=k,
                value=float(v) if isinstance(v, (int, float)) else None,
                source=f"{report.agent.value}:key_findings",
                asof=report.asof,
            )
            for k, v in report.key_findings.items()
            if isinstance(v, (int, float))
        ],
        hard_constraints=report.hard_constraints,
        metrics=report.key_findings,
        narrative=report.narrative_summary,
        data_quality=DataQuality(
            completeness=report.data_completeness,
            staleness_sec=0.0,
            confidence=report.confidence,
        ),
        errors=report.errors,
    )
