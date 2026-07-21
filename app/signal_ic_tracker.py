"""Ranking-Auswertung — misst, ob ein hoher Score hoehere Folgerenditen bedeutet.

DIE FRAGE, DIE HIER BEANTWORTET WIRD (R-B25, 21.07.2026)
=========================================================
"Funktioniert das Signal?" — getrennt von "hat der Bot verdient?".

Das ist nicht dieselbe Frage. Verliert der Bot, kann das am Signal liegen oder an
allem drumherum: Positionsgroesse, Ausstiege, Zeitpunkt, Gebuehren. Diese
Auswertung schaut nur auf das Signal, ueber das **gesamte** Universum, und ist
damit unabhaengig davon, welche 15 Namen der Bot zufaellig gehalten hat.

Der Hebel ist die Datenmenge: ~309 Aktien pro Schnappschuss statt ~0.6
abgeschlossener Round-Trips pro Tag.

DIE ZWEI KENNZAHLEN
-------------------
**Rang-IC** (Information Coefficient): Rang-Korrelation zwischen Score und
Folgerendite. +1 = perfekte Rangfolge, 0 = kein Zusammenhang, negativ = das
Signal zeigt in die falsche Richtung.

  Groessenordnung nicht falsch einschaetzen: In der Aktien-Quant-Praxis gilt ein
  dauerhafter Rang-IC von 0.02-0.05 bereits als brauchbar. Wer 0.3 erwartet, wird
  jedes echte Signal fuer wertlos halten. Entscheidend ist nicht die Hoehe,
  sondern ob er ueber viele Perioden **stabil positiv** bleibt.

**Quintils-Spanne**: Durchschnittliche Folgerendite des besten Fuenftels minus die
des schlechtesten Fuenftels. Anschaulicher als der IC — das ist die Rendite, die
eine Strategie "bestes Fuenftel kaufen" theoretisch erzielt haette. Vor Kosten.

WARUM NICHT-UEBERLAPPENDE PERIODEN (der wichtigste Punkt)
----------------------------------------------------------
Man koennte jeden Tag mit dem Tag 21 Handelstage spaeter vergleichen und haette
sehr viele Messpunkte. Diese Messpunkte waeren aber **nicht unabhaengig**: zwei um
einen Tag versetzte Perioden teilen 20 von 21 Tagen Kursverlauf. Wer sie als
unabhaengig zaehlt, berechnet eine viel zu kleine Streuung und haelt Rauschen fuer
ein Ergebnis.

Voreinstellung ist deshalb ``overlap=False``: Perioden werden luecken-frei
aneinandergereiht (Tag 0->21, 21->42, ...). Weniger Messpunkte, aber ehrliche.
Es ist derselbe Fehler wie beim Motor-Edge-Signal am 20.07., nur eine Ebene
tiefer: dort war die Alarmschwelle geraten, hier waere die Streuung geschoent.

WAS DAS MODUL NICHT TUT
-----------------------
Es handelt nicht und aendert keine Config. Reine Messung.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

AUDIT_METADATA = {
    "purpose": "Misst die Vorhersagekraft des Signal-Stack-Rankings (Rang-IC + Quintils-Spanne) auf der Score-Historie — beantwortet 'funktioniert das Signal' unabhaengig davon, ob der Bot verdient hat",
    "config_section": None,
    "state_files": ["signal_ic_report.json"],
    "self_tests": [],
    "scheduler_hooks": ["run_ic_report (woechentlich)"],
    "health_check": "ic_status",
    "added_in": "v39 (R-B25 — Score-Historie + Ranking-Auswertung)",
}

_REPORT_FILE = "signal_ic_report.json"

# Auswertungs-Horizont in Handelstagen. 21 ~ ein Monat und passt zur Haltedauer
# des Motors (mittelfristig-fundamental). 63 ~ ein Quartal als Gegenprobe: ein
# echtes Fundamental-Signal sollte auf dem laengeren Horizont nicht verschwinden.
DEFAULT_HORIZONS = (21, 63)

# Unter dieser Zahl gemeinsamer Symbole wird eine Periode verworfen. Der Rang-IC
# hat bei n Beobachtungen eine Streuung von rund 1/sqrt(n-1); bei n=30 sind das
# ~0.19 — eine Einzelperiode saehe dann selbst bei voelliger Zufaelligkeit oft
# nach einem starken Signal aus.
MIN_SYMBOLS_PER_PERIOD = 30


# ---------------------------------------------------------------- Statistik ---

def _ranks(values: list) -> list:
    """Raenge mit Mittelung bei Gleichstand (1-basiert).

    Bewusst ohne scipy/numpy: das Modul soll ueberall laufen, wo der Bot laeuft,
    und die Rechnung ist zu klein, um eine Abhaengigkeit zu rechtfertigen.
    """
    n = len(values)
    idx = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[idx[j + 1]] == values[idx[i]]:
            j += 1
        mittel = (i + j) / 2.0 + 1.0  # Durchschnittsrang bei Gleichstand
        for k in range(i, j + 1):
            ranks[idx[k]] = mittel
        i = j + 1
    return ranks


def _pearson(xs: list, ys: list) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    nenner = (sum(d * d for d in dx) * sum(d * d for d in dy)) ** 0.5
    if nenner == 0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / nenner


def spearman(xs: list, ys: list) -> Optional[float]:
    """Rang-Korrelation = Pearson auf den Raengen."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    return _pearson(_ranks(xs), _ranks(ys))


