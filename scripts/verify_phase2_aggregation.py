#!/usr/bin/env python3
"""
scripts/verify_phase2_aggregation.py

End-to-end Phase 2 pipeline check:
  config/positions.yaml
    → position_loader.load_portfolio()
    → pricing_router.price_option()   (BS for european TXO; binomial for american AAPL)
    → aggregate()                     (three-layer output + hard_constraints)
    → human-readable summary

⚠️  Spot prices and IV are PLACEHOLDER values (no live market data in Phase 2).
    Phase 4+ will wire in FinMind options_adapter for real backed-out IV.

FX:  Tries YFinanceFXAdapter (USDTWD) — degrades gracefully if network unavailable.
     A second run without the FX adapter explicitly demonstrates the degradation path.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# Allow running from repo root or from scripts/ directory
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.fx_adapter import YFinanceFXAdapter  # noqa: E402
from agents.risk.aggregation import (  # noqa: E402
    AggregationResult,
    aggregate,
)
from agents.risk.position_loader import Position, load_portfolio  # noqa: E402
from agents.risk.pricing_router import (  # noqa: E402
    DEFAULT_DIVIDEND_YIELD,
    DEFAULT_RISK_FREE_RATE,
    GreeksResult,
    OptionSpec,
    price_option,
)
from agents.risk.scenario import run_scenarios  # noqa: E402

# ─── Placeholder market data ──────────────────────────────────────────────────
# ⚠️ These are illustrative values. Replace with live feed in Phase 4+.

SPOT_MAP: dict[str, float] = {
    "2330.TW": 850.0,     # TWD — approximate TSMC spot (placeholder)
    "AAPL":    195.0,     # USD — approximate AAPL spot  (placeholder)
    "TXFF":  22_000.0,    # TWD — TAIEX index level      (placeholder)
    "TXO":   22_000.0,    # TWD — same TAIEX level for option pricing
}

# Assumed IV for all options — backed-out IV from FinMind comes in Phase 4.
PLACEHOLDER_IV: float = 0.20   # 20 % annualised


# ─── Helpers ─────────────────────────────────────────────────────────────────

W = 62   # print width

def _bar(char: str = "─") -> None:
    print(char * W)

def _section(title: str) -> None:
    print()
    _bar("═")
    print(f"  {title}")
    _bar("═")

def _ok(msg: str)   -> None: print(f"  ✓  {msg}")
def _warn(msg: str) -> None: print(f"  ⚠  {msg}")
def _err(msg: str)  -> None: print(f"  ✗  {msg}")


# ─── Step functions ──────────────────────────────────────────────────────────

def step1_load(path: Path | None = None) -> tuple[list[Position], float, str]:
    _section("Step 1 — Load positions.yaml")
    kwargs = {"path": path} if path else {}
    cfg = load_portfolio(**kwargs)
    positions     = cfg.positions
    portfolio_nav = cfg.portfolio_nav
    nav_currency  = cfg.nav_currency

    valid   = [p for p in positions if p.is_valid]
    invalid = [p for p in positions if not p.is_valid]

    print(f"  總部位: {len(positions)}   有效: {len(valid)}   無效: {len(invalid)}")
    print(f"  Portfolio NAV : {portfolio_nav:>12,.0f} {nav_currency}")
    print()

    for idx, pos in enumerate(positions):
        tag = "✓" if pos.is_valid else "✗"
        if pos.is_option:
            opt_detail = (f"K={pos.strike:.0f}  exp={pos.expiry}  "
                          f"{pos.option_type}  {pos.style}")
        else:
            opt_detail = ""
        print(f"  {tag} [{idx}] {pos.symbol:<10}  {pos.instrument_type:<8}  "
              f"qty={pos.quantity:+.0f}  mult={pos.multiplier:.0f}  "
              f"ccy={pos.currency}  {opt_detail}")
        for e in pos.errors:
            _err(f"       {e}")

    if invalid:
        print()
        _warn("有無效部位；aggregate 會略過，其餘照常計算。")

    return positions, portfolio_nav, nav_currency


def step2_fx() -> tuple[YFinanceFXAdapter | None, float | None, bool]:
    """
    Fetch USDTWD from Yahoo Finance.

    Returns (adapter, rate, success).  rate is the float value used to
    pre-convert USD spot/strike to TWD before pricing (so Greeks come out
    in TWD units natively).  Returns (None, None, False) on failure.
    """
    _section("Step 2 — FX rate（YFinanceFXAdapter → USDTWD）")
    _warn("⚠  FX rate 必須在 Greeks 計算之前取得，")
    _warn("   以便 USD 部位用 TWD spot 定價，確保 gamma 等 Greeks 已是 TWD 單位。")
    adapter = YFinanceFXAdapter()
    try:
        data   = adapter.fetch(pair="USDTWD")
        rate   = float(data.payload.rate)
        source = data.source
        asof   = data.asof
        _ok(f"USDTWD = {rate:.4f}   來源: {source}   asof: {asof:%Y-%m-%d}")
        return adapter, rate, True
    except Exception as exc:
        _err(f"FX 取值失敗: {exc}")
        _warn("Greeks 將以 USD spot 計算（gamma 單位不正確）。")
        return None, None, False


def step3_greeks(
    positions: list[Position],
    today: date,
    usdtwd: float | None,
) -> dict[int, GreeksResult]:
    """
    Price all option positions and build greeks_map.

    For USD-denominated positions, spot and strike are converted to TWD
    (spot_twd = spot_usd × usdtwd) before calling price_option so that all
    Greeks in the returned map are natively in TWD units.  This avoids the
    need for per-Greek FX conversion rules in aggregation (γ_TWD = γ_USD/fx,
    not γ_USD×fx — different from δ/ν/θ which scale linearly with FX).

    If usdtwd is None (FX fetch failed), USD spot is used as-is with a warning.
    """
    _section("Step 3 — Option Greeks（placeholder IV=20%，非實際 FinMind IV）")
    if usdtwd is not None:
        print(f"  USD → TWD 換算匯率: {usdtwd:.4f}（用於 spot/strike 預轉換）")
    else:
        _warn("usdtwd=None，USD 部位以原始 USD spot 定價，gamma 單位不正確。")
    print("  Spot prices (PLACEHOLDER, USD 部位如有 FX 則換算成 TWD):")
    for sym, px in SPOT_MAP.items():
        if usdtwd is not None and sym == "AAPL":
            print(f"    {sym:<10}  {px:>10,.1f} USD → {px * usdtwd:>12,.1f} TWD")
        else:
            print(f"    {sym:<10}  {px:>10,.1f}")
    print()

    greeks_map: dict[int, GreeksResult] = {}

    for idx, pos in enumerate(positions):
        if not pos.is_option:
            continue
        if not pos.is_valid:
            _warn(f"[{idx}] {pos.symbol}: 部位無效，略過 Greeks 計算")
            continue

        spot_orig = SPOT_MAP.get(pos.symbol)
        if spot_orig is None:
            _warn(f"[{idx}] {pos.symbol}: 不在 SPOT_MAP，略過")
            continue

        expiry_date = pos.expiry_date
        if expiry_date is None:
            _warn(f"[{idx}] {pos.symbol}: expiry 解析失敗，略過")
            continue

        T = (expiry_date - today).days / 365.0
        if T <= 0:
            _warn(f"[{idx}] {pos.symbol}: T={T:.4f} ≤ 0，略過")
            continue

        # Narrow Optional fields — guaranteed non-None for valid options by position_loader.
        assert pos.strike is not None
        assert pos.option_type is not None
        assert pos.style is not None

        # Pre-convert USD spot/strike to TWD so Greeks are in TWD units.
        # This is the source-of-truth fix for gamma units: γ_TWD = γ_USD / fx,
        # which pricing_router gives naturally when S and K are in TWD.
        if pos.currency == "USD" and usdtwd is not None:
            spot   = spot_orig * usdtwd
            strike = pos.strike * usdtwd
        else:
            spot   = spot_orig
            strike = pos.strike

        spec = OptionSpec(
            S=spot, K=strike, T=T,
            r=DEFAULT_RISK_FREE_RATE, q=DEFAULT_DIVIDEND_YIELD,
            sigma=PLACEHOLDER_IV,
            option_type=pos.option_type,
            style=pos.style,
            spot_currency="TWD",          # spot/strike already converted to TWD above
        )
        g = price_option(spec)
        greeks_map[idx] = g

        # Display using original spot for moneyness label
        moneyness = "ATM" if abs(spot_orig - pos.strike) / spot_orig < 0.02 else (
            "ITM" if (pos.option_type == "call" and spot_orig > pos.strike) or
                     (pos.option_type == "put"  and spot_orig < pos.strike) else "OTM"
        )
        ccy_note = f" [spot×{usdtwd:.4f}={spot:,.1f} TWD]" if pos.currency == "USD" and usdtwd else ""
        print(f"  [{idx}] {pos.symbol:<6} {pos.option_type.upper():<4} "
              f"K={pos.strike:.0f}  T={T:.3f}yr  {moneyness:<3}  "
              f"δ={g.delta:+.4f}  γ={g.gamma:.6f}  "
              f"ν={g.vega:.1f}  θ={g.theta:+.5f}/day  "
              f"[{g.model}]{ccy_note}")

    return greeks_map


def step4_aggregate(
    positions: list[Position],
    greeks_map: dict[int, GreeksResult],
    portfolio_nav: float,
    fx_adapter: YFinanceFXAdapter | None,
) -> AggregationResult:
    _section("Step 4 — aggregate()")
    result = aggregate(
        positions=positions,
        greeks_map=greeks_map,
        spot_map=SPOT_MAP,
        portfolio_nav=portfolio_nav,
        fx_adapter=fx_adapter,
    )
    if result.errors:
        print("  Errors / warnings from aggregate():")
        for e in result.errors:
            _warn(e)
    else:
        _ok("無錯誤")
    return result


def print_summary(result: AggregationResult, portfolio_nav: float) -> None:

    # ── Layer ①: by_currency ────────────────────────────────────────────────
    _section("Layer ①  by_currency（原幣別，不換算）")
    for ccy, sub in result.by_currency.items():
        print(f"  {ccy}:")
        print(f"    net_delta_notional : {sub.net_delta_notional:>+14,.0f}  {ccy}")
        print(f"    net_gamma          : {sub.net_gamma:>+14.4f}")
        print(f"    net_vega           : {sub.net_vega:>+14.2f}  {ccy}/unit σ")
        print(f"    net_theta          : {sub.net_theta:>+14.4f}  {ccy}/day")

    # ── Layer ②: consolidated_twd ────────────────────────────────────────────
    _section("Layer ②  consolidated_twd（全部換算成 TWD）")
    if result.consolidated_twd is None:
        _err("無法合算（FX 不可用或幣別缺匯率）")
        _warn("→ 需要 FX adapter 才能取得 TWD 合算值與 hard_constraints。")
    else:
        c = result.consolidated_twd
        print(f"  net_delta_notional_twd : {c.net_delta_notional_twd:>+14,.0f}  TWD")
        print(f"  net_gamma_twd          : {c.net_gamma_twd:>+14.4f}")
        print(f"  net_vega_twd           : {c.net_vega_twd:>+14.2f}  TWD/unit σ")
        print(f"  net_theta_twd          : {c.net_theta_twd:>+14.4f}  TWD/day")
        print()
        if c.fx_rates:
            for snap in c.fx_rates:
                _ok(f"FX used: {snap.pair} = {snap.rate:.4f}  "
                    f"[{snap.source} @ {snap.asof:%Y-%m-%d}]")
        else:
            _ok("FX used: 無（純 TWD 組合，不需換算）")

    # ── Layer ③a: index_point_exposure ───────────────────────────────────────
    _section("Layer ③a  index_point_exposure（台指精確換算，method=exact）")
    if not result.index_point_exposure:
        print("  （無台指衍生品部位）")
    else:
        for rec in result.index_point_exposure:
            direction = "多頭" if rec.net_dollar_delta_per_point > 0 else "空頭"
            abs_ddp   = abs(rec.net_dollar_delta_per_point)
            abs_lots  = abs(rec.txf_lot_equivalent)

            print(f"  標的         : {rec.underlying}")
            print(f"  涵蓋合約     : {', '.join(rec.contributing_symbols)}")
            print(f"  換算方式     : {rec.method}（合約規格 × 現價，無 beta 代理）")
            print(f"  台指現貨水準 : {rec.spot_index:>10,.0f} 點  (placeholder)")
            print(f"  net_dollar_delta_per_point : {rec.net_dollar_delta_per_point:>+10.1f}  TWD/點")
            print(f"  txf_lot_equivalent         : {rec.txf_lot_equivalent:>+10.3f}  口（台指期等值）")
            print(f"  net_delta_notional_twd     : {rec.net_delta_notional_twd:>+14,.0f}  TWD")
            print()
            # 明確一行：方向 + 價格敏感度 + 口數等效
            print(f"  ★  對台指淨曝險：{direction}，"
                  f"每點漲跌損益 {abs_ddp:,.0f} TWD（≈ {abs_lots:.2f} 口台指期等值）")

    # ── Layer ③b: unmapped_single_name ───────────────────────────────────────
    _section("Layer ③b  unmapped_single_name_exposure（個股，待 Phase 3 beta 估計）")
    if not result.unmapped_single_name_exposure:
        print("  （無個股部位）")
    else:
        for entry in result.unmapped_single_name_exposure:
            print(f"  {entry.symbol:<12}  ccy={entry.currency}  "
                  f"net_delta_notional={entry.net_delta_notional:>+10,.0f}  {entry.currency}")
            print(f"               → {entry.note}")
        print()
        # 明確確認 2330.TW 和 AAPL 都在列
        found_syms = {e.symbol for e in result.unmapped_single_name_exposure}
        for expected_substr in ("2330", "AAPL"):
            found = any(expected_substr in s for s in found_syms)
            tag   = "✓ 確認在列" if found else "✗ 未找到！"
            print(f"  {expected_substr:<8}: {tag}")

    # ── Hard constraints ─────────────────────────────────────────────────────
    _section("Hard Constraints（fail-safe：始終評估，FX 缺失時部分合算）")
    for hc in result.hard_constraints:
        if hc.breached:
            status = "⛔  BREACHED"
        else:
            status = "✓  OK"
        print(f"  {hc.type:<26}  {status}")
        print(f"    current = {hc.current:+.4f}   limit = ±{hc.limit:.4f}")
        if hc.detail:
            print(f"    {hc.detail}")
        print()


def show_fx_degradation_demo(
    positions: list[Position],
    greeks_map: dict[int, GreeksResult],
    portfolio_nav: float,
) -> None:
    """
    Always runs aggregate WITHOUT an FX adapter to demonstrate the degradation path,
    regardless of whether the live FX fetch in Step 2 succeeded.
    """
    _section("FX 降級展示（強制 fx_adapter=None）")
    print("  （此段固定不傳 FX adapter，展示 TWD+USD 混合部位的降級行為）")
    r_no_fx = aggregate(
        positions=positions,
        greeks_map=greeks_map,
        spot_map=SPOT_MAP,
        portfolio_nav=portfolio_nav,
        fx_adapter=None,     # ← 強制不提供
    )
    print()
    if r_no_fx.errors:
        print("  aggregate() errors/warnings（期望看到 FX 相關警告）:")
        for e in r_no_fx.errors:
            _warn(e)
    else:
        _ok("無錯誤（可能是純 TWD 組合，不需 FX）")

    # Fail-safe: consolidated_twd is always produced (never None).
    # USD positions are excluded when FX adapter is missing, TWD positions still included.
    ctwd = r_no_fx.consolidated_twd
    excl = ctwd.excluded_currencies
    if excl:
        _warn("consolidated_twd 已部分合算  ← 預期行為（fail-safe）")
        _warn(f"  排除幣別（缺少匯率）: {excl}")
        _ok(f"  可用幣別（TWD）合算結果: {ctwd.net_delta_notional_twd:+,.0f} TWD")
        _ok("  hard_constraints 仍依可用資料評估，不因 USD 缺失而全部跳過")
    else:
        _ok(f"consolidated_twd（完整）: {ctwd.net_delta_notional_twd:+,.0f} TWD")

    hc_count = len(r_no_fx.hard_constraints)
    if hc_count == 0:
        _err("hard_constraints = []  ← 非預期！fail-safe 應始終評估約束")
    else:
        _ok(f"hard_constraints 共 {hc_count} 筆（fail-safe 確認）")
        for hc in r_no_fx.hard_constraints:
            status = "⛔ BREACHED" if hc.breached else "✓ OK"
            print(f"    {hc.type:<26}  {status}")


def step5_scenario(
    positions: list[Position],
    greeks_map: dict[int, GreeksResult],
    usdtwd: float | None,
) -> None:
    """
    Demonstrate negative convexity using positions.yaml (空2口TXFF + 空5口TXO call
    + 多5口TXO put).

    For USD positions the pre-converted TWD spot must be used (same caller contract
    as aggregation).  Here we build a twd_spot_map consistent with step3_greeks.
    """
    _section("Step 5 — Scenario Stress Test（負凸性驗證）")
    print("  ΔP ≈ Δ×ΔS + ½×Γ×(ΔS)² + ν×ΔIV + Θ×Δt  (Δt = 1/365 calendar day)")
    print()

    # twd_spot_map: pre-convert USD positions — same contract as aggregation.py
    twd_spot_map: dict[str, float] = {}
    for sym, px in SPOT_MAP.items():
        if sym == "AAPL" and usdtwd is not None:
            twd_spot_map[sym] = px * usdtwd
        else:
            twd_spot_map[sym] = px

    result = run_scenarios(
        positions, greeks_map, twd_spot_map, days_held=1.0
    )

    # Print compact table for key scenarios: index ±1%/3%/5% × iv=0
    print("  ── 固定 Δσ=0（純標的波動效果）──────────────────────────────────────")
    header = (f"  {'Shock':>7}  {'Δ-P&L':>12}  {'Γ-P&L':>12}  "
              f"{'ν-P&L':>10}  {'Θ-P&L':>10}  {'Total':>12}")
    print(header)
    print(f"  {'─'*7}  {'─'*12}  {'─'*12}  {'─'*10}  {'─'*10}  {'─'*12}")

    for row in result.scenarios:
        if row.iv_shock != 0.0:
            continue
        shock_pct = f"{row.index_shock:+.0%}"
        convex_flag = " ◀ 負凸性" if row.index_shock > 0 else ""
        print(
            f"  {shock_pct:>7}  {row.agg_delta_pnl:>+12,.0f}  "
            f"{row.agg_gamma_pnl:>+12,.0f}  {row.agg_vega_pnl:>+10,.0f}  "
            f"{row.agg_theta_pnl:>+10.1f}  {row.agg_total_pnl:>+12,.0f}"
            f"{convex_flag}"
        )

    print()
    print("  ── 負凸性驗證：空 TXFF + 空 TXO call + 多 TXO put（三個核心部位）─")
    row3 = next(r for r in result.scenarios if r.index_shock == 0.03 and r.iv_shock == 0.0)
    row5 = next(r for r in result.scenarios if r.index_shock == 0.05 and r.iv_shock == 0.0)

    # Core positions: TXFF (futures) + TXO positions only — user specifically asked about these
    core_syms = {"TXFF", "TXO"}
    core3 = sum(leg.total_pnl for leg in row3.legs if leg.symbol in core_syms)
    core5 = sum(leg.total_pnl for leg in row5.legs if leg.symbol in core_syms)
    ratio = abs(core5) / abs(core3) if core3 != 0.0 else float("inf")
    print(f"  核心三部位 +3% 合計 P&L : {core3:>+12,.0f} TWD")
    print(f"  核心三部位 +5% 合計 P&L : {core5:>+12,.0f} TWD")
    print(f"  |+5%| / |+3%| = {ratio:.4f}  （純線性應為 5/3 ≈ {5/3:.4f}；超過 → 負凸性）")
    if ratio > 5 / 3:
        _ok(f"ratio={ratio:.4f} > 5/3={5/3:.4f}  → 確認負凸性（空call short-gamma主導）")
    else:
        _warn(f"ratio={ratio:.4f} ≤ 5/3  → 核心部位淨 gamma 趨近中性或正值")

    # Full portfolio note: AAPL long call adds positive gamma, may dominate
    agg_gamma3 = row3.agg_gamma_pnl
    if agg_gamma3 > 0:
        print(f"  （全組合淨 gamma P&L = +{agg_gamma3:,.0f}：AAPL long call 使全組合偏多 gamma）")
    else:
        print(f"  （全組合淨 gamma P&L = {agg_gamma3:,.0f}：全組合空 gamma）")

    print()
    print("  ── +3% 各腿 P&L 明細 ──────────────────────────────────────────────")
    for leg in row3.legs:
        print(f"  [{leg.position_idx}] {leg.symbol:<8} {leg.instrument_type:<8}  "
              f"δ={leg.delta_pnl:>+10,.0f}  γ={leg.gamma_pnl:>+10,.0f}  "
              f"ν={leg.vega_pnl:>+8,.0f}  θ={leg.theta_pnl:>+8.2f}  "
              f"total={leg.total_pnl:>+10,.0f}  TWD")

    print()
    print("  ── +5% 各腿 P&L 明細 ──────────────────────────────────────────────")
    for leg in row5.legs:
        print(f"  [{leg.position_idx}] {leg.symbol:<8} {leg.instrument_type:<8}  "
              f"δ={leg.delta_pnl:>+10,.0f}  γ={leg.gamma_pnl:>+10,.0f}  "
              f"ν={leg.vega_pnl:>+8,.0f}  θ={leg.theta_pnl:>+8.2f}  "
              f"total={leg.total_pnl:>+10,.0f}  TWD")

    print()
    print("  ── IV 情境矩陣（僅 +3% 標的波動 × 所有 IV shock）─────────────────")
    hdr2 = (f"  {'Δσ':>7}  {'Δ-P&L':>12}  {'Γ-P&L':>12}  "
            f"{'ν-P&L':>10}  {'Θ-P&L':>10}  {'Total':>12}")
    print(hdr2)
    print(f"  {'─'*7}  {'─'*12}  {'─'*12}  {'─'*10}  {'─'*10}  {'─'*12}")
    for row in result.scenarios:
        if row.index_shock != 0.03:
            continue
        iv_str = f"{row.iv_shock:+.0%}"
        print(
            f"  {iv_str:>7}  {row.agg_delta_pnl:>+12,.0f}  "
            f"{row.agg_gamma_pnl:>+12,.0f}  {row.agg_vega_pnl:>+10,.0f}  "
            f"{row.agg_theta_pnl:>+10.1f}  {row.agg_total_pnl:>+12,.0f}"
        )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    today = date.today()

    print()
    _bar("═")
    print("  QuantDesk Phase 2 — Aggregation End-to-End Verification")
    print(f"  Run date : {today}")
    _bar("═")

    positions, portfolio_nav, nav_currency = step1_load()
    fx_adapter, usdtwd, fx_ok = step2_fx()       # FX first — needed for spot pre-conversion
    greeks_map = step3_greeks(positions, today, usdtwd=usdtwd)
    result = step4_aggregate(positions, greeks_map, portfolio_nav, fx_adapter)

    print_summary(result, portfolio_nav)
    show_fx_degradation_demo(positions, greeks_map, portfolio_nav)
    step5_scenario(positions, greeks_map, usdtwd)

    _bar("═")
    print("  驗證完成。")
    _bar("═")
    print()


if __name__ == "__main__":
    main()
