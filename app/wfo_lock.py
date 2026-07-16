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
- demo_trading.max_positions  (NEU v37e+, nur via manual_overrides)

Manuelle Overrides (v37e+, 02.07.2026 — Post-Soak-Rekalibrierung Schritt B)
---------------------------------------------------------------------------
``data/manual_lock_overrides.json`` uebersteuert die WFO-Mode fuer bewusst
rekalibrierte Params. Anlass: die WFO-Locks stammen vom ALT-TA-Backtester und
sind fuer den neuen Fundamental-Motor fehl-skaliert (min_scanner_score=40 =
bot_score>=40 = stack>=70 -> nur ~11 Namen -> Bot friert bei 11 Positionen ein,
$369k Cash brach). Overrides tragen die via signal_stack_backtester validierten
Werte (SL -8, min_scanner_score 25, max_positions 15) und haben Vorrang, BIS
eine echte Neu-Motor-WFO (Task #4) wfo_status.json neu baselined. Separates File,
damit der monatliche WFO-Cron sie nicht ueberschreibt.

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

AUDIT_METADATA = {
    "purpose": "WFO-Lock: schuetzt bewusst gesetzte Strategie-Params (stop_loss_pct, "
               "take_profit_pct, min_scanner_score, max_positions) vor Auto-Apply-"
               "Ueberschreibung durch Optimizer/ML/Backtest/Watchdog/Cloud-Restore. "
               "enforce_locks() greift bei jedem save_config, boot_drift_check() beim "
               "Bot-Start. data/manual_lock_overrides.json uebersteuert die (ggf. "
               "veraltete Alt-Motor-) WFO-Window-Mode mit validierten Post-Soak-Werten.",
    "config_section": None,
    "state_files": ["wfo_status.json", "manual_lock_overrides.json"],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": "detect_drift(config) == {} (Live-Config matcht die Locks)",
    "added_in": "v37r (WFO-Lock), v37e+ (manual_overrides + max_positions-Lock)",
}

#: WFO-Lock-Definitionen. (param_key_in_wfo, config_path_dotted, conservative_picker)
#: conservative_picker entscheidet bei Tie welcher Wert gewaehlt wird:
#:   "min" = niedrigster Wert (strenger SL = naeher zu null = -3 schlaegt -5)
#:   "max" = hoechster Wert (strenger Filter = 50 schlaegt 40)
LOCKED_KEYS = [
    ("stop_loss_pct", "demo_trading.stop_loss_pct", "max"),  # max: -3 > -5 (näher zu 0)
    ("take_profit_pct", "demo_trading.take_profit_pct", "min"),  # v37ct: min = konservativ (frueher Gewinn-Lock)
    ("min_scanner_score", "scanner.min_scanner_score", "max"),
    # v37h Tab-Audit-Day-2 (12.05.2026, Q3-1): zweiter Pfad fuer min_scanner_score
    # damit Backtester (liest demo_trading.min_scanner_score) und Live-Scanner
    # (liest scanner.min_scanner_score) konsistent gelockt sind. Vorher: 30 vs
    # 40 unsynchron -> Backtest-Live-Divergenz im WFO-Sharpe.
    ("min_scanner_score", "demo_trading.min_scanner_score", "max"),
    # v37e+ (02.07.2026, Post-Soak-Rekalibrierung Schritt B): max_positions als
    # Deployment-Control gelockt. Hat KEINE WFO-Window-Daten -> greift NUR via
    # manual_overrides (data/manual_lock_overrides.json). Picker "min" = konservativ.
    ("max_positions", "demo_trading.max_positions", "min"),
    # v37e+ (16.07.2026): die Rekalibrierungs-Werte, die NICHT im demo_trading/scanner
    # liegen, gegen Cloud-Restore-Revert schuetzen (nur via manual_overrides):
    # - min_risk_reward_ratio: der R/R-Gate-Fix (2.0->1.4), der die Buys entblockte;
    #   faellt er weg -> Code-Default 2.0 -> ALLE Aktien-Buys wieder blockiert.
    # - max_positions_by_capital: die Tier-Map ist der EFFEKTIVE Positions-Cap
    #   (resolve_max_positions nutzt sie, nicht demo_trading.max_positions);
    #   Revert auf '999999':20 -> Deployment driftet Richtung ~90%.
    ("min_risk_reward_ratio", "leverage.min_risk_reward_ratio", "max"),
    ("max_positions_by_capital", "portfolio_sizing.max_positions_by_capital", "min"),
]


# ============================================================
# READ: Manuelle Overrides (Post-Soak-Rekalibrierung)
# ============================================================

def _load_manual_overrides() -> dict[str, Any]:
    """Liest data/manual_lock_overrides.json — bewusst gesetzte, validierte
    Strategie-Werte, die die (ggf. veraltete Alt-Motor-) WFO-Mode uebersteuern.

    v37e+ (02.07.2026): Anlass = Post-Soak-Rekalibrierung. Die WFO-Locks stammten
    vom Alt-TA-Backtester (min_scanner_score=40/50, SL=-5) und sind fuer den neuen
    Fundamental-Signal-Stack-Motor fehl-skaliert (bot_score=(stack-50)*2 -> nur ~11
    Namen ueber 40 -> Bot friert bei 11 Positionen ein). Diese Overrides tragen die
    via signal_stack_backtester validierten Werte und haben Vorrang, BIS eine echte
    Neu-Motor-WFO (Task #4) wfo_status.json neu baselined.

    Separates File (nicht wfo_status.json) damit der monatliche WFO-Cron die
    Overrides NICHT ueberschreibt. Keys mit '_'-Praefix (z.B. _meta) sind Doku.

    Returns:
        Dict {param_name: value}. Leer wenn File fehlt/unlesbar (Backward-Compat).
    """
    try:
        from app.config_manager import load_json
        ov = load_json("manual_lock_overrides.json") or {}
    except Exception as e:
        logger.debug(f"manual_lock_overrides.json nicht ladbar: {e}")
        return {}
    if not isinstance(ov, dict):
        return {}
    return {k: v for k, v in ov.items() if not str(k).startswith("_")}


# ============================================================
# READ: WFO-Locked Values aus wfo_status.json
# ============================================================

def get_wfo_locked_params() -> dict[str, Any]:
    """Liest die locked params aus dem letzten WFO-Run, ueberlagert mit
    manuellen Overrides (Post-Soak-Rekalibrierung, haben Vorrang).

    Returns:
        Dict {param_name: value} mit den Mode-Werten ueber alle WFO-Windows,
        danach _load_manual_overrides() drueber (manual gewinnt, kann auch Keys
        OHNE Window-Daten hinzufuegen, z.B. max_positions).
        Leeres Dict nur wenn weder Windows noch Overrides existieren.
    """
    try:
        from app.config_manager import load_json
        wfo = load_json("wfo_status.json") or {}
    except Exception as e:
        logger.warning(f"wfo_status.json nicht ladbar: {e}")
        wfo = {}

    if not isinstance(wfo, dict):
        wfo = {}

    windows = wfo.get("windows", []) if isinstance(wfo.get("windows"), list) else []

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

    # v37e+ (Post-Soak Schritt B): manuelle Overrides ueberlagern die WFO-Mode.
    # Bewusst gesetzte, via signal_stack_backtester validierte Werte gewinnen —
    # kann auch Params OHNE Window-Daten setzen (z.B. max_positions).
    for k, v in _load_manual_overrides().items():
        locked[k] = v

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
        Dict {config_path: {"expected": <wfo>, "actual": <config>, "param": ..., "config_path": ...}}
        nur fuer Keys mit Drift. Leeres Dict wenn alles passt oder keine WFO-Daten.

    Key is config_path (not param_name), weil mehrere LOCKED_KEYS-Eintraege
    denselben param_name teilen koennen (z.B. min_scanner_score wird sowohl in
    scanner.* als auch in demo_trading.* gelockt — Q3-1 Tab-Audit-Day-2).
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
                drifts[config_path] = {
                    "expected": expected, "actual": actual,
                    "param": param_name, "config_path": config_path,
                }
        elif expected != actual:
            drifts[config_path] = {
                "expected": expected, "actual": actual,
                "param": param_name, "config_path": config_path,
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
    for drift in drifts.values():
        config_path = drift["config_path"]
        old = drift["actual"]
        new = drift["expected"]
        _set_nested(config, config_path, new)
        changes.append({
            "param": drift["param"],
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
        f"{d['param']}@{d['config_path']}: live={d['actual']} aber WFO empfiehlt {d['expected']}"
        for d in drifts.values()
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
