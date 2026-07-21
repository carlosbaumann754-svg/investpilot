"""Haltedauer-Analyse — wie lange haelt der Vorsprung der ausgewaehlten Namen?

DIE FRAGE (R-B26, 21.07.2026)
==============================
R-B25 hat belegt: die Top-15 schlagen das Universum um +1.08 % im ERSTEN Monat
(t=3.46 ueber 112 Perioden). Auf drei Monate war nichts mehr messbar.

Damit steht die Bot-Identitaet zur Debatte. "Mittelfristig-fundamental mit
Halten" ist nur dann richtig, wenn der Vorsprung auch mittelfristig da ist. Ist
er nach einem Monat verbraucht, haelt der Bot Namen ohne Vorteil — und die
Rendite entsteht dann nicht durch die Auswahl, sondern bestenfalls durch die
Ausstiege.

WAS HIER GEMESSEN WIRD
----------------------
**Zerfallskurve**: Namen werden im Monat t ausgewaehlt und dann NICHT mehr
angefasst. Gemessen wird ihre Mehrrendite gegenueber dem Universum getrennt fuer
den 1., 2., 3., ... Monat danach.

  Monat 1 stark, Monat 2-6 bei null  -> der Vorteil ist einmalig, Halten bringt nichts
  ueber mehrere Monate positiv       -> Halten ist gerechtfertigt

Das ist etwas anderes als "kumulierte Rendite ueber 3 Monate": dort kann ein
starker erster Monat zwei tote Monate ueberdecken. Genau diese Vermischung hat
am 20.07. zur Fehlentscheidung beim Exit-Sweep gefuehrt.

**Umschlag**: Wie viele der 15 Namen wechseln von Monat zu Monat? Daraus die
Kostenschwelle — eine Rotation, deren Vorteil die Handelskosten nicht uebersteigt,
ist wertlos.

ABGRENZUNG — WAS DAS NICHT IST
------------------------------
Kein Backtest. Keine Ausstiege, keine Positionsgroessen, keine Hebel. Nur die
Frage, wie lange die AUSWAHL traegt. Bewusst so: der Backtest vermischt Auswahl
und Ausfuehrung, und genau diese Vermischung hat das Projekt monatelang blind
gemacht.

AUFRUF
------
    python scripts/haltedauer_analyse.py [start] [ende]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOP_N = 15
MAX_LAG = 6          # so viele Monate nach der Auswahl wird verfolgt
MIN_UNIVERSUM = 50   # Perioden mit weniger Namen sind nicht vergleichbar

START = "2017-01-01"
END = "2026-07-01"

_CACHE = "signal_pit_snapshots.json"


def lade_oder_baue_snapshots(start: str, ende: str) -> dict:
    """{monat: {symbol: [score, kurs]}} — Point-in-Time, mit Zwischenspeicher.

    Das Scoring ist der teure Teil (EDGAR je Monat ueber ~300 Symbole). Einmal
    rechnen, dann beliebig oft auswerten.
    """
    from app.config_manager import load_json, save_json

    cache = load_json(_CACHE)
    if isinstance(cache, dict) and cache.get("start") == start \
            and cache.get("ende") == ende and cache.get("snapshots"):
        print(f"Zwischenspeicher: {len(cache['snapshots'])} Schnappschuesse", flush=True)
        return cache["snapshots"]

    from app import edgar_client, signal_stack, sp600_universe
    from app import signal_stack_backtester as bt
    from app.config_manager import load_config

    cfg = load_config()
    disabled = set(cfg.get("disabled_symbols") or [])
    symbols = [s for s in sp600_universe.get_symbols() if s not in disabled]
    facts = edgar_client.load_facts()

    print(f"Baue Schnappschuesse neu ({len(symbols)} Symbole)...", flush=True)
    price_hist = bt.load_price_history(symbols, start, ende)

    snaps = {}
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
            snaps[rb.isoformat()] = snap
        if i % 24 == 0:
            print(f"  {i}/{len(monate)} Monate", flush=True)

    save_json(_CACHE, {"start": start, "ende": ende, "snapshots": snaps})
    print(f"Gespeichert: data/{_CACHE} ({len(snaps)} Schnappschuesse)", flush=True)
    return snaps


def _t_und_schwelle(werte: list):
    """(mittel, streuung, t, schwelle) — None-sicher bei zu wenig Werten."""
    from app.signal_ic_tracker import t_kritisch
    n = len(werte)
    if n < 2:
        return (werte[0] if werte else None), None, None, None
    mittel = sum(werte) / n
    var = sum((w - mittel) ** 2 for w in werte) / (n - 1)
    streuung = var ** 0.5
    t = mittel / (streuung / n ** 0.5) if streuung > 0 else None
    return mittel, streuung, t, t_kritisch(n - 1)


def main() -> int:
    start = sys.argv[1] if len(sys.argv) > 1 else START
    ende = sys.argv[2] if len(sys.argv) > 2 else END

    snaps = lade_oder_baue_snapshots(start, ende)
    monate = sorted(snaps)
    if len(monate) < MAX_LAG + 2:
        print("Zu wenig Schnappschuesse.")
        return 1

    print()
    print("=" * 72)
    print("ZERFALLSKURVE — Mehrrendite der einmal gewaehlten Top-15,")
    print("                getrennt nach Monat nach der Auswahl")
    print("=" * 72)
    print(f"{'Monat danach':>13} | {'Mehrrendite':>12} | {'t-Wert':>7} | "
          f"{'noetig':>6} | {'Perioden':>8} | Befund")
    print("-" * 72)

    zerfall = {}
    for lag in range(1, MAX_LAG + 1):
        diffs = []
        for i, m in enumerate(monate):
            # Auswahl in Monat i, gemessen wird das Fenster i+lag-1 -> i+lag
            a, b = i + lag - 1, i + lag
            if b >= len(monate):
                break
            auswahl_snap = snaps[m]
            von, bis = snaps[monate[a]], snaps[monate[b]]

            # Top-15 aus dem AUSWAHL-Monat (nicht neu ranken!)
            rang = sorted(auswahl_snap.items(), key=lambda kv: kv[1][0], reverse=True)
            top = [s for s, _ in rang[:TOP_N]]

            rets_top, rets_alle = [], []
            for sym, (_, _) in von.items():
                zv, zb = von.get(sym), bis.get(sym)
                if not zv or not zb or zv[1] <= 0 or zb[1] <= 0:
                    continue
                r = zb[1] / zv[1] - 1.0
                rets_alle.append(r)
                if sym in top:
                    rets_top.append(r)
            if len(rets_alle) < MIN_UNIVERSUM or len(rets_top) < 5:
                continue
            diffs.append((sum(rets_top) / len(rets_top)
                          - sum(rets_alle) / len(rets_alle)) * 100)

        mittel, streuung, t, schwelle = _t_und_schwelle(diffs)
        if mittel is None:
            continue
        signifikant = t is not None and schwelle is not None and abs(t) >= schwelle
        befund = ("VORTEIL" if signifikant and t > 0 else
                  "NACHTEIL" if signifikant else "nichts")
        zerfall[lag] = {"mehrrendite_pct": round(mittel, 3),
                        "t_wert": round(t, 2) if t else None,
                        "n_perioden": len(diffs), "signifikant": signifikant}
        print(f"{lag:>13} | {mittel:>+11.3f}% | {_f(t, 2):>7} | "
              f"{_f(schwelle, 2):>6} | {len(diffs):>8} | {befund}")

    # ------------------------------------------------------------------
    # UMSCHLAG + KOSTENSCHWELLE
    # ------------------------------------------------------------------
    print()
    print("=" * 72)
    print("UMSCHLAG — wie viele der 15 Namen wechseln pro Monat?")
    print("=" * 72)
    wechsel = []
    for a, b in zip(monate, monate[1:]):
        ta = {s for s, _ in sorted(snaps[a].items(), key=lambda kv: kv[1][0],
                                   reverse=True)[:TOP_N]}
        tb = {s for s, _ in sorted(snaps[b].items(), key=lambda kv: kv[1][0],
                                   reverse=True)[:TOP_N]}
        if len(ta) == TOP_N and len(tb) == TOP_N:
            wechsel.append(len(tb - ta))

    if wechsel:
        mittel_w = sum(wechsel) / len(wechsel)
        print(f"  Neue Namen pro Monat : {mittel_w:.1f} von {TOP_N} "
              f"({mittel_w / TOP_N * 100:.0f} % Umschlag)")
        print(f"  Bleiben              : {TOP_N - mittel_w:.1f}")
        print()
        print("  Kosten der Rotation je nach Handelskosten pro Seite:")
        print(f"  {'Kosten/Seite':>13} | {'Kosten/Monat':>13} | Vorteil Monat 1 bleibt")
        print("  " + "-" * 58)
        vorteil = zerfall.get(1, {}).get("mehrrendite_pct") or 0.0
        for einweg in (0.05, 0.10, 0.25, 0.50):
            # Ein Wechsel = verkaufen + kaufen = 2 Seiten, betrifft 1/TOP_N des Depots
            kosten = mittel_w / TOP_N * 2 * einweg
            print(f"  {einweg:>12.2f}% | {kosten:>12.3f}% | "
                  f"{vorteil - kosten:>+.3f}%")

    print()
    print("=" * 72)
    print("LESART")
    print("=" * 72)
    print("  Nur Monat 1 signifikant -> der Vorteil ist EINMALIG. Halten ueber")
    print("  mehrere Monate traegt dann nichts bei; die Rendite muesste aus den")
    print("  Ausstiegen kommen, nicht aus der Auswahl.")
    print()
    print("  Mehrere Monate positiv  -> Halten ist gerechtfertigt, Rotation waere")
    print("  nur zusaetzliche Kosten.")
    print()

    from app.config_manager import save_json
    save_json("haltedauer_analyse.json", {
        "start": start, "ende": ende,
        "top_n": TOP_N,
        "zerfall": zerfall,
        "umschlag_pro_monat": round(sum(wechsel) / len(wechsel), 2) if wechsel else None,
    })
    print("Gespeichert: data/haltedauer_analyse.json")
    return 0


def _f(v, n):
    return "n/a" if v is None else f"{v:.{n}f}"


if __name__ == "__main__":
    raise SystemExit(main())
