"""Meilenstein-Wecker des Soaks — meldet 25 / 50 / 80 je genau EINMAL (R-B53/58).

WARUM ES DIESES SKRIPT GIBT
===========================
Seit dem Carlos-Entscheid vom 05.08.2026 gibt es kein Cutover-Datum mehr,
sondern Zaehler-Meilensteine. Ein Zaehler-Meilenstein ohne Wecker wuerde erst
beim naechsten manuellen Statuscheck auffallen — und Session-gebundene Wecker
sind verboten (Memory-Regel: Monitore sterben mit der Session, R-B43).

MEILENSTEINE (jeder feuert genau einmal, Marker im State-File):
  25  Zwischencheck (R-B53; GEFEUERT 26.08.2026, Protokoll gefahren 27.08.)
  50  FUTILITY-CHECK (R-B58, Carlos-Freigabe 27.08.2026, BINDEND):
      Ist PF(%-Basis) < 0.48 — der p01-Glueckspfad-Untergrenze der eigenen
      Referenz bei n=50 — ist der Bot schlechter als 99 von 100
      Zufallspfaden der nachweislich guten Backtest-Strategie -> STOPP-
      Empfehlung, Carlos entscheidet. EIN einziger Blick mit 1%-Schwelle,
      damit die Gesamt-Fehlerrate sauber bleibt (kein Optional Stopping).
  80  GO/NO-GO (bindende Kriterien im Protokoll-Doc, inkl. Abbruch-Kriterium
      R-B57): vorregistriertes Protokoll fahren.

WIE ES LAEUFT
-------------
Host-Cron via stdin-Pattern (Skript ist NICHT im Container-Image noetig),
Mo-Fr 20:15 UTC = 22:15 CH, Log logs/zwischencheck25.log.
Zaehlung = exakt dieselbe Quelle wie Dashboard-Soak-Karte und Gatekeeper
(clean_roundtrip_stats). Marker-File im gemounteten data/-Volume (im Backup).
Legacy-Migration: das alte Format {"fired_at": ...} des 25er-Weckers gilt
als fired_25.
"""
import json
import os

# R-B53/58: Marker fuer den Bauplan-Generator (Host-Skript, stdin-Pattern).
AUDIT_METADATA = {
    "purpose": (
        "Meilenstein-Wecker des Soaks: Host-Cron zaehlt taeglich nach "
        "Boersenschluss die sauberen Round-Trips (gleiche Quelle wie "
        "Soak-Karte/Gatekeeper) und meldet 25 (Zwischencheck), 50 "
        "(BINDENDER Futility-Check: PF%<0.48 = schlechter als 99% der "
        "eigenen Glueckspfade -> Stopp-Empfehlung) und 80 (Go/No-Go) je "
        "genau EINMAL per Pushover. Danach dauerhaft still (Marker-File)."
    ),
    "config_section": "alerts.pushover (nur lesend, fuer die Meldung)",
    "state_files": ["zwischencheck25_state.json"],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "R-B53 (13.08.2026), R-B58 Futility-50 + Go/No-Go-80 (27.08.2026)",
}

SOAK_START = "2026-07-21T12:00:00"   # identisch mit web/app.py Soak-Karte (R-B15)
FUTILITY_GRENZE_PF_PCT = 0.48        # p01 der R-B18-Referenz bei n=50 (R-B58)
MEILENSTEINE = ((25, "fired_25"), (50, "fired_50"), (80, "fired_80"))

STATE_FILE = os.environ.get(
    "ZWISCHENCHECK_STATE_FILE", "/app/data/zwischencheck25_state.json")
TRADE_HISTORY = os.environ.get(
    "ZWISCHENCHECK_TRADE_HISTORY", "/app/data/trade_history.json")
CONFIG_FILE = os.environ.get(
    "ZWISCHENCHECK_CONFIG_FILE", "/app/data/config.json")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"


