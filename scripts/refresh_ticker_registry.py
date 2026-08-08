"""
重新產生 data/tickers.jsonl —— 台股 + 美股代碼→名稱離線查表。

用法
----
    uv run python scripts/refresh_ticker_registry.py
    uv run python scripts/refresh_ticker_registry.py --tw-only    # 只更新台股
    uv run python scripts/refresh_ticker_registry.py --dry-run    # 只顯示不寫檔

何時該跑
--------
參考資料變動很慢（新上市櫃一個月數檔），建議**每季或有需要時手動跑**，
不需要排程。產出進版控，PR diff 可直接看到新增/更名的標的。

資料來源
--------
台股：FinMind TaiwanStockInfo（走 adapters/stock_info_adapter.py，
      沿用其 24h TTL 快取。無 FINMIND_KEY 時 FinMind 仍允許匿名取用此資料集）

美股：SEC company_tickers.json — 官方、免費、免 API key。
      ⚠️ SEC 規定必須帶可辨識的 User-Agent（含聯絡方式），
      否則會被擋。請設定環境變數：
          export SEC_USER_AGENT="YourName your@email.com"

      為什麼不用 yfinance：yfinance **無法列舉全部美股 ticker**，
      它只有 predefined screener（yf.screen），單次上限 250 筆，
      拿不到全市場清單。實測 most_actives 只回 250 檔。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.stock_info_adapter import StockInfoAdapter  # noqa: E402

OUTPUT_PATH: Path = Path(__file__).resolve().parent.parent / "data" / "tickers.jsonl"

SEC_TICKERS_URL: str = "https://www.sec.gov/files/company_tickers.json"
SEC_TIMEOUT_SEC: int = 60

# SEC 要求 User-Agent 含可辨識聯絡方式。預設值是佔位符，正式使用請設環境變數。
_DEFAULT_SEC_UA: str = "QuantDesk research (set SEC_USER_AGENT env var)"


def fetch_tw() -> list[dict[str, Any]]:
    """台股：FinMind TaiwanStockInfo。"""
    rows = StockInfoAdapter().fetch_all()
    return [
        {"symbol": s.stock_id, "name": s.stock_name, "market": "TW", "board": s.type}
        for s in rows
    ]


def fetch_us() -> list[dict[str, Any]]:
    """美股：SEC company_tickers.json。"""
    ua = os.environ.get("SEC_USER_AGENT", _DEFAULT_SEC_UA)
    resp = requests.get(
        SEC_TICKERS_URL, headers={"User-Agent": ua}, timeout=SEC_TIMEOUT_SEC
    )
    resp.raise_for_status()
    payload: dict[str, dict[str, Any]] = resp.json()
    return [
        {
            "symbol": str(v["ticker"]),
            "name": str(v["title"]),
            "market": "US",
            "board": "sec",
        }
        for v in payload.values()
        if v.get("ticker")
    ]


def build(tw_only: bool = False) -> list[dict[str, Any]]:
    rows = fetch_tw()
    print(f"  台股 : {len(rows):>6,} 檔（FinMind TaiwanStockInfo）")
    if not tw_only:
        us = fetch_us()
        print(f"  美股 : {len(us):>6,} 檔（SEC company_tickers.json）")
        rows += us

    # 去重：同一 symbol 保留先出現者（台股優先於美股，避免純數字代碼衝突）
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        key = str(row["symbol"]).upper()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    dropped = len(rows) - len(deduped)
    if dropped:
        print(f"  去重 : 移除 {dropped} 筆重複 symbol")
    return deduped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tw-only", action="store_true", help="只更新台股")
    parser.add_argument("--dry-run", action="store_true", help="只顯示不寫檔")
    args = parser.parse_args()

    print("重新產生 ticker registry…")
    rows = build(tw_only=args.tw_only)

    blob = "".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
        for r in sorted(rows, key=lambda r: (str(r["market"]), str(r["symbol"])))
    )

    print(f"  合計 : {len(rows):>6,} 筆  ({len(blob.encode()) / 1024:.0f} KB)")

    if args.dry_run:
        print("  (--dry-run，未寫檔)")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(blob, encoding="utf-8")
    print(f"✓ 已寫入 {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
