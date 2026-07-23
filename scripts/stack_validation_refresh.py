"""Stack-Validierungs-Karte auf die Bot-Realitaet umstellen (R-B41, 23.07.2026).

WAS HIER PASSIERT
=================
Die Dashboard-Karte 'Stack-Validierung (Robustheit)' las bisher eine Datei ohne
Datums- und Methoden-Stempel, gerechnet auf den TOP-20% (~60 Aktien). Der Bot
kauft aber die TOP-15 — und seit R-B25 wissen wir, dass der Vorsprung an der
SPITZE konzentriert ist; die Top-20%-Sicht verduennt ihn systematisch.

Dieses Skript regeneriert die Datei (data/stack_wfo_baseline.json) im GLEICHEN
Schema, aber:
  - Methode: Top-15 vs. Universum (exakt was der Bot tut)
  - Quelle: der Point-in-Time-Schnappschuss-Cache (signal_pit_snapshots.json,
    monatlich 2017-2026, filed<=asof — kein Look-Ahead)
  - NEU gestempelt: generated_at, method, recent-Block (2024+)

DER BEFUND, DER DAS AUSGELOEST HAT (Carlos' Frage 'ist das noch aktuell?')
--------------------------------------------------------------------------
Die Recency-Warnung der alten Karte bestaetigt sich auch in der scharfen
Top-15-Methode:
    2017-2023: +1.46 %/Monat Excess (t=3.77, 82 Monate)
    2024-2026: +0.03 %/Monat        (t=0.07, 30 Monate)
Der Unterschied selbst ist mit t~2.4 kein Rauschen. Moegliche Lesarten:
(a) der Edge ist seit ~2024 wegarbitriert, (b) Schwaechephase des Small-Cap-
Value/Quality-Stils, (c) Survivorship wirkt ZEITLICH ASYMMETRISCH — die
Historie besteht aus HEUTIGEN Index-Mitgliedern, fruehe Jahre enthalten also
ueberproportional spaetere Aufsteiger und sehen dadurch besser aus; die
juengsten Jahre sind die ehrlichsten. In JEDER Lesart gilt: Die Live-
Validierung im Soak ist der Ernstfall, nicht die Formalitaet — und das
Score-Archiv (survivorship-frei, ab 21.07. taeglich) ist die Datenquelle,
die diese Frage endgueltig beantwortet.

TRADING-NEUTRAL: schreibt nur die Display-Datei (einziger Leser:
web/app.py::api_stack_validation). Vorher wird ein .bak angelegt.
"""
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOP_N = 15
RECENT_AB = "2024"
MIN_SYMBOLE = 50


def _spearman(xs, ys):
    from app.signal_ic_tracker import spearman
    return spearman(xs, ys)


def _stats(monate):
    """Kennzahlen-Block im Karten-Schema aus [(excess_frac, ic), ...]."""
    if not monate:
        return {"n": 0, "excess_mo": None, "hit": None, "pf": None,
                "sharpe": None, "ic": None, "ic_pos": None}
    ex = [m[0] for m in monate]
    ics = [m[1] for m in monate if m[1] is not None]
    gw = sum(x for x in ex if x > 0)
    gl = abs(sum(x for x in ex if x <= 0))
    mean = statistics.mean(ex)
    sd = statistics.stdev(ex) if len(ex) > 1 else 0
    return {
        "n": len(ex),
        "excess_mo": mean,
        "hit": sum(1 for x in ex if x > 0) / len(ex),
        "pf": (gw / gl) if gl > 0 else None,
        "sharpe": (mean / sd * 12 ** 0.5) if sd > 0 else None,
        "ic": statistics.mean(ics) if ics else None,
        "ic_pos": (sum(1 for i in ics if i > 0) / len(ics)) if ics else None,
    }