def _quintile_spread(scores: list, rets: list) -> Optional[dict]:
    """Folgerendite bestes Fuenftel minus schlechtestes Fuenftel (in Prozent)."""
    n = len(scores)
    if n < 10:  # unter 10 Namen sind "Fuenftel" bedeutungslos
        return None
    paare = sorted(zip(scores, rets), key=lambda p: p[0])
    k = max(1, n // 5)
    unten = [r for _, r in paare[:k]]
    oben = [r for _, r in paare[-k:]]
    top = sum(oben) / len(oben)
    bot = sum(unten) / len(unten)
    return {
        "top_quintil_ret_pct": round(top * 100, 3),
        "bottom_quintil_ret_pct": round(bot * 100, 3),
        "spread_pct": round((top - bot) * 100, 3),
        "n_pro_quintil": k,
    }


# ------------------------------------------------------------- Auswertung ---

def _period_pairs(tage: list, horizon: int, overlap: bool) -> list:
    """(start, ende)-Paare mit ``horizon`` Schnappschuessen Abstand.

    Der Abstand wird in Schnappschuessen gezaehlt, nicht in Kalendertagen — die
    Historie enthaelt nur Handelstage, damit entspricht ein Schritt einem
    Handelstag. Faellt ein Lauf aus, verschiebt sich die Periode entsprechend;
    das ist gewollt, denn gemessen wird der Kursabstand zwischen zwei tatsaechlich
    vorhandenen Beobachtungen.
    """
    paare = []
    schritt = 1 if overlap else horizon
    i = 0
    while i + horizon < len(tage):
        paare.append((tage[i], tage[i + horizon]))
        i += schritt
    return paare


def compute_ic(history: dict, horizon: int = 21, overlap: bool = False,
               min_symbols: int = MIN_SYMBOLS_PER_PERIOD) -> dict:
    """Rang-IC + Quintils-Spanne ueber alle auswertbaren Perioden.

    ``overlap=False`` (Voreinstellung) liefert unabhaengige Perioden — siehe
    Modul-Kopf. ``overlap=True`` nur fuer explorative Blicke verwenden, niemals
    fuer eine Entscheidung.
    """
    tage = sorted(history or {})
    paare = _period_pairs(tage, horizon, overlap)

    perioden = []
    for start, ende in paare:
        s_snap, e_snap = history[start], history[ende]
        scores, rets, syms = [], [], []
        # R-B30: ueber den Index lesen, nicht per Tupel-Entpackung. Seit Schema 2
        # sind die Zeilen laenger ([score, kurs, eligible, *einzelsignale]) —
        # "for sym, (score, preis) in ..." waere daran hart gescheitert. So bleiben
        # alte (2-spaltige) und neue Zeilen gleichermassen lesbar.
        for sym, zeile in s_snap.items():
            spaeter = e_snap.get(sym)
            preis = zeile[1]
            if not spaeter or not preis or preis <= 0:
                continue
            preis_spaeter = spaeter[1]
            if not preis_spaeter or preis_spaeter <= 0:
                continue
            scores.append(zeile[0])
            rets.append(preis_spaeter / preis - 1.0)
            syms.append(sym)

        if len(scores) < min_symbols:
            continue

        ic = spearman(scores, rets)
        if ic is None:
            continue
        perioden.append({
            "start": start, "ende": ende,
            "n_symbole": len(scores),
            "rang_ic": round(ic, 4),
            "mittlere_rendite_pct": round(sum(rets) / len(rets) * 100, 3),
            "quintile": _quintile_spread(scores, rets),
        })

    return {
        "horizont_handelstage": horizon,
        "ueberlappend": overlap,
        "n_perioden": len(perioden),
        "perioden": perioden,
        **_aggregat(perioden),
    }


def _aggregat(perioden: list) -> dict:
    """Mittelwert, Streuung, t-Wert und Trefferquote ueber die Perioden.

    Der **t-Wert** ist die entscheidende Zahl: mittlerer IC geteilt durch seinen
    Standardfehler. Faustregel |t| > 2 fuer "wahrscheinlich kein Zufall". Er ist
    nur dann ehrlich, wenn die Perioden unabhaengig sind — deshalb die Warnung im
    Modul-Kopf zu ``overlap``.

    Bei weniger als zwei Perioden gibt es keine Streuung und damit keinen t-Wert.
    Dann bleibt das Feld None, statt eine Scheingenauigkeit auszuweisen.
    """
    if not perioden:
        return {"mittlerer_ic": None, "ic_streuung": None, "t_wert": None,
                "anteil_positiv_pct": None, "mittlere_quintils_spanne_pct": None}

    ics = [p["rang_ic"] for p in perioden]
    n = len(ics)
    mittel = sum(ics) / n

    if n < 2:
        streuung = t = None
    else:
        var = sum((x - mittel) ** 2 for x in ics) / (n - 1)
        streuung = var ** 0.5
        t = round(mittel / (streuung / (n ** 0.5)), 2) if streuung > 0 else None

    spannen = [p["quintile"]["spread_pct"] for p in perioden if p.get("quintile")]

    return {
        "mittlerer_ic": round(mittel, 4),
        "ic_streuung": round(streuung, 4) if streuung is not None else None,
        "t_wert": t,
        "anteil_positiv_pct": round(sum(1 for x in ics if x > 0) / n * 100, 1),
        "mittlere_quintils_spanne_pct": round(sum(spannen) / len(spannen), 3) if spannen else None,
    }


# Kritische t-Werte, zweiseitig, 5 % Irrtumswahrscheinlichkeit, nach Freiheitsgraden.
#
# WARUM NICHT EINFACH 2.0: Die Faustregel "|t| > 2" gilt fuer grosse Stichproben.
# Bei wenigen Perioden ist die Schwelle deutlich hoeher — bei 12 Perioden (11
# Freiheitsgrade) liegt sie bei 2.201. Mit 2.0 gemessen: 7.30 % Fehlalarm in
# 2000 Zufallslaeufen statt der angepeilten 5 % (rechnerisch 7.1 % — passt).
#
# Das ist derselbe Fehler wie beim Motor-Edge-Signal am 20.07.: eine plausibel
# aussehende Zahl uebernommen, statt sie zu messen. Aufgefallen ist er nur, weil
# die Kalibrierung mit reinem Zufall gegengeprueft wurde. Diese Gegenprobe gehoert
# ab jetzt zu jedem Schwellwert (siehe scripts/mc_ic_kalibrierung.py).
_T_KRITISCH = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980,
}
_T_KRITISCH_UNENDLICH = 1.960


