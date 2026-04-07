"""
Regression-Test fuer v10 Performance Pack (Hebel 1+2+3).

Validiert dass die neuen Code-Pfade BIT-IDENTISCHE Resultate liefern wie
die alten — d.h. wir gewinnen Speed OHNE Quality-Verlust.

Test-Matrix:
  A) simulate_trades  vs  simulate_trades_fast            (Hebel 1)
  B) run_grid_search  WORKERS=1  vs  WORKERS=2            (Hebel 3)
  C) Full Grid  vs  Union(Shard 0..3)                      (Hebel 2)

Alle drei Tests laufen auf einem kleinen Asset-Subset (5 Symbole, 2y Daten),
damit der Run lokal in <2 min durchgeht.
"""

import os
import sys
import json
import logging

# Quiet die meisten Logs - wir wollen nur das Test-Ergebnis sehen
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("Optimizer").setLevel(logging.INFO)
logging.getLogger("Backtester").setLevel(logging.WARNING)

# Sicherstellen dass wir vom Repo-Root importieren
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.backtester import (
    download_history,
    download_vix_history,
    simulate_trades,
    simulate_trades_fast,
    precompute_grid_data,
    calculate_metrics,
)
from app.optimizer import run_grid_search
from app.config_manager import load_config


SUBSET = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"]


def trade_signature(trades):
    """Reduziere Trade-Liste auf vergleichbare Signatur (deterministisch)."""
    sig = []
    for t in trades:
        sig.append((
            t.get("symbol"),
            str(t.get("entry_date")),
            str(t.get("exit_date")),
            round(float(t.get("entry_price", 0)), 6),
            round(float(t.get("exit_price", 0)), 6),
            round(float(t.get("pnl_pct", 0)), 4),
            t.get("exit_reason", ""),
        ))
    return sig


def grid_signature(grid_result):
    """Vergleichbare Signatur ueber alle Grid-Resultate."""
    sig = []
    for r in grid_result.get("all_results", []):
        sig.append((
            tuple(sorted(r["params"].items())),
            round(float(r.get("oos_sharpe", 0)), 4),
            round(float(r.get("oos_return", 0)), 4),
            round(float(r.get("is_sharpe", 0)), 4),
        ))
    return sorted(sig)


