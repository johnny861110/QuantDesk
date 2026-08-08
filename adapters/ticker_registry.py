"""
Ticker Registry — 台股 / 美股代碼→名稱的本地離線查表。

為什麼是本地檔案而不是打 API
----------------------------
代碼→名稱是**幾乎不變的參考資料**（新上市櫃一個月才幾檔），
沒有理由在每次新聞查詢時去打一次外部 API：

  - 執行期零網路：不受 FinMind / SEC 可用性與 rate limit 影響
  - 測試天然安全：純本地檔案讀取，測試不會意外打真實 API
  - 免憑證：不需要 FINMIND_KEY 也能拿到中文名
  - 快：模組層 lazy-load 一次，之後都是 dict O(1)

資料來源（見 scripts/refresh_ticker_registry.py）
------------------------------------------------
  台股 : FinMind TaiwanStockInfo（twse 上市 / tpex 上櫃 / emerging 興櫃）
  美股 : SEC company_tickers.json（官方、免費、免 API key）

  註：yfinance **無法**列舉全部美股 ticker——它只有 predefined screener，
      單次上限 250 筆，故美股改用 SEC 官方清單。

資料新鮮度
----------
`data/tickers.jsonl` 進版控，由 scripts/refresh_ticker_registry.py 重新產生。
新上市/新掛牌在下次 refresh 前查不到，此時 lookup_name() 回傳 None，
呼叫端一律降級為「直接用代碼」——與導入本模組前的行為相同，不會壞掉。

Public API
----------
    from adapters.ticker_registry import lookup_name

    lookup_name("2330")     → "台積電"
    lookup_name("2330.TW")  → "台積電"     （自動去除交易所後綴）
    lookup_name("AAPL")     → "Apple Inc."
    lookup_name("9999")     → None         （查無 → 呼叫端降級用代碼）
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# data/tickers.jsonl 相對於 repo root（本檔在 adapters/ 之下）
REGISTRY_PATH: Path = Path(__file__).resolve().parent.parent / "data" / "tickers.jsonl"

# 台股常見的交易所後綴，查表前先去掉
_TW_SUFFIXES: tuple[str, ...] = (".TW", ".TWO")


@dataclass(frozen=True)
class TickerInfo:
    symbol: str
    name: str
    market: str   # "TW" | "US"
    board: str    # TW: twse / tpex / emerging；US: sec


def _normalise(symbol: str) -> str:
    """'2330.TW' → '2330'；'aapl' → 'AAPL'。"""
    s = symbol.strip().upper()
    for suffix in _TW_SUFFIXES:
        if s.endswith(suffix):
            return s[: -len(suffix)]
    return s


@lru_cache(maxsize=1)
def _load_registry() -> dict[str, TickerInfo]:
    """
    讀取 data/tickers.jsonl 建索引（整個 process 只做一次）。

    檔案缺失或損毀時回傳空 dict 並記 warning——查詢一律降級回傳 None，
    絕不讓參考資料問題中斷新聞分析管線。
    """
    index: dict[str, TickerInfo] = {}
    try:
        with REGISTRY_PATH.open(encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                    symbol = str(row["symbol"]).upper()
                    index[symbol] = TickerInfo(
                        symbol=symbol,
                        name=str(row["name"]),
                        market=str(row.get("market", "")),
                        board=str(row.get("board", "")),
                    )
                except (json.JSONDecodeError, KeyError) as exc:
                    logger.warning(
                        "ticker_registry: 略過第 %d 行（格式錯誤）: %s", lineno, exc
                    )
    except FileNotFoundError:
        logger.warning(
            "ticker_registry: 找不到 %s，代碼→名稱查詢一律回傳 None。"
            "執行 `uv run python scripts/refresh_ticker_registry.py` 產生。",
            REGISTRY_PATH,
        )
    except OSError as exc:
        logger.warning("ticker_registry: 讀取 %s 失敗: %s", REGISTRY_PATH, exc)
    return index


def lookup(symbol: str) -> TickerInfo | None:
    """查完整 TickerInfo；查無回傳 None。"""
    return _load_registry().get(_normalise(symbol))


def lookup_name(symbol: str) -> str | None:
    """
    查代碼對應的公司名稱（台股中文名 / 美股英文名）；查無回傳 None。

    呼叫端請自行決定降級行為，慣例是直接改用代碼本身。
    """
    info = lookup(symbol)
    return info.name if info else None


def registry_size() -> int:
    """目前載入的筆數（0 = 檔案缺失或全空）。供健康檢查與測試使用。"""
    return len(_load_registry())
