"""R-B12 (20.07.2026) — WFO-Sweep des KOMPLETTEN Exit-Stacks.

ANLASS
------
Die Live-Round-Trips des Fundamental-Motors zeigen eine Payoff-Asymmetrie:
Trefferquote 66.7 %, aber O Gewinn +783 USD gegen O Verlust -4'077 USD
(Verhaeltnis 0.19) -> Profit-Faktor 0.38, netto -7'534 USD.
Break-even bei 67 % verlangt (1-p)/p = 0.5.

VERSION 2 — TRANCHEN NACHGERUESTET
----------------------------------
Der erste Sweep (v1) modellierte nur Trailing + TP und liess die LIVE aktiven
tp_tranches (30 % bei +8, 30 % bei +16, 40 % bei +30) aussen vor. Damit war die
Live-Config zu MILD abgebildet — sie landete trotzdem schon auf dem letzten
Platz von 25. Live feuerten seit Soak-Start 10 von 10 Teilverkaeufen bei +8 %,
KEIN einziger je bei +16 %: die erste Tranche ist real der wirksamste
Gewinner-Deckel. Ohne sie waere jede Empfehlung ein Halb-Fix.

Der Exit-Stack hat damit DREI Ebenen, die denselben Gewinner zerlegen:
  +6 %  Trailing scharf | +8 %  30 % der Position raus | -4 % Ruecksetzer: Rest raus

METHODIK (bewusst konservativ)
------------------------------
- Dieselben 7 OOS-Jahres-Fenster. Der Motor wird NICHT gefittet -> echtes OOS.
- Kein globales Optimum picken: gepoolte Kennzahlen UND Wert je Fenster.
  Auseinanderlaufende Fenster = instabil = nicht anfassen, egal wie gut der
  Mittelwert aussieht.
- Selektion nach Profit-Faktor + Payoff bei akzeptablem Drawdown, NICHT nach
  Rendite (Curve-Fitting-Falle).
- SL bleibt fix -8 (WFO-verteidigt) — zwei Dinge gleichzeitig zu aendern macht
  das Ergebnis uninterpretierbar.

WICHTIG: WFO-Tier-Evidenz, KEIN Live-Beweis (Hierarchie Live > WFO > Optimizer).
Read-only, aendert keine Config.

Aufruf: docker exec investpilot python scripts/wfo_trailing_sweep.py
Schreibt data/wfo_trailing_sweep.json + Konsolen-Report.
"""
import json
import statistics
import sys

TOP_N = 15
DEPLOYMENT = 0.70
SL = -8

# --- Exit-Dimensionen ---------------------------------------------------
TRANCHE_SETS = {
    "aktuell": [{"pct_of_position": 30, "profit_target_pct": 8},
                {"pct_of_position": 30, "profit_target_pct": 16},
                {"pct_of_position": 40, "profit_target_pct": 30}],
    "aus": None,
    "spaet": [{"pct_of_position": 30, "profit_target_pct": 16},
              {"pct_of_position": 30, "profit_target_pct": 30}],
    "eine@20": [{"pct_of_position": 30, "profit_target_pct": 20}],
}
TRAILS = [(6.0, 4.0), (6.0, 10.0), (8.0, 10.0), (10.0, 12.0), (None, None)]
TPS = [None, 12.0]

# Die echte LIVE-Config (Referenzpunkt): Trail 6/4 + TP 12 + Tranchen "aktuell"
LIVE = {"trail": (6.0, 4.0), "tp": 12.0, "tranches": "aktuell"}

START, END = "2019-01-01", "2026-07-01"
WINDOWS = [("2019-01-01", "2020-01-01"), ("2020-01-01", "2021-01-01"),
           ("2021-01-01", "2022-01-01"), ("2022-01-01", "2023-01-01"),
           ("2023-01-01", "2024-01-01"), ("2024-01-01", "2025-01-01"),
           ("2025-01-01", "2026-07-01")]


def payoff_metrics(trades: list) -> dict:
    """Die Kennzahlen, um die es geht — _metrics() des Backtesters hat sie nicht."""
    rets = [t["ret_net"] for t in trades]
    if not rets:
        return {"n": 0, "win_rate": 0, "avg_win": 0, "avg_loss": 0,
                "payoff": None, "payoff_needed": None, "pf": None, "expectancy_pct": 0}
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    avg_w = statistics.mean(wins) if wins else 0.0
    avg_l = statistics.mean(losses) if losses else 0.0
    wr = len(wins) / len(rets)
    return {
        "n": len(rets), "win_rate": round(wr * 100, 1),
        "avg_win": round(avg_w, 2), "avg_loss": round(avg_l, 2),
        "payoff": round(avg_w / abs(avg_l), 3) if avg_l else None,
        "payoff_needed": round((1 - wr) / wr, 3) if wr else None,
        "pf": round(gw / gl, 3) if gl else None,
        "expectancy_pct": round(statistics.mean(rets), 3),
    }


def label(trail, tp, tname):
    t = "Trail aus" if trail[0] is None else f"{trail[0]:g}/{trail[1]:g}"
    return f"{t:>9s} TP{'-' if tp is None else f'{tp:g}':<3s} Tr:{tname}"


