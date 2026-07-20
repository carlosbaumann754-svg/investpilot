"""R-B12 (20.07.2026) — WFO-Sweep der EXIT-Parameter (Trailing + TP).

ANLASS
------
Die Live-Round-Trips des Fundamental-Motors zeigen eine Payoff-Asymmetrie:
Trefferquote 66.7 %, aber O Gewinn +783 USD gegen O Verlust -4'077 USD
(Verhaeltnis 0.19) -> Profit-Faktor 0.38, netto -7'534 USD.

Break-even bei 67 % Trefferquote verlangt ein Payoff-Verhaeltnis von 0.5
((1-p)/p). Verdacht: der Trailing-Stop (Aktivierung +6 %, Trail 4 %) deckelt
Gewinner strukturell bei ~+2 %, waehrend der SL -8 % Verlusten erlaubt =
per Design festgezurrtes ~1:4. Der SL ist WFO-verteidigt (-8, regime-abhaengig)
-> der Hebel ist der TRAIL.

FRAGE AN DIESEN SWEEP
---------------------
Laesst sich das Payoff-Verhaeltnis durch weitere/spaetere Trails heben, OHNE
Profit-Faktor und Drawdown zu zerstoeren? Und: hilft Trailing ueberhaupt, oder
waere gar kein Trailing besser?

METHODIK (bewusst konservativ)
------------------------------
- Dieselben 7 OOS-Jahres-Fenster wie die SL-Baseline. Der Motor wird NICHT
  gefittet -> jedes Fenster ist echtes Out-of-Sample.
- Kein globales Optimum picken. Berichtet werden (a) gepoolte Kennzahlen ueber
  alle Fenster und (b) der beste Wert JE FENSTER — auseinanderlaufende Fenster
  = instabil = nicht anfassen, egal wie gut der Mittelwert aussieht.
- Selektionskriterium ist NICHT die Rendite (Curve-Fitting-Falle), sondern
  Profit-Faktor + Payoff-Verhaeltnis bei akzeptablem Drawdown.

WICHTIG: Ergebnis ist WFO-Tier-Evidenz, KEIN Live-Beweis (Hierarchie:
Live > WFO > Optimizer). Liefert einen begruendeten Kandidaten fuer die
Post-Soak-Rekalibrierung — nichts wird automatisch angewendet.

Read-only. Aufruf:
    docker exec investpilot python scripts/wfo_trailing_sweep.py
Schreibt data/wfo_trailing_sweep.json + Konsolen-Report.
"""
import json
import statistics
import sys

# Fixiert (= aktuelle Rekalibrierung / WFO-verteidigt)
TOP_N = 15
DEPLOYMENT = 0.70
SL = -8

# Exit-Varianten: (trail_act, trail, tp)  — None = aus
# Enthaelt die LIVE-Config (6/4 mit TP 12) und "gar kein Trailing" als Kontrollen.
VARIANTS = []
for act in (4.0, 6.0, 8.0, 10.0):
    for tr in (4.0, 6.0, 8.0, 10.0, 12.0):
        VARIANTS.append((act, tr, None))
VARIANTS += [
    (None, None, None),    # nur SL, kein Exit nach oben
    (None, None, 12.0),    # nur SL + festes TP 12
    (None, None, 20.0),    # nur SL + weites TP
    (6.0, 4.0, 12.0),      # LIVE-Config (Trailing 6/4 + TP 12)
    (8.0, 8.0, 20.0),      # weit + Notbremse
]

START, END = "2019-01-01", "2026-07-01"
WINDOWS = [("2019-01-01", "2020-01-01"), ("2020-01-01", "2021-01-01"),
           ("2021-01-01", "2022-01-01"), ("2022-01-01", "2023-01-01"),
           ("2023-01-01", "2024-01-01"), ("2024-01-01", "2025-01-01"),
           ("2025-01-01", "2026-07-01")]


