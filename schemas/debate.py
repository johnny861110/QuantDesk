"""
DebateOutput — Multi-agent Debate 的輸出 Schema

架構說明
--------
Multi-agent Debate 是在 Domain Agents 之上的「後設仲裁層」：

  DomainReport (x N) ──┬──► BullAgent  ──┐
                        │                  ├──► PM Agent ──► DebateOutput
                        └──► BearAgent  ──┘

三個角色：
  BullAgent  : 只找看多論據（只能用 key_findings 裡已有的數字，不能發明新數字）
  BearAgent  : 只找看空論據與風險（同上限制）
  PMAgent    : 聽完 Bull + Bear，做最終裁決（signal + confidence + 最終建議）

重要邊界：
  - DebateOutput 的 final_signal / final_confidence 僅供 Supervisor.aggregate_debate()
    更新 overall_narrative，不得覆蓋規則引擎的 overall_recommendation。
  - risk_override=True 時 Debate narrative 仍被跳過。
  - 所有數字必須來自 DomainReport.key_findings，三個 agent 都不可自行發明數字。

使用方式
--------
    from agents.debate_agents import run_debate
    from schemas.debate import DebateOutput

    debate = await run_debate(reports=domain_reports, symbol="2330")
    print(debate.bull.thesis)
    print(debate.bear.thesis)
    print(debate.pm_verdict.thesis)
    print(debate.final_signal)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from schemas.agent_signal import Signal


# ─── DebateParty ──────────────────────────────────────────────────────────────


@dataclass
class DebateParty:
    """
    單一辯論角色的論述。

    role        : "bull" | "bear" | "pm"
    thesis      : 完整論述段落（白話）
    key_points  : 最重要的 2-4 個論點（bullet point 風格）
    confidence  : 角色對自身論述的信心（0-1）
    """
    role: str                           # "bull" | "bear" | "pm"
    thesis: str                         # 完整論述
    key_points: list[str] = field(default_factory=list)   # 2-4 個主論點
    confidence: float = 0.5             # 角色自我評估信心


# ─── DebateOutput ─────────────────────────────────────────────────────────────


@dataclass
class DebateOutput:
    """
    Multi-agent Debate 的完整輸出。

    由 Supervisor.aggregate_debate() 接收後：
    - 若 risk_override=False：pm_verdict.thesis 取代 overall_narrative
    - 若 risk_override=True ：Debate narrative 跳過（硬約束優先）
    - final_signal / final_confidence 僅供前端顯示，不覆蓋規則引擎結論
    """
    symbol: str
    scenario: str
    bull: DebateParty                   # 多方論述
    bear: DebateParty                   # 空方論述
    pm_verdict: DebateParty             # PM 最終裁決（role="pm"）
    final_signal: Signal                # PM 裁決的方向（供前端顯示）
    final_confidence: float             # PM 裁決的信心（供前端顯示）
    method: str = "llm"                 # "llm" | "fallback"
    error: str = ""                     # 若有 LLM 呼叫失敗的錯誤描述
