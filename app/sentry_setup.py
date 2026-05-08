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
    # IBKR-Account-IDs:
    # Paper: DU<letter><digits> (z.B. DUP108015), DU<digits>, DF<digits>
    # Real:  U<digits> (mind. 7) — "U" am Wortanfang plus 7+ Ziffern
    (re.compile(r"\bD[UF][A-Z]?\d{5,}\b"), "[REDACTED_IBKR_ACCT]"),
    (re.compile(r"\bU\d{7,}\b"), "[REDACTED_IBKR_ACCT]"),
    # Pushover/API-Tokens (30+ Zeichen alphanumerisch)
    (re.compile(r"\b[a-zA-Z0-9]{30,}\b"), lambda m: m.group(0)[:8] + "[REDACTED]"),
    # Email-Adressen
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED_EMAIL]"),
]

# Felder die komplett rausgeworfen werden (auch wenn key existiert)
_DROP_FIELDS = {
    "user_key",
    "api_token",
    "pushover_user_key",
    "pushover_api_token",
    "telegram_token",
    "anthropic_api_key",
    "ibkr_password",
    "session_token",
    "jwt_secret",
}


def _redact_string(s: str) -> str:
    """Redacted PII aus String."""
    if not isinstance(s, str):
        return s
    for pat, repl in _REDACT_PATTERNS:
        s = pat.sub(repl, s)
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


def _before_send(event: dict, hint: dict) -> Optional[dict]:
    """Sentry before_send-Hook: PII-Scrub vor jeder Event-Sendung.

    Returns None → Event wird verworfen.
    """
    try:
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
    except Exception as e:
        # Nicht crashen wegen Filter-Bug — lieber Event durchlassen
        log.warning("Sentry before_send filter error: %s", e)
    return event


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
