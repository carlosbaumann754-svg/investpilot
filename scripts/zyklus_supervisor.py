"""Zyklus-Supervisor — Selbstheilung bei stehender Handelsschleife (R-B49).

WARUM ES DIESES SKRIPT GIBT
===========================
Vorfall 24.07.2026: Die Handelsschleife hing 3h35min an einem blockierten
Netzwerk-Abruf — mitten in der letzten Handelsstunde. Der Watchdog ALARMIERTE
korrekt alle 10 Minuten, aber nichts HEILTE: Herzschlag, SL/TP-Checks,
Zusammenfassung und Snapshot standen, bis der Socket von selbst starb.
Die Scan-Deadline (R-B44) deckt seither den konkreten Pfad; dieses Skript ist
der GENERELLE Selbstheiler und laut Roadmap Pflicht vor Real-Money.

WIE ES ARBEITET
---------------
Laeuft als HOST-Cron alle 5 Minuten (ein haengender Prozess kann sich nicht
selbst neu starten — deshalb bewusst ausserhalb des Containers):

  1. Liest den Herzschlag direkt aus dem gemounteten Volume
     (/opt/investpilot/data/alert_state.json — kein docker exec noetig,
     funktioniert auch wenn der Container selbst nicht mehr antwortet).
  2. Herzschlag aelter als 20 Min -> `docker restart investpilot`.
     (Der Scheduler schreibt ihn jeden ~5-Min-Zyklus, rund um die Uhr; der
     taegliche Neustart und Deploys erzeugen Luecken von nur 2-4 Min.)
  3. SCHUTZ GEGEN RESTART-SCHLEIFEN: nach einem Eingriff 30 Min Cooldown;
     maximal 3 Neustarts in 6 Stunden — danach gibt der Supervisor auf und
     eskaliert EINMAL per Pushover-Emergency (heilt ein Neustart nicht, ist
     es ein Problem, das Haende braucht; endloses Durchstarten wuerde es nur
     verschleiern).
  4. Jeder Eingriff wird per Pushover gemeldet (Zugaenge aus data/config.json,
     alerts.pushover — vom Host lesbar).

ZEITZONEN (die Falle der Woche): Der Herzschlag ist NAIVE Container-Zeit =
Europe/Zurich; der Host laeuft UTC. Verglichen wird deshalb ausschliesslich
in Zurich-Zeit.

TESTBARKEIT: decide() ist reine Logik; HEARTBEAT_FILE / STATE_FILE / NOW_ISO /
DRY_RUN kommen als Env-Overrides — die Suite testet jeden Zweig ohne Docker.

CRON (Host):
  */5 * * * * flock -n /tmp/zyklus_supervisor.lock python3 \
      /opt/investpilot/scripts/zyklus_supervisor.py \
      >> /opt/investpilot/logs/zyklus_supervisor.log 2>&1
"""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Zurich")

STALE_SEC = 20 * 60          # Herzschlag aelter -> Eingriff
COOLDOWN_SEC = 30 * 60       # nach einem Neustart: erst mal wirken lassen
MAX_RESTARTS = 3             # ... in ROLLING_WINDOW_SEC, danach aufgeben
ROLLING_WINDOW_SEC = 6 * 3600

HEARTBEAT_FILE = os.environ.get(
    "HEARTBEAT_FILE", "/opt/investpilot/data/alert_state.json")
STATE_FILE = os.environ.get(
    "STATE_FILE", "/opt/investpilot/supervisor_state.json")
CONFIG_FILE = os.environ.get(
    "CONFIG_FILE", "/opt/investpilot/data/config.json")
CONTAINER = os.environ.get("SUPERVISOR_CONTAINER", "investpilot")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"


# ---------------------------------------------------------------- Logik ---