def t_kritisch(freiheitsgrade: int) -> float:
    """Kritischer t-Wert (zweiseitig, 5 %) fuer die gegebenen Freiheitsgrade.

    Zwischen den Stuetzstellen wird linear interpoliert; oberhalb von 120 gilt der
    Grenzwert 1.96. Konservativ bei ungueltiger Eingabe: der hoechste Wert.
    """
    if freiheitsgrade < 1:
        return _T_KRITISCH[1]
    if freiheitsgrade in _T_KRITISCH:
        return _T_KRITISCH[freiheitsgrade]
    if freiheitsgrade > 120:
        return _T_KRITISCH_UNENDLICH
    stuetzen = sorted(_T_KRITISCH)
    unten = max(s for s in stuetzen if s < freiheitsgrade)
    oben = min(s for s in stuetzen if s > freiheitsgrade)
    anteil = (freiheitsgrade - unten) / (oben - unten)
    return _T_KRITISCH[unten] + anteil * (_T_KRITISCH[oben] - _T_KRITISCH[unten])


def bewertung(agg: dict) -> dict:
    """Uebersetzt die Zahlen in eine Aussage — inklusive 'noch zu frueh'.

    Die Reihenfolge ist Absicht: **zuerst** wird geprueft, ob ueberhaupt genug
    Perioden da sind. Ein t-Wert aus zwei Perioden ist keine Erkenntnis, sondern
    eine Einladung zum Selbstbetrug — genau der Fehler, der beim Motor-Edge-Signal
    zu 41.5 % Fehlalarmen gefuehrt hat.
    """
    n = agg.get("n_perioden") or 0
    ic = agg.get("mittlerer_ic")
    t = agg.get("t_wert")

    if n < 6 or ic is None:
        return {"status": "unklar", "farbe": "grau",
                "text": f"Noch zu wenig Historie ({n} von mindestens 6 Perioden). "
                        "Es laesst sich noch nichts sagen — das ist kein schlechtes "
                        "Zeichen, nur ein fruehes."}
    if t is None:
        return {"status": "unklar", "farbe": "grau",
                "text": "Perioden vorhanden, aber ohne Streuung kein t-Wert."}

    # Schwelle haengt an der Zahl der Perioden — NICHT die Faustregel 2.0,
    # die erst bei grossen Stichproben stimmt (siehe _T_KRITISCH).
    schwelle = t_kritisch(n - 1)

    if t >= schwelle and ic > 0:
        return {"status": "signal", "farbe": "gruen",
                "text": f"Das Ranking sagt Folgerenditen vorher (IC {ic:+.3f}, "
                        f"t={t} ueber der Schwelle {schwelle:.2f}). "
                        "Hoeher bewertete Aktien schnitten danach besser ab."}
    if t <= -schwelle and ic < 0:
        return {"status": "invers", "farbe": "rot",
                "text": f"Das Ranking zeigt in die FALSCHE Richtung (IC {ic:+.3f}, "
                        f"t={t} unter -{schwelle:.2f}). "
                        "Hoeher bewertete Aktien schnitten schlechter ab."}
    return {"status": "kein_nachweis", "farbe": "gelb",
            "text": f"Kein nachweisbarer Zusammenhang (IC {ic:+.3f}, t={t}, "
                    f"noetig waeren {schwelle:.2f}). Entweder gibt es keinen "
                    "Vorteil, oder er ist zu klein fuer die bisherige Datenmenge."}


