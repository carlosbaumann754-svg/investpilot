"""Exit-Entscheidung — die drei offenen Pruefpunkte, in einem Durchlauf (R-B28).

WORUM ES GEHT
=============
Der Halten-Backtest zeigte drei Varianten mit sehr unterschiedlichem Ergebnis
(+125.6 % / +234.0 % / +327.7 %) bei identischer Auswahl. Bevor daraus eine
Empfehlung wird, verlangt die Checkliste drei Nachweise:

  Punkt 3  Fenster-Stabilitaet — haelt der Sieger ueber die Jahre, oder kommt
           sein Vorsprung aus zwei guten Jahren?
  Punkt 5  Drawdown aus dem VERLAUF, nicht aus Monatsrenditen. Im Halten-Modus
           entsteht die Monatsrendite erst beim Ausstieg; eine Position, die
           30 % einbricht und sich erholt, ist dort unsichtbar.
  Punkt 2  Haengt das Ergebnis an Mechanismen, die der Bot nicht hat?
           (OPEN_AT_END = offene Papierwerte am Testende, REBAL = Zwangsverkauf)

UND DIE FRAGE DAHINTER
----------------------
Carlos will heute entscheiden statt in sechs Wochen festzustellen, dass es besser
ginge. Berechtigt — aber dann muss beantwortet werden, ob so eine Entscheidung
ueberhaupt traegt. Deshalb Teil 4:

  **Walk-Forward.** Fuer jedes Jahr: die beste Config auf allen VORHERIGEN Jahren
  bestimmen und schauen, wo sie im Folgejahr landet. Landet sie regelmaessig im
  Mittelfeld, ist Optimieren auf dieser Historie wertlos — dann ist jede heutige
  "beste Einstellung" geraten, egal wie gruendlich gerechnet wurde. Landet sie
  vorne, traegt die Entscheidung.

  Das ist der einzige ehrliche Test dafuer, ob "heute die beste Einstellung
  finden" ueberhaupt moeglich ist.

MEHRFACHTESTEN
--------------
27 Konfigurationen auf EINEM Datensatz. Wer 27 Varianten testet, findet auch in
reinem Rauschen eine, die heraussticht. Deshalb ist der Sieger der Gesamtrechnung
NICHT das Entscheidungskriterium — sondern Stabilitaet plus Walk-Forward.

AUFRUF
------
    python scripts/exit_entscheidung.py
"""
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

START = "2017-01-01"
END = "2026-07-01"
TOP_N = 15
DEPLOYMENT = 0.90
SL = -8.0          # gesperrt (validiert 02.07., manual_lock_overrides)
TP = None          # aus (999-Sentinel), entschieden 21.07.

TRANCHEN = {
    "keine": None,
    "16/30": [{"pct_of_position": 30, "profit_target_pct": 16},
              {"pct_of_position": 30, "profit_target_pct": 30}],
    "8/12": [{"pct_of_position": 30, "profit_target_pct": 8},
             {"pct_of_position": 30, "profit_target_pct": 12}],
}
TRAIL_AKTIV = [6.0, 8.0, 10.0]
TRAIL_ABSTAND = [4.0, 8.0, 12.0]


def konfigurationen():
    for tname, tr in TRANCHEN.items():
        for akt in TRAIL_AKTIV:
            for abst in TRAIL_ABSTAND:
                yield (f"Tr {tname:5s} | Trail {akt:.0f}/{abst:.0f}",
                       {"sl_pct": SL, "tp_pct": TP, "trail_act_pct": akt,
                        "trail_pct": abst, "tranches": tr})


def mae_je_trade(bt, price_hist, trades):
    """Groesster zwischenzeitlicher Rueckschlag je Position (Max Adverse Excursion).

    Rekonstruiert exakt: Einstiegskurs am Monatsanfang, dann die tatsaechlich
    gehaltenen Tage abgehen und das Minimum nehmen. Genau die Bewegung, die in
    Monatsrenditen unsichtbar bleibt.
    """
    from datetime import date as _date
    maes = []
    for t in trades:
        sym = t.get("sym")
        tage = t.get("days") or 0
        if not sym or tage <= 0:
            continue
        y, m, d = (int(x) for x in t["month"].split("-"))
        entry_day = _date(y, m, d)
        prices = bt._prices_asof(price_hist, entry_day)
        p = prices.get(sym)
        entry = p[0] if isinstance(p, (tuple, list)) else p
        if not entry or entry <= 0:
            continue
        serie = [c for (dd, c) in price_hist.get(sym, []) if dd > entry_day][:tage]
        if not serie:
            continue
        maes.append((min(serie) / entry - 1.0) * 100)
    return maes


