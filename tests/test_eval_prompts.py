"""
Phase 17 L1 —— Prompt 迴歸防護。

為什麼需要這層
--------------
Phase 16 稽核發現：**改動任何 prompt，全部測試照樣通過**。
所有 LLM 呼叫在測試中一律 mock，沒有任何測試斷言 prompt 內容。
對一個 LLM narrative 是核心輸出的系統，這是最大的迴歸盲區
（見 docs/tasks/phase_16.md §二 A2）。

兩層防護
--------
1. **快照**（TestPromptSnapshots）：偵測未經審查的 prompt 漂移。
   改 prompt 會讓測試紅燈，強迫在 PR 裡明確更新 hash —— 這是設計，不是麻煩。

2. **不變量**（其餘 class）：才是真正抓 bug 的部分。
   斷言的是「不管怎麼改寫措辭，這些性質都必須成立」，
   涵蓋 CLAUDE.md 三條設計原則、OpenAI API 硬性要求、跨檔案一致性。

本檔不呼叫任何真實 LLM。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agents.debate_agents import (
    _BEAR_SYSTEM_PROMPT,
    _BULL_SYSTEM_PROMPT,
    _PM_SYSTEM_PROMPT,
)
from agents.news_agent import _LLM_SYSTEM_PROMPT as _NEWS_SYSTEM_PROMPT
from router.intent_router import _QUERY_TYPE_AGENTS, _ROUTER_SYSTEM_PROMPT
from schemas.agent_signal import Signal
from supervisor.synthesis import _SYNTHESIS_SYSTEM_PROMPT

# 模組層 prompt 常數總表
ALL_PROMPTS: dict[str, str] = {
    "bull": _BULL_SYSTEM_PROMPT,
    "bear": _BEAR_SYSTEM_PROMPT,
    "pm": _PM_SYSTEM_PROMPT,
    "news": _NEWS_SYSTEM_PROMPT,
    "router": _ROUTER_SYSTEM_PROMPT,
    "synthesis": _SYNTHESIS_SYSTEM_PROMPT,
}

# 會產出敘述文字的 prompt（router 是分類器，不產敘述，故排除）
NARRATIVE_PROMPTS: tuple[str, ...] = ("bull", "bear", "pm", "news", "synthesis")

# 呼叫端使用 response_format={"type": "json_object"} 的 prompt。
# OpenAI 硬性要求：使用 json_object 模式時 messages 內必須出現 "json" 字樣，
# 否則 API 直接回 400。少了這個字是**執行期才會炸**的錯誤。
JSON_MODE_PROMPTS: tuple[str, ...] = ("bull", "bear", "pm", "news", "router", "synthesis")


# ─── 1. 快照 ──────────────────────────────────────────────────────────────────


class TestPromptSnapshots:
    """
    Prompt 內容快照。任何改動都會讓對應項目紅燈。

    測試失敗時該怎麼做（這是刻意的流程，不要繞過）：
      1. 確認 prompt 的改動是你有意為之
      2. 檢查下面的不變量測試是否仍然全過
      3. 把新的 hash 更新到 EXPECTED_HASHES
      4. 在 PR 描述說明改了什麼、為什麼

    絕對不要為了讓測試變綠而直接複製貼上新 hash 卻不看內容改了什麼。
    """

    EXPECTED_HASHES: dict[str, str] = {
        "bull": "ad067903ff727d49",
        "bear": "e6e98a30c5933dbe",
        "pm": "b12fb4562fa1980b",
        "news": "3da25fee8ed458bb",
        "router": "4d556342fdd91bf6",
        "synthesis": "d5f9868d547a5ea7",
    }

    @pytest.mark.parametrize("name", sorted(ALL_PROMPTS))
    def test_prompt_unchanged(self, name: str) -> None:
        actual = hashlib.sha256(ALL_PROMPTS[name].encode()).hexdigest()[:16]
        assert actual == self.EXPECTED_HASHES[name], (
            f"\n{name} prompt 已變動。\n"
            f"  舊 hash: {self.EXPECTED_HASHES[name]}\n"
            f"  新 hash: {actual}\n"
            f"若為有意改動，請更新 EXPECTED_HASHES['{name}'] 並在 PR 說明原因。"
        )

    def test_snapshot_table_covers_every_prompt(self) -> None:
        """新增 prompt 時不能忘了納入快照。"""
        assert set(self.EXPECTED_HASHES) == set(ALL_PROMPTS)


# ─── 2. 設計原則①：LLM 不得產出數字 ──────────────────────────────────────────


class TestNoNumberInventionInstruction:
    """
    CLAUDE.md 設計原則①的 prompt 層防線。
    verifier.py 是事後偵測，prompt 是事前約束，兩者都要在。
    """

    # 各 prompt 用詞不同，故用語意等價的關鍵詞組（任一命中即可）
    _FORBID_PATTERNS: tuple[str, ...] = (
        "不能發明",
        "不得出現任何數字",
        "不能加入任何其他數字",
        "禁止直接複讀數字",
        "不能發明或推算新數字",
    )

    @pytest.mark.parametrize("name", NARRATIVE_PROMPTS)
    def test_narrative_prompt_forbids_inventing_numbers(self, name: str) -> None:
        prompt = ALL_PROMPTS[name]
        assert any(pat in prompt for pat in self._FORBID_PATTERNS), (
            f"{name} prompt 未包含「不得發明數字」的約束，違反 CLAUDE.md 設計原則①。\n"
            f"預期出現下列其中之一：{self._FORBID_PATTERNS}"
        )


# ─── 3. 設計原則②：LLM 不得覆蓋硬約束 ────────────────────────────────────────


class TestHardConstraintNotOverridable:
    def test_synthesis_prompt_states_rules_engine_owns_hard_constraints(self) -> None:
        """
        Synthesis LLM 是唯一有機會「重新裁決」整體結論的 LLM，
        prompt 必須明確告知硬約束不歸它管（設計原則②）。
        """
        assert "硬約束" in _SYNTHESIS_SYSTEM_PROMPT
        assert "不能覆蓋" in _SYNTHESIS_SYSTEM_PROMPT, (
            "synthesis prompt 必須明確禁止 LLM 覆蓋風控硬約束"
        )


# ─── 4. OpenAI API 硬性要求：json_object 模式 ────────────────────────────────


class TestJsonModeRequirement:
    @pytest.mark.parametrize("name", JSON_MODE_PROMPTS)
    def test_json_mode_prompt_mentions_json(self, name: str) -> None:
        """
        呼叫端用 response_format={"type": "json_object"} 時，
        OpenAI 要求 messages 內必須出現 "json"（不分大小寫），否則 400。
        這是執行期才會炸的錯誤，測試在此攔下。
        """
        assert "json" in ALL_PROMPTS[name].lower(), (
            f"{name} prompt 未出現 'json' 字樣，但呼叫端使用 json_object 模式 → "
            f"OpenAI 會直接回 400"
        )


# ─── 5. 跨檔案一致性：router prompt ↔ _QUERY_TYPE_AGENTS ─────────────────────


class TestRouterPromptMatchesAgentMapping:
    """
    這組不變量的來由：Phase 16-E 新增 stock_investment 時，
    必須同步改 _QUERY_TYPE_AGENTS **和** _ROUTER_SYSTEM_PROMPT。
    當時是靠人工記得，沒有任何測試守護——漏改的話 Router LLM
    永遠不會輸出新類型，而且不會有任何錯誤訊息。
    """

    @pytest.mark.parametrize("query_type", sorted(_QUERY_TYPE_AGENTS))
    def test_every_query_type_documented_in_prompt(self, query_type: str) -> None:
        assert query_type in _ROUTER_SYSTEM_PROMPT, (
            f"_QUERY_TYPE_AGENTS 有 '{query_type}' 但 _ROUTER_SYSTEM_PROMPT 沒提到它。\n"
            f"Router LLM 不會知道這個類型的存在，永遠不會輸出它（且無錯誤訊息）。"
        )

    def test_prompt_declares_no_unknown_query_type(self) -> None:
        """反向檢查：prompt 描述的類型不能是 mapping 裡沒有的。"""
        declared = set(re.findall(r"\*\*([a-z_]+)\*\*（", _ROUTER_SYSTEM_PROMPT))
        unknown = declared - set(_QUERY_TYPE_AGENTS)
        assert not unknown, (
            f"prompt 描述了 _QUERY_TYPE_AGENTS 沒有的類型：{unknown}\n"
            f"Router LLM 可能輸出它們，但路由時會 KeyError 或被靜默 fallback。"
        )

    def test_stock_investment_prompt_warns_about_risk_exclusion(self) -> None:
        """
        16-E 的核心語意：個股建議不含 risk agent。
        prompt 若沒講清楚，Router LLM 會把個股查詢誤判成 investment_strategy，
        使組合層 breach 又回頭汙染個股建議——即 P0-1 復發。
        """
        assert "不含 risk" in _ROUTER_SYSTEM_PROMPT


# ─── 6. 安全性：prompt injection 防線 ────────────────────────────────────────


class TestPromptInjectionDefense:
    def test_news_prompt_retains_injection_guard(self) -> None:
        """
        news agent 把外部檢索內容塞進 prompt（spec.md §9.2 明列的風險）。
        system prompt 必須指示模型忽略外部內容中的指令。
        """
        assert "external_content" in _NEWS_SYSTEM_PROMPT
        assert "忽略以上指令" in _NEWS_SYSTEM_PROMPT, (
            "news prompt 的 prompt-injection 防線不見了"
        )


# ─── 7. 函式內 inline prompt（chip / fundamental）────────────────────────────


def _capture_openai_prompt(mock_client: MagicMock) -> str:
    """從 mock 的 OpenAI client 取出實際送出的 prompt 全文。"""
    call = mock_client.chat.completions.create.call_args
    assert call is not None, "OpenAI client 未被呼叫"
    messages: list[dict[str, Any]] = call.kwargs["messages"]
    return "\n".join(str(m.get("content", "")) for m in messages)


class TestInlinePrompts:
    """
    chip / fundamental 的 prompt 是函式內 f-string，沒有模組常數可快照。
    改用攔截實際送出內容的方式——測到的是含插值的真實 prompt，
    比快照模板更貼近實際行為。
    """

    def _run_chip_and_capture(self) -> str:
        from agents.chip_agent import _llm_synthesize_chip

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content="mocked"))
        ]
        with patch("langfuse.openai.OpenAI", return_value=mock_client):
            _llm_synthesize_chip(
                symbol="2330",
                signal=Signal.BULLISH,
                confidence=0.7,
                key_findings={"institutional_score": 0.5, "consecutive_days": 3.0},
            )
        return _capture_openai_prompt(mock_client)

    def test_chip_prompt_forbids_reciting_raw_numbers(self) -> None:
        prompt = self._run_chip_and_capture()
        assert "禁止直接複讀數字" in prompt, (
            "chip prompt 的「不得複讀原始數值」約束不見了（設計原則①）"
        )

    def test_chip_prompt_includes_symbol_and_findings(self) -> None:
        """prompt 必須帶入標的與確定性計算結果，否則 LLM 只能靠猜。"""
        prompt = self._run_chip_and_capture()
        assert "2330" in prompt
        assert "institutional_score" in prompt

    def test_chip_prompt_does_not_leak_raw_adapter_payload(self) -> None:
        """
        只應送入 key_findings 的數值摘要，不得夾帶原始 adapter payload。
        （送越多原始資料，LLM 越容易複讀出未經驗證的數字。）
        """
        prompt = self._run_chip_and_capture()
        for leaked in ("records", "TaiwanStockInstitutionalInvestorsBuySell"):
            assert leaked not in prompt, f"prompt 疑似夾帶原始資料：{leaked}"
