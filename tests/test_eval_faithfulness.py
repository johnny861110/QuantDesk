"""
Phase 17 L4 —— Narrative faithfulness 評估。

「Faithfulness」在本專案的定義（CLAUDE.md 設計原則①）
----------------------------------------------------
narrative 裡出現的每一個數字，都必須來自 metrics / key_findings 裡
經確定性工具算出的值。LLM 不得發明數字。

評分器直接復用 agents/verifier.py::check_narrative —— 它本來就在做這件事，
不需要另寫一套規則（兩套規則會漂移，是更糟的結果）。

三層
----
1. TestVerifierWiring     —— 結構性：**每個 agent 都必須接上 Verifier**。
                             這條會直接抓到 Phase 16-B 那個 bug
                             （chip_agent 曾是唯一漏接的）。
2. TestFaithfulnessScorer —— 評分器本身正確：對抗性 narrative 要被抓到，
                             合法引用不得誤判。鎖住 16-F 的修復成果。
3. TestLLMFaithfulness    —— llm_eval：真實 LLM 輸出的 faithfulness 率量測。

本檔預設不呼叫任何真實 LLM（第 3 層需 -m llm_eval 明確開啟）。
"""
from __future__ import annotations

import ast
import os
import pathlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agents.verifier import check_narrative, symbol_allowlist
from schemas.agent_signal import Signal

# 全部會產出 narrative 的 domain agent
AGENT_MODULES: tuple[str, ...] = (
    "technical_agent",
    "macro_agent",
    "news_agent",
    "cross_market_agent",
    "risk_agent",
    "chip_agent",
    "fundamental_agent",
)

AGENTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "agents"


def _called_function_names(source: str) -> set[str]:
    """取出原始碼中所有被呼叫的函式名（含 obj.method 的 method 名）。"""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


# ─── 1. 結構性：每個 agent 都必須接上 Verifier ───────────────────────────────


class TestVerifierWiring:
    """
    這組測試的來由
    --------------
    Phase 16-B 發現 chip_agent 是六個 agent 中**唯一**沒接
    agents/verifier.py 的，只靠 prompt 文字「【嚴格禁止】禁止直接複讀數字」
    自律，無任何程式化檢查——直接違反 CLAUDE.md 設計原則①。

    那個缺口存在了整整 9 個 Phase 都沒被發現，因為沒有任何測試
    在檢查「所有 agent 是否都遵守這條共同規則」。這組測試補上這個守門。

    用 AST 而非執行期 spy：不需要為 7 個 agent 各準備一套 adapter fixture，
    且新增 agent 時會自動被納入檢查（只要加進 AGENT_MODULES）。
    """

    @pytest.mark.parametrize("module_name", AGENT_MODULES)
    def test_agent_invokes_check_narrative(self, module_name: str) -> None:
        source = (AGENTS_DIR / f"{module_name}.py").read_text(encoding="utf-8")
        called = _called_function_names(source)
        hits = {n for n in called if n.endswith("check_narrative")}
        assert hits, (
            f"agents/{module_name}.py 沒有呼叫 check_narrative。\n"
            f"每個產出 narrative 的 agent 都必須接上 agents/verifier.py，\n"
            f"這是 CLAUDE.md 設計原則①「LLM 永遠不產出數字」唯一的程式化防線。\n"
            f"（Phase 16-B 就是在修 chip_agent 漏接這條的問題）"
        )

    def test_agent_module_list_matches_filesystem(self) -> None:
        """新增 agent 檔案時不能忘了納入本檔的檢查清單。"""
        on_disk = {
            p.stem
            for p in AGENTS_DIR.glob("*_agent.py")
            if p.stem not in ("debate_agents",)
        }
        missing = on_disk - set(AGENT_MODULES)
        assert not missing, (
            f"下列 agent 未納入 Verifier 接線檢查：{missing}\n"
            f"請加入 AGENT_MODULES，或說明為何該 agent 不產出 narrative。"
        )

    def test_detection_actually_works(self) -> None:
        """
        突變自檢：確認上面的偵測不是永遠會過的假測試。
        拿掉呼叫後必須偵測不到。
        """
        source = (AGENTS_DIR / "chip_agent.py").read_text(encoding="utf-8")
        mutated = source.replace("_lf_check_narrative(", "_noop_disabled(").replace(
            "check_narrative(narrative, metrics", "noop(narrative, metrics"
        )
        hits = {n for n in _called_function_names(mutated) if n.endswith("check_narrative")}
        assert not hits, "偵測邏輯有問題：拿掉呼叫後仍判定有接上 Verifier"


