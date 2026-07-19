"""
QuantDesk 核心共同合約：AgentSignal

這是整個系統的骨架。所有 domain agent 都必須輸出這個結構，Supervisor 才能
程式化匯總，而非「叫 LLM 讀六段文字自己腦補」。

⚠️ 這個 schema 是六個 agent 的共同語言。修改它會影響所有 agent 與 Supervisor。
   在 Phase 1-4 平行開發期間，任何 agent 都不得擅自修改此檔——需要新欄位請先問人。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Signal(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class TimeHorizon(str, Enum):
    """時間框架——整個 schema 最關鍵的欄位之一。
    技術面只對短期有效、基本面對中長期有效。Supervisor 靠這個欄位做分層呈現
    （短期看空、長期看多），而不是把所有 agent 硬融成一句話。
    """
    INTRADAY = "intraday"
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


class AgentType(str, Enum):
    RISK = "risk"
    TECHNICAL = "technical"
    FUNDAMENTAL = "fundamental"
    NEWS = "news"
    MACRO = "macro"
    CROSS_MARKET = "cross_market"


class Target(BaseModel):
    """
    輸出對象。單標的 agent（fundamental / technical / news）填真實市場代碼；
    組合層級 agent 使用保留值。

    ⚠️ 保留值 "PORTFOLIO"：risk_agent 的 target.symbol 固定為此值，代表整個組合
    層級的輸出，而非可查詢的真實市場標的代碼。實際涵蓋的標的清單在
    AgentSignal.metrics["covered_symbols"]。下游任何要用 target.symbol 去查
    資料源的邏輯（FinMind 拉價格、新聞 API 搜標的等），都必須先檢查是否等於
    "PORTFOLIO" 並排除，否則會查到不存在的代碼。
    """
    symbol: str
    market: str  # e.g. "TW", "US"
    asof: datetime


class Evidence(BaseModel):
    """每個判斷的可稽核載體。source + asof 讓結論能回溯到帶時間戳的原始來源。"""
    claim: str
    value: float | None = None
    source: str  # e.g. "financial_facts#2330_2025Q3"
    asof: datetime


class HardConstraint(BaseModel):
    """風控專用。breached=True 時 Supervisor 必須強制降級，不由 LLM 裁量。"""
    type: str  # e.g. "gamma_limit", "net_delta_pct_nav", "sector_concentration"
    current: float
    limit: float
    breached: bool
    detail: str | None = None


class DataQuality(BaseModel):
    completeness: float = Field(ge=0.0, le=1.0)
    staleness_sec: float = Field(ge=0.0)
    confidence: float = Field(ge=0.0, le=1.0)


class AgentSignal(BaseModel):
    """所有 domain agent 的統一輸出。Supervisor 只接受這個結構。"""
    agent: AgentType
    target: Target
    signal: Signal
    confidence: float = Field(ge=0.0, le=1.0)
    time_horizon: TimeHorizon
    key_evidence: list[Evidence] = Field(default_factory=list)
    hard_constraints: list[HardConstraint] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    narrative: str = ""  # LLM 生成，僅組織語言；不得含未經工具驗證的數字
    data_quality: DataQuality
    errors: list[str] = Field(default_factory=list)

    def has_breach(self) -> bool:
        return any(c.breached for c in self.hard_constraints)
