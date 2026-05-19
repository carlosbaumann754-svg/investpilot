"""v37h+2 (17.05.2026) — Wöchentlicher Phantom-Feature / Health-Audit.

Continuous-Discovery-System fuer "Tools die intern kaputt sind aber im
Dashboard als aktiv erscheinen". Carlos's Entscheidung 17.05.: Option C
(Hybrid) — wöchentlicher Code-Audit hier + monatlicher LLM-Tief-Audit
via Spawn-Task-Chip.

ENTSTAND: 17.05.2026 nach Phantom-Feature-Sprint (9 Items gefixt).
Beobachtung: viele der Findings waren konkrete Patterns (stale-cache,
fehlende-API-key, leerer-empfehlungs-output), die mit deterministischen
Checks erkennbar gewesen waeren. Dieses Modul macht genau das.

CRON-INTEGRATION:
    0 13 * * 6  docker exec investpilot python -m app.health_audit

OUTPUT:
  - data/health_audit_state.json (fuer Diff-Tracking + Dashboard)
  - data/audits/health_audit_YYYY-MM-DD.md (Markdown-Report)
  - Pushover-Alert: NUR bei NEUEN Findings (= "Stille = OK")

USAGE:
    python -m app.health_audit            # full audit + alert
    python -m app.health_audit --dry-run  # nur ausgeben, kein State/Alert
"""
from __future__ import annotations

import ast
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# v37h+3 (Sprint-Tag-9, 19.05.2026): Self-Audit-Marker (rekursiv)
AUDIT_METADATA = {
    "purpose": "Weekly Health-Audit + Continuous-Phantom-Discovery + Module-Coverage (Reflection)",
    "config_section": None,
    "state_files": ["health_audit_state.json"],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "v37h+2 (17.05.2026), Module-Coverage v37h+3 (19.05.2026)",
}
# Hinweis: health_audit laeuft als Linux-Cron (scripts/health_audit_cron.sh Sa 13 UTC),
# NICHT als scheduler-Background-Thread. Daher scheduler_hooks=[].


STATE_FILE = "health_audit_state.json"
REPORTS_DIR = "audits"
PUSHOVER_THROTTLE_HOURS = 1  # max 1 Alert pro Stunde bei Re-Run

# v37h+3 (Sprint-Tag-9, 19.05.2026): Reflection-basiertes Audit-Coverage.
# Jedes "AUDIT_REQUIRED"-Modul muss AUDIT_METADATA dict am Top der Datei
# haben. Audit liest via AST (keine Side-Effects beim Import) und generiert
# Health-Checks pro Modul automatisch. Wenn ein Required-Modul keinen
# Marker hat -> CRITICAL-Alert. Wenn ein NEUES Modul ohne Marker in
# AUDIT_REQUIRED-Liste added wird -> Pre-Commit-Hook blockt strukturell.
AUDIT_REQUIRED_MODULES = {
    # Core-Trading-Logic
    "trader", "risk_manager", "scheduler", "market_scanner", "ibkr_client",
    "brain", "hedging", "market_context",
    "ibkr_contract_resolver",  # R-A28 (19.05.2026): R-A15 Quote-Fallback-Code, kritisch fuer Order-Execution
    # Safety + Monitoring
    "health_audit", "self_test", "alerts", "wfo_drift_watchdog",
    "sentry_setup",  # R-A13 (19.05.2026): mission-critical Pre-Cutover-Sicherheitsnetz
    # Periodic-Jobs
    "asset_discovery", "walk_forward_optimizer", "universe_health_watcher",
}


def _extract_audit_metadata_via_ast(py_file: Path) -> Optional[dict]:
    """Liest AUDIT_METADATA via AST ohne Module zu importieren.

    Verhindert Side-Effects (DB-Connections, IBKR-Init, etc.) waehrend
    Discovery. AST literal_eval = nur primitive Werte erlaubt (dict, str,
    int, list, None, bool) -> sicher gegen Code-Execution.

    Returns:
        AUDIT_METADATA dict wenn vorhanden + parseable, sonst None.
    """
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "AUDIT_METADATA":
                    try:
                        return ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        return None
    return None