def decide(heartbeat_age_sec, state, now_epoch):
    """Reine Entscheidungslogik — einzeln getestet, keine Seiteneffekte.

    Returns (aktion, begruendung):
      'ok'        Herzschlag frisch
      'cooldown'  stale, aber juengster Eingriff wirkt evtl. noch
      'restart'   stale -> Container neu starten
      'give_up'   stale trotz MAX_RESTARTS Eingriffen -> einmal eskalieren
      'silent'    stale + bereits aufgegeben + eskaliert -> nichts wiederholen
    """
    if heartbeat_age_sec is None:
        return "ok", "kein Herzschlag lesbar — kein Eingriff auf Verdacht"
    if heartbeat_age_sec <= STALE_SEC:
        return "ok", f"Herzschlag {heartbeat_age_sec:.0f}s alt"

    restarts = [t for t in (state.get("restarts") or [])
                if now_epoch - t <= ROLLING_WINDOW_SEC]
    if restarts and now_epoch - max(restarts) < COOLDOWN_SEC:
        return "cooldown", (f"stale ({heartbeat_age_sec/60:.0f} min), letzter "
                            f"Neustart vor {(now_epoch - max(restarts))/60:.0f} min")
    if len(restarts) >= MAX_RESTARTS:
        if state.get("gave_up_at") and now_epoch - state["gave_up_at"] <= ROLLING_WINDOW_SEC:
            return "silent", "bereits eskaliert — keine Alert-Flut"
        return "give_up", (f"{len(restarts)} Neustarts in 6h ohne Heilung — "
                           "das braucht Haende, kein weiteres Durchstarten")
    return "restart", f"Herzschlag {heartbeat_age_sec/60:.0f} min alt (> 20)"


# ------------------------------------------------------------ Umgebung ---

def heartbeat_age_sec(now):
    try:
        with open(HEARTBEAT_FILE, encoding="utf-8") as f:
            hb = (json.load(f) or {}).get("last_heartbeat")
        if not hb:
            return None
        dt = datetime.fromisoformat(str(hb))
        if dt.tzinfo is None:                    # naive Container-Zeit = Zurich
            dt = dt.replace(tzinfo=TZ)
        return (now - dt).total_seconds()
    except Exception as e:
        print(f"  Herzschlag unlesbar: {e}")
        return None


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def pushover(title, message, emergency=False):
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            po = ((json.load(f) or {}).get("alerts") or {}).get("pushover") or {}
        user, token = po.get("user_key"), po.get("api_token")
        if not user or not token:
            print("  Pushover: keine Zugaenge in config.json")
            return
        daten = {"token": token, "user": user, "title": title,
                 "message": message, "priority": 2 if emergency else 1}
        if emergency:
            daten["retry"] = 60
            daten["expire"] = 600
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json",
            data=urllib.parse.urlencode(daten).encode())
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"  Pushover fehlgeschlagen (non-fatal): {e}")


def restart_container():
    if DRY_RUN:
        print(f"  DRY_RUN: wuerde `docker restart {CONTAINER}` ausfuehren")
        return True
    r = subprocess.run(["docker", "restart", "-t", "20", CONTAINER],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print(f"  docker restart FEHLGESCHLAGEN: {r.stderr.strip()[:200]}")
        return False
    return True


# ----------------------------------------------------------------- Main ---

def main() -> int:
    now_iso = os.environ.get("NOW_ISO")
    now = (datetime.fromisoformat(now_iso).replace(tzinfo=TZ) if now_iso
           else datetime.now(TZ))
    now_epoch = now.timestamp()

    alter = heartbeat_age_sec(now)
    state = load_state()
    aktion, grund = decide(alter, state, now_epoch)
    print(f"[{now:%Y-%m-%d %H:%M}] {aktion}: {grund}")

    if aktion == "restart":
        ok = restart_container()
        if ok:
            state.setdefault("restarts", []).append(now_epoch)
            state["restarts"] = [t for t in state["restarts"]
                                 if now_epoch - t <= ROLLING_WINDOW_SEC]
            state.pop("gave_up_at", None)
            if not DRY_RUN:
                save_state(state)
                pushover("InvestPilot Supervisor",
                         f"Handelsschleife stand ({grund}) — Container wurde "
                         "automatisch neu gestartet. Der Bot sollte in ~1 Min "
                         "wieder Herzschlaege senden.")
    elif aktion == "give_up":
        state["gave_up_at"] = now_epoch
        if not DRY_RUN:
            save_state(state)
            pushover("InvestPilot Supervisor — MANUELL EINGREIFEN",
                     f"{grund}. Der Supervisor startet NICHT weiter durch.",
                     emergency=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
