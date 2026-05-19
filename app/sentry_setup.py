"""Sentry Error-Tracking Setup (v37g, 08.05.2026).

WIESO:
======
Real-Money-Phase wird Errors werfen die nur via Stack-Trace + Context
diagnostizierbar sind: ib_insync-Drifts, Order-Reject-Edge-Cases,
Network-Timeouts, Race-Conditions in async-Code. Pushover ist fuer
Trading-Alerts (TRADE/MISSED_FILL/RECONCILE_DRIFT) — bleibt unveraendert.
Sentry sammelt automatisch Stack-Traces mit Local-Variables + Breadcrumbs.

DATENSCHUTZ:
============
Bot handelt mit echtem Geld + IBKR-Account-Daten. Wir SCHICKEN NICHT:
  - IBKR-Account-IDs (DUP108015, U-Konto)
  - Cash-Betraege oder Position-Werte
  - Trade-IDs / Order-IDs
  - Authentication-Tokens / API-Keys
  - eMail / Telefonnummer / Carlos's Privat-Daten

Filter via before_send-Callback unten.

AKTIVIERUNG:
============
1. config.json: {"sentry": {"enabled": true}}
2. VPS env var: SENTRY_DSN=<dsn-from-sentry-account>
3. Container restart

Wenn DSN fehlt ODER enabled=false: Setup ist No-Op (kein Crash).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

log = logging.getLogger("sentry_setup")


# v37h+3 (Sprint-Tag-9, 19.05.2026): Audit-Coverage-Marker (R-A12 Disziplin)
AUDIT_METADATA = {
    "purpose": "Sentry Error-Tracking: PII-Filter (IBKR-Accts/Cash/Tokens redacted), DSN aus env, FastAPI+Logging-Integrations, Init in beiden Prozessen (Web + Scheduler)",
    "config_section": "sentry",
    "state_files": [],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "Fr 08.05.2026 (Sentry-Setup vorgezogen), R-A13 scharfgeschaltet Sprint-Tag-9 (19.05.2026)",
}


# ============================================================
# Konfigurations-Lookup
# ============================================================

def _load_sentry_config() -> dict:
    """Liefert sentry-Sektion aus config.json (oder default-disabled)."""
    try:
        from app.config_manager import load_json
        cfg = load_json("config.json") or {}
        return cfg.get("sentry", {}) or {}
    except Exception as e:
        log.debug("Sentry-Config-Load fehlgeschlagen: %s", e)
        return {}


def _get_dsn() -> Optional[str]:
    """DSN aus env var. NICHT aus config.json — soll nicht im Repo landen."""
    return os.environ.get("SENTRY_DSN") or None


# ============================================================
# PII-Filter (Datenschutz)
# ============================================================

# Patterns die in Event-Bodies redacted werden (= Backup-Layer falls
# Sentry-SDK's eigene Filter durchrutschen).
_REDACT_PATTERNS = [
    # IBKR-Account-IDs (case-insensitive — falls jemand die IDs lowercased):
    # Paper: DU<letter><digits> (z.B. DUP108015), DU<digits>, DF<digits>
    # Real:  U<digits> (mind. 7) — "U" am Wortanfang plus 7+ Ziffern
    (re.compile(r"\bD[UF][A-Z]?\d{5,}\b", re.IGNORECASE), "[REDACTED_IBKR_ACCT]"),
    (re.compile(r"\bU\d{7,}\b", re.IGNORECASE), "[REDACTED_IBKR_ACCT]"),
    # Cash/Equity-Values mit Währungs-Suffix (CRIT für Real-Money-Phase)
    # Pattern: 12500.50 USD, $1,234.56, €100.00, 50,000 CHF, etc.
    (re.compile(r"\$?\s?\d{1,3}(?:[,]\d{3})*\.?\d*\s*(?:USD|EUR|CHF|GBP)\b", re.IGNORECASE),
     "[REDACTED_AMOUNT]"),
    (re.compile(r"\$\s?\d{1,3}(?:[,]\d{3})*(?:\.\d{2})?\b"), "[REDACTED_AMOUNT]"),
    # Pushover/API-Tokens (30+ Zeichen alphanumerisch)
    (re.compile(r"\b[a-zA-Z0-9]{30,}\b"), lambda m: m.group(0)[:8] + "[REDACTED]"),
    # Email-Adressen
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED_EMAIL]"),
]

# Felder die komplett rausgeworfen werden (auch wenn key existiert)
# Lowercase-vergleich (siehe _scrub_dict). v37g-review: erweitert um
# Cash/Position/Order-Felder + breitere Auth/Secret-Coverage.
_DROP_FIELDS = {
    # Auth/Secrets
    "user_key", "api_token", "pushover_user_key", "pushover_api_token",
    "telegram_token", "anthropic_api_key", "ibkr_password", "session_token",
    "jwt_secret", "password", "passwd", "pw", "secret", "private_key",
    "client_secret", "auth_header", "cookie", "authorization", "auth", "token",
    "key", "bearer",
    # Account-IDs (numerisch oder als String)
    "account", "acct", "account_id", "accountid", "ibkr_account",
    # Cash/Position/Trade-Werte (CRIT vor Real-Money)
    "cash", "nlv", "equity", "credit", "available_funds", "buying_power",
    "balance", "portfolio_value", "total_value", "unrealized_pnl",
    "realized_pnl", "position_value", "market_value", "cost_basis",
    "amount_usd",
    # Order/Trade-Identifiers
    "order_id", "orderid", "perm_id", "permid", "exec_id", "execid",
    "trade_id", "tradeid", "client_id", "clientid",
}


def _redact_string(s: Any) -> Any:
    """Redacted PII aus String. Konvertiert non-string types (int, bytes)
    zu String + check, damit numerische Account-IDs nicht durchsickern.

    v37g-review-fix LOW 7: int/bytes/etc. waren vorher unscanned.
    """
    if isinstance(s, str):
        for pat, repl in _REDACT_PATTERNS:
            s = pat.sub(repl, s)
        return s
    if isinstance(s, bytes):
        try:
            decoded = s.decode("utf-8", errors="replace")
            redacted = _redact_string(decoded)
            return redacted if redacted != decoded else s
        except Exception:
            return s
    if isinstance(s, int) and len(str(s)) >= 7:
        # Numerische IDs ab 7 Stellen koennten Account-IDs sein
        as_str = str(s)
        for pat, repl in _REDACT_PATTERNS:
            redacted = pat.sub(repl, as_str)
            if redacted != as_str:
                return redacted
        return s
    return s


def _scrub_dict(d: dict, depth: int = 0) -> dict:
    """Rekursiv durch Dict, redactet PII + dropt Secret-Felder.

    Max-Depth-Schutz gegen circular refs.
    """
    if depth > 8:
        return {"_truncated": "max_depth"}
    out = {}
    for k, v in d.items():
        if k.lower() in _DROP_FIELDS:
            out[k] = "[REDACTED_SECRET]"
            continue
        if isinstance(v, dict):
            out[k] = _scrub_dict(v, depth + 1)
        elif isinstance(v, list):
            out[k] = [_scrub_dict(item, depth + 1) if isinstance(item, dict)
                      else _redact_string(item) if isinstance(item, str)
                      else item
                      for item in v[:50]]  # max 50 items
        elif isinstance(v, str):
            out[k] = _redact_string(v)
        else:
            out[k] = v
    return out


# R-A20 (Sprint-Tag-9, 19.05.2026): Sentry-Noise-Filter.
# Bekannte ib_insync-Library-Quirks die als ERROR geloggt werden aber
# KEIN echter Bot-Bug sind. Reduziert Sentry-Issue-Noise damit nur
# actionable Events durchkommen.
#
# Anlass: Carlos's Beobachtung 19.05. abend - "diverse Fehlermeldungen
# in Sentry". Inspektion zeigte: 8 Events in 6h, davon 4 unique:
#   2x Error 300 (ib_insync cancelMktData-Quirk, harmlos)
#   1x Hohe Latenz 2019ms (Performance-Warning, kein Bug)
#   3x Stale ib.portfolio()-Eintrag USO (Boot-Race-Detection wirkt, OK)
# Alle 4 sind erwartete Patterns -> filter sie aus Sentry raus.

_SENTRY_NOISE_PATTERNS = (
    # IBKR-Quirk nach cancelMktData (Error-Code 300): tickerId existiert
    # nicht mehr weil Quote-Snapshot bereits cancelled. Harmlos, Order
    # geht trotzdem durch (siehe Logs: Pending→PreSubmitted→Submitted→Filled).
    "Error 300, reqId",
    "Can't find EId with tickerId",
    # Boot-Race-Detection (v37ce-Filter wirkt korrekt): Filter ueberspringt
    # stale ib.portfolio()-Eintraege nach Close-Fill ohne updatePortfolio-
    # Event. Das ist erwuenschtes Verhalten, kein Bug.
    "Stale ib.portfolio()-Eintrag uebersprungen",
    # Performance-Warning bei Quote-Fetch — kein Bot-Fehler, nur Latency-Info.
    # Wenn das systematisch wird, separater Alert via watchdog/cron.
    "Hohe Latenz:",
    # Uvicorn-HTTP-Warnings von externen Health-Probes (random scanners,
    # CDN-Health-Checks). Nichts mit Bot-Logik zu tun.
    "Invalid HTTP request received",
    # R-A24 (19.05.2026 abend): Container-Restart-Artefakte.
    # Bei jedem `docker compose up -d --build` werden bestehende IBKR-
    # Socket-Verbindungen abrupt geschlossen. ib_insync loggt das als
    # ERROR, ist aber **erwartetes** Verhalten. Defense-Layer:
    # Self-Test #11 tc_ibkr_session_watchdog prueft hourly ob Connection
    # live ist — echter Disconnect wuerde dort failen.
    # Plus geplant: R-A25 Graceful-Shutdown-Hook (morgen) — Bot
    # disconnected sauber vor Container-Stop. Bis dahin filter hier.
    "Peer closed connection",
    "_get_account_value(NetLiquidation) failed: Socket disconnect",
    "Socket disconnect",
    # ib_insync API-Connection-Tasks beim Restart (Cascade aus Peer-Close)
    "Task <Task pending name='Task-",
    "connectAsync running at",
)


def _is_noise(message: str) -> bool:
    """True wenn Event als bekannter, harmloser Pattern erkannt wird."""
    if not isinstance(message, str):
        return False
    return any(pattern in message for pattern in _SENTRY_NOISE_PATTERNS)


def _before_send(event: dict, hint: dict) -> Optional[dict]:
    """Sentry before_send-Hook: PII-Scrub + Noise-Filter vor Event-Sendung.

    Returns None → Event wird verworfen.

    v37g-review-fix HIGH 1: Bei Filter-Crash WIRD Event GEDROPPED (None
    statt unscrubbed Event durchlassen). Vor Real-Money-Phase ist das
    konservativ-default — lieber Visibility-Verlust als PII-Leak.

    R-A20 (19.05.2026): zusaetzlich Noise-Filter fuer bekannte ib_insync-
    Library-Quirks die als ERROR geloggt werden aber kein echter Bot-Bug.
    """
    try:
        # R-A20 Noise-Filter (vor PII-Scrub, spart Aufwand)
        msg = event.get("message") or ""
        # Auch in logentry-Format (LoggingIntegration) checken
        logentry = event.get("logentry") or {}
        log_msg = (logentry.get("message") if isinstance(logentry, dict) else "") or ""
        # Plus exception-Values
        exc_msgs = []
        for exc in (event.get("exception", {}) or {}).get("values", []) or []:
            if isinstance(exc, dict) and exc.get("value"):
                exc_msgs.append(str(exc["value"]))

        combined = " | ".join([msg, log_msg] + exc_msgs)
        if _is_noise(combined):
            return None  # Event komplett drop, kein Sentry-Spam

        # Scrub message
        if isinstance(event.get("message"), str):
            event["message"] = _redact_string(event["message"])
        # Scrub extra-context
        if isinstance(event.get("extra"), dict):
            event["extra"] = _scrub_dict(event["extra"])
        # Scrub tags (falls Symbol/Trade-Werte da landen)
        if isinstance(event.get("tags"), dict):
            event["tags"] = _scrub_dict(event["tags"])
        # Scrub request-data (FastAPI-Integration kann body inkludieren)
        if isinstance(event.get("request"), dict):
            event["request"] = _scrub_dict(event["request"])
        # Exception-Werte: Local-Variables redacten
        for exc in (event.get("exception", {}) or {}).get("values", []):
            for frame in (exc.get("stacktrace", {}) or {}).get("frames", []):
                if isinstance(frame.get("vars"), dict):
                    frame["vars"] = _scrub_dict(frame["vars"])
        return event
    except Exception as e:
        # v37g-review HIGH 1: Filter-Crash → Event DROPPEN, NICHT durchlassen.
        # Lieber Visibility-Verlust als Risk dass IBKR-IDs/Cash/Tokens leaken.
        log.error("Sentry before_send filter crashed: %s — event DROPPED for safety.", e)
        return None


# ============================================================
# Public API
# ============================================================

def setup_sentry() -> bool:
    """Initialisiert Sentry SDK falls aktiviert + DSN vorhanden.

    Returns:
        True wenn Sentry aktiv, False wenn skipped (no-op).

    Idempotent: mehrfacher Aufruf ist harmlos.
    """
    cfg = _load_sentry_config()
    enabled = bool(cfg.get("enabled", False))
    dsn = _get_dsn()

    if not enabled:
        log.info("Sentry: deaktiviert (config.sentry.enabled = false)")
        return False
    if not dsn:
        log.warning("Sentry: aktiviert in config, aber SENTRY_DSN env var fehlt — skip.")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        # Sample-Rate aus config (default 1.0 = alle Errors)
        # Performance-Sampling separat (transactions): default 0.1 (10%)
        traces_rate = float(cfg.get("traces_sample_rate", 0.1))
        environment = cfg.get("environment", "production")
        release = cfg.get("release", "investpilot@v37g")

        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=traces_rate,
            environment=environment,
            release=release,
            send_default_pii=False,  # SDK-Default-Filter aktiv
            attach_stacktrace=True,  # auch bei Logger-Errors Stack mitschicken
            max_breadcrumbs=50,
            before_send=_before_send,  # PII-Scrub-Layer
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                LoggingIntegration(
                    level=logging.INFO,    # Breadcrumbs ab INFO
                    event_level=logging.ERROR,  # Events ab ERROR
                ),
            ],
        )
        log.info("Sentry: aktiv (env=%s, release=%s, traces=%.2f)",
                 environment, release, traces_rate)
        return True
    except Exception as e:
        # Niemals den Bot crashen wegen Sentry-Init-Fail
        log.error("Sentry-Init fehlgeschlagen: %s — Bot laeuft ohne Sentry weiter.", e)
        return False