def _discover_module_metadata() -> dict[str, dict]:
    """Scannt app/*.py + sammelt AUDIT_METADATA von jedem Modul.

    Returns:
        dict {module_name: metadata_dict} fuer alle Module mit Marker.
    """
    metadata = {}
    app_dir = Path(__file__).parent
    for py_file in sorted(app_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        meta = _extract_audit_metadata_via_ast(py_file)
        if meta is not None:
            metadata[py_file.stem] = meta
    return metadata


def _validate_metadata_schema(name: str, meta: dict) -> list[str]:
    """Validate dass AUDIT_METADATA korrekte Keys + Typen hat.

    Returns:
        Liste von Error-Strings (leer = valid).
    """
    errors = []
    required_keys = {"purpose", "added_in"}
    optional_keys = {"config_section", "state_files", "self_tests",
                     "scheduler_hooks", "health_check"}
    allowed_keys = required_keys | optional_keys

    for k in required_keys:
        if k not in meta:
            errors.append(f"missing required key '{k}'")

    for k in meta:
        if k not in allowed_keys:
            errors.append(f"unknown key '{k}' (allowed: {sorted(allowed_keys)})")

    # Type-Checks
    type_specs = {
        "purpose": str, "added_in": str, "config_section": (str, type(None)),
        "state_files": list, "self_tests": list,
        "scheduler_hooks": list, "health_check": (str, type(None)),
    }
    for k, expected in type_specs.items():
        if k in meta and not isinstance(meta[k], expected):
            errors.append(f"key '{k}' has wrong type "
                          f"(expected {expected}, got {type(meta[k]).__name__})")

    return errors


@dataclass
class HealthCheckResult:
    name: str
    category: str       # 'api', 'state', 'toggle', 'cron', 'gh', 'code'
    passed: bool
    severity: str       # 'critical' | 'warning' | 'info'
    message: str
    detail: dict = field(default_factory=dict)

    @property
    def status_icon(self) -> str:
        if self.passed:
            return "OK"
        return {"critical": "FAIL", "warning": "WARN", "info": "NOTE"}.get(
            self.severity, "?")


def _load_json_safe(filename: str) -> Optional[Any]:
    try:
        from app.config_manager import load_json
        return load_json(filename)
    except Exception as e:
        log.debug(f"load_json({filename}) failed: {e}")
        return None


def _ts_age_hours(ts_str: Optional[str]) -> Optional[float]:
    """Returns Stunden seit ISO-Timestamp, oder None wenn unparseable."""
    if not ts_str:
        return None
    try:
        s = str(ts_str).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return (datetime.now() - dt).total_seconds() / 3600
    except Exception:
        return None


# ============================================================
# CHECK GROUP 1: API-Keys (5 Checks)
# ============================================================

def check_api_keys() -> list[HealthCheckResult]:
    keys = [
        ("FINNHUB_API_KEY", "critical", "Insider/Earnings/Sentiment-Cascade"),
        ("ANTHROPIC_API_KEY", "warning", "Sentiment-Haiku-Layer"),
        ("POLYGON_API_KEY", "warning", "Crypto + Cross-Validation"),
        ("ALPHA_VANTAGE_API_KEY", "warning", "Forex + Fallback"),
        ("GITHUB_TOKEN", "warning", "Cloud-Restore + Workflows"),
    ]
    out = []
    for key_name, severity, purpose in keys:
        val = os.environ.get(key_name, "")
        if val and len(val) >= 8:
            out.append(HealthCheckResult(
                name=f"api_key_{key_name.lower()}",
                category="api", passed=True, severity="info",
                message=f"{key_name} gesetzt ({len(val)} chars, {purpose})",
            ))
        else:
            out.append(HealthCheckResult(
                name=f"api_key_{key_name.lower()}",
                category="api", passed=False, severity=severity,
                message=f"{key_name} FEHLT/leer — Auswirkung: {purpose} silent dead",
            ))
    return out


# ============================================================
# CHECK GROUP 2: State-File Freshness (8 Checks)
# ============================================================

_STATE_FILE_CHECKS = [
    # (filename, max_age_hours, severity, key_for_ts, purpose)
    ("brain_state.json", 24, "warning", None,
     "Bot-Cycle-Snapshots — wenn alt, Bot inactive"),
    ("universe_health.json", 48, "warning", "last_update",
     "Universe-Health-Watcher — wenn None, lief seit Setup nie"),
    ("wfo_status.json", 35 * 24, "warning", None,
     "WFO-Empfehlungen — wenn >35d, monatlicher Cron broken"),
    ("market_context.json", 6, "critical", "last_update",
     "VIX/F&G-Cache — wenn stale, Regime-Filter blind"),
    ("risk_state.json", 24 * 7, "warning", None,
     "Risk-State — keine harte Frequenz, aber sollte existieren"),
    ("pending_closes.json", 999999, "info", None,
     "Pending-Closes — kein Age-Check, nur Existenz"),
    ("trade_history.json", 999999, "info", None,
     "Trade-History — kein Age-Check"),
    ("cost_model_calibration.json", 14 * 24, "warning", "generated_at",
     "Cost-Model — wenn >14d, Calibrator-Cron broken oder kein Trade-Volume"),
]


def check_state_files() -> list[HealthCheckResult]:
    out = []
    for fname, max_age_h, severity, ts_key, purpose in _STATE_FILE_CHECKS:
        data = _load_json_safe(fname)
        if data is None or (isinstance(data, dict) and not data) or (isinstance(data, list) and not data):
            # Spezial: einige Files duerfen leer sein (pending_closes)
            if severity == "info":
                out.append(HealthCheckResult(
                    name=f"state_{fname.replace('.json','')}",
                    category="state", passed=True, severity="info",
                    message=f"{fname}: leer (OK fuer {purpose.split(' — ')[0]})",
                ))
            else:
                out.append(HealthCheckResult(
                    name=f"state_{fname.replace('.json','')}",
                    category="state", passed=False, severity=severity,
                    message=f"{fname} fehlt oder leer — {purpose}",
                ))
            continue

        # Age-Check wenn Key gegeben
        age_h = None
        if ts_key and isinstance(data, dict):
            age_h = _ts_age_hours(data.get(ts_key))
        elif fname == "brain_state.json" and isinstance(data, dict):
            # Spezial: brain_state hat performance_snapshots-Liste
            snaps = data.get("performance_snapshots") or []
            if snaps:
                age_h = _ts_age_hours(snaps[-1].get("timestamp"))

        if max_age_h >= 999999 or age_h is None:
            # No age-check oder kein TS-Key — nur Existenz
            out.append(HealthCheckResult(
                name=f"state_{fname.replace('.json','')}",
                category="state", passed=True, severity="info",
                message=f"{fname}: existiert ({len(data) if isinstance(data,(list,dict)) else '?'} entries)",
            ))
        elif age_h > max_age_h:
            out.append(HealthCheckResult(
                name=f"state_{fname.replace('.json','')}_freshness",
                category="state", passed=False, severity=severity,
                message=(f"{fname}: STALE — {age_h:.1f}h alt "
                         f"(threshold {max_age_h}h). {purpose}"),
                detail={"age_hours": round(age_h, 1), "threshold": max_age_h},
            ))
        else:
            out.append(HealthCheckResult(
                name=f"state_{fname.replace('.json','')}_freshness",
                category="state", passed=True, severity="info",
                message=f"{fname}: fresh ({age_h:.1f}h alt)",
                detail={"age_hours": round(age_h, 1)},
            ))
    return out


# ============================================================
# CHECK GROUP 3: Feature-Toggle vs Reality (6 Checks)
# ============================================================

def check_feature_toggles() -> list[HealthCheckResult]:
    out = []
    try:
        from app.config_manager import load_config
        cfg = load_config() or {}
    except Exception as e:
        out.append(HealthCheckResult(
            name="config_loadable", category="toggle", passed=False,
            severity="critical", message=f"config.json nicht ladbar: {e}",
        ))
        return out

    dt = cfg.get("demo_trading", {}) or {}
    mc = cfg.get("market_context", {}) or {}
    sc = cfg.get("scanner", {}) or {}
    hg = cfg.get("hedging", {}) or {}
    rm = cfg.get("risk_management", {}) or {}

    # 3a: use_ml_scoring vs Modell vorhanden
    use_ml = dt.get("use_ml_scoring", False)
    if use_ml:
        ml_meta = _load_json_safe("ml_model_meta.json") or {}
        has_model = bool(ml_meta) and ml_meta.get("model_path")
        out.append(HealthCheckResult(
            name="toggle_ml_scoring", category="toggle",
            passed=has_model, severity="warning",
            message=(f"use_ml_scoring=True + Modell vorhanden"
                     if has_model else
                     "use_ml_scoring=True ABER ml_model_meta.json fehlt — ML silent dead"),
        ))
    else:
        out.append(HealthCheckResult(
            name="toggle_ml_scoring", category="toggle", passed=True,
            severity="info", message="use_ml_scoring=False (ML default-off, OK)",
        ))

    # 3b: use_sentiment_filter vs API-Keys
    use_sent = mc.get("use_sentiment_filter", False)
    if use_sent:
        has_finnhub = bool(os.environ.get("FINNHUB_API_KEY"))
        has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
        passed = has_finnhub or has_anthropic
        out.append(HealthCheckResult(
            name="toggle_sentiment_filter", category="toggle",
            passed=passed, severity="warning",
            message=(f"use_sentiment_filter=True + API-Keys vorhanden "
                     f"(Finnhub={has_finnhub}, Anthropic={has_anthropic})"
                     if passed else
                     "use_sentiment_filter=True ABER beide API-Keys fehlen"),
        ))

    # 3c: insider_signal_enabled vs FINNHUB
    if sc.get("insider_signal_enabled"):
        has_finnhub = bool(os.environ.get("FINNHUB_API_KEY"))
        out.append(HealthCheckResult(
            name="toggle_insider_signals", category="toggle",
            passed=has_finnhub, severity="warning",
            message=("insider_signal_enabled=True + FINNHUB_API_KEY vorhanden"
                     if has_finnhub else
                     "insider_signal_enabled=True ABER FINNHUB_API_KEY fehlt — silent dead"),
        ))

    # 3d: hedging.enabled vs defensive_sectors
    if hg.get("enabled"):
        sectors = hg.get("defensive_sectors", [])
        passed = bool(sectors)
        out.append(HealthCheckResult(
            name="toggle_hedging", category="toggle",
            passed=passed, severity="warning",
            message=(f"hedging.enabled=True + {len(sectors)} defensive_sectors"
                     if passed else
                     "hedging.enabled=True ABER defensive_sectors leer"),
        ))

    # 3e: earnings_exit_enabled vs yfinance
    if mc.get("use_earnings_filter"):
        try:
            import yfinance  # noqa: F401
            yf_avail = True
        except ImportError:
            yf_avail = False
        out.append(HealthCheckResult(
            name="toggle_earnings_exit", category="toggle",
            passed=yf_avail, severity="warning",
            message=("use_earnings_filter=True + yfinance vorhanden"
                     if yf_avail else
                     "use_earnings_filter=True ABER yfinance nicht installiert"),
        ))

    # 3f: Cash-Reserve aktiv
    floor = rm.get("min_cash_reserve_chf", 0)
    pct = rm.get("min_cash_reserve_pct", 0)
    if floor > 0 or pct > 0:
        out.append(HealthCheckResult(
            name="toggle_cash_reserve", category="toggle", passed=True,
            severity="info",
            message=f"Cash-Reserve aktiv: max({floor} CHF, {pct*100:.1f}% Equity)",
        ))
    else:
        out.append(HealthCheckResult(
            name="toggle_cash_reserve", category="toggle", passed=False,
            severity="warning", message="Cash-Reserve deaktiviert (floor=0, pct=0)",
        ))

    return out


# ============================================================
# CHECK GROUP 4: Cron / Auto-Run Status (4 Checks)
# ============================================================

_BACKUP_DIR = "/var/backups/investpilot"
_LOG_DIR = "/var/log"


def _check_file_age(path: str, max_age_h: float, name: str, severity: str,
                    purpose: str) -> HealthCheckResult:
    p = Path(path)
    if not p.exists():
        # v37h+2: Audit laeuft im Docker-Container, aber /var/log + /var/backups
        # sind auf dem Host. Wenn der Pfad im Container nicht reachable ist:
        # info-Status (= passed) statt warning. Die echten Logs werden vom
        # Host-Cron-Wrapper + Self-Test-Card separat geprueft.
        return HealthCheckResult(
            name=name, category="cron", passed=True, severity="info",
            message=(f"{path} nicht im Container reachable (=Host-Pfad). "
                     "Status wird via Host-Cron-Wrapper + Self-Test geprueft."),
        )
    age_h = (datetime.now().timestamp() - p.stat().st_mtime) / 3600
    if age_h > max_age_h:
        return HealthCheckResult(
            name=name, category="cron", passed=False, severity=severity,
            message=(f"{path} ist {age_h:.1f}h alt (threshold {max_age_h}h). "
                     f"{purpose}"),
            detail={"age_hours": round(age_h, 1)},
        )
    return HealthCheckResult(
        name=name, category="cron", passed=True, severity="info",
        message=f"{path}: fresh ({age_h:.1f}h)",
    )


def check_cron_freshness() -> list[HealthCheckResult]:
    out = []
    # 4a: heutiger Backup vorhanden? (Host-Pfad — Container-tolerant)
    backup_dir = Path(_BACKUP_DIR)
    if not backup_dir.exists():
        # Container kann /var/backups nicht sehen — info-Status, Host-Cron prueft
        out.append(HealthCheckResult(
            name="cron_backup_today", category="cron", passed=True,
            severity="info",
            message=(f"{_BACKUP_DIR} nicht im Container reachable. "
                     "Status wird via Host-Cron + Restore-Test geprueft."),
        ))
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        today_backup = backup_dir / f"state_{today}_040001.tar.gz"
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        yesterday_backup = backup_dir / f"state_{yesterday}_040001.tar.gz"
        if today_backup.exists():
            out.append(HealthCheckResult(
                name="cron_backup_today", category="cron", passed=True,
                severity="info",
                message=f"Heute-Backup vorhanden: {today_backup.name}",
            ))
        elif yesterday_backup.exists():
            if datetime.utcnow().hour < 4:
                out.append(HealthCheckResult(
                    name="cron_backup_today", category="cron", passed=True,
                    severity="info",
                    message=f"Heute-Backup pending (04:00 UTC), gestern OK",
                ))
            else:
                out.append(HealthCheckResult(
                    name="cron_backup_today", category="cron", passed=False,
                    severity="critical",
                    message=f"Heute-Backup FEHLT (heute>04:00 UTC)! Hard-Gate #4 broken",
                ))
        else:
            out.append(HealthCheckResult(
                name="cron_backup_today", category="cron", passed=False,
                severity="critical",
                message="Backup-Cron broken: weder heute noch gestern Backup",
            ))

    # 4b: Self-Test-Log frisch?
    out.append(_check_file_age(
        f"{_LOG_DIR}/self-test.log", max_age_h=2,
        name="cron_self_test", severity="warning",
        purpose="Self-Test-Cron stuendlich — wenn >2h, Cron broken",
    ))

    # 4c: Reconcile-Log frisch?
    out.append(_check_file_age(
        "/opt/investpilot/logs/reconcile.log", max_age_h=1,
        name="cron_reconcile", severity="warning",
        purpose="Reconcile-Cron alle 30 Min — wenn >1h, Cron broken",
    ))

    # 4d: IBKR-Watchdog-Log frisch?
    out.append(_check_file_age(
        f"{_LOG_DIR}/ibkr-session-watchdog.log", max_age_h=0.5,
        name="cron_ibkr_watchdog", severity="warning",
        purpose="IBKR-Watchdog alle 3 Min — wenn >30min, Cron broken",
    ))

    return out


# ============================================================
# CHECK GROUP 5: Code-Anti-Patterns (4 Checks)
# ============================================================

def check_code_antipatterns() -> list[HealthCheckResult]:
    out = []
    repo = Path(__file__).parent.parent

    # 5a: legacy/ Files duerfen nicht von app/* importiert werden
    legacy_files = list((repo / "legacy").glob("*.py")) + list((repo / "legacy").glob("*.skeleton"))
    legacy_modules = [f.stem.split(".")[0] for f in legacy_files]
    imports_into_legacy = []
    for py in (repo / "app").glob("*.py"):
        try:
            content = py.read_text(encoding="utf-8")
        except Exception:
            continue
        for mod in legacy_modules:
            if f"from app.{mod}" in content or f"import {mod}" in content:
                imports_into_legacy.append(f"{py.name} -> {mod}")
    out.append(HealthCheckResult(
        name="code_no_legacy_imports", category="code",
        passed=not imports_into_legacy, severity="warning",
        message=("Keine app/-Imports auf legacy/-Module"
                 if not imports_into_legacy else
                 f"WARN: {len(imports_into_legacy)} imports auf legacy: {imports_into_legacy[:3]}"),
    ))

    # 5b: Pairs-Trading nicht wieder aktiv
    pairs_in_app = (repo / "app" / "pairs_trading.py").exists()
    out.append(HealthCheckResult(
        name="code_pairs_trading_legacy", category="code",
        passed=not pairs_in_app, severity="warning",
        message=("Pairs-Trading in legacy/ (korrekt)"
                 if not pairs_in_app else
                 "WARN: app/pairs_trading.py existiert wieder — bug?"),
    ))

    # 5c: BrainDeprecatedError nicht aus Cycle aufgerufen
    cycle_file = (repo / "app" / "brain.py")
    if cycle_file.exists():
        content = cycle_file.read_text(encoding="utf-8", errors="ignore")
        # Suche nach learn_rules()/optimize_strategy() im aktiven Code-Pfad
        # (=ausserhalb von def-Definitionen). Heuristik: in _run_brain_cycle_locked.
        in_cycle = False
        for line in content.split("\n"):
            if "def _run_brain_cycle_locked" in line:
                in_cycle = True
                continue
            if in_cycle and line.startswith("def "):
                in_cycle = False
            if in_cycle and ("learn_rules()" in line or "optimize_strategy()" in line):
                if not line.strip().startswith("#"):
                    out.append(HealthCheckResult(
                        name="code_no_deprecated_in_cycle", category="code",
                        passed=False, severity="critical",
                        message=f"DEPRECATED Function im Brain-Cycle aufgerufen: {line.strip()}",
                    ))
                    break
        else:
            out.append(HealthCheckResult(
                name="code_no_deprecated_in_cycle", category="code",
                passed=True, severity="info",
                message="learn_rules + optimize_strategy nicht im aktiven Cycle",
            ))

    # 5d: WFO-Lock-Keys konsistent mit live config
    try:
        from app.wfo_lock import LOCKED_KEYS, detect_drift
        from app.config_manager import load_config
        cfg = load_config() or {}
        drifts = detect_drift(cfg)
        out.append(HealthCheckResult(
            name="code_wfo_lock_no_drift", category="code",
            passed=not drifts, severity="warning",
            message=("WFO-Lock: keine Drift" if not drifts else
                     f"WFO-Lock Drift: {list(drifts.keys())}"),
        ))
    except Exception as e:
        out.append(HealthCheckResult(
            name="code_wfo_lock_no_drift", category="code",
            passed=False, severity="info",
            message=f"WFO-Lock-Check fehlgeschlagen: {e}",
        ))

    return out


# ============================================================
# CHECK GROUP 6: Module-Coverage (v37h+3, 19.05.2026)
# ============================================================
# Reflection-basierte Audit-Coverage. Jedes Required-Modul muss AUDIT_METADATA
# haben. Audit generiert dann automatisch pro Modul: Config-Section-Existence,
# State-File-Freshness, Self-Test-Linkage, Scheduler-Hook-Linkage, optional
# Custom health_check. Self-extending: neue Module mit Marker bekommen
# automatisch volle Coverage ohne Code-Change in health_audit.py.

def _check_config_section_exists(section_path: str, cfg: dict) -> tuple[bool, str]:
    """Dot-notation Pfad ('risk_management.symbol_concentration') in dict navigieren."""
    parts = section_path.split(".")
    node = cfg
    for p in parts:
        if not isinstance(node, dict) or p not in node:
            return False, f"missing path '{section_path}' at part '{p}'"
        node = node[p]
    return True, f"section '{section_path}' present"


def _check_health_function(module_name: str, fn_name: str) -> HealthCheckResult:
    """Lazy-Import + Aufruf von Module-spezifischer health_check-Funktion."""
    try:
        import importlib
        mod = importlib.import_module(f"app.{module_name}")
        fn = getattr(mod, fn_name, None)
        if fn is None or not callable(fn):
            return HealthCheckResult(
                name=f"module_{module_name}_health_check",
                category="module_coverage", passed=False, severity="warning",
                message=f"declared health_check '{fn_name}' not callable on app.{module_name}",
            )
        result = fn()
        healthy = bool(result.get("healthy", False)) if isinstance(result, dict) else False
        detail = result.get("detail", "") if isinstance(result, dict) else str(result)
        return HealthCheckResult(
            name=f"module_{module_name}_health_check",
            category="module_coverage", passed=healthy,
            severity="warning" if not healthy else "info",
            message=f"{module_name}.{fn_name}: {detail}",
        )
    except Exception as e:
        return HealthCheckResult(
            name=f"module_{module_name}_health_check",
            category="module_coverage", passed=False, severity="warning",
            message=f"{module_name}.{fn_name} crashed: {type(e).__name__}: {e}",
        )


def check_module_coverage() -> list[HealthCheckResult]:
    """Reflection-basierte Audit-Coverage fuer alle App-Module.

    Pro Modul (mit AUDIT_METADATA-Marker):
      - Schema-Validitaet (Required-Keys vorhanden + korrekte Typen)
      - Config-Section existiert (wenn deklariert)
      - State-Files existieren (wenn deklariert)
      - Self-Tests sind in ALL_TESTS registriert (wenn deklariert)
      - Scheduler-Hooks existieren in scheduler.py (wenn deklariert)
      - Custom health_check ausgefuehrt (wenn deklariert)

    Plus: required-Module ohne Marker -> CRITICAL.
    """
    out: list[HealthCheckResult] = []
    metadata = _discover_module_metadata()

    # 6a: required Module mit fehlendem Marker -> CRITICAL
    missing = AUDIT_REQUIRED_MODULES - set(metadata.keys())
    if missing:
        out.append(HealthCheckResult(
            name="module_coverage_required_complete",
            category="module_coverage", passed=False, severity="critical",
            message=(f"{len(missing)} required Module ohne AUDIT_METADATA: "
                     f"{sorted(missing)} — neue Tools muessen Marker setzen"),
            detail={"missing_required": sorted(missing)},
        ))
    else:
        out.append(HealthCheckResult(
            name="module_coverage_required_complete",
            category="module_coverage", passed=True, severity="info",
            message=(f"alle {len(AUDIT_REQUIRED_MODULES)} required Module "
                     f"haben AUDIT_METADATA"),
        ))

    # 6b: Cross-Validation pro registriertem Modul
    try:
        from app.config_manager import load_config
        cfg = load_config() or {}
    except Exception as e:
        cfg = {}
        out.append(HealthCheckResult(
            name="module_coverage_config_load",
            category="module_coverage", passed=False, severity="warning",
            message=f"config.json nicht ladbar: {e}",
        ))

    try:
        from app.self_test import ALL_TESTS
        known_self_tests = {fn.__name__ for fn in ALL_TESTS}
    except Exception:
        known_self_tests = set()

    try:
        scheduler_src = (Path(__file__).parent / "scheduler.py").read_text(encoding="utf-8")
    except Exception:
        scheduler_src = ""

    for mod_name, meta in metadata.items():
        # Schema-Validitaet
        schema_errors = _validate_metadata_schema(mod_name, meta)
        if schema_errors:
            out.append(HealthCheckResult(
                name=f"module_{mod_name}_metadata_schema",
                category="module_coverage", passed=False, severity="warning",
                message=f"{mod_name} AUDIT_METADATA invalid: {'; '.join(schema_errors)}",
            ))
            continue  # weitere checks ohne valides Schema sinnlos

        # Config-Section Existence
        cs = meta.get("config_section")
        if cs:
            ok, detail = _check_config_section_exists(cs, cfg)
            out.append(HealthCheckResult(
                name=f"module_{mod_name}_config",
                category="module_coverage", passed=ok,
                severity="warning" if not ok else "info",
                message=f"{mod_name}: {detail}",
            ))

        # State-Files Existence
        for sf in meta.get("state_files", []) or []:
            data = _load_json_safe(sf)
            exists = data is not None
            out.append(HealthCheckResult(
                name=f"module_{mod_name}_state_{sf.replace('.json','')}",
                category="module_coverage", passed=exists,
                severity="info" if exists else "warning",
                message=(f"{mod_name}: state file {sf} {'present' if exists else 'missing'}"),
            ))

        # Self-Test-Linkage
        for st in meta.get("self_tests", []) or []:
            in_registry = st in known_self_tests
            out.append(HealthCheckResult(
                name=f"module_{mod_name}_selftest_{st}",
                category="module_coverage", passed=in_registry,
                severity="warning" if not in_registry else "info",
                message=(f"{mod_name}: self-test {st} "
                         f"{'in ALL_TESTS' if in_registry else 'NOT REGISTERED'}"),
            ))

        # Scheduler-Hook-Linkage (Variable-Name muss in scheduler.py erscheinen)
        for hook in meta.get("scheduler_hooks", []) or []:
            present = hook in scheduler_src
            out.append(HealthCheckResult(
                name=f"module_{mod_name}_scheduler_{hook}",
                category="module_coverage", passed=present,
                severity="warning" if not present else "info",
                message=(f"{mod_name}: scheduler hook {hook} "
                         f"{'defined' if present else 'NOT FOUND in scheduler.py'}"),
            ))

        # Custom health_check
        hc = meta.get("health_check")
        if hc:
            out.append(_check_health_function(mod_name, hc))

    return out


# ============================================================
# RUN FULL AUDIT
# ============================================================

def run_full_audit(dry_run: bool = False) -> dict:
    """Fuehrt alle Checks aus, persistiert State + Markdown, sendet Alert.

    Args:
        dry_run: Wenn True, kein State-Save + kein Pushover.

    Returns:
        dict mit total, passed, failed, new_findings, alert_sent.
    """
    log.info("=" * 55)
    log.info("HEALTH-AUDIT START (dry_run=%s)", dry_run)
    log.info("=" * 55)

    results: list[HealthCheckResult] = []
    for fn in (check_api_keys, check_state_files, check_feature_toggles,
               check_cron_freshness, check_code_antipatterns,
               check_module_coverage):  # v37h+3 Sprint-Tag-9
        try:
            results.extend(fn())
        except Exception as e:
            log.error(f"Audit-Group {fn.__name__} failed: {e}", exc_info=True)
            results.append(HealthCheckResult(
                name=f"audit_group_{fn.__name__}", category="meta",
                passed=False, severity="warning",
                message=f"Audit-Group crashed: {type(e).__name__}: {e}",
            ))

    failed = [r for r in results if not r.passed]
    passed_count = len(results) - len(failed)

    log.info(f"Audit-Resultate: {passed_count}/{len(results)} OK, {len(failed)} fail")
    for r in failed:
        log.warning(f"  [{r.status_icon}] {r.name}: {r.message}")

    # Diff gegen letzten State
    prev_state = _load_json_safe(STATE_FILE) or {}
    prev_failed_names = set(prev_state.get("failed_names", []))
    new_failed = [r for r in failed if r.name not in prev_failed_names]
    resolved_names = list(prev_failed_names - set(r.name for r in failed))

    summary = {
        "ran_at": datetime.now().isoformat(),
        "total_checks": len(results),
        "passed": passed_count,
        "failed": len(failed),
        "new_findings_count": len(new_failed),
        "resolved_count": len(resolved_names),
        "new_findings": [r.name for r in new_failed],
        "resolved": resolved_names,
        "failed_names": [r.name for r in failed],
        "results": [asdict(r) for r in results],
    }

    if not dry_run:
        # Persistiere State
        from app.config_manager import save_json
        save_json(STATE_FILE, summary)

        # Markdown-Report
        try:
            report_path = _write_markdown_report(results, summary)
            log.info(f"Markdown-Report: {report_path}")
        except Exception as e:
            log.warning(f"Markdown-Report failed: {e}")

        # Pushover nur bei NEUEN Findings
        if new_failed:
            _send_pushover_alert(new_failed, resolved_names)
            summary["alert_sent"] = True
        else:
            log.info("Keine neuen Findings — kein Pushover (Stille = OK)")
            summary["alert_sent"] = False

    return summary


def _write_markdown_report(results: list[HealthCheckResult],
                           summary: dict) -> str:
    """Schreibt Markdown-Report nach data/audits/health_audit_YYYY-MM-DD.md."""
    from app.config_manager import get_data_path
    audits_dir = get_data_path(REPORTS_DIR)
    audits_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    fpath = audits_dir / f"health_audit_{today}.md"

    lines = [
        f"# Health-Audit Report {today}",
        f"",
        f"**Ran at:** {summary['ran_at']}",
        f"**Status:** {summary['passed']}/{summary['total_checks']} OK · "
        f"{summary['failed']} fail · "
        f"{summary['new_findings_count']} neu · "
        f"{summary['resolved_count']} resolved",
        f"",
    ]
    if summary.get("new_findings"):
        lines.append("## 🆕 Neue Findings")
        for name in summary["new_findings"]:
            r = next((x for x in results if x.name == name), None)
            if r:
                lines.append(f"- **{r.name}** ({r.severity}): {r.message}")
        lines.append("")
    if summary.get("resolved"):
        lines.append("## ✅ Resolved seit letztem Audit")
        for name in summary["resolved"]:
            lines.append(f"- {name}")
        lines.append("")
    # Alle Findings nach Kategorie
    by_cat = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)
    for cat in sorted(by_cat.keys()):
        lines.append(f"## Kategorie: {cat}")
        for r in by_cat[cat]:
            lines.append(f"- [{r.status_icon}] **{r.name}**: {r.message}")
        lines.append("")
    fpath.write_text("\n".join(lines), encoding="utf-8")
    return str(fpath)