def compute_ic_je_signal(history: dict, horizon: int = 21,
                         min_symbols: int = MIN_SYMBOLS_PER_PERIOD) -> dict:
    """Rang-IC getrennt fuer jedes der fuenf Einzelsignale (R-B30).

    Die logische Anschlussfrage an R-B25: die Gesamtnote traegt an der Spitze —
    aber traegt sie, WEIL der Value-Teil funktioniert, oder TROTZ eines Teils, der
    schadet? Ohne diese Aufschluesselung optimiert man blind an fuenf Reglern.

    Braucht Schema-2-Zeilen. Bei aelteren Daten (nur [score, kurs]) kommt ein
    leeres Ergebnis zurueck statt einer erfundenen Zahl.
    """
    from app.signal_score_history import SIGNAL_NAMES, row_signals

    tage = sorted(history or {})
    paare = _period_pairs(tage, horizon, overlap=False)

    je_signal = {n: [] for n in SIGNAL_NAMES}
    for start, ende in paare:
        s_snap, e_snap = history[start], history[ende]
        werte = {n: ([], []) for n in SIGNAL_NAMES}   # (signalwerte, renditen)
        for sym, zeile in s_snap.items():
            spaeter = e_snap.get(sym)
            preis = zeile[1]
            if not spaeter or not preis or preis <= 0:
                continue
            preis_spaeter = spaeter[1]
            if not preis_spaeter or preis_spaeter <= 0:
                continue
            ret = preis_spaeter / preis - 1.0
            for n, v in row_signals(zeile).items():
                if v is None:
                    continue
                werte[n][0].append(v)
                werte[n][1].append(ret)

        for n, (xs, ys) in werte.items():
            if len(xs) < min_symbols:
                continue
            ic = spearman(xs, ys)
            if ic is not None:
                je_signal[n].append(round(ic, 4))

    ergebnis = {}
    for n, ics in je_signal.items():
        if not ics:
            ergebnis[n] = {"n_perioden": 0, "mittlerer_ic": None, "t_wert": None}
            continue
        agg = _aggregat([{"rang_ic": x, "quintile": None} for x in ics])
        agg["n_perioden"] = len(ics)
        agg["bewertung"] = bewertung(agg)
        ergebnis[n] = agg
    return {"horizont_handelstage": horizon, "je_signal": ergebnis}


