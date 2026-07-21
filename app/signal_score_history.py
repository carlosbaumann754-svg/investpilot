"""Score-Historie — archiviert die taeglichen Signal-Stack-Scores samt Kurs.

WARUM ES DIESES MODUL GIBT (21.07.2026, R-B25)
================================================
Der Shadow-Runner bewertet jeden Handelstag ~309 Aktien mit allen fuenf Signalen
und schreibt das Ergebnis nach signal_stack_shadow.json. Diese Datei wurde bisher
bei jedem Lauf **ueberschrieben**. Die Backups sichern zehn andere Dateien, die
Shadow-Datei ist nicht darunter (geprueft: 0 Treffer in allen 32 Archiven).

Damit wurde die aussagekraeftigste Datenquelle des Projekts taeglich geloescht:
rund 309 Beobachtungen pro Tag, aus denen sich direkt beantworten liesse, ob ein
hoher Score tatsaechlich hoehere Folgerenditen bedeutet.

Stattdessen wartete die Validierung auf abgeschlossene Round-Trips — ~0.6 pro Tag.
Fuer eine belastbare Aussage braucht es dort rund 100 Stueck (R-B18/R-B23), also
etwa acht Monate. Die Ranking-Auswertung kommt mit denselben Daten in zwei bis
drei Monaten zum Ziel, weil sie das gesamte Universum misst statt nur die 15
Positionen, die der Bot zufaellig haelt.

WAS HIER GESPEICHERT WIRD
-------------------------
Pro Handelstag ein Schnappschuss ``{symbol: [score, kurs]}``.

Der **Kurs gehoert zwingend dazu**: nur so ist das Archiv selbsttragend. Die
Folgerendite eines Schnappschusses ergibt sich dann allein aus einem spaeteren
Schnappschuss desselben Archivs — ohne nachtraeglichen Kursabruf, ohne
Abhaengigkeit von einer externen Quelle, und vor allem ohne die Gefahr, spaeter
mit revidierten oder angepassten Kursen zu rechnen.

ABGRENZUNG: KEIN BACKFILL
-------------------------
Eine rueckwirkende Rekonstruktion waere Selbstbetrug. Die Scores haengen an
EDGAR-Fundamentaldaten, und die heutige Faktenlage enthaelt Zahlen, die es zum
damaligen Zeitpunkt noch nicht gab (Nachmeldungen, Korrekturen, Restatements).
Ein Backfill wuerde exakt den Look-Ahead-Bias erzeugen, gegen den die ganze
Messung antritt. Die Historie beginnt deshalb heute bei null.

Wer die historische Sicht braucht, nimmt den signal_stack_backtester — der
arbeitet bewusst mit Point-in-Time-Facts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

AUDIT_METADATA = {
    "purpose": "Archiviert die taeglichen Signal-Stack-Scores samt Kurs, damit die Vorhersagekraft des Rankings ueber das ganze Universum messbar wird (statt nur ueber die ~15 gehaltenen Positionen)",
    "config_section": None,
    "state_files": ["signal_score_history.json"],
    "self_tests": [],
    "scheduler_hooks": ["append_snapshot (aus signal_stack_runner.run_shadow_scan, taeglich)"],
    "health_check": "history_status",
    "added_in": "v39 (R-B25 — Score-Historie + Ranking-Auswertung)",
}

_HISTORY_FILE = "signal_score_history.json"

# ============================================================
# SCHEMA 2 (R-B30, 21.07.2026) — Einzelsignale mitspeichern
# ============================================================
# Schema 1 speicherte nur [score, kurs]. Damit liesse sich beantworten, OB das
# Ranking funktioniert — aber nie, WELCHES der fuenf Signale es traegt.
#
# Der Scan berechnet die Einzelwerte ohnehin (signal_stack_shadow.json fuehrt sie
# unter "percentiles"); sie wurden nur weggeworfen. Und sie lassen sich NICHT
# nachtraeglich ergaenzen: die EDGAR-Faktenlage von morgen enthaelt Zahlen, die es
# heute noch nicht gab (Nachmeldungen, Restatements). Wer sie spaeter rekonstruiert,
# baut exakt den Look-Ahead-Bias ein, gegen den die ganze Messung antritt.
#
# Deshalb JETZT, vor der ersten Sammlung: der Unterschied kostet ~40 Bytes pro
# Aktie und Tag (18 KB statt 7 KB taeglich, ~9 MB ueber 500 Tage) und ermoeglicht
# spaeter die Frage "traegt der Value-Teil oder der Quality-Teil?" — die logische
# Anschlussfrage an R-B25.
SCHEMA_VERSION = 2

# Reihenfolge der Einzelsignale in jeder Zeile. NICHT umsortieren — die Position
# ist der Schluessel; ein Tausch wuerde die Historie still verfaelschen.
SIGNAL_NAMES = ("value", "quality", "reversal", "lev", "earngrowth")

# Zeilenaufbau: [score, kurs, eligible, *einzelsignale]
_IDX_SCORE, _IDX_PRICE, _IDX_ELIGIBLE = 0, 1, 2
_IDX_SIGNALS = 3


def row_score(row):
    """Gesamtnote aus einer Archiv-Zeile (schema-unabhaengig)."""
    return row[_IDX_SCORE]


def row_price(row):
    """Kurs aus einer Archiv-Zeile (schema-unabhaengig)."""
    return row[_IDX_PRICE]


def row_eligible(row):
    """War die Aktie an diesem Tag ueberhaupt kaufbar? None bei Schema 1."""
    return bool(row[_IDX_ELIGIBLE]) if len(row) > _IDX_ELIGIBLE else None


def row_signals(row) -> dict:
    """{signalname: perzentil} — leer bei Schema-1-Zeilen."""
    if len(row) <= _IDX_SIGNALS:
        return {}
    werte = row[_IDX_SIGNALS:]
    return {n: v for n, v in zip(SIGNAL_NAMES, werte) if v is not None}

# Aufbewahrung. 309 Symbole * ~22 Bytes ~ 7 KB/Tag -> 500 Tage ~ 3.5 MB.
# 500 Handelstage sind knapp zwei Jahre; laenger zurueck ist fuer die
# Auswertung ohnehin von begrenztem Wert, weil sich das Signal aendert.
MAX_SNAPSHOTS = 500


def _round_or_none(v, digits: int):
    try:
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


def build_snapshot(scores: dict, prices: dict) -> dict:
    """{symbol: [score, kurs, eligible, *einzelsignale]} aus Score- und Preis-Dict.

    ``prices`` kommt vom Preis-Provider als ``{symbol: (kurs_jetzt, kurs_vor_21d)}``;
    gebraucht wird hier nur der aktuelle Kurs. Ein einzelner Float wird ebenfalls
    akzeptiert, damit Tests und kuenftige Aufrufer nicht an das Tupel gebunden sind.

    Symbole ohne Kurs werden **weggelassen** — ohne Kurs laesst sich spaeter keine
    Folgerendite bilden, der Eintrag waere nur Ballast.

    Fehlt ein Einzelsignal (unvollstaendige EDGAR-Abdeckung), steht dort None statt
    eines geratenen Ersatzwerts. Ein 0.5-Platzhalter waere spaeter nicht mehr von
    einem echten Median zu unterscheiden.
    """
    snap = {}
    for sym, entry in (scores or {}).items():
        ist_dict = isinstance(entry, dict)
        score = entry.get("score") if ist_dict else entry
        raw = (prices or {}).get(sym)
        price = raw[0] if isinstance(raw, (tuple, list)) and raw else raw
        s, p = _round_or_none(score, 2), _round_or_none(price, 4)
        if s is None or p is None or p <= 0:
            continue

        eligible = int(bool(entry.get("eligible"))) if ist_dict else 0
        pc = (entry.get("percentiles") or {}) if ist_dict else {}
        signale = [_round_or_none(pc.get(n), 4) for n in SIGNAL_NAMES]

        snap[sym] = [s, p, eligible, *signale]
    return snap


def append_snapshot(asof: str, scores: dict, prices: dict,
                    max_snapshots: int = MAX_SNAPSHOTS) -> dict:
    """Haengt den Schnappschuss fuer ``asof`` an die Historie an.

    **Idempotent**: ein erneuter Lauf am selben Tag ersetzt den Eintrag, statt zu
    duplizieren. Wichtig, weil der Shadow-Scan bei einem Fehlschlag von Hand
    nachgezogen werden koennen muss, ohne den Datenbestand zu verfaelschen.

    Best-effort: schlaegt das Schreiben fehl, wird geloggt statt geworfen — die
    Archivierung darf den Shadow-Scan niemals zum Absturz bringen.
    """
    snap = build_snapshot(scores, prices)
    if not snap:
        log.warning("Score-Historie: Schnappschuss %s ist leer -> nicht gespeichert", asof)
        return {"ok": False, "reason": "leerer Schnappschuss", "asof": asof}

    try:
        from app.config_manager import load_json, save_json
        hist = load_json(_HISTORY_FILE) or {}
        if not isinstance(hist, dict):
            hist = {}
        snapshots = hist.get("snapshots")
        if not isinstance(snapshots, dict):
            snapshots = {}

        ersetzt = asof in snapshots
        snapshots[asof] = snap

        # Aelteste zuerst verwerfen (Datums-Strings sind ISO -> lexikografisch sortierbar)
        if len(snapshots) > max_snapshots:
            for alt in sorted(snapshots)[:len(snapshots) - max_snapshots]:
                del snapshots[alt]

        save_json(_HISTORY_FILE, {
            "schema": SCHEMA_VERSION,
            "signal_names": list(SIGNAL_NAMES),
            "zeilenaufbau": "[score, kurs, eligible, *einzelsignale]",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "n_snapshots": len(snapshots),
            "snapshots": snapshots,
        })
        log.info("Score-Historie: %s %s (%d Symbole), %d Schnappschuesse gesamt",
                 asof, "ersetzt" if ersetzt else "ergaenzt", len(snap), len(snapshots))
        return {"ok": True, "asof": asof, "n_symbols": len(snap),
                "n_snapshots": len(snapshots), "replaced": ersetzt}
    except Exception as e:  # pragma: no cover - IO best effort
        log.warning("Score-Historie schreiben fehlgeschlagen: %s", e)
        return {"ok": False, "reason": str(e), "asof": asof}


def load_history() -> dict:
    """{datum: {symbol: [score, kurs]}} — leeres Dict, wenn nichts da ist."""
    try:
        from app.config_manager import load_json
        hist = load_json(_HISTORY_FILE) or {}
        snaps = hist.get("snapshots")
        return snaps if isinstance(snaps, dict) else {}
    except Exception:
        return {}


def history_status() -> dict:
    """Health: Umfang der Historie + wie weit sie schon reicht."""
    snaps = load_history()
    if not snaps:
        return {"ok": False, "reason": "noch keine Score-Historie",
                "n_snapshots": 0}
    tage = sorted(snaps)
    return {
        "ok": True,
        "n_snapshots": len(tage),
        "first": tage[0],
        "last": tage[-1],
        "n_symbols_last": len(snaps[tage[-1]]),
    }
