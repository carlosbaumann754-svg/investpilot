"""Kelly-Sweep auf dem NEUEN Motor — Positionsgroesse empirisch statt geerbt (R-B39).

DIE FRAGE (Carlos, 22.07.2026)
==============================
Der 4%-Kelly-Deckel stammt aus dem April-Sweep ueber 1'325 Trades des ALTEN
TA-Motors. Laesst er sich fuer den neuen Fundamental-Motor frueher bestimmen
als ueber Monate von Live-Round-Trips? JA — mit demselben Trick wie beim
Edge-Nachweis (R-B25): rueckwirkend ueber den Point-in-Time-Backtester,
1'051 Neu-Motor-Trades aus 9.5 Jahren, mit den finalen Live-Exits.

WIE GERECHNET WIRD
------------------
run_backtest_hold setzt je Position ein fixes Gewicht w = deployment/top_n des
jeweiligen Depotstands ein. Ein Sweep ueber deployment ist damit exakt ein Sweep
ueber die Positionsgroesse k:

    deployment 0.30 -> k = 2.0 %   |   0.75 -> 5.0 %
    deployment 0.45 -> k = 3.0 %   |   0.90 -> 6.0 %
    deployment 0.60 -> k = 4.0 %   |   1.00 -> 6.67 %  (Vollinvestition)
                       ^= HEUTIGE LIVE-EINSTELLUNG

Bewertet wird wie im April-Sweep: Rendite UND maximaler Einbruch, mit dem
harten Gate MaxDD < 8 %. Dazu (Lehre aus R-B28): Jahres-Stabilitaet und ein
Walk-Forward — die beste Groesse auf den Vorjahren waehlen und schauen, ob die
Wahl im Folgejahr traegt. Ohne den Test waere auch dieses Optimum geraten.

WAS DAS BEWUSST NICHT IST
-------------------------
KEINE Config-Aenderung. Berechnen ist handels-neutral; Anwenden setzt die
Soak-Uhr zurueck und ist eine separate Carlos-Entscheidung.

EHRLICHE GRENZEN
----------------
- MaxDD aus MONATS-Renditen: unterschaetzt zwischenzeitliche Einbrueche
  (Checklisten-Punkt 5). Der Vergleich ZWISCHEN den k-Werten bleibt fair,
  weil der Fehler alle gleich trifft und mit k skaliert.
- Analytisches Kelly (aus Trefferquote/Payoff) steht nur als Referenz dabei:
  es ignoriert, dass 15 gleichgerichtete Small-Caps korreliert fallen —
  deshalb liegt das ehrliche empirische Optimum IMMER deutlich darunter.
- Backtest = Vorschlag, nicht Wahrheit (Validierungs-Hierarchie).

AUFRUF
------
    python scripts/kelly_neu_motor.py
"""
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

START = "2017-01-01"
END = "2026-07-01"
TOP_N = 15
MAXDD_GATE_PCT = -8.0     # wie im April-Sweep

# Live-Exits (final seit 21.07.): SL -8, TP aus, Trailing 6/4, keine Tranchen
EXITS = {"sl_pct": -8.0, "tp_pct": None,
         "trail_act_pct": 6.0, "trail_pct": 4.0, "tranches": None}

DEPLOYMENTS = [0.30, 0.45, 0.60, 0.75, 0.90, 1.00]


def jahres_renditen(monthly_pct, rebals):
    nach_jahr = defaultdict(float)
    for r, d in zip(monthly_pct, rebals):
        nach_jahr[d.year] = (1 + nach_jahr[d.year] / 100.0) * (1 + r / 100.0) * 100 - 100
    return dict(sorted(nach_jahr.items()))


