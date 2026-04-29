"""Tests fuer das WFO-Lock-System (v37r)."""

from __future__ import annotations

import pytest


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("INVESTPILOT_DATA_DIR", str(tmp_path))
    import importlib
    from app import config_manager
    importlib.reload(config_manager)
    yield tmp_path


def _write_wfo_status(temp_data_dir, windows: list[dict]):
    from app.config_manager import save_json
    save_json("wfo_status.json", {"state": "done", "windows": windows})


# ============================================================
# READ: get_wfo_locked_params
# ============================================================

def test_locked_params_unanimous(temp_data_dir):
    """5/5 Windows einig auf SL=-3, score=40 -> klar gelocked."""
    from app.wfo_lock import get_wfo_locked_params
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"stop_loss_pct": -3.0, "min_scanner_score": 40}},
    ] * 5)
    locked = get_wfo_locked_params()
    assert locked["stop_loss_pct"] == -3.0
    assert locked["min_scanner_score"] == 40


def test_locked_params_majority(temp_data_dir):
    """3/5 SL=-3, 2/5 SL=-4 -> -3 gewinnt (majority)."""
    from app.wfo_lock import get_wfo_locked_params
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"stop_loss_pct": -3.0}},
        {"best_params": {"stop_loss_pct": -3.0}},
        {"best_params": {"stop_loss_pct": -3.0}},
        {"best_params": {"stop_loss_pct": -4.0}},
        {"best_params": {"stop_loss_pct": -4.0}},
    ])
    assert get_wfo_locked_params()["stop_loss_pct"] == -3.0


def test_locked_params_tie_max_picker(temp_data_dir):
    """Tie -> "max" picker fuer SL = naehesten zu null = strenger."""
    from app.wfo_lock import get_wfo_locked_params
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"stop_loss_pct": -3.0}},
        {"best_params": {"stop_loss_pct": -5.0}},
    ])
    # max(-3, -5) = -3
    assert get_wfo_locked_params()["stop_loss_pct"] == -3.0


def test_locked_params_no_wfo_status(temp_data_dir):
    """Kein wfo_status.json -> leeres Dict, kein Crash."""
    from app.wfo_lock import get_wfo_locked_params
    assert get_wfo_locked_params() == {}


def test_locked_params_empty_windows(temp_data_dir):
    """wfo_status mit leerem windows-Array -> leeres Dict."""
    from app.wfo_lock import get_wfo_locked_params
    _write_wfo_status(temp_data_dir, [])
    assert get_wfo_locked_params() == {}


# ============================================================
# DETECT: drift detection
# ============================================================

def test_detect_drift_finds_mismatch(temp_data_dir):
    from app.wfo_lock import detect_drift
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"stop_loss_pct": -3.0, "min_scanner_score": 40}},
    ] * 5)

    config = {
        "demo_trading": {"stop_loss_pct": -5},
        "scanner": {"min_scanner_score": None},
    }
    drifts = detect_drift(config)
    assert "stop_loss_pct" in drifts
    assert drifts["stop_loss_pct"]["expected"] == -3.0
    assert drifts["stop_loss_pct"]["actual"] == -5
    assert "min_scanner_score" in drifts


def test_detect_drift_no_drift(temp_data_dir):
    from app.wfo_lock import detect_drift
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"stop_loss_pct": -3.0, "min_scanner_score": 40}},
    ] * 5)

    config = {
        "demo_trading": {"stop_loss_pct": -3.0},
        "scanner": {"min_scanner_score": 40},
    }
    assert detect_drift(config) == {}


def test_detect_drift_float_tolerance(temp_data_dir):
    """-3.0 vs -3 sollte als kein Drift gewertet werden."""
    from app.wfo_lock import detect_drift
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"stop_loss_pct": -3.0}},
    ] * 5)
    config = {"demo_trading": {"stop_loss_pct": -3}}
    assert detect_drift(config) == {}


# ============================================================
# ENFORCE: in-place corrections
# ============================================================

def test_enforce_locks_corrects_drift(temp_data_dir):
    from app.wfo_lock import enforce_locks
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"stop_loss_pct": -3.0, "min_scanner_score": 40}},
    ] * 5)

    config = {
        "demo_trading": {"stop_loss_pct": -5, "take_profit_pct": 18},
        "scanner": {"min_scanner_score": None},
    }
    changes = enforce_locks(config)
    assert len(changes) == 2
    assert config["demo_trading"]["stop_loss_pct"] == -3.0
    assert config["scanner"]["min_scanner_score"] == 40
    # take_profit unangetastet
    assert config["demo_trading"]["take_profit_pct"] == 18
    # Audit-Trail dokumentiert
    assert "_audit" in config
    assert "wfo_lock_enforcements" in config["_audit"]


