"""R-B67 (11.09.2026): Einmal-Reparatur fehl-verbuchter Verkaeufe.

PROBLEM: Verkaufs-Orders, die erst NACH dem Warte-Fenster fuellen, wurden mit
dem Status der zuvor stornierten LIMIT-Stufe verbucht ("cancelled"). Dadurch
fielen echte, abgeschlossene Round-Trips aus saemtlichen Trade-Statistiken
(app/roundtrip_metrics.py filtert "cancelled" als NICHT_GEFUELLT weg).
Betroffen sind fast nur Gewinner, weil TRAILING_SL_CLOSE nur im Gewinn feuert.

Die Ursache ist ab R-B67 behoben (app/ibkr_client.py Status-Aggregation +
order_id im Close-Eintrag + E27-Selbstheilung). Dieses Skript repariert die
ALTBESTAENDE.

WICHTIG — es wird NICHT blind umgeschrieben: Unter den Kandidaten sind auch
ECHTE Stornos (Order lief ins Leere, Position blieb offen, der Bot versuchte
es danach erneut). Wuerde man die auf "executed" setzen, entstuenden
Phantom-Round-Trips — genau der Fehler, gegen den R-B54 absichert.

VERIFIKATION je Kandidat ueber die Trade-Abfolge desselben Symbols:
  - naechster Trade ist ein KAUF          -> Position war weg  -> FILL
  - naechster Trade ist erneuter VERKAUF  -> Position war da   -> STORNO
  - kein weiterer Trade + heute nicht im Depot                 -> FILL
  - kein weiterer Trade + heute offen                          -> STORNO
  - Symbol unbekannt                      -> nicht entscheidbar -> bleibt

Aufruf (im Container):
    python3 -m scripts.fix_cancelled_fills            # Dry-Run (Default)
    python3 -m scripts.fix_cancelled_fills --apply    # schreibt wirklich
"""
import json
import os
import shutil
import sys
from datetime import datetime

AUDIT_METADATA = {
    "purpose": (
        "Einmal-Reparatur (R-B67): setzt Verkaufs-Eintraege, die real gefuellt "
        "wurden aber als 'cancelled' verbucht sind, auf 'executed' — aber nur "
        "nach Einzel-Verifikation ueber die Trade-Abfolge. Echte Stornos und "
        "nicht entscheidbare Faelle bleiben unangetastet (Phantom-Schutz R-B54)."
    ),
    "config_section": None,
    "state_files": ["trade_history.json"],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "R-B67 (11.09.2026)",
}

DATA = os.environ.get("BOT_DATA_DIR", "/app/data")
HIST = os.path.join(DATA, "trade_history.json")
BRAIN = os.path.join(DATA, "brain_state.json")

# Voll-Schliessungen, die in den Round-Trip-Metriken zaehlen
FULL_CLOSE = (
    "STOP_LOSS_CLOSE", "TRAILING_SL_CLOSE", "SCANNER_SELL", "MANUAL_SELL",
    "OVERNIGHT_CLOSE", "PROFIT_LOCK_CLOSE", "HORIZON_CLOSE",
    "EARNINGS_BLACKOUT_CLOSE", "TIME_STOP_CLOSE", "TAKE_PROFIT_CLOSE",
)
NICHT_GEFUELLT = ("cancelled", "rejected", "failed")