def main() -> int:
    from app import edgar_client, sp600_universe
    from app import signal_stack_backtester as bt
    from app.config_manager import load_config, save_json

    cfg = load_config()
    disabled = set(cfg.get("disabled_symbols") or [])
    symbols = [s for s in sp600_universe.get_symbols() if s not in disabled]
    facts = edgar_client.load_facts()

    print("Lade Kurshistorie + Rankings (einmalig)...", flush=True)
    ph = bt.load_price_history(symbols, START, END)
    picks = bt.precompute_monthly_picks(ph, facts, START, END)
    rebals = bt._month_starts(START, END)

    # ---------------------------------------------------------------------
    # Referenz: analytisches Kelly aus den Trade-Statistiken (NUR Einordnung)
    # ---------------------------------------------------------------------
    basis = bt.run_backtest_hold(ph, facts, symbols, START, END, top_n=TOP_N,
                                 deployment=0.60, picks_by_month=picks, **EXITS)
    rets = [t["ret_net"] for t in basis["trades"]]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    p = len(wins) / len(rets)
    b = (statistics.mean(wins) / abs(statistics.mean(losses))) if losses else 0
    kelly_voll = p - (1 - p) / b if b > 0 else 0
    print()
    print(f"Analytisches Kelly (nur Referenz): p={p:.3f}, payoff={b:.3f} "
          f"-> voll {kelly_voll*100:.1f} %, halb {kelly_voll*50:.1f} % je Position")
    print("  (ignoriert Korrelation von 15 gleichgerichteten Small-Caps — "
          "das empirische Optimum liegt IMMER darunter)")

    # ---------------------------------------------------------------------
    # Empirischer Sweep
    # ---------------------------------------------------------------------
    print()
    print("=" * 100)
    print(f"EMPIRISCHER SWEEP — {len(DEPLOYMENTS)} Positionsgroessen, "
          f"Live-Exits, Gate MaxDD > {MAXDD_GATE_PCT} %")
    print("=" * 100)
    print(f"{'k je Pos.':>9} | {'deploy':>6} | {'Rendite':>9} | {'Sharpe':>6} | "
          f"{'MaxDD':>7} | {'schl.Jahr':>9} | {'+Jahre':>6} | {'Gate':>7}")
    print("-" * 100)

    ergebnisse = []
    for dep in DEPLOYMENTS:
        r = bt.run_backtest_hold(ph, facts, symbols, START, END, top_n=TOP_N,
                                 deployment=dep, picks_by_month=picks, **EXITS)
        m = bt._metrics(r["monthly_pct"], r["equity_final"], r["trades"])
        jr = jahres_renditen(r["monthly_pct"], rebals)
        k_pct = dep / TOP_N * 100
        gate_ok = m["max_drawdown_pct"] > MAXDD_GATE_PCT
        ergebnisse.append({
            "k_pct": round(k_pct, 2), "deployment": dep,
            "rendite_pct": m["total_return_pct"], "sharpe": m["sharpe_ann"],
            "maxdd_pct": m["max_drawdown_pct"],
            "jahre": jr,
            "schlechtestes_jahr_pct": round(min(jr.values()), 1),
            "jahre_positiv": sum(1 for v in jr.values() if v > 0),
            "gate_ok": gate_ok,
        })
        e = ergebnisse[-1]
        print(f"{e['k_pct']:>8.2f}% | {dep:>6.2f} | {e['rendite_pct']:>8.1f}% | "
              f"{e['sharpe']:>6.2f} | {e['maxdd_pct']:>6.1f}% | "
              f"{e['schlechtestes_jahr_pct']:>8.1f}% | "
              f"{e['jahre_positiv']:>2}/{len(e['jahre'])} | "
              f"{'OK' if gate_ok else 'VERLETZT':>7}")

    # ---------------------------------------------------------------------
    # Walk-Forward: traegt die Wahl? (Lehre aus R-B28)
    # ---------------------------------------------------------------------
    print()
    print("=" * 100)
    print("WALK-FORWARD — beste Groesse (Sharpe, Gate eingehalten) auf den "
          "Vorjahren waehlen, Rang im Folgejahr")
    print("=" * 100)
    jahre = sorted(ergebnisse[0]["jahre"])
    perzentile = []
    for ziel in jahre[2:]:
        vor = [j for j in jahre if j < ziel]
        kandidaten = []
        for e in ergebnisse:
            werte = [e["jahre"][j] for j in vor]
            mittel = statistics.mean(werte)
            sd = statistics.pstdev(werte)
            # Gate auf den Vorjahren: kein Jahr schlechter als das DD-Gate
            gate_vor = min(werte) > MAXDD_GATE_PCT
            kandidaten.append((mittel / sd if sd > 0 else 0, gate_vor, e))
        mit_gate = [x for x in kandidaten if x[1]] or kandidaten
        gewaehlt = max(mit_gate, key=lambda x: x[0])[2]
        rangliste = sorted(ergebnisse, key=lambda x: -x["jahre"].get(ziel, -999))
        rang = next(i for i, e in enumerate(rangliste, 1)
                    if e["k_pct"] == gewaehlt["k_pct"])
        perz = rang / len(rangliste) * 100
        perzentile.append(perz)
        print(f"  {ziel}: gewaehlt k={gewaehlt['k_pct']:.2f}% -> "
              f"Rang {rang}/{len(rangliste)} ({perz:.0f}%), "
              f"Jahr {gewaehlt['jahre'].get(ziel, 0):+.1f}%")
    if perzentile:
        print(f"\n  Mittleres Perzentil: {statistics.mean(perzentile):.0f} % "
              "(50 % = Muenzwurf)")

    save_json("kelly_neu_motor.json", {
        "start": START, "ende": END, "exits": EXITS,
        "analytisch_voll_pct": round(kelly_voll * 100, 2),
        "ergebnisse": ergebnisse,
        "walk_forward_mittel_pct": round(statistics.mean(perzentile), 1) if perzentile else None,
        "hinweis": ("Berechnung ist handels-neutral. MaxDD aus Monatsrenditen "
                    "(unterschaetzt zwischenzeitliche Einbrueche; Vergleich "
                    "zwischen k bleibt fair). Anwendung = separate Entscheidung, "
                    "setzt die Soak-Uhr zurueck."),
    })
    print("\nGespeichert: data/kelly_neu_motor.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
