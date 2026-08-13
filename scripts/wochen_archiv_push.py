"""Wochen-Archiv-Push — Off-VPS-Kopie der zwei unersetzlichen Datendateien (R-B54).

WARUM ES DIESES SKRIPT GIBT
===========================
Audit-Finding 13.08.2026: `signal_score_history.json` (taegliche Point-in-Time-
Scores) und `signal_pit_snapshots.json` (PIT-Kurs/Signal-Cache der monatlichen
Stack-Karte) existierten AUSSCHLIESSLICH auf der VPS-Platte — das taegliche
Host-tar liegt auf derselben Platte, und der Zyklus-Gist darf sie nicht tragen
(er wird ~550x/Tag gepusht; 2.3 MB je Push waeren >1 GB Upload/Tag).
Beide sind laut eigener Doku (R-B25/R-B41) nicht rekonstruierbar, ohne
Look-Ahead-Bias einzuschleppen: Verlust waere endgueltig.

WIE ES LAEUFT
-------------
Host-Cron woechentlich (So 04:30 UTC, nach dem 04:00-tar), stdin-Pattern:
  30 4 * * 0 docker exec -i -e SENTRY_DSN= investpilot python3 - \
      < /opt/investpilot/scripts/wochen_archiv_push.py \
      >> /opt/investpilot/logs/wochen_archiv_push.log 2>&1

Eigenes, dediziertes Gist (per GIST_DESCRIPTION identifiziert, beim ersten
Lauf angelegt) — bewusst getrennt vom hochfrequenten Zyklus-Gist. Token:
GITHUB_TOKEN aus der Container-Umgebung (gist-Scope reicht).
Ein Wochen-Rhythmus verliert im schlimmsten Fall 6 Tage Archiv — akzeptiert;
taeglich waere Gist-History-Bloat (Gist speichert jede Revision voll).
"""
import json
import os
import urllib.request

# R-B54 (13.08.2026): Marker fuer den Bauplan-Generator (Host-Cron-Skript).
AUDIT_METADATA = {
    "purpose": (
        "Wochen-Archiv-Push: sichert die zwei UNERSETZLICHEN Datendateien "
        "(signal_score_history.json, signal_pit_snapshots.json) woechentlich "
        "in ein dediziertes GitHub-Gist — vorher existierten sie nur auf der "
        "VPS-Platte (Single-Point-of-Failure; tar liegt auf derselben Platte). "
        "Bewusst getrennt vom ~550x/Tag gepushten Zyklus-Gist."
    ),
    "config_section": None,
    "state_files": ["signal_score_history.json", "signal_pit_snapshots.json"],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "R-B54 (13.08.2026)",
}

GIST_DESCRIPTION = "InvestPilot Wochen-Archiv (unersetzliche PIT-Daten, auto-managed)"
ARCHIV_DATEIEN = ["signal_score_history.json", "signal_pit_snapshots.json"]
DATA_DIR = os.environ.get("ARCHIV_DATA_DIR", "/app/data")
API = "https://api.github.com"


def _req(url, data=None, method=None, token=None):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        # .env wird im Container nach os.environ geladen; Fallback direkt lesen
        try:
            for line in open("/app/.env", encoding="utf-8"):
                if line.startswith("GITHUB_TOKEN="):
                    token = line.split("=", 1)[1].strip()
        except Exception:
            pass
    if not token:
        print("FEHLER: kein GITHUB_TOKEN — Push uebersprungen")
        return 1

    files = {}
    for name in ARCHIV_DATEIEN:
        pfad = os.path.join(DATA_DIR, name)
        try:
            with open(pfad, encoding="utf-8") as f:
                inhalt = f.read()
            files[name] = {"content": inhalt}
            print(f"  {name}: {len(inhalt)} bytes")
        except Exception as e:
            print(f"  WARNUNG: {name} nicht lesbar ({e}) — bleibt aus diesem Push")
    if not files:
        print("FEHLER: keine Archiv-Datei lesbar")
        return 1

    # Bestehendes Archiv-Gist suchen (per Beschreibung), sonst anlegen
    gist_id = None
    try:
        for g in _req(f"{API}/gists?per_page=100", token=token):
            if g.get("description") == GIST_DESCRIPTION:
                gist_id = g["id"]
                break
    except Exception as e:
        print(f"FEHLER: Gist-Liste nicht abrufbar: {e}")
        return 1

    payload = json.dumps({
        "description": GIST_DESCRIPTION,
        "public": False,
        "files": files,
    }).encode()
    try:
        if gist_id:
            _req(f"{API}/gists/{gist_id}", data=payload, method="PATCH", token=token)
            print(f"OK: Wochen-Archiv aktualisiert (gist {gist_id[:8]}..., "
                  f"{len(files)} Dateien)")
        else:
            neu = _req(f"{API}/gists", data=payload, method="POST", token=token)
            print(f"OK: Wochen-Archiv-Gist NEU angelegt ({neu.get('id', '?')[:8]}..., "
                  f"{len(files)} Dateien)")
    except Exception as e:
        print(f"FEHLER: Gist-Push fehlgeschlagen: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