def run_ic_report(horizons=DEFAULT_HORIZONS) -> dict:
    """Vollstaendiger Bericht ueber alle Horizonte. Schreibt signal_ic_report.json."""
    from app.signal_score_history import load_history
    hist = load_history()

    berichte = {}
    for h in horizons:
        erg = compute_ic(hist, horizon=h)
        erg["bewertung"] = bewertung(erg)
        berichte[str(h)] = erg

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_snapshots": len(hist),
        "horizonte": berichte,
    }
    try:
        from app.config_manager import save_json
        save_json(_REPORT_FILE, payload)
    except Exception as e:  # pragma: no cover - IO best effort
        log.warning("signal_ic_report.json schreiben fehlgeschlagen: %s", e)

    for h, b in berichte.items():
        log.info("Rang-IC (%s Tage): %s Perioden, IC %s, t %s -> %s",
                 h, b["n_perioden"], b["mittlerer_ic"], b["t_wert"],
                 b["bewertung"]["status"])
    return payload


def ic_status() -> dict:
    """Health: letzter Bericht."""
    try:
        from app.config_manager import load_json
        p = load_json(_REPORT_FILE)
    except Exception:
        p = None
    if not isinstance(p, dict) or not p.get("horizonte"):
        return {"ok": False, "reason": "noch kein IC-Bericht"}
    haupt = p["horizonte"].get("21") or {}
    return {"ok": True, "generated_at": p.get("generated_at"),
            "n_snapshots": p.get("n_snapshots"),
            "mittlerer_ic": haupt.get("mittlerer_ic"),
            "status": (haupt.get("bewertung") or {}).get("status")}
