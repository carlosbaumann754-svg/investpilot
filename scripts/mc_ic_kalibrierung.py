"""Kalibrierung der Ranking-Auswertung gegen reinen Zufall (R-B25).

ZWECK
-----
Prueft, wie oft die Auswertung ein Signal meldet, wo garantiert keines ist.
Score und Folgerendite werden unabhaengig voneinander gewuerfelt — jede Meldung
"Signal gefunden" ist damit per Konstruktion ein Fehlalarm.

Erwartung: ~5 %, weil die Schwelle auf 5 % Irrtumswahrscheinlichkeit ausgelegt ist.

WARUM ES DIESES SKRIPT GIBT
---------------------------
Der erste Lauf am 21.07.2026 ergab **7.30 %** statt 5 %. Ursache war die
Faustregel "|t| > 2", die erst bei grossen Stichproben gilt; bei 12 Perioden
liegt die korrekte Schwelle bei 2.201. Der Fehler war in den Unit-Tests
unsichtbar — die pruefen, ob die Rechnung tut was ich erwartet habe, nicht ob
meine Erwartung stimmte.

Dieselbe Fehlerklasse hatte am 20.07. das Motor-Edge-Signal mit 41.5 %
Fehlalarmen unbrauchbar gemacht (dort war die Mindestmenge geraten).

  ==> Jeder Schwellwert in diesem Projekt gehoert so gegengeprueft, BEVOR er
      live geht. Das Skript kostet zwei Minuten.

AUFRUF
------
    python scripts/mc_ic_kalibrierung.py [laeufe] [perioden] [symbole]
"""
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.signal_ic_tracker import bewertung, compute_ic  # noqa: E402


def lauf(n_perioden: int, n_symbole: int, rng: random.Random) -> dict:
    """Eine Historie aus reinem Zufall -> Auswertung."""
    hist = {}
    for d in range(n_perioden + 1):
        # Datum nur als Sortierschluessel; Abstand egal, weil horizon=1 in
        # Schnappschuessen zaehlt, nicht in Kalendertagen.
        tag = f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}"
        hist[tag] = {f"S{i}": [rng.uniform(0, 100), rng.uniform(50, 150)]
                     for i in range(n_symbole)}
    erg = compute_ic(hist, horizon=1)
    return {"ic": erg["mittlerer_ic"], "status": bewertung(erg)["status"]}


def main() -> int:
    laeufe = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    perioden = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    symbole = int(sys.argv[3]) if len(sys.argv) > 3 else 309

    rng = random.Random(42)  # fest, damit das Ergebnis reproduzierbar bleibt
    ics, gruen, rot = [], 0, 0

    for _ in range(laeufe):
        r = lauf(perioden, symbole, rng)
        ics.append(r["ic"])
        gruen += r["status"] == "signal"
        rot += r["status"] == "invers"

    fehlalarm = (gruen + rot) / laeufe * 100

    print(f"{laeufe} Laeufe, je {perioden} Perioden x {symbole} Symbole — "
          "Score und Rendite unabhaengig gewuerfelt")
    print()
    print(f"  Mittlerer IC              : {statistics.mean(ics):+.5f}   (Soll ~0)")
    print(f"  Streuung der IC-Mittel    :  {statistics.stdev(ics):.5f}")
    print()
    print(f"  Faelschlich GRUEN         : {gruen / laeufe * 100:5.2f} %")
    print(f"  Faelschlich ROT           : {rot / laeufe * 100:5.2f} %")
    print(f"  Fehlalarm gesamt          : {fehlalarm:5.2f} %   (Soll ~5 %)")
    print()

    # Toleranz: bei 2000 Laeufen hat eine 5-%-Quote eine Streuung von ~0.5
    # Prozentpunkten. 3 bis 7 % ist damit unauffaellig, alles darueber nicht.
    if 3.0 <= fehlalarm <= 7.0:
        print("  ==> Kalibrierung in Ordnung.")
        return 0
    print("  ==> ABWEICHUNG. Schwelle pruefen, bevor auf die Auswertung "
          "vertraut wird.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