def test_enforce_locks_idempotent(temp_data_dir):
    """2x Aufruf -> 2tes Mal keine Aenderung mehr."""
    from app.wfo_lock import enforce_locks
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"stop_loss_pct": -3.0, "min_scanner_score": 40}},
    ] * 5)
    config = {"demo_trading": {"stop_loss_pct": -5}, "scanner": {"min_scanner_score": None}}

    changes_1 = enforce_locks(config)
    assert len(changes_1) == 2
    changes_2 = enforce_locks(config)
    assert len(changes_2) == 0


def test_enforce_locks_creates_missing_keys(temp_data_dir):
    """Wenn config.scanner gar nicht existiert -> wird angelegt."""
    from app.wfo_lock import enforce_locks
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"stop_loss_pct": -3.0, "min_scanner_score": 40}},
    ] * 5)
    config = {"demo_trading": {}}
    enforce_locks(config)
    assert config["demo_trading"]["stop_loss_pct"] == -3.0
    assert config["scanner"]["min_scanner_score"] == 40


def test_enforce_locks_no_wfo_data_no_crash(temp_data_dir):
    """Ohne wfo_status -> kein Crash, leere Aenderungen."""
    from app.wfo_lock import enforce_locks
    config = {"demo_trading": {"stop_loss_pct": -5}}
    assert enforce_locks(config) == []
    # Config unveraendert
    assert config["demo_trading"]["stop_loss_pct"] == -5


# ============================================================
# SAVE-CONFIG INTEGRATION: locks greifen automatisch
# ============================================================

def test_save_config_enforces_wfo_locks_automatically(temp_data_dir):
    """save_config(config_with_drift) -> persisted config hat WFO-Werte."""
    from app.config_manager import save_config, load_config, save_json
    save_json("wfo_status.json", {
        "state": "done",
        "windows": [
            {"best_params": {"stop_loss_pct": -3.0, "min_scanner_score": 40}},
        ] * 5,
    })

    # Caller writes drifted config
    bad_config = {
        "demo_trading": {"stop_loss_pct": -5, "take_profit_pct": 18},
        "scanner": {"min_scanner_score": 25},
    }
    save_config(bad_config)

    # Load back: drift muss korrigiert sein
    persisted = load_config()
    assert persisted["demo_trading"]["stop_loss_pct"] == -3.0
    assert persisted["scanner"]["min_scanner_score"] == 40
    assert persisted["demo_trading"]["take_profit_pct"] == 18  # nicht angefasst


def test_save_config_no_wfo_data_passes_through(temp_data_dir):
    """Ohne wfo_status.json wird save_config ganz normal durchgelassen."""
    from app.config_manager import save_config, load_config
    config = {"demo_trading": {"stop_loss_pct": -5}}
    save_config(config)
    persisted = load_config()
    assert persisted["demo_trading"]["stop_loss_pct"] == -5


# ============================================================
# BOOT-CHECK
# ============================================================

def test_boot_drift_check_no_drift(temp_data_dir):
    from app.config_manager import save_config, save_json
    save_json("wfo_status.json", {
        "state": "done",
        "windows": [
            {"best_params": {"stop_loss_pct": -3.0, "min_scanner_score": 40}},
        ] * 5,
    })
    save_config({
        "demo_trading": {"stop_loss_pct": -3.0},
        "scanner": {"min_scanner_score": 40},
    })

    from app.wfo_lock import boot_drift_check
    result = boot_drift_check(send_alert=False, auto_restore=False)
    assert result["drifts_detected"] == 0
    assert result["restored"] == []


def test_boot_drift_check_detects_and_restores(temp_data_dir, monkeypatch):
    """Boot-Check findet Drift, ruft enforce, save_config schreibt korrigierte Werte."""
    from app.config_manager import save_config, save_json, load_config
    save_json("wfo_status.json", {
        "state": "done",
        "windows": [
            {"best_params": {"stop_loss_pct": -3.0, "min_scanner_score": 40}},
        ] * 5,
    })

    # Direct-File-Write (umgeht save_config-Hook): simuliert Cloud-Restore
    import json
    config_path = temp_data_dir / "config.json"
    bad_config = {
        "demo_trading": {"stop_loss_pct": -5, "take_profit_pct": 12},
        "scanner": {"min_scanner_score": None},
    }
    with open(config_path, "w") as f:
        json.dump(bad_config, f)

    # Boot-Check fires
    from app.wfo_lock import boot_drift_check
    result = boot_drift_check(send_alert=False, auto_restore=True)
    assert result["drifts_detected"] == 2
    assert len(result["restored"]) == 2

    # Config nun korrigiert
    persisted = load_config()
    assert persisted["demo_trading"]["stop_loss_pct"] == -3.0
    assert persisted["scanner"]["min_scanner_score"] == 40
