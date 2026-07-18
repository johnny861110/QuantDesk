"""
Supervisor 匯總與仲裁層（Phase 0 為骨架 stub，Phase 5 才做實仲裁邏輯）。

三層仲裁邏輯（Phase 5 實作）：
  ① 硬約束優先（規則層，不可被 LLM 覆蓋）
  ② 時間框架分層（不強融成單一結論）
  ③ 信心加權（同一時間框架內多 agent 融合）
"""
from __future__ import annotations

from schemas.agent_signal import AgentSignal


class SupervisorResult:
    def __init__(self, layered_view: dict, forced_warnings: list[str], summary: str):
        self.layered_view = layered_view
        self.forced_warnings = forced_warnings
        self.summary = summary


class Supervisor:
    """Phase 0：只驗證能接收 signal list 並跑通流程，仲裁回 stub。
    Phase 5：實作三層仲裁。
    """

    def aggregate(self, signals: list[AgentSignal]) -> SupervisorResult:
        # ── ① 硬約束優先（Phase 5 擴充：依 breach 強制降級）──
        forced_warnings: list[str] = []
        for s in signals:
            if s.has_breach():
                for c in s.hard_constraints:
                    if c.breached:
                        forced_warnings.append(
                            f"[風控強制警告] {c.type} 觸限："
                            f"目前 {c.current} vs 上限 {c.limit}"
                        )

        # ── ② 時間框架分層（Phase 5 實作真正的分層匯總）──
        layered_view: dict[str, list[AgentSignal]] = {}
        for s in signals:
            layered_view.setdefault(s.time_horizon.value, []).append(s)

        # ── ③ 信心加權（Phase 5 實作）──
        summary = "[stub] Supervisor 骨架已跑通，仲裁邏輯待 Phase 5 實作。"

        return SupervisorResult(layered_view, forced_warnings, summary)
