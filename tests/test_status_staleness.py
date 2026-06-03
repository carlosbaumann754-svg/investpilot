"""R-B8 (03.06.2026) — Staleness-Guard fuer asynchrone Job-Status.

Kelly-Sweep / Discovery / Optimizer schreiben Status (running -> done/error).
Wird der Job hart gekillt (SIGKILL/OOM/Neustart), umgeht das den except-Block
des Runners -> kein End-Status -> Status friert auf 'running' ein -> Dashboard
zeigt ewig 'laeuft...'. mark_stale_if_old() markiert solche toten Jobs
SERVER-seitig als 'stale' (gleiche Uhr wie der Writer -> kein Browser-Zeitzonen-
Skew, anders als eine Frontend-Altersrechnung auf naiven Timestamps).
"""
from datetime import datetime, timedelta, timezone

from app.status_staleness import mark_stale_if_old


def _status(state, age_min, aware=False):
    base = datetime.now(timezone.utc) if aware else datetime.now()
    ts = (base - timedelta(minutes=age_min)).isoformat()
    return {"state": state, "updated_at": ts}


def test_old_running_becomes_stale():
    out = mark_stale_if_old(_status("running", 120), timeout_min=45)
    assert out["state"] == "stale"
    assert out["stale_age_min"] >= 100


def test_fresh_running_stays_running():
    out = mark_stale_if_old(_status("running", 5), timeout_min=45)
    assert out["state"] == "running"


def test_done_unchanged():
    out = mark_stale_if_old({"state": "done", "updated_at": "2020-01-01T00:00:00"}, timeout_min=45)
    assert out["state"] == "done"


def test_aware_timestamp_old_becomes_stale():
    """updated_at mit tzinfo (UTC) -> korrekt verglichen, kein naive/aware-Crash."""
    out = mark_stale_if_old(_status("running", 120, aware=True), timeout_min=45)
    assert out["state"] == "stale"


def test_aware_timestamp_fresh_stays_running():
    out = mark_stale_if_old(_status("running", 5, aware=True), timeout_min=45)
    assert out["state"] == "running"


def test_unparsable_timestamp_stays_running():
    """Kaputtes updated_at -> NICHT faelschlich stale (fail-safe)."""
    out = mark_stale_if_old({"state": "running", "updated_at": "kaputt"}, timeout_min=45)
    assert out["state"] == "running"


def test_missing_timestamp_stays_running():
    out = mark_stale_if_old({"state": "running"}, timeout_min=45)
    assert out["state"] == "running"


def test_falls_back_to_started_at():
    base = (datetime.now() - timedelta(minutes=200)).isoformat()
    out = mark_stale_if_old({"state": "running", "started_at": base}, timeout_min=45)
    assert out["state"] == "stale"