def _load(pfad, default):
    try:
        with open(pfad, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("FEHLER beim Lesen von {}: {}".format(pfad, e))
        return default


def offene_symbole():
    """Symbole, die laut letztem Bot-Snapshot aktuell im Depot liegen."""
    brain = _load(BRAIN, {}) or {}
    snaps = brain.get("performance_snapshots") or []
    if not snaps:
        return set()
    return {str(p.get("symbol")) for p in (snaps[-1].get("positions") or [])}


def verifiziere(kandidat, alle_sortiert, offen):
    """Returns (entscheidung, begruendung) mit entscheidung in
    {"FILL", "STORNO", "UNKLAR"}."""
    sym = kandidat.get("symbol")
    if not sym or str(sym).lower() in ("none", "null", ""):
        return "UNKLAR", "Symbol im Eintrag nicht gesetzt"

    ts = str(kandidat.get("timestamp", ""))
    spaeter = [t for t in alle_sortiert
               if str(t.get("symbol")) == str(sym)
               and str(t.get("timestamp", "")) > ts]

    if spaeter:
        nxt = spaeter[0]
        akt = str(nxt.get("action", "")).upper()
        wann = str(nxt.get("timestamp", ""))[:16]
        if akt in ("SCANNER_BUY", "BUY"):
            return "FILL", "danach Neukauf am {}".format(wann)
        if akt.endswith("_CLOSE") or akt == "MANUAL_SELL":
            return "STORNO", "danach erneuter Verkaufsversuch am {}".format(wann)
        return "UNKLAR", "naechster Trade ist {}".format(akt)

    if str(sym) in offen:
        return "STORNO", "kein weiterer Trade, Position heute noch offen"
    return "FILL", "kein weiterer Trade, Symbol heute nicht im Depot"


def main(argv):
    apply_changes = "--apply" in argv
    hist = _load(HIST, [])
    if not hist:
        print("trade_history.json leer oder unlesbar — Abbruch")
        return 1

    eintraege = [t for t in hist if isinstance(t, dict)]
    sortiert = sorted(eintraege, key=lambda t: str(t.get("timestamp", "")))
    offen = offene_symbole()

    kandidaten = [
        t for t in sortiert
        if str(t.get("action", "")).upper() in FULL_CLOSE
        and str(t.get("status", "")).lower() in NICHT_GEFUELLT
        and abs(float(t.get("pnl_usd") or 0)) > 0.01
    ]

    print("=" * 70)
    print("R-B67 Reparatur fehl-verbuchter Verkaeufe — {}".format(
        "ANWENDEN" if apply_changes else "DRY-RUN (nichts wird geschrieben)"))
    print("=" * 70)
    print("Kandidaten (Voll-Close, storniert-Status, PnL != 0): {}".format(
        len(kandidaten)))
    print()

    zu_fixen, summe = [], 0.0
    for k in kandidaten:
        entscheidung, grund = verifiziere(k, sortiert, offen)
        pnl = float(k.get("pnl_usd") or 0)
        marke = {"FILL": "-> KORRIGIEREN", "STORNO": "   echter Storno, bleibt",
                 "UNKLAR": "   nicht entscheidbar, bleibt"}[entscheidung]
        print("  {} {:6} {:+10.2f} USD  {}".format(
            str(k.get("timestamp", ""))[:16], str(k.get("symbol")), pnl, marke))
        print("      {}".format(grund))
        if entscheidung == "FILL":
            zu_fixen.append(k)
            summe += pnl

    print()
    print("-" * 70)
    print("Zu korrigieren: {} Eintraege, PnL-Summe {:+.2f} USD".format(
        len(zu_fixen), summe))
    print("Unangetastet:   {} Eintraege".format(len(kandidaten) - len(zu_fixen)))

    if not zu_fixen:
        return 0
    if not apply_changes:
        print()
        print("DRY-RUN — mit --apply wirklich schreiben.")
        return 0

    stempel = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = "{}.vor_rb67_{}".format(HIST, stempel)
    shutil.copy(HIST, backup)
    print("Backup: {}".format(backup))

    jetzt = datetime.now().isoformat()
    for k in zu_fixen:
        k["status"] = "executed"
        k["korrigiert_am"] = jetzt
        k["korrektur_grund"] = (
            "R-B67: real gefuellt, war durch LIMIT-Storno-Status faelschlich "
            "als 'cancelled' verbucht (verifiziert ueber Trade-Abfolge)")
        k["status_vor_korrektur"] = "cancelled"

    with open(HIST, "w", encoding="utf-8") as f:
        json.dump(hist, f, indent=2)
    print("{} Eintraege korrigiert und geschrieben.".format(len(zu_fixen)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
