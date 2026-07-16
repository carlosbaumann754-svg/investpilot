"""Baut eine NEU-MOTOR wfo_status.json (Drift-Watchdog-Schema) via signal_stack_backtester.

Per-Window SL-Sweep ueber OOS-Jahres-Fenster -> best_params (WFO-SL) + oos_metrics
(PF + Sharpe, PF ist die R-B5-Primaermetrik). Ersetzt die veraltete Alt-TA-Baseline,
gegen die der Watchdog sonst Aepfel-mit-Birnen vergleicht.

WICHTIG (Task #4 Rest-Caveat): der MONATLICHE WFO-Cron (github-action) laeuft noch auf
dem ALT-TA walk_forward_optimizer und wuerde diese Datei beim naechsten Lauf wieder mit
einer Alt-TA-Baseline UEBERSCHREIBEN. Bis der Cron auf den Neu-Motor umgestellt (oder
deaktiviert) ist: nach jedem Cron-Lauf dieses Skript neu laufen lassen. Die LOCK-Werte
(SL/min_score/...) sind davon UNABHAENGIG geschuetzt (manual_lock_overrides ueberlagern).

Aufruf: docker exec investpilot python scripts/wfo_build_status.py
-> schreibt data/wfo_status_signal_stack_NEW.json (Kandidat; Live-Swap separat).
Read-only ausser der Kandidaten-Datei.
"""
import json
from datetime import datetime, timezone
from collections import Counter

TOP_N, DEPLOYMENT, TRAIL_ACT, TRAIL = 15, 0.70, 6.0, 4.0
SL_GRID = [-5, -6, -8, -10, -12]
LIVE_TP, LIVE_MIN_SCORE = 12, 25
START, END = "2019-01-01", "2026-07-01"
WINDOWS = [("2019-01-01", "2020-01-01"), ("2020-01-01", "2021-01-01"), ("2021-01-01", "2022-01-01"),
           ("2022-01-01", "2023-01-01"), ("2023-01-01", "2024-01-01"), ("2024-01-01", "2025-01-01"),
           ("2025-01-01", "2026-07-01")]


def pf_of(trades):
    w = sum(t["ret_net"] for t in trades if t["ret_net"] > 0)
    l = abs(sum(t["ret_net"] for t in trades if t["ret_net"] <= 0))
    return round(w / l, 3) if l > 0 else (999.0 if w > 0 else 0.0)


def main():
    from app import signal_stack_backtester as bt, edgar_client, sp600_universe
    from app.config_manager import load_config
    cfg = load_config()
    disabled = set(cfg.get("disabled_symbols", []) or [])
    symbols = [s for s in sp600_universe.get_symbols() if s not in disabled]
    facts = edgar_client.load_facts()
    print(f"Universe {len(symbols)}, Facts {len(facts)} - lade Preise + Picks (dauert)...", flush=True)
    ph = bt.load_price_history(symbols, START, END)
    picks = bt.precompute_monthly_picks(ph, facts, START, END)
    print(f"Picks {len(picks)} Monate.", flush=True)

    windows_out, best_sls = [], []
    for idx, (ws, we) in enumerate(WINDOWS):
        by_sl = {}
        for sl in SL_GRID:
            r = bt.run_backtest(ph, facts, symbols, ws, we, top_n=TOP_N, deployment=DEPLOYMENT,
                                sl_pct=sl, tp_pct=None, trail_act_pct=TRAIL_ACT, trail_pct=TRAIL,
                                picks_by_month=picks)
            m = bt._metrics(r["monthly_pct"], r["equity_final"], r["trades"])
            m["pf"] = pf_of(r["trades"])
            by_sl[sl] = m
        best_sl = max(SL_GRID, key=lambda s: by_sl[s]["sharpe_ann"])
        best_sls.append(best_sl)
        bm = by_sl[best_sl]
        windows_out.append({
            "idx": idx, "test_start": ws, "test_end": we,
            "train": "n/a (Motor nicht gefittet, reines OOS)",
            "best_params": {"stop_loss_pct": float(best_sl), "take_profit_pct": LIVE_TP,
                            "min_scanner_score": LIVE_MIN_SCORE},
            "oos_metrics": {"sharpe": bm["sharpe_ann"], "pf": bm["pf"],
                            "annual_return": bm["total_return_pct"], "max_dd": bm["max_drawdown_pct"],
                            "win_rate": bm["win_rate_pct"], "trades": bm["trades"]},
            "oos_sharpe": bm["sharpe_ann"], "oos_pf": bm["pf"],
            "sl_sweep_pf": {str(sl): by_sl[sl]["pf"] for sl in SL_GRID},
        })
        print(f"  {ws[:4]}: best SL {best_sl} | PF {bm['pf']} Sharpe {bm['sharpe_ann']}", flush=True)

    out = {
        "state": "done", "phase": "completed", "trigger": "signal_stack_wfo_rebaseline",
        "generated_for": "signal_stack_motor (Task #4 Re-Baseline)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "windows_total": len(WINDOWS), "windows": windows_out,
        "mean_oos_pf": round(sum(w["oos_pf"] for w in windows_out) / len(windows_out), 3),
        "mean_oos_sharpe": round(sum(w["oos_sharpe"] for w in windows_out) / len(windows_out), 3),
        "config": {"top_n": TOP_N, "deployment": DEPLOYMENT, "trail": [TRAIL_ACT, TRAIL]},
        "note": "min_scanner_score/take_profit_pct sind LIVE-Werte (Backtester top_n-basiert, sweept nur SL).",
    }
    with open("/app/data/wfo_status_signal_stack_NEW.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nMode best SL: {Counter(best_sls).most_common()} | mean OOS-PF {out['mean_oos_pf']}")
    print("-> data/wfo_status_signal_stack_NEW.json (Kandidat) geschrieben.")


if __name__ == "__main__":
    main()
