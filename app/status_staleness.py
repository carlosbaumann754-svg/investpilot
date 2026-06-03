"""R-B8 (03.06.2026) — Staleness-Guard fuer asynchrone Job-Status.

PROBLEM: Kelly-Sweep / Discovery / Optimizer schreiben einen Status
(running -> done/error). Wird der Job HART gekillt (SIGKILL/OOM/Container-
Neustart), umgeht das den except-Block des Runners -> kein End-Status ->
der Status friert auf 'running' ein -> das Dashboard zeigt ewig 'laeuft...'
fuer einen laengst toten Lauf. (Beobachtet 03.06.: Kelly-Sweep haengt seit
~9h auf phase='rescore'.)

LOESUNG: mark_stale_if_old() markiert solche toten Jobs SERVER-seitig als
'stale'. Server-seitig ist wichtig: `now` und `updated_at` liegen auf
derselben Uhr -> kein Browser-Zeitzonen-Skew (eine Frontend-Altersrechnung
auf naiven Timestamps wuerde bei UTC-Server + lokalem Browser um den TZ-Offset
verschieben und frische Jobs faelschlich als 'abgebrochen' zeigen).

Fail-safe: bei fehlendem/unparsbarem Timestamp bleibt 'running' (lieber ein
'laeuft...' zu lang als ein laufender Job faelschlich 'abgebrochen').
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# R-A12 Reflection-Audit-Marker
AUDIT_METADATA = {
    "purpose": "Staleness-Guard: markiert 'running'-Job-Status (Kelly/Discovery/Optimizer) server-seitig als 'stale' wenn updated_at aelter als Timeout (toter Job nach Kill/OOM/Neustart) — verhindert ewiges 'laeuft...' im Dashboard",
    "config_section": None,
    "state_files": [],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "R-B8 (03.06.2026)",
}

# Default-Timeout: GitHub-Action-Workflow-Timeout ist 30 Min; legitime Laeufe
# (Discovery/Backtest/Kelly) dauern <15 Min. 45 Min = sicherer Puffer ->
# alles >45 Min im 'running' ist ein toter Job.
DEFAULT_TIMEOUT_MIN = 45.0


def age_minutes(iso_ts) -> Optional[float]:
    """Alter eines ISO-Timestamps in Minuten (server-seitig). None bei leer/
    unparsbar. Behandelt naive UND tz-aware Timestamps korrekt (vergleicht
    jeweils gegen das passende 'now')."""
    if not iso_ts:
        return None
    try:
        t = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    now = datetime.now(timezone.utc) if t.tzinfo is not None else datetime.now()
    return (now - t).total_seconds() / 60.0


def mark_stale_if_old(status: dict, timeout_min: float = DEFAULT_TIMEOUT_MIN) -> dict:
    """Markiert einen 'running'-Status als 'stale', wenn updated_at (Fallback:
    started_at) aelter als timeout_min ist.

    Returns eine Kopie mit state='stale' + stale_age_min, wenn veraltet —
    sonst das unveraenderte Original. Fail-safe: kein/unparsbarer Timestamp
    -> bleibt 'running' (kein False-Positive auf einen echten Lauf).
    """
    if not isinstance(status, dict) or status.get("state") != "running":
        return status
    age = age_minutes(status.get("updated_at") or status.get("started_at"))
    if age is None or age <= timeout_min:
        return status
    out = dict(status)
    out["state"] = "stale"
    out["stale_age_min"] = round(age)
    return out
