"""Shared narrative Verifier — guards LLM/deterministic text for all domain agents."""
from __future__ import annotations

import re
from typing import Any

_NUM_RE = re.compile(
    r"""
    (?<!\d)               # not preceded by digit
    -?                    # optional negative
    (?!(?:19|20)\d{2}(?!\d))  # exclude 4-digit years 19xx / 20xx
    \d{1,3}(?:,\d{3})*    # integer part, optional thousands-separator
    (?:\.\d+)?            # optional decimal
    (?!\d)                # not followed by digit
    """,
    re.VERBOSE,
)
_REL_TOL = 0.005
_ABS_TOL = 0.01


def _parse_numbers(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in _NUM_RE.findall(text)]


def _known_values(metrics: dict[str, Any]) -> set[float]:
    """Flatten all numeric leaf values from the metrics dict."""
    result: set[float] = set()
    for v in metrics.values():
        if isinstance(v, (int, float)) and v == v:  # exclude NaN
            result.add(float(v))
    return result


def _value_matches(num: float, known: set[float]) -> bool:
    return any(
        abs(num - kv) <= max(_REL_TOL * abs(kv), _ABS_TOL)
        for kv in known
    )


def check_narrative(narrative: str, metrics: dict[str, Any]) -> list[str]:
    """
    Scan the LLM-generated narrative for numbers that do NOT appear in
    the verified metrics dict.  Any such number is flagged as an error —
    it means the LLM hallucinated a figure rather than citing a tool output.

    Returns a list of error strings (empty = all numbers verified).
    """
    if not narrative:
        return []
    known = _known_values(metrics)
    errors: list[str] = []
    for num in _parse_numbers(narrative):
        if not _value_matches(num, known):
            errors.append(
                f"[Verifier] 敘述中出現未經工具驗證的數字 {num}，"
                "違反「LLM 不得產出數字」原則。"
            )
    return errors
