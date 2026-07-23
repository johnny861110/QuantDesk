"""
Langfuse smoke test — 執行 risk_agent + fundamental_agent 各一次，
確認 trace 出現在 localhost:3000

此腳本需先 load .env 並驗證 LANGFUSE_ENABLED/TRACING_ACTIVE，才能安全
import 後續 agent/observability 模組，因此 import 故意不在檔案最上方。
E402 noqa 均為刻意設計，非偷懶。

用法：
  uv run python scripts/demo_langfuse_smoke.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

if os.getenv("LANGFUSE_ENABLED", "false").lower() != "true":
    print("⚠️  LANGFUSE_ENABLED 未設為 true，請確認 .env 內容")
    sys.exit(1)

from observability.langfuse_setup import TRACING_ACTIVE  # noqa: E402

if not TRACING_ACTIVE:
    print("⚠️  Langfuse 未成功啟動（可能 PUBLIC_KEY/SECRET_KEY 有誤或 host 連不到）")
    sys.exit(1)

print("✅ Langfuse tracing active")

# ─── Risk Agent smoke run ─────────────────────────────────────────────────────
print("\n▶ Running risk_agent ...")

from adapters.base import DataSourceAdapter, SourcedData  # noqa: E402
from adapters.fx_adapter import FXRate  # noqa: E402


class MockFXAdapter(DataSourceAdapter):
    @property
    def source_name(self) -> str:
        return "mock_fx"

    def fetch(self, pair: str = "USDTWD", **kwargs: object) -> SourcedData:
        return SourcedData(
            payload=FXRate(pair=pair, rate=32.5),
            source=self.source_name,
            asof=datetime(2026, 7, 23, 9, 0),
        )


from agents.risk_agent import run_risk_agent  # noqa: E402

risk_sig = run_risk_agent(fx_adapter=MockFXAdapter())
print(f"   signal={risk_sig.signal.value}  confidence={risk_sig.confidence:.2f}")
print(f"   hard_constraints={len(risk_sig.hard_constraints)}")
print(f"   errors={risk_sig.errors[:1] if risk_sig.errors else []}")

# ─── Fundamental Agent smoke run ──────────────────────────────────────────────
print("\n▶ Running fundamental_agent ...")

from agents.fundamental_agent import FundamentalAgent  # noqa: E402
from tests.conftest import (  # noqa: E402
    SQLiteStore,
    _insert_fact,
    _insert_filing,
    _set_quality_score,
)

try:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/smoke.db"
        store = SQLiteStore(db_path)
        fid = _insert_filing(store, "2330", "台積電", 2024, "Q1")
        for field, value in [
            ("net_revenue", 625_763_000.0), ("gross_profit", 265_457_000.0),
            ("operating_income", 249_519_000.0), ("net_income", 225_491_000.0),
            ("eps_basic", 8.70), ("total_assets", 6_580_023_000.0),
            ("total_liabilities", 2_790_000_000.0), ("equity", 3_790_023_000.0),
            ("operating_cash_flow", 381_232_000.0),
            ("current_assets", 1_200_000_000.0), ("current_liabilities", 800_000_000.0),
        ]:
            _insert_fact(store, fid, field, value, "xbrl", 1.0)
        _set_quality_score(store, fid, 0.95)

        agent = FundamentalAgent(db_path)
        fund_sig = agent.run("2330", 2024, "Q1")
        print(f"   signal={fund_sig.signal.value}  confidence={fund_sig.confidence:.2f}")
        print(f"   hard_constraints={len(fund_sig.hard_constraints)}")
        print(f"   errors={fund_sig.errors[:1] if fund_sig.errors else []}")
except Exception as exc:
    print(f"   ⚠️  fundamental_agent 跑失敗: {exc}")
    print("   → risk_agent 的 trace 應該已經在 Langfuse 了，可先確認那個")

# ─── flush ────────────────────────────────────────────────────────────────────
print("\n⏳ Flushing Langfuse spans ...")
from langfuse import get_client  # noqa: E402

get_client().flush()
print("✅ Done — 請開 localhost:3000 確認 trace")