def _send_pushover_alert(new_failed: list[HealthCheckResult],
                         resolved: list[str]) -> None:
    """Pushover-WARNING bei neuen Findings."""
    try:
        from app.alerts import send_alert
        n = len(new_failed)
        criticals = sum(1 for r in new_failed if r.severity == "critical")
        msg_parts = [
            f"Health-Audit: {n} NEUE Findings"
            + (f" ({criticals} CRITICAL)" if criticals else "")
            + "."
        ]
        for r in new_failed[:5]:
            msg_parts.append(f"- {r.severity.upper()}: {r.message[:120]}")
        if n > 5:
            msg_parts.append(f"(+{n-5} weitere — Details im Dashboard)")
        if resolved:
            msg_parts.append(f"({len(resolved)} resolved seit letztem Audit)")
        send_alert("\n".join(msg_parts),
                   level="ERROR" if criticals else "WARNING")
    except Exception as e:
        log.warning(f"Health-Audit Pushover failed: {e}")


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Audit ausgeben aber kein State/Alert")
    p.add_argument("--json", action="store_true",
                   help="Output als JSON (sonst human-readable)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    summary = run_full_audit(dry_run=args.dry_run)

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(f"\nHealth-Audit Summary:")
        print(f"  Total:        {summary['total_checks']}")
        print(f"  Passed:       {summary['passed']}")
        print(f"  Failed:       {summary['failed']}")
        print(f"  New Findings: {summary['new_findings_count']}")
        print(f"  Resolved:     {summary['resolved_count']}")
        print(f"  Alert sent:   {summary.get('alert_sent', False)}")

    # Exit-Code: 0 wenn alles OK, 1 wenn Failures, 2 wenn neue Findings
    return 0 if summary["failed"] == 0 else (
        2 if summary["new_findings_count"] > 0 else 1)


if __name__ == "__main__":
    sys.exit(main())