def decide(n_clean, state):
    """Reine Entscheidungslogik — einzeln getestet, keine Seiteneffekte.

    Returns (aktion, begruendung, meilenstein):
      'waiting'  Zaehler unter dem naechsten unerreichten Meilenstein
      'fire'     Meilenstein erreicht und noch nie gemeldet
      'silent'   alle Meilensteine gemeldet
    Legacy: {"fired_at": ...} (altes 25er-Format) zaehlt als fired_25.
    """
    if n_clean is None:
        return "waiting", "Zaehler nicht lesbar — keine Meldung auf Verdacht", None
    state = state or {}
    for ziel, key in MEILENSTEINE:
        fired = bool(state.get(key)) or (ziel == 25 and bool(state.get("fired_at")))
        if n_clean >= ziel and not fired:
            return "fire", f"{n_clean}/{ziel} erreicht", ziel
    naechster = next((z for z, _k in MEILENSTEINE if n_clean < z), None)
    if naechster is None:
        return "silent", "alle Meilensteine gemeldet", None
    return "waiting", f"{n_clean}/{naechster} — noch nicht erreicht", None


def meldung_fuer(meilenstein, n, stats):
    """Baut (titel, text, prioritaet) fuer den erreichten Meilenstein."""
    pf_pct = stats.get("pf_pct")
    net = stats.get("net_usd")
    if meilenstein == 25:
        return ("InvestPilot: Zwischencheck 25 erreicht",
                f"{n} saubere Round-Trips (netto {net} USD, PF% {pf_pct}). "
                "Vorregistriertes Protokoll fahren: "
                "docs/ZWISCHENCHECK_25_PROTOKOLL.md.", 0)
    if meilenstein == 50:
        if pf_pct is None:
            return ("InvestPilot: FUTILITY-CHECK 50 — PF nicht berechenbar",
                    f"{n} Round-Trips, aber PF%-Basis fehlt — manuell pruefen "
                    "(R-B58).", 1)
        if pf_pct < FUTILITY_GRENZE_PF_PCT:
            return ("InvestPilot: FUTILITY-STOPP-SIGNAL bei 50",
                    f"PF% {pf_pct} < {FUTILITY_GRENZE_PF_PCT} — schlechter als "
                    "99% der Glueckspfade der eigenen Referenz (R-B58, "
                    "bindend). Empfehlung: Soak STOPPEN, Carlos entscheidet "
                    f"beim naechsten Check. Netto {net} USD.", 1)
        return ("InvestPilot: Futility-Check 50 BESTANDEN",
                f"PF% {pf_pct} >= {FUTILITY_GRENZE_PF_PCT} (netto {net} USD) "
                "— weiter bis 80. Kein Handlungsbedarf.", 0)
    return ("InvestPilot: GO/NO-GO-MARKE 80 ERREICHT",
            f"{n} saubere Round-Trips (netto {net} USD, PF% {pf_pct}). Beim "
            "naechsten Claude-Check das bindende Go/No-Go-Protokoll fahren "
            "(inkl. Abbruch-Kriterium R-B57).", 1)


def _pushover(title, message, priority=0):
    import urllib.parse
    import urllib.request
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            po = ((json.load(f) or {}).get("alerts") or {}).get("pushover") or {}
        user, token = po.get("user_key"), po.get("api_token")
        if not user or not token:
            print("  Pushover: keine Zugaenge in config.json")
            return
        daten = {"token": token, "user": user, "title": title,
                 "message": message, "priority": priority}
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json",
            data=urllib.parse.urlencode(daten).encode())
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"  Pushover fehlgeschlagen (non-fatal): {e}")


def main() -> int:
    import sys
    from datetime import datetime
    sys.path.insert(0, "/app")
    from app.roundtrip_metrics import clean_roundtrip_stats

    try:
        with open(TRADE_HISTORY, encoding="utf-8") as f:
            hist = json.load(f) or []
        stats = clean_roundtrip_stats(hist, SOAK_START)
        n = stats.get("n")
    except Exception as e:
        print(f"Zaehlung fehlgeschlagen: {e}")
        n, stats = None, {}

    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f) or {}
    except Exception:
        state = {}   # unlesbar/fehlt -> lieber einmal doppelt als nie

    aktion, grund, meilenstein = decide(n, state)
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] {aktion}: {grund}")

    if aktion == "fire" and not DRY_RUN:
        titel, text, prio = meldung_fuer(meilenstein, n, stats)
        _pushover(titel, text, priority=prio)
        state[f"fired_{meilenstein}"] = datetime.now().isoformat()
        state[f"n_bei_{meilenstein}"] = n
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
