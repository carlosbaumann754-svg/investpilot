"""Tests fuer R-A36 — WFO History-Append integration in run_walk_forward().

Bug entdeckt waehrend Sprint-Tag-11 Block 6 (21.05.2026):
WFO-Card im Dashboard zeigte nur 1 Run im Trend obwohl tatsaechlich
3 Runs gelaufen waren (28.04. + 10.05. + 19.05.). Wurzel: History-Append
war nur in app/wfo_runner.py (Cron-Wrapper) integriert, nicht in
app/walk_forward_optimizer.py run_walk_forward() selbst. Manual-Trigger
via /api/wfo/run oder CLI -m app.walk_forward_optimizer --run schrieben
nur status.json aber NICHT history.json.

Fix R-A36: _append_history_to_trend() direkt am Ende von
run_walk_forward() — Single-Source-of-Truth. Idempotent gegen
Doppelschreibung.

Source-based Tests (Module-Import wuerde pyotp brauchen, lokal nicht da).
"""

import re
from pathlib import Path


WFO_PY = Path(__file__).parent.parent / "app" / "walk_forward_optimizer.py"


def test_r_a36_append_history_in_run_walk_forward():
    """run_walk_forward() MUSS _append_history_to_trend nach write_status('done')
    aufrufen — sonst landen Manual/CLI-Runs nicht im Trend."""
    src = WFO_PY.read_text(encoding="utf-8")
    # Locate end of run_walk_forward function
    run_start = src.index("def run_walk_forward(")
    # Look for write_status('done') + then _append_history_to_trend
    run_body_end = src.index("def _append_history_to_trend", run_start)
    body = src[run_start:run_body_end]
    assert 'write_status("done"' in body, "write_status done call fehlt"
    assert "_append_history_to_trend(final_status)" in body, (
        "R-A36 _append_history_to_trend Aufruf in run_walk_forward fehlt"
    )


def test_r_a36_append_history_func_defined():
    """_append_history_to_trend() Funktion muss definiert sein."""
    src = WFO_PY.read_text(encoding="utf-8")
    assert "def _append_history_to_trend(status_snapshot: dict)" in src, (
        "R-A36 _append_history_to_trend Funktion fehlt"
    )


def test_r_a36_history_append_is_idempotent():
    """Append darf nicht doppelt schreiben wenn gleicher timestamp existiert
    (Re-Trigger-Schutz Manual + Cron im selben Slot)."""
    src = WFO_PY.read_text(encoding="utf-8")
    func_start = src.index("def _append_history_to_trend(")
    # Read large window — function body is ~70 lines / ~3000 chars
    body = src[func_start:func_start + 4000]
    # Look for idempotency check pattern
    assert 'r.get("timestamp") == last_run_ts' in body, (
        "Idempotency-Check (kein doppelter Append bei gleichem Timestamp) fehlt"
    )
    assert "bereits drin" in body or "skip" in body.lower(), (
        "Skip-Log bei Duplicate-Append fehlt"
    )


def test_r_a36_history_append_uses_status_last_run():
    """Timestamp MUSS aus status_snapshot.last_run kommen (= echte Run-Zeit),
    nicht datetime.now() (= Append-Zeit). Sonst Drift zwischen status.json
    und history.json."""
    src = WFO_PY.read_text(encoding="utf-8")
    func_start = src.index("def _append_history_to_trend(")
    body = src[func_start:func_start + 3000]
    assert 'status_snapshot.get("last_run")' in body, (
        "last_run aus status_snapshot wird nicht gelesen"
    )


def test_r_a36_history_keeps_aggregate_fields():
    """History-Eintrag muss alle aggregate-Felder enthalten fuer Trend-Chart."""
    src = WFO_PY.read_text(encoding="utf-8")
    func_start = src.index("def _append_history_to_trend(")
    body = src[func_start:func_start + 3000]
    required_fields = [
        "mean_oos_sharpe",
        "mean_is_sharpe",
        "sharpe_decay_pct",
        "oos_stability_std",
        "mean_oos_trades",
        "mean_oos_max_dd",
    ]
    for field in required_fields:
        assert f'"{field}": agg.get("{field}")' in body, (
            f"R-A36 aggregate field {field} fehlt im History-Eintrag"
        )


def test_r_a36_history_limit_60_entries():
    """History-Cap bei 60 Eintraegen (5J x 12 Mo) — gegen unbeschraenktes Wachstum."""
    src = WFO_PY.read_text(encoding="utf-8")
    func_start = src.index("def _append_history_to_trend(")
    body = src[func_start:func_start + 3000]
    assert "len(runs) > 60" in body, "History-Cap bei 60 fehlt"
    assert "runs = runs[-60:]" in body, "Cap-Trim runs[-60:] fehlt"


def test_r_a36_history_exception_is_non_fatal():
    """History-Append-Exception darf den WFO-Run nicht failen lassen — der
    Run selber war erfolgreich, History ist nur Visibility."""
    src = WFO_PY.read_text(encoding="utf-8")
    # Look for try/except around _append_history_to_trend call
    run_start = src.index("def run_walk_forward(")
    body = src[run_start:run_start + 5000]
    assert "_append_history_to_trend(final_status)" in body
    # Es muss ein try/except drum sein
    assert "try:" in body and "except Exception as e:" in body
    assert "non-fatal" in body or "non_fatal" in body, (
        "non-fatal-Kommentar fehlt - Code-Reviewer sollte sehen dass das gewollt ist"
    )
