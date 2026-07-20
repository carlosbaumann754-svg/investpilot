"""Cutover-Gatekeeper: realisierte Round-Trips des AKTIVEN Motors.

Warum es dieses Skript gibt
---------------------------
Die Frage "sind die abgeschlossenen Trades netto profitabel?" hat mehrere
plausible Abgrenzungen, die sich um >20k USD unterscheiden. Am 16.07. und
20.07.2026 wurden versehentlich zwei verschiedene gerechnet (-8.3k vs +14.8k)
— dieselben Daten, anderes Fenster.

DIE LEITMETRIK: Eine Position zaehlt nur, wenn der Bot sie im Fenster SOWOHL
eroeffnet ALS AUCH geschlossen hat. Nur dann ist das Ergebnis eine Entscheidung
des aktiven Motors. Geerbte Positionen tragen Gewinne aus alten Parametern.

Die Rechenlogik liegt in app/roundtrip_metrics.py — DAS IST ABSICHT: derselbe
Code speist auch das Motor-Edge-Zweitsignal des WFO-Drift-Watchdogs. Zwei
getrennte Implementierungen waren die Ursache des Bugs, den das adressiert.

Aufruf:  docker exec -i investpilot python - < scripts/gatekeeper_roundtrips.py
"""
import json
import os

from app.roundtrip_metrics import clean_roundtrip_stats, DEFAULT_REGIME_START


def main():
    with open(os.path.join("data", "trade_history.json")) as f:
        trades = json.load(f)

    s = clean_roundtrip_stats(trades, since_iso=DEFAULT_REGIME_START)
    inh, allw = s["inherited"], s["all_in_window"]

    print("=" * 72)
    print(f"CUTOVER-GATEKEEPER  (Fenster ab {s['since']})")
    print("=" * 72)
    print("\n>>> LEITMETRIK — Round-Trips VOLLSTAENDIG im Fenster (aktiver Motor):")
    print(f"    n              : {s['n']}")
    print(f"    NETTO          : {s['net_usd']:+,.0f} USD")
    if s["n"]:
        print(f"    Trefferquote   : {s['win_rate']:.1f}%")
        print(f"    O Gewinn       : {s['avg_win']:+,.0f}" if s["avg_win"] else "")
        print(f"    O Verlust      : {s['avg_loss']:+,.0f}" if s["avg_loss"] else "")
        pf = s["pf"]
        print(f"    Profit-Faktor  : {pf if pf is not None else 'inf (verlustfrei)'}"
              f"   (Ziel > 1.0; WFO-Baseline 1.71)")
    print(f"\n    GATE (netto > 0): {'ERFUELLT' if s['net_usd'] > 0 else 'NICHT ERFUELLT'}")

    print("\n--- Kontrollrechnungen (NICHT das Gate) ---")
    print(f"    alle Closes im Fenster : {allw['net_usd']:+,.0f} USD (n={allw['n']})")
    print(f"    geerbte Positionen     : {inh['net_usd']:+,.0f} USD (n={inh['n']})")

    print("\n--- Detail Leitmetrik ---")
    for e in s["episodes"]:
        print(f"    {e['entry_ts'][:10]} -> {e['exit_ts'][:10]}  "
              f"{e['symbol']:<8s} {e['pnl_usd']:>+11,.0f}")

    print("\n--- Offene Positionen (unrealisiert, zaehlen NICHT ins Gate) ---")
    try:
        with open(os.path.join("data", "brain_state.json")) as f:
            snaps = json.load(f).get("performance_snapshots") or []
        pos = snaps[-1].get("positions") or []
        print(f"    {len(pos)} live: {', '.join(sorted(str(p.get('symbol')) for p in pos))}")
    except Exception as e:
        print(f"    brain_state nicht lesbar ({e})")


if __name__ == "__main__":
    main()
