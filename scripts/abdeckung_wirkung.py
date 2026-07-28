"""Bringt mehr Signal-Abdeckung bessere Auswahl? (R-B48, 28.07.2026)

DIE FRAGE (Carlos)
==================
Nur ~40 % der Aktien haben alle fuenf Signale; bei den uebrigen wird jedes
fehlende Signal neutral auf 0.5 gesetzt. Laesst sich mit dieser Information
BESSER auswaehlen — z.B. indem man eine hoehere Mindest-Abdeckung verlangt?

Aktuell: MIN_SIGNAL_COVERAGE = 3 (weniger -> nicht kaufbar). Gemessen wird,
was strengere Schwellen historisch gebracht haetten.

WARUM DAS EINEN EIGENEN LAUF BRAUCHT
------------------------------------
Der PIT-Cache speichert nur [score, kurs] — die Abdeckung fehlt. Sie wird hier
je Monat neu berechnet (Point-in-Time, filed<=asof) und mitgeschrieben.

NEBENBEI KORRIGIERT: Die Rang-Band-Analyse (R-B47) nahm die Top-15 nach reinem
Score — OHNE den eligible-Filter, den der Bot anwendet. Die Zeile 'wie der Bot
kauft' unten ist die korrekte Referenz.

TRADING-NEUTRAL: reine Messung, schreibt nur data/abdeckung_wirkung.json.
"""
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

START, ENDE, TOP_N = "2017-01-01", "2026-07-01", 15


def main() -> int:
    from app import edgar_client, signal_stack, sp600_universe
    from app import signal_stack_backtester as bt
    from app.config_manager import load_config, save_json

    cfg = load_config()
    disabled = set(cfg.get("disabled_symbols") or [])
    symbols = [s for s in sp600_universe.get_symbols() if s not in disabled]
    facts = edgar_client.load_facts()
    print(f"Lade Kurse ({len(symbols)} Symbole)...", flush=True)
    ph = bt.load_price_history(symbols, START, ENDE)

    print("Berechne Scores + Abdeckung je Monat...", flush=True)
    snaps = {}
    monate = bt._month_starts(START, ENDE)
    for i, rb in enumerate(monate, 1):
        prices = bt._prices_asof(ph, rb)
        if not prices:
            continue
        sc = signal_stack.score_universe(list(prices.keys()), facts, prices, rb.isoformat())
        snap = {}
        for sym, d in sc.items():
            k = prices.get(sym)
            k = k[0] if isinstance(k, (tuple, list)) else k
            if d.get("score") is not None and k and k > 0:
                snap[sym] = (round(float(d["score"]), 2), round(float(k), 4),
                             int(d.get("coverage") or 0))
        if snap:
            snaps[rb.isoformat()] = snap
        if i % 24 == 0:
            print(f"  {i}/{len(monate)}", flush=True)

    tage = sorted(snaps)
    print(f"\n{len(tage)} Monate. Vergleiche Mindest-Abdeckungen:\n", flush=True)

    VARIANTEN = [("ohne Filter (R-B47)", 0), ("min. 3 = HEUTE (Bot)", 3),
                 ("min. 4", 4), ("nur volle 5", 5)]
    erg = {}
    for name, mincov in VARIANTEN:
        diffs, uebersprungen = [], 0
        for a, b in zip(tage, tage[1:]):
            sa, sb = snaps[a], snaps[b]
            paare = []
            for sym, (score, preis, cov) in sa.items():
                z = sb.get(sym)
                if not z or preis <= 0 or z[1] <= 0:
                    continue
                if cov < mincov:
                    continue
                paare.append((score, z[1] / preis - 1.0))
            alle_rets = [z[1] / sa[s][1] - 1.0 for s, z in sb.items()
                         if s in sa and sa[s][1] > 0 and z[1] > 0]
            if len(paare) < TOP_N or len(alle_rets) < 50:
                uebersprungen += 1
                continue
            paare.sort(key=lambda p: -p[0])
            top = [r for _, r in paare[:TOP_N]]
            diffs.append((sum(top) / len(top) - sum(alle_rets) / len(alle_rets)) * 100)
        if len(diffs) < 3:
            continue
        m, sd = statistics.mean(diffs), statistics.stdev(diffs)
        t = m / (sd / len(diffs) ** 0.5) if sd > 0 else None
        erg[name] = {"mehrrendite_pct": round(m, 3), "t_wert": round(t, 2) if t else None,
                     "n_perioden": len(diffs), "uebersprungen": uebersprungen,
                     "anteil_positiv_pct": round(sum(1 for d in diffs if d > 0) / len(diffs) * 100, 1)}
        print(f"  {name:<22} {m:+.3f}%/Mt | t={t:5.2f} | {len(diffs)} Perioden | "
              f"{erg[name]['anteil_positiv_pct']:.0f}% Monate positiv"
              + (f" | {uebersprungen} Monate zu duenn" if uebersprungen else ""))

    save_json("abdeckung_wirkung.json", {"start": START, "ende": ENDE,
                                         "top_n": TOP_N, "varianten": erg})
    print("\nGespeichert: data/abdeckung_wirkung.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