def main():
    from app import signal_stack_backtester as bt
    from app import edgar_client, sp600_universe
    from app.config_manager import load_config

    cfg = load_config()
    disabled = set(cfg.get("disabled_symbols", []) or [])
    symbols = [s for s in sp600_universe.get_symbols() if s not in disabled]
    facts = edgar_client.load_facts()
    print(f"Universe {len(symbols)} | EDGAR {len(facts)}", flush=True)
    print("Lade Kurshistorie (dauert)...", flush=True)
    price_hist = bt.load_price_history(symbols, START, END)
    print("Praeberechne monatliche Picks (EINMAL)...", flush=True)
    picks = bt.precompute_monthly_picks(price_hist, facts, START, END)

    variants = [(tr, tp, tn) for tn in TRANCHE_SETS for tr in TRAILS for tp in TPS]
    print(f"\n=== EXIT-STACK-SWEEP (SL fix {SL}, top_n {TOP_N}, deploy {DEPLOYMENT}) ===")
    print(f"{len(variants)} Varianten x {len(WINDOWS)} OOS-Fenster\n", flush=True)

    results = []
    for trail, tp, tname in variants:
        act, tr = trail
        pooled_trades, per_window = [], []
        for w_start, w_end in WINDOWS:
            res = bt.run_backtest(price_hist, facts, symbols, w_start, w_end,
                                  top_n=TOP_N, deployment=DEPLOYMENT, sl_pct=SL,
                                  tp_pct=tp, trail_act_pct=act, trail_pct=tr,
                                  tranches=TRANCHE_SETS[tname], picks_by_month=picks)
            m = bt._metrics(res["monthly_pct"], res["equity_final"], res["trades"])
            pm = payoff_metrics(res["trades"])
            pooled_trades += res["trades"]
            per_window.append({"window": w_start[:4], "ret": m["total_return_pct"],
                               "sharpe": m["sharpe_ann"], "mdd": m["max_drawdown_pct"],
                               "pf": pm["pf"]})
        pooled = payoff_metrics(pooled_trades)
        pfs = [w["pf"] for w in per_window if w["pf"] is not None]
        is_live = (trail == LIVE["trail"] and tp == LIVE["tp"] and tname == LIVE["tranches"])
        results.append({
            "label": label(trail, tp, tname), "is_live": is_live,
            "variant": {"trail_act": act, "trail": tr, "tp": tp, "tranches": tname},
            "pooled": pooled,
            "mean_ret": round(statistics.mean(w["ret"] for w in per_window), 2),
            "mean_sharpe": round(statistics.mean(w["sharpe"] for w in per_window), 2),
            "worst_mdd": round(min(w["mdd"] for w in per_window), 2),
            "worst_year": round(min(w["ret"] for w in per_window), 2),
            "windows_pf_gt1": sum(1 for p in pfs if p > 1.0),
            "min_window_pf": round(min(pfs), 2) if pfs else None,
            "per_window": per_window,
        })
        print(f"  {results[-1]['label']}  PF {pooled['pf']}  Payoff {pooled['payoff']}  "
              f"O Rend {results[-1]['mean_ret']:+.1f}%"
              f"{'   <<< LIVE' if is_live else ''}", flush=True)

    # ---------- Report ----------
    print("\n" + "=" * 112)
    print("RANGLISTE nach Profit-Faktor (gepoolt ueber alle 7 OOS-Fenster)")
    print("=" * 112)
    hdr = (f"{'Exit-Stack':<26}{'PF':>6}{'Payoff':>8}{'noetig':>8}{'Treffer':>9}"
           f"{'O Gew':>8}{'O Verl':>8}{'O Rend':>9}{'Sharpe':>8}{'wstMDD':>8}"
           f"{'wstJahr':>9}{'PF>1':>7}")
    print(hdr); print("-" * len(hdr))
    for r in sorted(results, key=lambda x: (x["pooled"]["pf"] or 0), reverse=True):
        p = r["pooled"]
        mark = "  <<< LIVE" if r["is_live"] else ""
        print(f"{r['label']:<26}{p['pf'] or 0:>6.2f}{p['payoff'] or 0:>8.2f}"
              f"{p['payoff_needed'] or 0:>8.2f}{p['win_rate']:>8.1f}%"
              f"{p['avg_win']:>8.2f}{p['avg_loss']:>8.2f}{r['mean_ret']:>+8.1f}%"
              f"{r['mean_sharpe']:>8.2f}{r['worst_mdd']:>7.1f}%{r['worst_year']:>+8.1f}%"
              f"{r['windows_pf_gt1']:>4d}/{len(WINDOWS)}{mark}")

    live = next((r for r in results if r["is_live"]), None)
    best = max(results, key=lambda x: (x["pooled"]["pf"] or 0))
    print("\n" + "-" * 112)
    if live:
        print(f"LIVE  {live['label']}: PF {live['pooled']['pf']}  "
              f"Payoff {live['pooled']['payoff']}  O Rendite {live['mean_ret']:+.1f}%  "
              f"schlechtestes Jahr {live['worst_year']:+.1f}%")
    print(f"BEST  {best['label']}: PF {best['pooled']['pf']}  "
          f"Payoff {best['pooled']['payoff']}  O Rendite {best['mean_ret']:+.1f}%  "
          f"schlechtestes Jahr {best['worst_year']:+.1f}%")
    print(f"      Stabilitaet: PF>1 in {best['windows_pf_gt1']}/{len(WINDOWS)} Fenstern, "
          f"schlechtestes Fenster PF {best['min_window_pf']}")
    print("\nHINWEIS: WFO-Tier-Evidenz, KEIN Live-Beweis. Ein Kandidat zaehlt nur, wenn er in")
    print("FAST ALLEN Fenstern PF>1 haelt — sonst ist der Mittelwert ein Artefakt weniger Fenster.")

    out = {"generated_for": "exit_stack_sweep_v2_mit_tranchen (R-B12)",
           "fixed": {"sl": SL, "top_n": TOP_N, "deployment": DEPLOYMENT},
           "tranche_sets": TRANCHE_SETS, "start": START, "end": END,
           "windows": len(WINDOWS), "results": results}
    with open("/app/data/wfo_trailing_sweep.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n-> data/wfo_trailing_sweep.json geschrieben.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
