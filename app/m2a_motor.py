"""M2A-Motor-Logik (R-B66) — Horizont-Exits, Kauf-Fenster, Geerbt-Schutz.

Bindendes Regelwerk: docs/M2A_MOTOR_REGELWERK.md (Carlos-Freigabe
28.08.2026). Kernaenderung ggue. M0: Positionen werden 126 HANDELSTAGE
gehalten und dann zum Tagesschluss verkauft — kein Stop-Loss, kein
Trailing, keine Tranchen, kein Earnings-Exit. Einzige Absicherung ist der
broker-seitige E6-Katastrophen-Stop (-40%, VPS-Ausfall-Schutz).

ARCHITEKTUR: Der Code traegt BEIDE Modi. `ist_aktiv(config)` schaltet —
der Montags-Schnitt ist damit ein reiner Config/Locks-Flip mit Rollback,
kein Deploy. Alt-Positionen stehen in data/m2a_geerbt.json und laufen
unter den M0-Exits aus (Bestandsschutz, zaehlen fuer keine M2a-Messung).
"""
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("M2aMotor")

AUDIT_METADATA = {
    "purpose": (
        "M2A-Motor-Logik (bindendes Regelwerk R-B65/66): Horizont-Exit nach "
        "126 Handelstagen (einziger Software-Exit; SL/Trailing/Earnings fuer "
        "M2a-Positionen deaktiviert), Kauf-Fenster Handelstag 1-3 des Monats, "
        "Anlauf-Limit Neukaeufe/Monat, Geerbt-Schutz fuer Alt-Positionen "
        "(data/m2a_geerbt.json, laufen unter M0-Exits aus). Flag-gated via "
        "config['m2a']['aktiv'] — Schnitt = Config-Flip, kein Deploy."
    ),
    "config_section": "m2a",
    "state_files": ["m2a_geerbt.json"],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "R-B66 (28.08.2026)",
}

HORIZON_HANDELSTAGE = 126
KAUF_FENSTER_HANDELSTAGE = 3       # Neukaeufe nur Handelstag 1-3 des Monats
ANLAUF_MAX_NEUKAEUFE_PRO_MONAT = 5  # Z4-Staffelung waehrend des Depot-Aufbaus

_GEERBT_PATH = Path(__file__).resolve().parent.parent / "data" / "m2a_geerbt.json"
_geerbt_cache = {"mtime": None, "ids": frozenset()}


def ist_aktiv(config) -> bool:
    """True sobald der Montags-Schnitt das Flag gesetzt hat."""
    return bool(((config or {}).get("m2a") or {}).get("aktiv"))


def horizon_handelstage(config) -> int:
    return int(((config or {}).get("m2a") or {}).get(
        "horizon_handelstage", HORIZON_HANDELSTAGE))


def geerbte_ids() -> frozenset:
    """Position-IDs der Alt-Positionen (Bestandsschutz). mtime-gecacht."""
    global _geerbt_cache
    try:
        mtime = os.path.getmtime(_GEERBT_PATH)
    except OSError:
        return frozenset()
    if _geerbt_cache["mtime"] != mtime:
        try:
            with open(_GEERBT_PATH, encoding="utf-8") as f:
                d = json.load(f) or {}
            ids = frozenset(str(x) for x in (d.get("position_ids") or []))
            _geerbt_cache = {"mtime": mtime, "ids": ids}
        except Exception as e:
            log.warning(f"m2a_geerbt.json unlesbar ({e}) — Bestandsschutz "
                        "greift NICHT, M2a-Regeln gelten fuer alle")
            return frozenset()
    return _geerbt_cache["ids"]


def ist_geerbt(position_id) -> bool:
    return str(position_id) in geerbte_ids()


def handelstage_zwischen(start: date, ende: date) -> int:
    """Mo-Fr-Tage in (start, ende] — US-Feiertage bewusst ignoriert
    (zaehlt sie mit -> verkauft ~2-3 Tage 'zu frueh' pro Halbjahr;
    konservativ und im Regelwerk-Sinn 'fix 126 Handelstage' vertretbar)."""
    if start >= ende:
        return 0
    tage = 0
    d = start
    while d < ende:
        d += timedelta(days=1)
        if d.weekday() < 5:
            tage += 1
    return tage


def handelstage_seit(open_dt, heute: date = None) -> int | None:
    if open_dt is None:
        return None
    heute = heute or datetime.now().date()
    start = open_dt.date() if hasattr(open_dt, "date") else open_dt
    return handelstage_zwischen(start, heute)


def handelstag_im_monat(heute: date = None) -> int:
    """1-basierter Handelstag (Mo-Fr) des laufenden Monats."""
    heute = heute or datetime.now().date()
    erster = heute.replace(day=1)
    return handelstage_zwischen(erster - timedelta(days=1), heute)


def im_kauf_fenster(heute: date = None, config=None) -> bool:
    fenster = int(((config or {}).get("m2a") or {}).get(
        "kauf_fenster_handelstage", KAUF_FENSTER_HANDELSTAGE))
    heute = heute or datetime.now().date()
    if heute.weekday() >= 5:
        return False
    return 1 <= handelstag_im_monat(heute) <= fenster


def neukaeufe_diesen_monat(trade_history: list, heute: date = None) -> int:
    """Zaehlt gefuellte SCANNER_BUYs des laufenden Monats (Anlauf-Limit)."""
    heute = heute or datetime.now().date()
    prefix = heute.strftime("%Y-%m")
    schlecht = ("cancelled", "rejected", "failed")
    n = 0
    for t in trade_history or []:
        if not isinstance(t, dict):
            continue
        if (str(t.get("timestamp", "")).startswith(prefix)
                and str(t.get("action", "")).upper() == "SCANNER_BUY"
                and str(t.get("status", "")).lower() not in schlecht):
            n += 1
    return n


def anlauf_limit(config) -> int:
    return int(((config or {}).get("m2a") or {}).get(
        "max_neukaeufe_pro_monat", ANLAUF_MAX_NEUKAEUFE_PRO_MONAT))