# ─── 2. 評分器本身的正確性 ───────────────────────────────────────────────────


class TestFaithfulnessScorer:
    """
    鎖住 Phase 16-F 的修復成果（4 位數以上數字曾完全偵測不到），
    並涵蓋容差、白名單、邊界。
    """

    METRICS: dict[str, Any] = {
        "institutional_score": 0.52,
        "consecutive_days": 5.0,
        "margin_balance": 31915.0,
    }

    @pytest.mark.parametrize(
        "narrative",
        [
            "外資買超力道轉強，籌碼面偏多。",          # 純質化，零數字
            "法人同步偏多，短線動能延續。",
            "",                                        # 空字串
        ],
    )
    def test_clean_narrative_scores_faithful(self, narrative: str) -> None:
        assert check_narrative(narrative, self.METRICS) == []

    @pytest.mark.parametrize(
        "narrative",
        [
            "融資餘額達 987654 張。",        # 6 位數 —— 16-F 修復前完全漏掉
            "外資淨股數 -80773006 股。",     # 負號 + 8 位數
            "本益比來到 1234 倍。",          # 4 位數 —— 16-F 修復前漏掉
            "分數為 0.99。",                 # 小數，不在 metrics 中
        ],
    )
    def test_hallucinated_number_is_caught(self, narrative: str) -> None:
        errors = check_narrative(narrative, self.METRICS)
        assert errors, f"未攔下幻覺數字：{narrative!r}"
        assert all("未經工具驗證的數字" in e for e in errors)

    @pytest.mark.parametrize(
        "narrative",
        [
            "外資連續買超 5 日。",             # consecutive_days = 5.0
            "融資餘額 31915 張。",             # margin_balance —— 5 位數合法引用
            "綜合分數 0.52。",                 # institutional_score
        ],
    )
    def test_legitimate_metric_citation_passes(self, narrative: str) -> None:
        assert check_narrative(narrative, self.METRICS) == [], (
            f"合法引用 metrics 被誤判：{narrative!r}"
        )

    def test_year_is_not_treated_as_hallucination(self) -> None:
        """年份（19xx/20xx）不是分析數字，不應誤判。"""
        assert check_narrative("2024 年以來外資持續買超。", self.METRICS) == []

    def test_ticker_allowed_via_allowlist(self) -> None:
        """
        台股代號是 4 碼，修好 regex 後會被當成數字。
        呼叫端傳入 symbol_allowlist 後不得誤判。
        """
        n = "台積電 2330 的外資買超延續。"
        assert check_narrative(n, self.METRICS) != []          # 沒白名單 → 被抓
        assert check_narrative(n, self.METRICS, allow=symbol_allowlist("2330")) == []

    def test_ticker_allowlist_does_not_whitelist_everything(self) -> None:
        """白名單只放行該標的代號，不得順便放行其他幻覺數字。"""
        n = "台積電 2330 的融資餘額達 987654 張。"
        errors = check_narrative(n, self.METRICS, allow=symbol_allowlist("2330"))
        assert errors and any("987654" in e for e in errors)


# ─── 3. 真實 LLM 的 faithfulness 率量測（預設 skip）──────────────────────────


