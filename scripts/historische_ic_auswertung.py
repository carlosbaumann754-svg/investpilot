"""Historische Ranking-Auswertung — beantwortet HEUTE, ob das Signal funktioniert.

DIE FRAGE
---------
Sagt ein hoher Signal-Stack-Score hoehere Folgerenditen vorher? Gemessen ueber
das gesamte Universum, nicht nur ueber die ~15 Positionen, die der Bot haelt.

WARUM DAS NICHT AUF DIE LIVE-HISTORIE WARTEN MUSS
-------------------------------------------------
Die Live-Archivierung (R-B25) beginnt heute bei null. Eine Leistungsmessung
braucht **unabhaengige Zeitperioden**, und bei Monatshorizont ist eine Periode
ein Monat — die Trefferquote-Simulation zeigt: fuer ein schwaches, aber echtes
Signal (IC 0.03) braeuchte es 45-60 Perioden, also vier bis fuenf Jahre.

Der Backtester rechnet aber bereits **monatliche Point-in-Time-Scores** fuer das
ganze Universum (``precompute_monthly_picks`` -> ``score_universe(..., asof)``,
mit ``filed <= asof`` im signal_stack erzwungen). Damit liegen dieselben Daten
rueckwirkend ueber Jahre vor — ohne Look-Ahead, weil nur Fakten einfliessen, die
zum jeweiligen Stichtag bereits eingereicht waren.

  Live-Archiv  = Bestaetigung out-of-sample, faengt Signal-Verfall auf
  Diese Analyse = die Antwort auf die Grundsatzfrage, sofort

ABGRENZUNG ZUM BACKTEST
-----------------------
Der Backtest misst den **Bot**: Selektion + Positionsgroesse + Ausstiege +
Kosten. Faellt er schlecht aus, weiss man nicht, welcher Teil schuld ist.
Diese Auswertung misst nur die **Selektion**. Sie kann zeigen, dass das Ranking
funktioniert, waehrend der Bot verliert (dann liegt es an der Ausfuehrung) —
oder dass das Ranking nichts taugt (dann ist alles andere Kosmetik).

AUFRUF
------
    python scripts/historische_ic_auswertung.py [start] [ende]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOP_N = 15  # so viele Namen haelt der Bot

START = "2017-01-01"
END = "2026-07-01"


def main() -> int:
    start = sys.argv[1] if len(sys.argv) > 1 else START
    ende = sys.argv[2] if len(sys.argv) > 2 else END

    from app import edgar_client, signal_stack, sp600_universe
    from app import signal_stack_backtester as bt
    from app.config_manager import load_config, save_json
    from app.signal_ic_tracker import bewertung, compute_ic

    cfg = load_config()
    disabled = set(cfg.get("disabled_symbols") or [])
    symbols = [s for s in sp600_universe.get_symbols() if s not in disabled]
    facts = edgar_client.load_facts()

    print(f"Zeitraum {start} .. {ende}, {len(symbols)} Symbole", flush=True)
    print("Lade Kurshistorie...", flush=True)
    price_hist = bt.load_price_history(symbols, start, ende)

    # Monatliche Schnappschuesse im Format der Live-Historie aufbauen:
    # {datum: {symbol: [score, kurs]}}. Damit laeuft exakt derselbe
    # Auswertungscode wie auf den Live-Daten — kein zweiter Rechenweg,
    # der stillschweigend anders rechnet.
    print("Berechne Point-in-Time-Scores je Monat...", flush=True)
    hist = {}
    monate = bt._month_starts(start, ende)
    for i, rb in enumerate(monate, 1):
        prices = bt._prices_asof(price_hist, rb)
        if not prices:
            continue
        scores = signal_stack.score_universe(list(prices.keys()), facts, prices,
                                             rb.isoformat())
        snap = {}
        for sym, eintrag in scores.items():
            kurs = prices.get(sym)
            kurs = kurs[0] if isinstance(kurs, (tuple, list)) else kurs
            score = eintrag.get("score") if isinstance(eintrag, dict) else eintrag
            if score is None or not kurs or kurs <= 0:
                continue
            snap[sym] = [round(float(score), 2), round(float(kurs), 4)]
        if snap:
            hist[rb.isoformat()] = snap
        if i % 12 == 0:
            print(f"  {i}/{len(monate)} Monate ({rb.isoformat()}), "
                  f"zuletzt {len(snap)} Symbole", flush=True)

    print(f"\n{len(hist)} monatliche Schnappschuesse aufgebaut.\n", flush=True)
    if len(hist) < 7:
        print("Zu wenig Schnappschuesse fuer eine Auswertung.")
        return 1

    # horizon zaehlt in SCHNAPPSCHUESSEN. Die Historie ist hier monatlich,
    # ein Schritt entspricht also einem Monat (nicht einem Handelstag wie im
    # Live-Archiv).
    ergebnisse = {}
    for h, name in ((1, "1 Monat"), (3, "3 Monate"), (6, "6 Monate")):
        erg = compute_ic(hist, horizon=h)
        erg["bewertung"] = bewertung(erg)
        ergebnisse[name] = erg

        b = erg["bewertung"]
        print("=" * 68)
        print(f"HORIZONT {name}")
        print("=" * 68)
        print(f"  Perioden (nicht ueberlappend) : {erg['n_perioden']}")
        print(f"  Mittlerer Rang-IC             : {_f(erg['mittlerer_ic'], 4)}")
        print(f"  Streuung                      : {_f(erg['ic_streuung'], 4)}")
        print(f"  t-Wert                        : {_f(erg['t_wert'], 2)}")
        print(f"  Perioden mit positivem IC     : {_f(erg['anteil_positiv_pct'], 1)} %")
        print(f"  Quintils-Spanne (Mittel)      : {_f(erg['mittlere_quintils_spanne_pct'], 2)} %")
        print()
        print(f"  BEFUND [{b['status']}]: {b['text']}")
        print()

    # ---------------------------------------------------------------------
    # SPITZEN-ANALYSE
    # ---------------------------------------------------------------------
    # Der Rang-IC bewertet die GESAMTE Rangfolge — auch die Frage, ob Platz 200
    # korrekt vor Platz 250 liegt. Das interessiert den Bot ueberhaupt nicht: er
    # kauft die besten 15 von ~300. Ein Vorteil koennte ausschliesslich an der
    # Spitze sitzen und im Gesamt-IC untergehen.
    #
    # Deshalb hier direkt die Frage, die zaehlt: Haben die Top-15 danach besser
    # abgeschnitten als der Durchschnitt des Universums?
    print("=" * 68)
    print("SPITZEN-ANALYSE — kauft der Bot die richtigen Namen?")
    print("=" * 68)
    top_erg = {}
    for h, name in ((1, "1 Monat"), (3, "3 Monate")):
        diffs = []
        tage = sorted(hist)
        for i in range(0, len(tage) - h, h):  # nicht ueberlappend
            s_snap, e_snap = hist[tage[i]], hist[tage[i + h]]
            paare = []
            for sym, (score, preis) in s_snap.items():
                sp = e_snap.get(sym)
                if not sp or preis <= 0 or sp[1] <= 0:
                    continue
                paare.append((score, sp[1] / preis - 1.0))
            if len(paare) < 50:
                continue
            paare.sort(key=lambda p: p[0], reverse=True)
            top = [r for _, r in paare[:TOP_N]]
            alle = [r for _, r in paare]
            diffs.append((sum(top) / len(top) - sum(alle) / len(alle)) * 100)

        if len(diffs) < 2:
            continue
        mittel = sum(diffs) / len(diffs)
        var = sum((d - mittel) ** 2 for d in diffs) / (len(diffs) - 1)
        streuung = var ** 0.5
        t = mittel / (streuung / len(diffs) ** 0.5) if streuung > 0 else None
        from app.signal_ic_tracker import t_kritisch
        schwelle = t_kritisch(len(diffs) - 1)
        top_erg[name] = {"n_perioden": len(diffs), "mehrrendite_pct": round(mittel, 3),
                         "streuung": round(streuung, 3),
                         "t_wert": round(t, 2) if t else None,
                         "schwelle": round(schwelle, 2),
                         "anteil_positiv_pct": round(
                             sum(1 for d in diffs if d > 0) / len(diffs) * 100, 1)}
        print(f"\n  Horizont {name} ({len(diffs)} Perioden)")
        print(f"    Mehrrendite Top-{TOP_N} vs. Universum : {mittel:+.3f} % pro Periode")
        print(f"    Streuung                            :  {streuung:.3f} %")
        print(f"    t-Wert                              :  {_f(t, 2)}  "
              f"(noetig {schwelle:.2f})")
        print(f"    Perioden mit Mehrrendite            :  "
              f"{sum(1 for d in diffs if d > 0) / len(diffs) * 100:.1f} %")
        if t and abs(t) >= schwelle:
            print(f"    ==> {'VORTEIL' if t > 0 else 'NACHTEIL'} nachweisbar.")
        else:
            print("    ==> Kein nachweisbarer Unterschied zum Zufall.")
    print()

    save_json("signal_ic_historisch.json", {
        "start": start, "ende": ende,
        "n_snapshots": len(hist),
        "hinweis": ("Point-in-Time-Scores aus dem Backtester (filed<=asof). "
                    "Misst nur die Selektion, nicht Ausfuehrung/Kosten."),
        "horizonte": ergebnisse,
        "spitzen_analyse": top_erg,
    })
    print("Gespeichert: data/signal_ic_historisch.json")
    return 0


def _f(v, n):
    return "n/a" if v is None else f"{v:.{n}f}"


if __name__ == "__main__":
    raise SystemExit(main())
