"""Monatlicher Auto-Refresh der Stack-Validierungs-Karte (R-B42, 23.07.2026).

WARUM ES DIESES SKRIPT GIBT
===========================
R-B41 hat die Karte auf die Top-15-Methode umgestellt — aber nur als einmaliger
Handlauf. Carlos' Nachfrage 'wird die Karte immer aktualisiert?' deckte die
fehlende Verdrahtung auf (dieselbe Fehlerklasse wie R-B34: gebaut, aber kein
Ausloeser). Dieses Skript ist der Ausloeser: Host-Cron am 1. jedes Monats.

BEWUSST SELBSTTRAGEND
---------------------
Der Cron pipe't dieses Skript vom HOST-Checkout in den Container
(`docker exec -i ... python3 - < /opt/investpilot/scripts/...py`). Damit haelt
ein `git pull` auf dem Host den Refresh aktuell, OHNE Image-Rebuild. Preis:
das Skript darf NUR von app/-Modulen abhaengen (im Image seit langem stabil),
nicht von anderen scripts/-Dateien — die Cache- und Karten-Logik ist deshalb
hier dupliziert. Erste Fassung lud die Helfer aus /app/scripts und waere am
1.8. gescheitert, weil das Refresh-Skript noch in keinem Image steckt.

WAS ES TUT
----------
1. PIT-Schnappschuss-Cache bis zum letzten ABGESCHLOSSENEN Monat verlaengern
   (angebrochene Monate wuerden halbfertige Renditen einmischen). Cache-Hit
   wenn kein neuer Monat -> Sekunden; sonst Neubau (~Minuten).
2. data/stack_wfo_baseline.json regenerieren (Top-15-Methode, gleiches Schema,
   generated_at/method/recent-Block). Einziger Leser: api_stack_validation.
"""
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

sys.path.insert(0, "/app")

START = "2017-01-01"
TOP_N = 15
RECENT_AB = "2024"
MIN_SYMBOLE = 50
CACHE_FILE = "signal_pit_snapshots.json"
KARTEN_FILE = "stack_wfo_baseline.json"


# ---------------------------------------------------------------- Schritt 1 ---

def cache_aktualisieren(ende: str) -> dict:
    from app.config_manager import load_json, save_json
    cache = load_json(CACHE_FILE) or {}
    if (cache.get("start") == START and cache.get("ende") == ende
            and cache.get("snapshots")):
        print(f"Cache aktuell ({len(cache['snapshots'])} Schnappschuesse) — kein Neubau")
        return cache["snapshots"]

    print(f"Neuer Monat -> Cache-Neubau {START}..{ende}", flush=True)
    from app import edgar_client, signal_stack, sp600_universe
    from app import signal_stack_backtester as bt
    from app.config_manager import load_config

    cfg = load_config()
    disabled = set(cfg.get("disabled_symbols") or [])
    symbols = [s for s in sp600_universe.get_symbols() if s not in disabled]
    facts = edgar_client.load_facts()
    ph = bt.load_price_history(symbols, START, ende)

    snaps = {}
    for rb in bt._month_starts(START, ende):
        prices = bt._prices_asof(ph, rb)
        if not prices:
            continue
        scores = signal_stack.score_universe(list(prices.keys()), facts, prices,
                                             rb.isoformat())
        snap = {}
        for sym, eintrag in scores.items():
            kurs = prices.get(sym)
            kurs = kurs[0] if isinstance(kurs, (tuple, list)) else kurs
            score = eintrag.get("score") if isinstance(eintrag, dict) else eintrag
            if score is not None and kurs and kurs > 0:
                snap[sym] = [round(float(score), 2), round(float(kurs), 4)]
        if snap:
            snaps[rb.isoformat()] = snap

    save_json(CACHE_FILE, {"start": START, "ende": ende, "snapshots": snaps})
    print(f"Cache neu: {len(snaps)} Schnappschuesse")
    return snaps


# ---------------------------------------------------------------- Schritt 2 ---

def _stats(monate):
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
        "n": len(ex), "excess_mo": mean,
        "hit": sum(1 for x in ex if x > 0) / len(ex),
        "pf": (gw / gl) if gl > 0 else None,
        "sharpe": (mean / sd * 12 ** 0.5) if sd > 0 else None,
        "ic": statistics.mean(ics) if ics else None,
        "ic_pos": (sum(1 for i in ics if i > 0) / len(ics)) if ics else None,
    }


def karte_schreiben(snaps: dict) -> None:
    from app.config_manager import save_json
    from app.signal_ic_tracker import spearman

    tage = sorted(snaps)
    je_monat = []
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
        je_monat.append((a[:4], excess, spearman(scores, rets)))

    per_year_raw = defaultdict(list)
    for jahr, ex, ic in je_monat:
        per_year_raw[jahr].append((ex, ic))
    years = sorted(per_year_raw)
    alle = [(ex, ic) for _, ex, ic in je_monat]
    mitte = len(alle) // 2
    recent = [(ex, ic) for j, ex, ic in je_monat if j >= RECENT_AB]
    frueher = [(ex, ic) for j, ex, ic in je_monat if j < RECENT_AB]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_for": "signal_stack_motor (Dashboard Stack-Validierung)",
        "method": (f"Top-{TOP_N} vs. Universum (wie der Bot kauft) — Point-in-Time-"
                   "Schnappschuesse, monatlich, nicht ueberlappend. "
                   "Auto-Refresh: Host-Cron am 1. des Monats (R-B42)."),
        "note": "R-B42: monatlicher Auto-Refresh (selbsttragend via stdin-Cron).",
        "overall": _stats(alle),
        "first_half": _stats(alle[:mitte]),
        "second_half": _stats(alle[mitte:]),
        "recent": {"ab": RECENT_AB, **_stats(recent)},
        "pre_recent": {"bis": str(int(RECENT_AB) - 1), **_stats(frueher)},
        "per_year": {j: _stats(per_year_raw[j]) for j in years},
        "years": years,
        "n_months": len(alle),
        "caveat": ("Historisch, monatlich, VOR Kosten; Methode: Top-15 (wie der Bot "
                   "kauft), Fenster 2017+. Universum = heutige S&P-600-Member -> "
                   "Survivorship, zeitlich ASYMMETRISCH: fruehe Jahre sehen zu gut "
                   "aus, die juengsten sind die ehrlichsten. 2024-2026 Excess ~0: "
                   "die Live-Validierung im Soak ist der Ernstfall. Auto-Refresh "
                   "monatlich am 1. (R-B42)."),
    }
    save_json(KARTEN_FILE, payload)
    ov, rc = payload["overall"], payload["recent"]
    print(f"Karte: {len(alle)} Monate {years[0]}-{years[-1]} | "
          f"Gesamt {ov['excess_mo']*100:+.2f}%/Mt | ab {rc['ab']}: "
          f"{rc['excess_mo']*100:+.2f}%/Mt (n={rc['n']})")


def main() -> int:
    heute = date.today()
    ende = date(heute.year, heute.month, 1).isoformat()
    print(f"Stack-Validierung Monats-Refresh: Fenster {START} .. {ende}")
    snaps = cache_aktualisieren(ende)
    if len(snaps) < 24:
        print("Zu wenig Schnappschuesse — Abbruch ohne Schreiben.")
        return 1
    karte_schreiben(snaps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
