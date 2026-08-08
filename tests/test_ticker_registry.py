"""
adapters/ticker_registry.py —— 離線代碼→名稱查表。

背景：原本 news_adapter 用一份手寫的 30 筆 TW_STOCK_NAMES dict，
除那 30 檔藍籌股外，新聞搜尋只能用純數字代碼，涵蓋率不到全市場 1%。
改為由 data/tickers.jsonl（FinMind 台股 + SEC 美股）離線查表。

這些測試**不打任何網路**——registry 是本地檔案讀取。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters import ticker_registry as tr
from adapters.news_adapter import _stock_chinese_name


# ─── 真實 registry（data/tickers.jsonl 進版控，測試可直接依賴）────────────────


class TestRealRegistry:
    def test_registry_file_exists(self) -> None:
        assert tr.REGISTRY_PATH.exists(), (
            f"找不到 {tr.REGISTRY_PATH}；"
            "執行 `uv run python scripts/refresh_ticker_registry.py` 產生。"
        )

    def test_registry_covers_full_market_not_a_handful(self) -> None:
        """
        涵蓋率回歸防護：舊的手寫 dict 只有 30 筆。
        門檻設 5000 以確保是全市場等級而非又退回小抄表。
        """
        assert tr.registry_size() > 5000, (
            f"registry 只有 {tr.registry_size()} 筆，疑似退回手寫小表"
        )

    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [
            ("2330", "台積電"),
            ("2330.TW", "台積電"),   # 交易所後綴應被去除
            ("2317", "鴻海"),
            ("AAPL", "Apple Inc."),
            ("aapl", "Apple Inc."),  # 大小寫不敏感
        ],
    )
    def test_known_symbols_resolve(self, symbol: str, expected: str) -> None:
        assert tr.lookup_name(symbol) == expected

    def test_non_bluechip_tw_stock_resolves(self) -> None:
        """
        關鍵回歸：3034 聯詠不在舊的 30 筆手寫表裡。
        這正是本次修改要解決的涵蓋率問題。
        """
        assert tr.lookup_name("3034") == "聯詠"

    def test_unknown_symbol_returns_none(self) -> None:
        assert tr.lookup_name("9999") is None
        assert tr.lookup_name("") is None

    def test_lookup_returns_market_and_board(self) -> None:
        tw = tr.lookup("2330")
        assert tw is not None and tw.market == "TW" and tw.board == "twse"
        us = tr.lookup("AAPL")
        assert us is not None and us.market == "US" and us.board == "sec"


# ─── 降級行為（檔案缺失 / 損毀不得中斷管線）──────────────────────────────────


class TestGracefulDegradation:
    @staticmethod
    def _reset_cache() -> None:
        tr._load_registry.cache_clear()

    def test_missing_file_returns_none_not_crash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(tr, "REGISTRY_PATH", tmp_path / "nope.jsonl")
        self._reset_cache()
        try:
            assert tr.lookup_name("2330") is None
            assert tr.registry_size() == 0
        finally:
            self._reset_cache()

    def test_malformed_line_is_skipped_others_still_load(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        f = tmp_path / "tickers.jsonl"
        f.write_text(
            json.dumps({"symbol": "1111", "name": "好資料", "market": "TW"}, ensure_ascii=False)
            + "\n"
            + "{ this is not valid json\n"
            + json.dumps({"missing_symbol_key": True})
            + "\n"
            + json.dumps({"symbol": "2222", "name": "也是好的", "market": "TW"}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(tr, "REGISTRY_PATH", f)
        self._reset_cache()
        try:
            assert tr.lookup_name("1111") == "好資料"
            assert tr.lookup_name("2222") == "也是好的"
            assert tr.registry_size() == 2   # 兩筆壞的被略過
        finally:
            self._reset_cache()


# ─── news_adapter 接線 ────────────────────────────────────────────────────────


class TestNewsAdapterWiring:
    def test_known_stock_uses_chinese_name(self) -> None:
        assert _stock_chinese_name("2330.TW") == "台積電"

    def test_non_bluechip_now_gets_name_instead_of_bare_code(self) -> None:
        """修改前：3034 不在手寫表 → 回傳 '3034'，新聞只能用數字搜。"""
        assert _stock_chinese_name("3034") == "聯詠"

    def test_unknown_stock_falls_back_to_code(self) -> None:
        """降級行為必須與導入 registry 前一致。"""
        assert _stock_chinese_name("9999.TW") == "9999"

    def test_hardcoded_dict_is_gone(self) -> None:
        """防止有人把手寫小抄表加回來。"""
        import adapters.news_adapter as na

        assert not hasattr(na, "TW_STOCK_NAMES"), (
            "TW_STOCK_NAMES 已由 ticker_registry 取代，不應復活"
        )
