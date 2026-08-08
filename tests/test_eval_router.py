"""
Phase 17 L2 —— Router golden set。

兩種消費方式
------------
1. **確定性迴歸**（預設執行，進 CI）
   跑 _regex_fallback 對照 golden set。不需要 LLM、不需要網路。
   這是 LLM 掛掉時的降級路徑，本來就該被嚴格守護。

2. **LLM 準確率評估**（`-m llm_eval`，預設 skip）
   實際呼叫 Router LLM 量測分類正確率。需要 OPENAI_API_KEY，
   會產生 API 費用，故不進 CI：
       uv run pytest -m llm_eval -q -s

golden set 的價值已先行驗證
---------------------------
建立這份對照表的過程本身就揪出兩個真實路由 bug：
  · 「鴻海的毛利率趨勢」被判成 macro_outlook
    （「毛利率」含子字串「利率」，且 macro 分支排在 fundamental 之前）
  · 「現在該加碼還是減碼」無關鍵字命中，落到預設值
前者已修（(?<!毛) lookbehind），後者記錄為 regex 的已知限制。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from router.intent_router import _QUERY_TYPE_AGENTS, _regex_fallback

GOLDEN_SET_PATH = Path(__file__).parent / "data" / "router_golden_set.yaml"


def _load_cases() -> list[dict[str, Any]]:
    with GOLDEN_SET_PATH.open(encoding="utf-8") as fh:
        cases: list[dict[str, Any]] = yaml.safe_load(fh)
    return cases


CASES: list[dict[str, Any]] = _load_cases()


def _case_id(case: dict[str, Any]) -> str:
    q = str(case["query"])
    return f"{case['expect']}::{q[:24] or '<empty>'}"


# ─── golden set 自身的健全性 ─────────────────────────────────────────────────


class TestGoldenSetIntegrity:
    def test_not_empty_and_reasonably_sized(self) -> None:
        assert len(CASES) >= 30, f"golden set 只有 {len(CASES)} 筆，覆蓋不足"

    def test_every_case_has_required_fields(self) -> None:
        for i, c in enumerate(CASES):
            assert "query" in c, f"case #{i} 缺 query"
            assert "expect" in c, f"case #{i} 缺 expect"

    def test_every_expect_is_a_real_query_type(self) -> None:
        for c in CASES:
            assert c["expect"] in _QUERY_TYPE_AGENTS, (
                f"golden set 的 expect='{c['expect']}' 不在 _QUERY_TYPE_AGENTS 中"
            )

    def test_every_query_type_is_covered(self) -> None:
        """每個 query_type 都要有 golden case，避免新類型沒被評估到。"""
        covered = {c["expect"] for c in CASES}
        missing = set(_QUERY_TYPE_AGENTS) - covered
        assert not missing, f"下列 query_type 沒有任何 golden case：{missing}"

    def test_fallback_gap_values_are_real_query_types(self) -> None:
        for c in CASES:
            gap = c.get("fallback_gap")
            if gap is not None:
                assert gap in _QUERY_TYPE_AGENTS, f"fallback_gap='{gap}' 不是合法 query_type"


# ─── 確定性迴歸：regex fallback ──────────────────────────────────────────────


class TestRegexFallbackGolden:
    """
    LLM 不可用時的降級路徑。純確定性，必須永遠正確。
    """

    @pytest.mark.parametrize("case", CASES, ids=_case_id)
    def test_fallback_routes_as_documented(self, case: dict[str, Any]) -> None:
        # 有 fallback_gap 表示 regex 給不出理想答案，斷言它的已知實際行為
        expected = case.get("fallback_gap") or case["expect"]
        result = _regex_fallback(str(case["query"]))
        assert result.query_type == expected, (
            f"query={case['query']!r}\n"
            f"  期望 {expected}，實際 {result.query_type}\n"
            f"  （expect={case['expect']}, fallback_gap={case.get('fallback_gap')}）"
        )

    @pytest.mark.parametrize(
        "case", [c for c in CASES if c.get("targets")], ids=_case_id
    )
    def test_fallback_extracts_expected_targets(self, case: dict[str, Any]) -> None:
        result = _regex_fallback(str(case["query"]))
        assert result.targets == case["targets"], (
            f"query={case['query']!r} 期望 targets={case['targets']}，實際 {result.targets}"
        )

    @pytest.mark.parametrize("case", CASES, ids=_case_id)
    def test_fallback_never_raises_and_agents_consistent(
        self, case: dict[str, Any]
    ) -> None:
        """降級路徑不得拋例外，且 agents 必須與 mapping 一致。"""
        result = _regex_fallback(str(case["query"]))
        assert result.agents == _QUERY_TYPE_AGENTS[result.query_type]

    def test_fallback_gaps_are_still_gaps(self) -> None:
        """
        若 regex fallback 被改進到能給出理想答案，此測試會提醒移除 fallback_gap
        （避免 golden set 永久記錄一個已經不存在的限制）。
        """
        stale: list[str] = []
        for c in CASES:
            if c.get("fallback_gap") is None:
                continue
            if _regex_fallback(str(c["query"])).query_type == c["expect"]:
                stale.append(str(c["query"]))
        assert not stale, (
            "下列 case 的 regex fallback 已能給出理想答案，"
            f"請從 golden set 移除其 fallback_gap 欄位：{stale}"
        )


# ─── LLM 準確率評估（預設 skip）──────────────────────────────────────────────


@pytest.mark.llm_eval
class TestRouterLLMAccuracy:
    """
    實際呼叫 Router LLM 量測分類正確率。

    執行：uv run pytest -m llm_eval -q -s
    需要 OPENAI_API_KEY，會產生 API 費用，故不進 CI。
    """

    # 低於此正確率視為 Router prompt 品質退步
    MIN_ACCURACY: float = 0.80

    def test_llm_routing_accuracy(self) -> None:
        if not os.environ.get("OPENAI_API_KEY"):
            pytest.skip("需要 OPENAI_API_KEY")

        from router.intent_router import route

        hits = 0
        failures: list[str] = []
        for case in CASES:
            query = str(case["query"])
            if not query:
                continue   # 空字串不送 LLM
            try:
                got = route(query).query_type
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{query!r} → 例外 {exc}")
                continue
            if got == case["expect"]:
                hits += 1
            else:
                failures.append(f"{query!r} → 期望 {case['expect']}，實際 {got}")

        total = len([c for c in CASES if str(c["query"])])
        accuracy = hits / total if total else 0.0
        print(f"\nRouter LLM 分類正確率：{hits}/{total} = {accuracy:.1%}")
        for f in failures:
            print(f"  ✗ {f}")

        assert accuracy >= self.MIN_ACCURACY, (
            f"正確率 {accuracy:.1%} 低於門檻 {self.MIN_ACCURACY:.0%}"
        )
