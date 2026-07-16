"""WFO-Re-Baseline auf dem NEUEN Fundamental-Signal-Stack-Motor (Task #4).

Motor-korrekte Walk-Forward-Validierung via app/signal_stack_backtester: die
monatlichen Point-in-Time-Picks werden EINMAL vorberechnet (teuer), dann pro
Out-of-Sample-Jahres-Fenster ein SL-Sweep gefahren (billig) -> pro Fenster der
beste SL nach Sharpe -> Mode ueber alle Fenster = WFO-Empfehlung.

Zweck: (1) unabhaengige WFO-Tier-Evidenz ergaenzend zum Live-Soak; (2) pruefen ob
die manual_overrides-Rekalibrierung (SL -8, top_n 15) WFO-robust ist oder ob die
Neu-Motor-WFO einen anderen Wert empfiehlt; (3) Fundament fuer spaeteren Drift-
Watchdog-Re-Enable (loest die manual_overrides-'Bruecke' ab).

Read-only Analyse. Aufruf: docker exec investpilot python scripts/wfo_signal_stack_validation.py
Schreibt data/wfo_signal_stack_baseline.json + Konsolen-Report.
"""
import json
import sys
from collections import Counter
from datetime import date

# Rekalibrierte Config (= manual_lock_overrides): top_n 15, Deployment 0.70,
# Trailing 6/4, TP via Trailing (kein fixes TP). SL wird gesweept.
TOP_N = 15
DEPLOYMENT = 0.70
TRAIL_ACT, TRAIL = 6.0, 4.0
SL_GRID = [-5, -6, -8, -10, -12]
START, END = "2019-01-01", "2026-07-01"
# Jahres-Fenster (jedes ist OOS, da der Motor nicht gefittet wird)
WINDOWS = [("2019-01-01", "2020-01-01"), ("2020-01-01", "2021-01-01"),
           ("2021-01-01", "2022-01-01"), ("2022-01-01", "2023-01-01"),
           ("2023-01-01", "2024-01-01"), ("2024-01-01", "2025-01-01"),
           ("2025-01-01", "2026-07-01")]


def main():
    from app import signal_stack_backtester as bt
    from app import edgar_client, sp600_universe
    from app.config_manager import load_config

    cfg = load_config()
    disabled = set(cfg.get("disabled_symbols", []) or [])
    symbols = [s for s in sp600_universe.get_symbols() if s not in disabled]
    print(f"Universe: {len(symbols)} Symbole (nach disabled_symbols-Filter)", flush=True)

    facts = edgar_client.load_facts()
    print(f"EDGAR-Facts: {len(facts)} Symbole geladen", flush=True)

    print("Lade Kurshistorie 2019-2026 (yfinance-Bulk, dauert)...", flush=True)
    price_hist = bt.load_price_history(symbols, START, END)
    print(f"Preise: {len(price_hist)} Symbole mit History", flush=True)

    print("Praeberechne monatliche Picks (EINMAL, teuer)...", flush=True)
    picks = bt.precompute_monthly_picks(price_hist, facts, START, END)
    print(f"Picks fuer {len(picks)} Rebalance-Monate", flush=True)

    print(f"\n=== WFO: SL-Sweep {SL_GRID} pro Fenster (top_n={TOP_N}, deploy={DEPLOYMENT}, trail {TRAIL_ACT}/{TRAIL}) ===", flush=True)
    hdr = f"{'Fenster':11} | " + " ".join(f"SL{sl:>4}" for sl in SL_GRID) + " | best  IWM"
    print(hdr); print("-" * len(hdr))
    windows_out = []
    best_sls = []
    for w_start, w_end in WINDOWS:
        sharpes = {}
        rets = {}
        for sl in SL_GRID:
            res = bt.run_backtest(price_hist, facts, symbols, w_start, w_end,
                                  top_n=TOP_N, deployment=DEPLOYMENT, sl_pct=sl,
                                  tp_pct=None, trail_act_pct=TRAIL_ACT, trail_pct=TRAIL,
                                  picks_by_month=picks)
            m = bt._metrics(res["monthly_pct"], res["equity_final"], res["trades"])
            sharpes[sl] = m["sharpe_ann"]; rets[sl] = m["total_return_pct"]
        best_sl = max(SL_GRID, key=lambda s: sharpes[s])
        best_sls.append(best_sl)
        iwm = bt.benchmark_iwm(price_hist, w_start, w_end).get("iwm_sharpe_ann", 0)
        row = f"{w_start[:4]:11} | " + " ".join(f"{sharpes[sl]:>5.2f}" for sl in SL_GRID) + f" | {best_sl:>4}  {iwm:>4.2f}"
        print(row, flush=True)
        windows_out.append({"window": [w_start, w_end], "best_sl": best_sl,
                            "sharpe_by_sl": sharpes, "return_by_sl": rets, "iwm_sharpe": iwm})

    # Aggregat: Mode des besten SL (konservativ bei Tie = engster/naeher zu 0)
    ctr = Counter(best_sls)
    mode_sl = max(ctr.items(), key=lambda kv: (kv[1], kv[0]))[0]  # Tie -> hoeherer (engerer) SL
    print("-" * len(hdr))
    print(f"\nBester SL je Fenster: {best_sls}")
    print(f"Mode (WFO-Empfehlung): SL = {mode_sl}  |  aktuell live (manual_override): SL = -8")
    verdict = ("SL -8 IST WFO-robust (= Mode oder benachbart)" if mode_sl in (-8, -6, -10)
               else f"WFO empfiehlt SL {mode_sl} statt -8 -> pruefen")
    print(f"VERDIKT: {verdict}")

    out = {"generated_for": "signal_stack_motor", "start": START, "end": END,
           "config": {"top_n": TOP_N, "deployment": DEPLOYMENT, "trail_act": TRAIL_ACT, "trail": TRAIL},
           "sl_grid": SL_GRID, "windows": windows_out,
           "wfo_recommended_sl": mode_sl, "live_sl": -8, "verdict": verdict}
    with open("/app/data/wfo_signal_stack_baseline.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n-> data/wfo_signal_stack_baseline.json geschrieben.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
