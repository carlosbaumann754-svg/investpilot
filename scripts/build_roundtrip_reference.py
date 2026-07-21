"""R-B18 (21.07.2026) — Referenzverteilung fuer das Motor-Edge-Signal.

WARUM
-----
Das Signal alarmierte urspruenglich bei "Profit-Faktor mehr als 30 % unter der
WFO-Baseline", ab einem geratenen Mindest-Sample von 12 Round-Trips. Am
21.07.2026 gemessen: Bei n=12 loest diese Regel bei einem KERNGESUNDEN System in
41.5 % der Faelle Fehlalarm aus. Ursache ist nicht der Schwellwert, sondern die
Kennzahl: der Profit-Faktor ist ein Quotient aus Summen, extrem schief verteilt
und konvergiert sehr langsam. Selbst bei n=80 liegt sein 10 %-Quantil (1.06)
noch unter der alten Alarmgrenze (1.20).

DIE LOESUNG
-----------
Nicht eine feste Schwelle plus Mindest-n, sondern die EMPIRISCHE VERTEILUNG als
Massstab: Alarm, wenn der Live-PF unter das 5 %-Quantil dessen faellt, was ein
gesundes System bei GENAU DIESEM n produziert. Damit ist die Fehlalarm-Rate per
Konstruktion 5 % — bei jedem n, ohne Raterei. Das Signal darf frueh sprechen,
nur eben mit weiter Toleranz.

Gerechnet wird im HOLD-Modus (run_backtest_hold) — dem Modus, der das echte
Bot-Verhalten abbildet — und in PROZENT-Renditen, weil der Live-PF fuer den
Vergleich ebenfalls auf Prozentbasis gerechnet wird (roundtrip_metrics.pf_pct).

WICHTIG: Nach JEDER Aenderung an Motor oder Exits neu erzeugen. Die Datei traegt
die Config, gegen die sie gebaut wurde; der Watchdog warnt bei Abweichung.

Aufruf: docker exec investpilot python scripts/build_roundtrip_reference.py
Schreibt data/roundtrip_pf_reference.json
"""
import json
import sys
from datetime import datetime, timezone

N_GRID = [5, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100]
QUANTILE = {"p01": 0.01, "p05": 0.05, "p10": 0.10, "p25": 0.25,
            "p50": 0.50, "p75": 0.75}
START, END = "2019-01-01", "2026-07-01"
TOP_N, DEPLOYMENT = 15, 0.70


def _pf(vals):
    gw = sum(v for v in vals if v > 0)
    gl = abs(sum(v for v in vals if v <= 0))
    return (gw / gl) if gl > 0 else None


def main():
    from app import signal_stack_backtester as bt
    from app import edgar_client, sp600_universe
    from app.config_manager import load_config

    cfg = load_config()
    lev = cfg.get("leverage", {}) or {}
    dt = cfg.get("demo_trading", {}) or {}
    sl = dt.get("stop_loss_pct", -8)
    tp = dt.get("take_profit_pct")
    # Sentinel-TP (999) bedeutet "aus" — im Backtest als None fuehren
    tp = None if (tp is None or tp >= 100) else float(tp)
    tranches = lev.get("tp_tranches") or None
    exit_cfg = {"sl_pct": sl, "tp_pct": tp,
                "trail_act_pct": lev.get("trailing_sl_activation_pct"),
                "trail_pct": lev.get("trailing_sl_pct"),
                "tranches": tranches}
    print(f"Live-Exit-Config: {json.dumps(exit_cfg, default=str)}", flush=True)

    disabled = set(cfg.get("disabled_symbols") or [])
    symbols = [s for s in sp600_universe.get_symbols() if s not in disabled]
    facts = edgar_client.load_facts()
    print("Lade Kurshistorie...", flush=True)
    price_hist = bt.load_price_history(symbols, START, END)
    print("Praeberechne Picks...", flush=True)
    picks = bt.precompute_monthly_picks(price_hist, facts, START, END)

    res = bt.run_backtest_hold(price_hist, facts, symbols, START, END,
                               top_n=TOP_N, deployment=DEPLOYMENT,
                               picks_by_month=picks, **exit_cfg)
    trades = sorted(res["trades"], key=lambda t: t["exit"])
    rets = [t["ret_net"] for t in trades]
    gesamt = _pf(rets)
    print(f"Referenzlauf: n={len(rets)}  PF={gesamt:.3f}\n", flush=True)

    by_n = {}
    print(f"{'n':>5}{'Fenster':>10}" + "".join(f"{q:>9}" for q in QUANTILE))
    print("-" * (15 + 9 * len(QUANTILE)))
    for n in N_GRID:
        if n >= len(rets):
            break
        # Aufeinanderfolgende Fenster: erhaelt die Regime-Klumpung. Zufaellige
        # Ziehungen wuerden die Streuung unterschaetzen, weil sie gute und
        # schlechte Phasen mischen.
        fenster = [_pf(rets[i:i + n]) for i in range(len(rets) - n + 1)]
        fenster = sorted(f for f in fenster if f is not None)
        if len(fenster) < 20:
            continue
        entry = {q: round(fenster[min(int(len(fenster) * v), len(fenster) - 1)], 4)
                 for q, v in QUANTILE.items()}
        entry["n_windows"] = len(fenster)
        by_n[str(n)] = entry
        print(f"{n:>5}{len(fenster):>10}" + "".join(f"{entry[q]:>9.2f}" for q in QUANTILE))

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_for": "motor_edge_signal (R-B18)",
        "basis": "prozent-renditen, hold-modus, aufeinanderfolgende fenster",
        "backtest": {"start": START, "end": END, "top_n": TOP_N,
                     "deployment": DEPLOYMENT, "n_trades": len(rets),
                     "pf_gesamt": round(gesamt, 4) if gesamt else None},
        "exit_config": exit_cfg,
        "by_n": by_n,
        "hinweis": ("Nach jeder Aenderung an Motor oder Exits neu erzeugen. "
                    "Der Watchdog vergleicht exit_config mit der Live-Config "
                    "und warnt bei Abweichung."),
    }
    with open("/app/data/roundtrip_pf_reference.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n-> data/roundtrip_pf_reference.json geschrieben.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