@pytest.mark.llm_eval
class TestLLMFaithfulness:
    """
    量測真實 LLM 輸出的 faithfulness 率。

    執行：uv run pytest -m llm_eval -q -s
    需要 OPENAI_API_KEY，會產生 API 費用，故不進 CI。

    這是 L1/L2 做不到的一層：前兩者驗證「程式碼行為正確」，
    這裡量測「LLM 實際輸出的品質」。
    """

    MIN_FAITHFULNESS: float = 0.90

    def test_chip_narrative_faithfulness(self) -> None:
        if not os.environ.get("OPENAI_API_KEY"):
            pytest.skip("需要 OPENAI_API_KEY")

        from agents.chip_agent import _llm_synthesize_chip

        findings_variants: list[dict[str, Any]] = [
            {"institutional_score": 0.62, "consecutive_days": 5.0},
            {"institutional_score": -0.41, "consecutive_days": -3.0,
             "margin_balance": 28450.0},
            {"institutional_score": 0.08, "foreign_ownership_ratio": 47.2},
            {"futures_signal_score": 0.35, "foreign_net_position": 8200.0},
            {"margin_pressure_score": -0.22, "margin_change_5d": 1580.0},
        ]

        clean = 0
        for findings in findings_variants:
            narrative = _llm_synthesize_chip(
                symbol="2330",
                signal=Signal.BULLISH,
                confidence=0.65,
                key_findings=findings,
            )
            errors = check_narrative(
                narrative, findings, allow=symbol_allowlist("2330")
            )
            status = "✓" if not errors else "✗"
            print(f"\n  {status} {narrative[:60]}")
            for e in errors:
                print(f"      {e}")
            if not errors:
                clean += 1

        rate = clean / len(findings_variants)
        print(f"\nchip narrative faithfulness：{clean}/{len(findings_variants)} = {rate:.0%}")
        assert rate >= self.MIN_FAITHFULNESS, (
            f"faithfulness {rate:.0%} 低於門檻 {self.MIN_FAITHFULNESS:.0%}——"
            f"prompt 的「禁止複讀數字」約束可能需要加強"
        )


# ─── 4. 降級路徑不得繞過 Verifier ────────────────────────────────────────────


class TestFallbackPathStillVerified:
    def test_chip_llm_failure_fallback_is_also_checked(self) -> None:
        """
        LLM 失敗時走 _fallback_narrative()，那條路徑同樣會經過 Verifier。
        若降級敘述含未驗證數字，一樣必須被記錄——不能因為是 fallback 就放行。
        """
        from agents.chip_agent import run_chip_agent
        from tests.test_phase7_agentic import _MockChipAdapter, _inst_rows

        adapter = _MockChipAdapter({
            "TaiwanStockInstitutionalInvestorsBuySell": _inst_rows(
                [("2026-07-22", 2000.0, 100.0, 0.0)]
            ),
            "TaiwanStockMarginPurchaseShortSale": [],
            "TaiwanStockShareholding": [],
            "TaiwanFuturesInstitutionalInvestors": [],
        })
        from datetime import UTC, datetime

        with patch(
            "agents.chip_agent._llm_synthesize_chip",
            return_value="降級敘述：融資餘額 987654 張。",
        ):
            report = run_chip_agent(
                symbol="2330",
                adapter=adapter,
                asof=datetime(2026, 7, 23, 9, 0, 0, tzinfo=UTC),
            )
        assert any("未經工具驗證的數字" in e for e in report.errors), (
            "降級路徑繞過了 Verifier"
        )

    def test_mock_client_never_hits_network(self) -> None:
        """本檔的非 llm_eval 測試不得產生任何真實 API 呼叫。"""
        mock = MagicMock()
        with patch("langfuse.openai.OpenAI", return_value=mock):
            from agents.chip_agent import _llm_synthesize_chip

            mock.chat.completions.create.return_value.choices = [
                MagicMock(message=MagicMock(content="測試敘述"))
            ]
            _llm_synthesize_chip(
                symbol="2330",
                signal=Signal.NEUTRAL,
                confidence=0.5,
                key_findings={"institutional_score": 0.0},
            )
        assert mock.chat.completions.create.called