def payoff_metrics(trades: list) -> dict:
    """Die Kennzahlen, um die es geht — _metrics() des Backtesters hat sie nicht."""
    rets = [t["ret_net"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    avg_w = statistics.mean(wins) if wins else 0.0
    avg_l = statistics.mean(losses) if losses else 0.0
    wr = len(wins) / len(rets) if rets else 0.0
    return {
        "n": len(rets),
        "win_rate": round(wr * 100, 1),
        "avg_win": round(avg_w, 2),
        "avg_loss": round(avg_l, 2),
        # Payoff = O Gewinn / |O Verlust|. Break-even braucht (1-p)/p.
        "payoff": round(avg_w / abs(avg_l), 3) if avg_l else None,
        "payoff_needed": round((1 - wr) / wr, 3) if wr else None,
        "pf": round(gw / gl, 3) if gl else None,
        "expectancy_pct": round(statistics.mean(rets), 3) if rets else 0.0,
    }


def label(v):
    act, tr, tp = v
    trail = "kein Trail" if act is None else f"{act:g}/{tr:g}"
    return f"{trail:>10s} TP{'-' if tp is None else f'{tp:g}'}"


def main():
    from app import signal_stack_backtester as bt
    from app import edgar_client, sp600_universe
    from app.config_manager import load_config

    cfg = load_config()
    disabled = set(cfg.get("disabled_symbols", []) or [])
    symbols = [s for s in sp600_universe.get_symbols() if s not in disabled]
    print(f"Universe: {len(symbols)} Symbole", flush=True)

    facts = edgar_client.load_facts()
    print(f"EDGAR-Facts: {len(facts)} Symbole", flush=True)
    print("Lade Kurshistorie (dauert)...", flush=True)
    price_hist = bt.load_price_history(symbols, START, END)
    print(f"Preise: {len(price_hist)} Symbole", flush=True)
    print("Praeberechne monatliche Picks (EINMAL)...", flush=True)
    picks = bt.precompute_monthly_picks(price_hist, facts, START, END)
    print(f"Picks fuer {len(picks)} Monate\n", flush=True)

    print(f"=== EXIT-SWEEP  (SL fix {SL}, top_n {TOP_N}, deploy {DEPLOYMENT}) ===")
    print(f"{len(VARIANTS)} Varianten x {len(WINDOWS)} OOS-Fenster\n", flush=True)

    results = []
    for v in VARIANTS:
        act, tr, tp = v
        pooled_trades, per_window = [], []
        for w_start, w_end in WINDOWS:
            res = bt.run_backtest(price_hist, facts, symbols, w_start, w_end,
                                  top_n=TOP_N, deployment=DEPLOYMENT, sl_pct=SL,
                                  tp_pct=tp, trail_act_pct=act, trail_pct=tr,
                                  picks_by_month=picks)
            m = bt._metrics(res["monthly_pct"], res["equity_final"], res["trades"])
            pm = payoff_metrics(res["trades"])
            pooled_trades += res["trades"]
            per_window.append({
                "window": w_start[:4], "ret": m["total_return_pct"],
                "sharpe": m["sharpe_ann"], "mdd": m["max_drawdown_pct"],
                "pf": pm["pf"], "payoff": pm["payoff"],
            })
        pooled = payoff_metrics(pooled_trades)
        rets = [w["ret"] for w in per_window]
        shs = [w["sharpe"] for w in per_window]
        pfs = [w["pf"] for w in per_window if w["pf"] is not None]
        results.append({
            "variant": {"trail_act": act, "trail": tr, "tp": tp},
            "label": label(v),
            "pooled": pooled,
            "mean_ret": round(statistics.mean(rets), 2),
            "mean_sharpe": round(statistics.mean(shs), 2),
            "worst_mdd": round(min(w["mdd"] for w in per_window), 2),
            "windows_pf_gt1": sum(1 for p in pfs if p > 1.0),
            "min_window_pf": round(min(pfs), 2) if pfs else None,
            "per_window": per_window,
        })
        print(f"  {label(v)}  PF {pooled['pf']}  Payoff {pooled['payoff']} "
              f"(noetig {pooled['payoff_needed']})  Trefferq {pooled['win_rate']}%  "
              f"O Rendite {results[-1]['mean_ret']:+.1f}%", flush=True)

    # ---------- Report ----------
    print("\n" + "=" * 100)
    print("RANGLISTE nach Profit-Faktor (gepoolt ueber alle 7 OOS-Fenster)")
    print("=" * 100)
    hdr = (f"{'Exit-Variante':<22}{'PF':>7}{'Payoff':>8}{'noetig':>8}{'Treffer':>9}"
           f"{'O Gew':>8}{'O Verl':>8}{'O Rend':>9}{'Sharpe':>8}{'wstMDD':>8}{'Fenster PF>1':>14}")
    print(hdr); print("-" * len(hdr))
    for r in sorted(results, key=lambda x: (x["pooled"]["pf"] or 0), reverse=True):
        p = r["pooled"]
        print(f"{r['label']:<22}{p['pf'] or 0:>7.2f}{p['payoff'] or 0:>8.2f}"
              f"{p['payoff_needed'] or 0:>8.2f}{p['win_rate']:>8.1f}%"
              f"{p['avg_win']:>8.2f}{p['avg_loss']:>8.2f}{r['mean_ret']:>+8.1f}%"
              f"{r['mean_sharpe']:>8.2f}{r['worst_mdd']:>7.1f}%"
              f"{r['windows_pf_gt1']:>8d}/{len(WINDOWS)}")

    live = next((r for r in results
                 if r["variant"] == {"trail_act": 6.0, "trail": 4.0, "tp": 12.0}), None)
    best = max(results, key=lambda x: (x["pooled"]["pf"] or 0))
    print("\n" + "-" * 100)
    if live:
        lp, bp = live["pooled"], best["pooled"]
        print(f"LIVE-Config  {live['label']}: PF {lp['pf']}  Payoff {lp['payoff']}  "
              f"O Rendite {live['mean_ret']:+.1f}%")
        print(f"BESTE        {best['label']}: PF {bp['pf']}  Payoff {bp['payoff']}  "
              f"O Rendite {best['mean_ret']:+.1f}%")
        print(f"STABILITAET  beste Variante: PF>1 in {best['windows_pf_gt1']}/{len(WINDOWS)} "
              f"Fenstern, schlechtestes Fenster PF {best['min_window_pf']}")
        print("\nHINWEIS: WFO-Tier-Evidenz, KEIN Live-Beweis. Eine Variante ist nur dann ein "
              "ernsthafter Kandidat,\nwenn sie in FAST ALLEN Fenstern PF>1 haelt — sonst ist "
              "der Mittelwert ein Artefakt weniger Fenster.")

    out = {"generated_for": "exit_parameter_sweep (R-B12)",
           "fixed": {"sl": SL, "top_n": TOP_N, "deployment": DEPLOYMENT},
           "start": START, "end": END, "windows": len(WINDOWS), "results": results}
    with open("/app/data/wfo_trailing_sweep.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n-> data/wfo_trailing_sweep.json geschrieben.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
