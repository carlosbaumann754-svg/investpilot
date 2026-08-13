"""Zwischencheck-25-Wecker — meldet EINMAL, wenn der Soak-Zaehler 25 erreicht (R-B53).

WARUM ES DIESES SKRIPT GIBT
===========================
Seit dem Carlos-Entscheid vom 05.08.2026 gibt es kein Cutover-Datum mehr,
sondern Zaehler-Meilensteine: Zwischencheck bei 25 sauberen Round-Trips,
Go/No-Go bei 80. Ein Zaehler-Meilenstein ohne Wecker wuerde erst beim
naechsten manuellen Statuscheck auffallen — und Session-gebundene Wecker
sind verboten (Memory-Regel: Monitore sterben mit der Session, R-B43).
Also: Host-Cron, taeglich nach Boersenschluss, feuert genau EINE
Pushover-Meldung, sobald n >= 25, und schweigt danach fuer immer.

WIE ES LAEUFT
-------------
Host-Cron via stdin-Pattern (Skript ist NICHT im Container-Image noetig):
  15 20 * * 1-5 docker exec -i -e SENTRY_DSN= investpilot \
      python3 - < /opt/investpilot/scripts/zwischencheck_trigger.py \
      >> /opt/investpilot/logs/zwischencheck25.log 2>&1
(20:15 UTC = 22:15 CH, direkt nach US-Boersenschluss.)

Zaehlung = exakt dieselbe Quelle wie Dashboard-Soak-Karte und Gatekeeper:
app.roundtrip_metrics.clean_roundtrip_stats seit SOAK_START (eine
Definition, nicht zwei). Marker-File im gemounteten data/-Volume
ueberlebt Container-Restarts und wandert mit ins Backup.

Bewusst KEIN Alarm-Kanal-Missbrauch: Priority 0 (normale Meldung, kein
Notfall) — der Meilenstein ist eine gute Nachricht, kein Vorfall.
"""
import json
import os

# R-B53 (13.08.2026): Marker fuer den Bauplan-Generator (Host-Skript,
# laeuft per stdin im Container — siehe Docstring).
AUDIT_METADATA = {
    "purpose": (
        "Zwischencheck-25-Wecker: Host-Cron zaehlt taeglich nach Boersenschluss "
        "die sauberen Soak-Round-Trips (gleiche Quelle wie Soak-Karte/Gatekeeper) "
        "und meldet EINMALIG per Pushover, wenn die Marke 25 erreicht ist — der "
        "Meilenstein loest sich selbst aus, statt auf den naechsten manuellen "
        "Statuscheck zu warten. Danach dauerhaft still (Marker-File)."
    ),
    "config_section": "alerts.pushover (nur lesend, fuer die Meldung)",
    "state_files": ["zwischencheck25_state.json"],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "R-B53 (13.08.2026)",
}

TARGET = 25
SOAK_START = "2026-07-21T12:00:00"   # identisch mit web/app.py Soak-Karte (R-B15)
STATE_FILE = os.environ.get(
    "ZWISCHENCHECK_STATE_FILE", "/app/data/zwischencheck25_state.json")
TRADE_HISTORY = os.environ.get(
    "ZWISCHENCHECK_TRADE_HISTORY", "/app/data/trade_history.json")
CONFIG_FILE = os.environ.get(
    "ZWISCHENCHECK_CONFIG_FILE", "/app/data/config.json")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"


def decide(n_clean, already_fired):
    """Reine Entscheidungslogik — einzeln getestet, keine Seiteneffekte.

    Returns (aktion, begruendung):
      'waiting'  Zaehler unter der Marke
      'fire'     Marke erreicht und noch nie gemeldet -> genau jetzt melden
      'silent'   bereits gemeldet -> nie wieder
    """
    if n_clean is None:
        return "waiting", "Zaehler nicht lesbar — keine Meldung auf Verdacht"
    if n_clean < TARGET:
        return "waiting", f"{n_clean}/{TARGET} — noch nicht erreicht"
    if already_fired:
        return "silent", "bereits gemeldet — Meilenstein feuert nur einmal"
    return "fire", f"{n_clean}/{TARGET} erreicht"


def _pushover(title, message):
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
                 "message": message, "priority": 0}
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

    fired = False
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            fired = bool((json.load(f) or {}).get("fired_at"))
    except Exception:
        fired = False   # unlesbar/fehlt -> lieber einmal doppelt als nie

    aktion, grund = decide(n, fired)
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] {aktion}: {grund}")

    if aktion == "fire" and not DRY_RUN:
        pf = stats.get("pf")
        net = stats.get("net_usd")
        _pushover(
            "InvestPilot: Zwischencheck 25 erreicht",
            f"{n} saubere Round-Trips seit Soak-Start (netto {net} USD, "
            f"PF {pf}). Beim naechsten Claude-Check das vorregistrierte "
            "Protokoll fahren: docs/ZWISCHENCHECK_25_PROTOKOLL.md. "
            "Kein Handlungsbedarf am Bot — reiner Meilenstein.")
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"fired_at": datetime.now().isoformat(), "n": n}, f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