def jahres_renditen(eq_curve):
    """{jahr: rendite_pct} aus der Equity-Kurve."""
    nach_jahr = defaultdict(list)
    for d, e in eq_curve:
        nach_jahr[d.year].append((d, e))
    out = {}
    for jahr, punkte in sorted(nach_jahr.items()):
        punkte.sort()
        if len(punkte) >= 2:
            out[jahr] = (punkte[-1][1] / punkte[0][1] - 1) * 100
    return out


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

    konfigs = list(konfigurationen())
    print(f"{len(konfigs)} Konfigurationen. SL {SL} und TP aus sind gesperrt.\n", flush=True)

    ergebnisse = []
    for i, (name, ex) in enumerate(konfigs, 1):
        r = bt.run_backtest_hold(ph, facts, symbols, START, END, top_n=TOP_N,
                                 deployment=DEPLOYMENT, picks_by_month=picks, **ex)
        m = bt._metrics(r["monthly_pct"], r["equity_final"], r["trades"])
        trades = r["trades"]
        maes = mae_je_trade(bt, ph, trades)
        maes_sort = sorted(maes)
        jr = jahres_renditen(r["eq_curve"])

        gruende = defaultdict(int)
        for t in trades:
            gruende[t["reason"]] += 1
        papier = sum(n for g, n in gruende.items()
                     if "OPEN_AT_END" in g or "REBAL" in g)

        ergebnisse.append({
            "name": name, "config": ex,
            "rendite_pct": m["total_return_pct"],
            "sharpe": m["sharpe_ann"],
            "maxdd_monat_pct": m["max_drawdown_pct"],
            "mae_median_pct": round(statistics.median(maes), 2) if maes else None,
            "mae_p95_pct": round(maes_sort[int(len(maes_sort) * 0.05)], 2) if maes else None,
            "n_trades": len(trades),
            "haltedauer_median": statistics.median([t["days"] for t in trades
                                                    if t.get("days")]) if trades else None,
            "papier_anteil_pct": round(papier / max(1, len(trades)) * 100, 1),
            "jahre": jr,
            "schlechtestes_jahr_pct": round(min(jr.values()), 1) if jr else None,
            "jahre_positiv": sum(1 for v in jr.values() if v > 0),
            "jahre_gesamt": len(jr),
        })
        if i % 9 == 0:
            print(f"  {i}/{len(konfigs)}", flush=True)

    # ---------------------------------------------------------------- TABELLE 1
    print()
    print("=" * 118)
    print("PUNKT 3 + 5 + 2 — alle Konfigurationen (sortiert nach Sharpe)")
    print("=" * 118)
    print(f"{'Konfiguration':<26} | {'Rendite':>8} | {'Sharpe':>6} | "
          f"{'MaxDD':>6} | {'MAE med':>7} | {'MAE p95':>7} | {'schl.Jahr':>9} | "
          f"{'+Jahre':>6} | {'Papier':>6} | {'Halte':>5}")
    print("-" * 118)
    for e in sorted(ergebnisse, key=lambda x: -(x["sharpe"] or 0)):
        print(f"{e['name']:<26} | {e['rendite_pct']:>7.1f}% | {e['sharpe']:>6.2f} | "
              f"{e['maxdd_monat_pct']:>5.1f}% | {_f(e['mae_median_pct'],1):>6}% | "
              f"{_f(e['mae_p95_pct'],1):>6}% | {_f(e['schlechtestes_jahr_pct'],1):>8}% | "
              f"{e['jahre_positiv']:>2}/{e['jahre_gesamt']:<3} | "
              f"{e['papier_anteil_pct']:>5.1f}% | {_f(e['haltedauer_median'],0):>4}d")

    # ---------------------------------------------------------------- TABELLE 2
    print()
    print("=" * 118)
    print("PUNKT 3 im Detail — Jahresrenditen der fuenf besten (nach Sharpe)")
    print("=" * 118)
    beste = sorted(ergebnisse, key=lambda x: -(x["sharpe"] or 0))[:5]
    jahre = sorted({j for e in ergebnisse for j in e["jahre"]})
    print(f"{'Konfiguration':<26} | " + " | ".join(f"{j:>6}" for j in jahre))
    print("-" * 118)
    for e in beste:
        print(f"{e['name']:<26} | " +
              " | ".join(f"{e['jahre'].get(j, float('nan')):>5.1f}%" for j in jahre))

    # ---------------------------------------------------------------- TABELLE 3
    print()
    print("=" * 118)
    print("WALK-FORWARD — haette 'die beste Einstellung waehlen' in der Vergangenheit funktioniert?")
    print("=" * 118)
    print("  Vorgehen: beste Config nach Sharpe auf allen Jahren VOR Jahr X bestimmen,")
    print("  dann ihren Rang im Jahr X ablesen. Rang 1 = beste von "
          f"{len(konfigs)}, Rang {len(konfigs)} = schlechteste.")
    print()
    print(f"{'Jahr':>6} | {'gewaehlt (auf Vorjahren)':<26} | {'Rang im Jahr':>12} | "
          f"{'Perzentil':>9} | {'Rendite':>8} | {'bestmoeglich':>12}")
    print("-" * 100)

    wf_perzentile = []
    for ziel in jahre[2:]:  # mind. zwei Jahre Vorlauf
        vor = [j for j in jahre if j < ziel]
        gewichtet = []
        for e in ergebnisse:
            werte = [e["jahre"][j] for j in vor if j in e["jahre"]]
            if len(werte) < 2:
                continue
            mittel = statistics.mean(werte)
            sd = statistics.pstdev(werte)
            gewichtet.append((mittel / sd if sd > 0 else 0, e))
        if not gewichtet:
            continue
        gewichtet.sort(key=lambda x: -x[0])
        gewaehlt = gewichtet[0][1]

        rangliste = sorted(ergebnisse, key=lambda x: -(x["jahre"].get(ziel, -999)))
        rang = next(i for i, e in enumerate(rangliste, 1)
                    if e["name"] == gewaehlt["name"])
        perz = rang / len(rangliste) * 100
        wf_perzentile.append(perz)
        print(f"{ziel:>6} | {gewaehlt['name']:<26} | {rang:>6} / {len(rangliste):<3} | "
              f"{perz:>8.0f}% | {gewaehlt['jahre'].get(ziel, 0):>7.1f}% | "
              f"{rangliste[0]['jahre'].get(ziel, 0):>11.1f}%")

    print()
    if wf_perzentile:
        mittel_perz = statistics.mean(wf_perzentile)
        print(f"  Mittleres Perzentil der gewaehlten Config: {mittel_perz:.0f} %")
        print(f"  (50 % = Muenzwurf, das Optimieren bringt nichts. "
              f"Deutlich unter 50 % = es traegt.)")
        print()
        if mittel_perz <= 35:
            print("  ==> Optimieren TRAEGT. Eine heutige Entscheidung ist belastbar.")
        elif mittel_perz >= 45:
            print("  ==> Optimieren traegt NICHT. Die 'beste Einstellung' von heute ist")
            print("      auf dieser Historie nicht von Zufall zu unterscheiden.")
        else:
            print("  ==> Grenzfall. Schwacher Hinweis, kein Beleg.")

    save_json("exit_entscheidung.json", {
        "start": START, "ende": END, "n_konfigs": len(konfigs),
        "gesperrt": {"sl_pct": SL, "tp_pct": TP},
        "ergebnisse": ergebnisse,
        "walk_forward_perzentile": wf_perzentile,
        "walk_forward_mittel": round(statistics.mean(wf_perzentile), 1) if wf_perzentile else None,
    })
    print()
    print("Gespeichert: data/exit_entscheidung.json")
    return 0


def _f(v, n):
    return "n/a" if v is None else f"{v:.{n}f}"


if __name__ == "__main__":
    raise SystemExit(main())