def main() -> int:
    from app.config_manager import load_json, save_json, get_data_path

    cache = load_json("signal_pit_snapshots.json") or {}
    snaps = cache.get("snapshots") or {}
    if len(snaps) < 24:
        print("PIT-Cache fehlt/zu klein — erst scripts/haltedauer_analyse.py laufen lassen.")
        return 1

    tage = sorted(snaps)
    je_monat = []          # (jahr, excess_frac, ic)
    for a, b in zip(tage, tage[1:]):
        sa, sb = snaps[a], snaps[b]
        scores, rets = [], []
        for sym, (score, preis) in sa.items():
            z = sb.get(sym)
            if z and preis and preis > 0 and z[1] and z[1] > 0:
                scores.append(score)
                rets.append(z[1] / preis - 1.0)
        if len(rets) < MIN_SYMBOLE:
            continue
        paare = sorted(zip(scores, rets), key=lambda p: -p[0])
        top = [r for _, r in paare[:TOP_N]]
        excess = sum(top) / len(top) - sum(rets) / len(rets)
        je_monat.append((a[:4], excess, _spearman(scores, rets)))

    per_year_raw = defaultdict(list)
    for jahr, ex, ic in je_monat:
        per_year_raw[jahr].append((ex, ic))

    years = sorted(per_year_raw)
    per_year = {j: _stats(per_year_raw[j]) for j in years}
    alle = [(ex, ic) for _, ex, ic in je_monat]
    mitte = len(alle) // 2
    recent = [(ex, ic) for jahr, ex, ic in je_monat if jahr >= RECENT_AB]
    frueher = [(ex, ic) for jahr, ex, ic in je_monat if jahr < RECENT_AB]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_for": "signal_stack_motor (Dashboard Stack-Validierung)",
        "method": (f"Top-{TOP_N} vs. Universum (wie der Bot kauft; ersetzt am "
                   "23.07.2026 die verduennende Top-20%-Sicht) — Point-in-Time-"
                   "Schnappschuesse, monatlich, nicht ueberlappend"),
        "note": ("R-B41: regeneriert aus signal_pit_snapshots.json. Fenster 2017+ "
                 "(solide EDGAR-Abdeckung); die frueheren 2012-2016-Zeilen der "
                 "Vorgaenger-Datei beruhten auf duennerer Faktenlage."),
        "overall": _stats(alle),
        "first_half": _stats(alle[:mitte]),
        "second_half": _stats(alle[mitte:]),
        "recent": {"ab": RECENT_AB, **_stats(recent)},
        "pre_recent": {"bis": str(int(RECENT_AB) - 1), **_stats(frueher)},
        "per_year": per_year,
        "years": years,
        "n_months": len(alle),
        "caveat": ("Historisch, monatlich, VOR Kosten; Methode: Top-15 (wie der Bot "
                   "kauft), Fenster 2017+. Universum = heutige S&P-600-Member -> "
                   "Survivorship, und zwar ZEITLICH ASYMMETRISCH: fruehe Jahre "
                   "enthalten ueberproportional spaetere Aufsteiger und sehen "
                   "dadurch besser aus — die juengsten Jahre sind die ehrlichsten. "
                   "2024-2026 liegt der Excess bei ~0: die Live-Validierung im "
                   "Soak ist deshalb der Ernstfall, nicht die Formalitaet. Das "
                   "Score-Archiv (survivorship-frei, taeglich seit 21.07.) "
                   "beantwortet die Frage endgueltig."),
    }

    # Sicherung der Vorgaenger-Datei (einmalig pro Tag)
    try:
        alt = load_json("stack_wfo_baseline.json")
        if alt and not alt.get("method"):
            bak = get_data_path("stack_wfo_baseline.json.bak-vor-R-B41")
            if not bak.exists():
                import json as _json
                bak.write_text(_json.dumps(alt, indent=2))
                print(f"Backup: {bak}")
    except Exception as e:
        print("Backup fehlgeschlagen:", e)
        return 1

    save_json("stack_wfo_baseline.json", payload)

    ov, rc, pr = payload["overall"], payload["recent"], payload["pre_recent"]
    print(f"Geschrieben: {len(alle)} Monate, {len(years)} Jahre "
          f"({years[0]}-{years[-1]})")
    print(f"  Gesamt    : {ov['excess_mo']*100:+.2f}%/Mt | PF {ov['pf']:.2f} | "
          f"Sharpe {ov['sharpe']:.2f} | IC {ov['ic']:+.3f}")
    print(f"  bis {pr['bis']}  : {pr['excess_mo']*100:+.2f}%/Mt (n={pr['n']})")
    print(f"  ab {rc['ab']}   : {rc['excess_mo']*100:+.2f}%/Mt (n={rc['n']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
