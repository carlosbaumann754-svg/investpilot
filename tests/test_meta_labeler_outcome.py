"""Tests fuer R-A41 — Meta-Labeler actual_outcome-Feedback-Loop.

Bug: log_shadow_decision schrieb {p_win, decision, ...} aber NIE einen
actual_outcome zurueck → UI-Treffer-Quote war immer 0%.
Plus: position_id wurde als eToro-Field-Name ('positionID') gelesen → bei
IBKR null → check_and_maybe_activate-Match unmoeglich.

Fix R-A41: update_outcome_for_close() Funktion + Hook in save_trade()
+ compute_hit_rate() fuer Display + Backfill-Funktion + UI "N/A" wenn
<5 matured.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


def _temp_data_dir():
    """Erstellt temp-Verzeichnis fuer isolierte JSON-Tests."""
    return tempfile.mkdtemp(prefix="ml_test_")


def _setup_shadow(tmpdir, decisions: list):
    """Schreibt shadow-log in temp data-dir."""
    Path(tmpdir, "meta_labeling_shadow.json").write_text(
        json.dumps(decisions), encoding="utf-8"
    )


def test_r_a41_update_outcome_matches_by_symbol_and_time():
    """update_outcome_for_close findet matching shadow-decision via
    symbol + time-window (auch wenn position_id null ist)."""
    from app import meta_labeler
    tmp = _temp_data_dir()
    shadow = [{
        "timestamp": "2026-05-20T10:00:00",
        "position_id": None,
        "symbol": "AAPL",
        "decision": "shadow_take",
        "p_win": 0.8,
    }]
    _setup_shadow(tmp, shadow)
    with patch.object(meta_labeler, "SHADOW_LOG_FILE", "meta_labeling_shadow.json"), \
         patch("app.meta_labeler.load_json", side_effect=lambda f:
               json.loads(Path(tmp, f).read_text()) if Path(tmp, f).exists() else None), \
         patch("app.meta_labeler.save_json", side_effect=lambda f, d:
               Path(tmp, f).write_text(json.dumps(d))):
        n = meta_labeler.update_outcome_for_close(
            symbol="AAPL", pnl_pct=3.5,
            close_timestamp="2026-05-25T15:00:00"
        )
    assert n == 1
    updated = json.loads(Path(tmp, "meta_labeling_shadow.json").read_text())
    assert updated[0]["actual_outcome"] == "win"
    assert updated[0]["actual_pnl_pct"] == 3.5


def test_r_a41_update_outcome_marks_loss_on_negative_pnl():
    """pnl_pct <= 0 → outcome="loss"."""
    from app import meta_labeler
    tmp = _temp_data_dir()
    shadow = [{
        "timestamp": "2026-05-20T10:00:00",
        "symbol": "TSLA",
        "decision": "shadow_take",
        "p_win": 0.7,
    }]
    _setup_shadow(tmp, shadow)
    with patch.object(meta_labeler, "SHADOW_LOG_FILE", "meta_labeling_shadow.json"), \
         patch("app.meta_labeler.load_json", side_effect=lambda f:
               json.loads(Path(tmp, f).read_text()) if Path(tmp, f).exists() else None), \
         patch("app.meta_labeler.save_json", side_effect=lambda f, d:
               Path(tmp, f).write_text(json.dumps(d))):
        meta_labeler.update_outcome_for_close(
            symbol="TSLA", pnl_pct=-2.1,
            close_timestamp="2026-05-22T12:00:00"
        )
    updated = json.loads(Path(tmp, "meta_labeling_shadow.json").read_text())
    assert updated[0]["actual_outcome"] == "loss"


def test_r_a41_update_outcome_is_idempotent():
    """Re-run der gleichen Close darf bestehenden outcome NICHT ueberschreiben."""
    from app import meta_labeler
    tmp = _temp_data_dir()
    shadow = [{
        "timestamp": "2026-05-20T10:00:00",
        "symbol": "KO",
        "decision": "shadow_take",
        "actual_outcome": "win",  # bereits gesetzt
        "actual_pnl_pct": 4.5,
    }]
    _setup_shadow(tmp, shadow)
    with patch.object(meta_labeler, "SHADOW_LOG_FILE", "meta_labeling_shadow.json"), \
         patch("app.meta_labeler.load_json", side_effect=lambda f:
               json.loads(Path(tmp, f).read_text()) if Path(tmp, f).exists() else None), \
         patch("app.meta_labeler.save_json", side_effect=lambda f, d:
               Path(tmp, f).write_text(json.dumps(d))):
        n = meta_labeler.update_outcome_for_close(
            symbol="KO", pnl_pct=-10,  # waere LOSS aber existing ist WIN
            close_timestamp="2026-05-22T15:00:00"
        )
    assert n == 0  # nichts upgedated
    updated = json.loads(Path(tmp, "meta_labeling_shadow.json").read_text())
    assert updated[0]["actual_outcome"] == "win"  # unveraendert
    assert updated[0]["actual_pnl_pct"] == 4.5


def test_r_a41_update_outcome_time_window_30d_max():
    """Decision >30d alt darf NICHT mit Close gematched werden (Time-Stop-Bound)."""
    from app import meta_labeler
    tmp = _temp_data_dir()
    shadow = [{
        "timestamp": "2026-01-01T10:00:00",  # 5+ Monate alt
        "symbol": "MSFT",
        "decision": "shadow_take",
    }]
    _setup_shadow(tmp, shadow)
    with patch.object(meta_labeler, "SHADOW_LOG_FILE", "meta_labeling_shadow.json"), \
         patch("app.meta_labeler.load_json", side_effect=lambda f:
               json.loads(Path(tmp, f).read_text()) if Path(tmp, f).exists() else None), \
         patch("app.meta_labeler.save_json", side_effect=lambda f, d:
               Path(tmp, f).write_text(json.dumps(d))):
        n = meta_labeler.update_outcome_for_close(
            symbol="MSFT", pnl_pct=2.5,
            close_timestamp="2026-05-22T15:00:00"
        )
    assert n == 0  # nicht matched (zu alt)


def test_r_a41_compute_hit_rate_returns_na_below_5():
    """compute_hit_rate gibt 'N/A' wenn <5 matured shadow_takes."""
    from app import meta_labeler
    tmp = _temp_data_dir()
    # 3 matured, davon 2 wins
    shadow = [
        {"decision": "shadow_take", "actual_outcome": "win"},
        {"decision": "shadow_take", "actual_outcome": "win"},
        {"decision": "shadow_take", "actual_outcome": "loss"},
        {"decision": "shadow_skip"},
    ]
    _setup_shadow(tmp, shadow)
    with patch.object(meta_labeler, "SHADOW_LOG_FILE", "meta_labeling_shadow.json"), \
         patch("app.meta_labeler.load_json", side_effect=lambda f:
               json.loads(Path(tmp, f).read_text()) if Path(tmp, f).exists() else None):
        result = meta_labeler.compute_hit_rate()
    assert result["hit_rate_display"] == "N/A"
    assert result["matured"] == 3
    assert result["shadow_takes"] == 3
    assert result["hits"] == 2


def test_r_a41_compute_hit_rate_real_percentage_above_5():
    """compute_hit_rate gibt echte Prozentzahl wenn >=5 matured shadow_takes."""
    from app import meta_labeler
    tmp = _temp_data_dir()
    shadow = [
        {"decision": "shadow_take", "actual_outcome": "win"},
        {"decision": "shadow_take", "actual_outcome": "win"},
        {"decision": "shadow_take", "actual_outcome": "win"},
        {"decision": "shadow_take", "actual_outcome": "loss"},
        {"decision": "shadow_take", "actual_outcome": "loss"},
    ]
    _setup_shadow(tmp, shadow)
    with patch.object(meta_labeler, "SHADOW_LOG_FILE", "meta_labeling_shadow.json"), \
         patch("app.meta_labeler.load_json", side_effect=lambda f:
               json.loads(Path(tmp, f).read_text()) if Path(tmp, f).exists() else None):
        result = meta_labeler.compute_hit_rate()
    assert result["hit_rate_display"] == "60.0%"
    assert result["matured"] == 5
    assert result["shadow_takes"] == 5
    assert result["hits"] == 3
    assert abs(result["precision"] - 0.6) < 0.001


def test_r_a41_save_trade_triggers_outcome_hook():
    """save_trade() ruft update_outcome_for_close auf bei Close-Trade mit pnl_pct."""
    src = Path(__file__).parent.parent / "app" / "trader.py"
    body = src.read_text(encoding="utf-8")
    save_trade_start = body.index("def save_trade(")
    save_trade_body = body[save_trade_start:save_trade_start + 2000]
    assert "update_outcome_for_close" in save_trade_body, (
        "R-A41 Hook in save_trade fehlt"
    )
    assert "R-A41" in save_trade_body, "R-A41 Tag fehlt"


def test_r_a41_v12_status_includes_hit_rate():
    """/api/v12-status response.meta_labeler enthaelt hit_rate-Field."""
    src = Path(__file__).parent.parent / "web" / "app.py"
    body = src.read_text(encoding="utf-8")
    assert "compute_hit_rate" in body, "compute_hit_rate import fehlt"
    assert '"hit_rate": meta_hit_rate' in body, (
        "hit_rate-Field im meta_labeler-Response fehlt"
    )


def test_r_a41_frontend_reads_hit_rate_display():
    """app.js liest m.hit_rate.hit_rate_display fuer 'Treffer-Quote'-Anzeige."""
    src = Path(__file__).parent.parent / "web" / "static" / "app.js"
    body = src.read_text(encoding="utf-8")
    assert "m.hit_rate?.hit_rate_display" in body, (
        "R-A41 Frontend reads m.hit_rate.hit_rate_display fehlt"
    )


def test_r_a41_backfill_function_exists():
    """backfill_outcomes_from_trade_history() ist verfuegbar fuer Single-Use-Backfill."""
    from app import meta_labeler
    assert hasattr(meta_labeler, "backfill_outcomes_from_trade_history"), (
        "Backfill-Funktion fehlt"
    )