def main():
    print("=" * 70)
    print("v10 Performance Pack — Regression Test")
    print("=" * 70)

    print(f"\n[1/4] Download data for {SUBSET} (2y)...")
    histories = download_history(symbols=SUBSET, years=2)
    if not histories or len(histories) < 3:
        print(f"FEHLER: Konnte nicht genug Daten downloaden ({len(histories) if histories else 0})")
        sys.exit(1)
    print(f"      OK — {len(histories)} symbols")

    vix_history = download_vix_history(years=2)
    print(f"      VIX: {len(vix_history)} days")

    config = load_config()
    # Erzwinge realistische Defaults fuer den Test
    config.setdefault("demo_trading", {})
    config["demo_trading"]["min_scanner_score"] = 30
    config["demo_trading"]["stop_loss_pct"] = -5
    config["demo_trading"]["take_profit_pct"] = 8
    config.setdefault("leverage", {})
    config["leverage"]["trailing_sl_pct"] = 2.0
    config["leverage"]["trailing_sl_activation_pct"] = 1.0

    # ---------- TEST A: simulate_trades vs simulate_trades_fast ----------
    print("\n[2/4] Test A — simulate_trades vs simulate_trades_fast (Hebel 1)")
    trades_old = simulate_trades(
        histories, config,
        use_realistic_filters=True,
        vix_history=vix_history,
        earnings_blackouts={},
    )
    pre = precompute_grid_data(histories, vix_history)
    trades_new = simulate_trades_fast(
        pre, config,
        earnings_blackouts={},
        use_realistic_filters=True,
    )

    sig_old = trade_signature(trades_old)
    sig_new = trade_signature(trades_new)
    print(f"      old: {len(trades_old)} trades  |  new: {len(trades_new)} trades")
    if sig_old == sig_new:
        print("      [PASS] Bit-identische Trades")
    else:
        print("      [FAIL] Trades unterscheiden sich")
        # Zeige die ersten paar Diffs
        from difflib import unified_diff
        old_lines = [str(s) for s in sig_old]
        new_lines = [str(s) for s in sig_new]
        diff = list(unified_diff(old_lines, new_lines, lineterm="", n=2))
        for line in diff[:30]:
            print(f"        {line}")
        sys.exit(2)

    # ---------- TEST B: WORKERS=1 vs WORKERS=2 ----------
    print("\n[3/4] Test B — Grid-Search WORKERS=1 vs WORKERS=2 (Hebel 3)")

    # Wir reduzieren das Grid temporaer, damit der Test schnell ist
    import app.optimizer as opt
    saved_grid = dict(opt.PARAM_GRID)
    opt.PARAM_GRID = {
        "min_scanner_score": [30, 40],
        "stop_loss_pct": [-3, -5],
        "take_profit_pct": [5, 8],
        "trailing_sl_pct": [2.0],
        "trailing_sl_activation_pct": [1.0],
    }

    try:
        os.environ["INVESTPILOT_OPTIMIZER_WORKERS"] = "1"
        res_seq = run_grid_search(histories, config, vix_history=vix_history,
                                  earnings_blackouts={})
        os.environ["INVESTPILOT_OPTIMIZER_WORKERS"] = "2"
        res_par = run_grid_search(histories, config, vix_history=vix_history,
                                  earnings_blackouts={})

        sig_seq = grid_signature(res_seq)
        sig_par = grid_signature(res_par)
        print(f"      seq: {len(sig_seq)} combos  |  par: {len(sig_par)} combos")
        if sig_seq == sig_par:
            print("      [PASS] Sequenziell == Parallel")
        else:
            print("      [FAIL] Sequenziell != Parallel")
            for i, (a, b) in enumerate(zip(sig_seq, sig_par)):
                if a != b:
                    print(f"        diff[{i}]: seq={a}  par={b}")
                    break
            sys.exit(3)

        # ---------- TEST C: Full Grid vs Union(Shards) ----------
        print("\n[4/4] Test C — Full Grid vs Union of 4 Shards (Hebel 2)")
        os.environ["INVESTPILOT_OPTIMIZER_WORKERS"] = "1"  # Determinismus
        res_full = run_grid_search(histories, config, vix_history=vix_history,
                                   earnings_blackouts={})

        merged = []
        for shard_id in range(4):
            r = run_grid_search(
                histories, config,
                vix_history=vix_history, earnings_blackouts={},
                shard_id=shard_id, num_shards=4,
            )
            merged.extend(r.get("all_results", []))
        # Selbe Sortier-Logik wie Merge-Job
        merged.sort(key=lambda r: r["oos_sharpe"], reverse=True)
        merged_wrapper = {"all_results": merged}

        sig_full = grid_signature(res_full)
        sig_merged = grid_signature(merged_wrapper)
        print(f"      full: {len(sig_full)} combos  |  union(shards): {len(sig_merged)} combos")
        if sig_full == sig_merged:
            print("      [PASS] Full == Union(Shards)")
        else:
            print("      [FAIL] Sharding bricht Determinismus")
            print(f"        nur in full: {set(sig_full) - set(sig_merged)}")
            print(f"        nur in merged: {set(sig_merged) - set(sig_full)}")
            sys.exit(4)
    finally:
        opt.PARAM_GRID = saved_grid
        os.environ.pop("INVESTPILOT_OPTIMIZER_WORKERS", None)

    print("\n" + "=" * 70)
    print("ALLE TESTS BESTANDEN — Hebel 1+2+3 sind bit-identisch")
    print("=" * 70)


if __name__ == "__main__":
    main()
