"""
WFO-Lock (v37r) — Schutzmechanismus fuer WFO-empfohlene Strategie-Parameter.
==============================================================================

Problem: am 28.04.2026 wurden die WFO-Werte (stop_loss_pct=-3.0,
min_scanner_score=40) live in die Config geschrieben. Bis 30.04. waren sie
wieder zurueckgesetzt auf -5/None — vermutlich von einem Auto-Run
(Optimizer/ML/Watchdog) der die Config ueberschrieb. User merkte es
nur durch Zufall beim Dashboard-Check.

Loesung: Source-of-Truth fuer WFO-Empfehlungen ist data/wfo_status.json.
Vor jedem ``save_config()`` werden die WFO-locked Keys aus dem aktuellen
Save-Vorgang heraus auf die WFO-Werte zurueckgesetzt. Damit kann KEIN
Auto-Apply-Pfad (Optimizer, ML-Training, Backtest, Watchdog, Brain-Save,
Cloud-Restore) die WFO-Werte mehr ueberschreiben.

Plus: beim Bot-Start prueft der Scheduler einmal ob die laufende Config
mit den WFO-Werten matcht. Bei Drift -> Pushover-Alert + Auto-Restore.

Source of Truth
---------------
``data/wfo_status.json`` mit Struktur::

    {
        "windows": [
            {"best_params": {"stop_loss_pct": -3.0, "take_profit_pct": 12,
                             "min_scanner_score": 40}, ...},
            ...
        ]
    }

Aus den N Windows wird der Mode (haeufigster Wert) genommen — wenn z.B.
in 5/5 Windows SL=-3 als best gewaehlt wurde, ist das der Lock-Wert.
Bei Tie wird der konservativste Wert genommen (niedrigster SL = strenger,
hoechster min_scanner_score = strenger).

Locked Keys
-----------
- demo_trading.stop_loss_pct  (im Backtester benannt 'stop_loss_pct')
- demo_trading.take_profit_pct (NEU v37ct)
- scanner.min_scanner_score   (in der Live-Config aliased)

v37ct (2026-05-03): take_profit_pct jetzt auch gelockt. Vorher BEWUSST
ausgenommen mit Begruendung 'WFO-Range war 9-18, kein klarer Modus'.
Aber: heutiger WFO-Run bestaetigt 60%% Konsens fuer TP=15 (3/5 Windows).
Der Mode-basierte Lock-Mechanismus kann das auch handhaben.
Live-Discovery: TP war 18.0 ohne Audit-Spur — vermutlich Initial-Default
oder pre-v37r Optimizer-Override. Pre-Cutover-Aufraeum-Item.
Picker 'min' = konservativ (frueher Gewinn sichern bei Tie).
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: WFO-Lock-Definitionen. (param_key_in_wfo, config_path_dotted, conservative_picker)
#: conservative_picker entscheidet bei Tie welcher Wert gewaehlt wird:
#:   "min" = niedrigster Wert (strenger SL = naeher zu null = -3 schlaegt -5)
#:   "max" = hoechster Wert (strenger Filter = 50 schlaegt 40)
LOCKED_KEYS = [
    ("stop_loss_pct", "demo_trading.stop_loss_pct", "max"),  # max: -3 > -5 (näher zu 0)
    ("take_profit_pct", "demo_trading.take_profit_pct", "min"),  # v37ct: min = konservativ (frueher Gewinn-Lock)
    ("min_scanner_score", "scanner.min_scanner_score", "max"),
]


# ============================================================
# READ: WFO-Locked Values aus wfo_status.json
# ============================================================

def get_wfo_locked_params() -> dict[str, Any]:
    """Liest die locked params aus dem letzten WFO-Run.

    Returns:
        Dict {param_name: value} mit den Mode-Werten ueber alle WFO-Windows.
        Leeres Dict wenn wfo_status.json fehlt oder keine Windows hat.
    """
    try:
        from app.config_manager import load_json
        wfo = load_json("wfo_status.json") or {}
    except Exception as e:
        logger.warning(f"wfo_status.json nicht ladbar: {e}")
        return {}

    if not isinstance(wfo, dict):
        return {}

    windows = wfo.get("windows", []) if isinstance(wfo.get("windows"), list) else []
    if not windows:
        return {}

    locked: dict[str, Any] = {}
    for param_name, _, picker in LOCKED_KEYS:
        # Sammle Werte ueber alle Windows
        values = []
        for w in windows:
            bp = w.get("best_params", {}) if isinstance(w, dict) else {}
            if isinstance(bp, dict) and param_name in bp:
                values.append(bp[param_name])

        if not values:
            continue

        # Mode (haeufigster Wert)
        counter = Counter(values)
        max_count = max(counter.values())
        candidates = [v for v, c in counter.items() if c == max_count]

        # Bei Tie: conservative picker
        if len(candidates) == 1:
            locked[param_name] = candidates[0]
        elif picker == "min":
            locked[param_name] = min(candidates)
        elif picker == "max":
            locked[param_name] = max(candidates)
        else:
            locked[param_name] = candidates[0]

    return locked


# ============================================================
# READ: aktueller Wert aus Config
# ============================================================

def _get_nested(d: dict, dotted_path: str) -> Any:
    """Liest geschachtelten Wert. Returns None wenn nicht vorhanden."""
    cur: Any = d
    for key in dotted_path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _set_nested(d: dict, dotted_path: str, value: Any) -> None:
    """Setzt geschachtelten Wert. Erstellt fehlende Keys."""
    parts = dotted_path.split(".")
    cur = d
    for key in parts[:-1]:
        if key not in cur or not isinstance(cur[key], dict):
            cur[key] = {}
        cur = cur[key]
    cur[parts[-1]] = value


# ============================================================
# DETECT: Drift zwischen Config und WFO
# ============================================================

def detect_drift(config: dict) -> dict[str, dict]:
    """Vergleicht Config gegen WFO-Locks.

    Returns:
        Dict {param_name: {"expected": <wfo>, "actual": <config>, "config_path": ...}}
        nur fuer Keys mit Drift. Leeres Dict wenn alles passt oder keine WFO-Daten.
    """
    locked = get_wfo_locked_params()
    if not locked:
        return {}

    drifts: dict[str, dict] = {}
    for param_name, config_path, _ in LOCKED_KEYS:
        if param_name not in locked:
            continue
        expected = locked[param_name]
        actual = _get_nested(config, config_path)
        # Float-Vergleich tolerant
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            if abs(float(expected) - float(actual)) > 1e-6:
                drifts[param_name] = {
                    "expected": expected, "actual": actual,
                    "config_path": config_path,
                }
        elif expected != actual:
            drifts[param_name] = {
                "expected": expected, "actual": actual,
                "config_path": config_path,
            }
    return drifts


# ============================================================
# ENFORCE: WFO-Locks erzwingen vor save_config
# ============================================================

def enforce_locks(config: dict) -> list[dict]:
    """Setzt WFO-Locks im Config-Dict (in-place).

    Returns:
        Liste der vorgenommenen Aenderungen (fuer Audit-Log).
        Leere Liste wenn keine Aenderungen noetig waren.

    Idempotent: kann beliebig oft gerufen werden.
    """
    drifts = detect_drift(config)
    if not drifts:
        return []

    changes = []
    for param_name, drift in drifts.items():
        config_path = drift["config_path"]
        old = drift["actual"]
        new = drift["expected"]
        _set_nested(config, config_path, new)
        changes.append({
            "param": param_name,
            "path": config_path,
            "old": old,
            "new": new,
        })

    if changes:
        # Audit-Trail im Config selbst dokumentieren (max 50 Eintraege)
        audit = config.setdefault("_audit", {})
        log_list = audit.setdefault("wfo_lock_enforcements", [])
        from datetime import datetime, timezone
        log_list.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "changes": changes,
        })
        # Rolling-Cap
        if len(log_list) > 50:
            audit["wfo_lock_enforcements"] = log_list[-50:]

        logger.warning(
            f"WFO-Lock greift: {len(changes)} Drift(s) korrigiert: "
            + ", ".join(f"{c['param']}: {c['old']!r} -> {c['new']!r}" for c in changes)
        )

    return changes


# ============================================================
# BOOT-CHECK
# ============================================================

def boot_drift_check(*, send_alert: bool = True, auto_restore: bool = True) -> dict:
    """Pruefe beim Bot-Start ob Live-Config mit WFO-Locks matcht.

    Args:
        send_alert: Wenn True, Pushover-Alert bei Drift.
        auto_restore: Wenn True, Drift via save_config korrigieren.

    Returns:
        Dict mit drift-Details, restored-Liste, alert-sent.
    """
    try:
        from app.config_manager import load_config, save_config
    except Exception as e:
        return {"error": f"config_manager nicht ladbar: {e}"}

    config = load_config()
    drifts = detect_drift(config)

    result: dict = {
        "drifts_detected": len(drifts),
        "drifts": drifts,
        "restored": [],
        "alert_sent": False,
    }

    if not drifts:
        logger.info("Boot-Drift-Check: Config matcht WFO-Locks (alles gruen)")
        return result

    # Drift-Details fuer Logs + Alerts
    drift_summary = ", ".join(
        f"{p}: live={d['actual']} aber WFO empfiehlt {d['expected']}"
        for p, d in drifts.items()
    )
    logger.warning(f"Boot-Drift-Check: {len(drifts)} Drift(s) — {drift_summary}")

    if auto_restore:
        changes = enforce_locks(config)
        try:
            save_config(config)
            result["restored"] = changes
            logger.warning(f"Boot-Drift-Check: {len(changes)} Param(e) auto-restored")
        except Exception as e:
            logger.error(f"Boot-Drift-Auto-Restore failed: {e}")

    if send_alert:
        try:
            from app.alerts import send_alert as _send
            msg = (f"WFO-DRIFT bei Bot-Start erkannt: {drift_summary}. "
                   + (f"Auto-Restore aktiv ({len(result['restored'])} Param(e) korrigiert)."
                      if result["restored"] else "Manuell pruefen!"))
            _send(msg, level="WARNING")
            result["alert_sent"] = True
        except Exception as e:
            logger.warning(f"WFO-Drift-Alert konnte nicht gesendet werden: {e}")

    return result
