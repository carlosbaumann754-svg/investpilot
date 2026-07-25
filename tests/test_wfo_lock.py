"""Tests fuer das WFO-Lock-System (v37r)."""

from __future__ import annotations

import pytest


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    """Isoliertes data/-Verzeichnis pro Test — via DATA_DIR-Attribut-Patch.

    Warum setattr statt setenv+importlib.reload: reload() liess
    config_manager.DATA_DIR nach dem Teardown auf dem tmp_path des
    VORHERIGEN Tests haengen — fixture-lose Tests lasen fremden Test-State.
    Details: temp_data_dir in tests/test_earnings_exit.py (Fix 25.07.2026).
    """
    from app import config_manager
    monkeypatch.setattr(config_manager, "DATA_DIR", tmp_path)
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


def test_locked_take_profit_majority(temp_data_dir):
    """v37ct: Take-Profit jetzt auch gelockt — 3/5 TP=15 -> 15 gewinnt."""
    from app.wfo_lock import get_wfo_locked_params
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"take_profit_pct": 12}},
        {"best_params": {"take_profit_pct": 9}},
        {"best_params": {"take_profit_pct": 15}},
        {"best_params": {"take_profit_pct": 15}},
        {"best_params": {"take_profit_pct": 15}},
    ])
    assert get_wfo_locked_params()["take_profit_pct"] == 15


def test_locked_take_profit_tie_min_picker(temp_data_dir):
    """v37ct: TP-Lock nutzt 'min' picker (konservativ — frueher Gewinn-Lock)."""
    from app.wfo_lock import get_wfo_locked_params
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"take_profit_pct": 12}},
        {"best_params": {"take_profit_pct": 18}},
    ])
    # Tie -> min(12, 18) = 12
    assert get_wfo_locked_params()["take_profit_pct"] == 12


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
    # drifts ist keyed by config_path (Q3-1 fix), nicht param_name
    assert "demo_trading.stop_loss_pct" in drifts
    assert drifts["demo_trading.stop_loss_pct"]["expected"] == -3.0
    assert drifts["demo_trading.stop_loss_pct"]["actual"] == -5
    assert drifts["demo_trading.stop_loss_pct"]["param"] == "stop_loss_pct"
    assert "scanner.min_scanner_score" in drifts


def test_detect_drift_no_drift(temp_data_dir):
    from app.wfo_lock import detect_drift
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"stop_loss_pct": -3.0, "min_scanner_score": 40}},
    ] * 5)

    config = {
        # min_scanner_score muss in BEIDEN Pfaden matchen (Q3-1 Tab-Audit-Day-2)
        "demo_trading": {"stop_loss_pct": -3.0, "min_scanner_score": 40},
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
    # 3 changes: stop_loss_pct + scanner.min_scanner_score + demo_trading.min_scanner_score
    # (Q3-1: zweiter Pfad wird konsistent enforced damit Backtester/Live-Scanner synchron sind)
    assert len(changes) == 3
    assert config["demo_trading"]["stop_loss_pct"] == -3.0
    assert config["scanner"]["min_scanner_score"] == 40
    assert config["demo_trading"]["min_scanner_score"] == 40
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
    # 3 changes: stop_loss_pct + beide min_scanner_score-Pfade (Q3-1)
    assert len(changes_1) == 3
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
    # 3 Drifts: stop_loss + beide min_scanner_score-Pfade (Q3-1)
    assert result["drifts_detected"] == 3
    assert len(result["restored"]) == 3

    # Config nun korrigiert
    persisted = load_config()
    assert persisted["demo_trading"]["stop_loss_pct"] == -3.0
    assert persisted["scanner"]["min_scanner_score"] == 40


# ============================================================
# MANUAL OVERRIDES (v37e+, 02.07.2026 — Post-Soak Schritt B)
# ============================================================

def _write_overrides(temp_data_dir, overrides):
    from app.config_manager import save_json
    save_json("manual_lock_overrides.json", overrides)


def test_manual_overrides_win_over_window_mode(temp_data_dir):
    """Bewusst gesetzte Overrides schlagen die (Alt-Motor-) WFO-Window-Mode."""
    from app.wfo_lock import get_wfo_locked_params
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"stop_loss_pct": -5.0, "min_scanner_score": 40}},
    ] * 5)
    _write_overrides(temp_data_dir, {
        "stop_loss_pct": -8.0, "min_scanner_score": 25, "max_positions": 15,
        "_meta": {"reason": "post-soak-recalibration"},
    })
    locked = get_wfo_locked_params()
    assert locked["stop_loss_pct"] == -8.0     # Override schlaegt Window-Mode -5
    assert locked["min_scanner_score"] == 25   # Override schlaegt Window-Mode 40
    assert locked["max_positions"] == 15       # Override ohne Window-Daten
    assert "_meta" not in locked               # _-Praefix (Doku) gefiltert


def test_manual_overrides_apply_without_windows(temp_data_dir):
    """Overrides greifen auch wenn wfo_status.json fehlt."""
    from app.wfo_lock import get_wfo_locked_params
    _write_overrides(temp_data_dir, {"stop_loss_pct": -8.0, "max_positions": 15})
    assert get_wfo_locked_params() == {"stop_loss_pct": -8.0, "max_positions": 15}


def test_max_positions_lock_enforced_via_override(temp_data_dir):
    """max_positions wird (nur) via Override gelockt + auf die Config enforced."""
    from app.wfo_lock import enforce_locks
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"stop_loss_pct": -5.0, "min_scanner_score": 40}},
    ] * 5)
    _write_overrides(temp_data_dir, {
        "stop_loss_pct": -8.0, "min_scanner_score": 25, "max_positions": 15,
    })
    config = {
        "demo_trading": {"stop_loss_pct": -5.0, "min_scanner_score": 40, "max_positions": 20},
        "scanner": {"min_scanner_score": 40},
    }
    changes = enforce_locks(config)
    assert config["demo_trading"]["stop_loss_pct"] == -8.0
    assert config["demo_trading"]["min_scanner_score"] == 25
    assert config["scanner"]["min_scanner_score"] == 25       # 2. Pfad mit-synchron
    assert config["demo_trading"]["max_positions"] == 15
    assert {c["param"] for c in changes} == {
        "stop_loss_pct", "min_scanner_score", "max_positions"}


def test_no_overrides_file_backward_compat(temp_data_dir):
    """Ohne Override-File: reines Window-Mode-Verhalten, max_positions NICHT gelockt."""
    from app.wfo_lock import get_wfo_locked_params
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"stop_loss_pct": -3.0, "min_scanner_score": 40}},
    ] * 5)
    locked = get_wfo_locked_params()
    assert locked["stop_loss_pct"] == -3.0
    assert locked["min_scanner_score"] == 40
    assert "max_positions" not in locked


def test_malformed_overrides_ignored(temp_data_dir):
    """Nicht-Dict Override-File (kaputt) -> ignoriert, Window-Mode bleibt."""
    from app.wfo_lock import get_wfo_locked_params
    _write_wfo_status(temp_data_dir, [{"best_params": {"stop_loss_pct": -3.0}}] * 3)
    _write_overrides(temp_data_dir, ["kaputt"])   # kein Dict
    assert get_wfo_locked_params()["stop_loss_pct"] == -3.0


def test_detect_drift_clean_when_config_matches_overrides(temp_data_dir):
    """Config == Override-Werte -> kein Drift (kein Boot-Alert)."""
    from app.wfo_lock import detect_drift
    _write_wfo_status(temp_data_dir, [
        {"best_params": {"stop_loss_pct": -5.0, "min_scanner_score": 40}},
    ] * 5)
    _write_overrides(temp_data_dir, {
        "stop_loss_pct": -8.0, "min_scanner_score": 25, "max_positions": 15,
    })
    config = {
        "demo_trading": {"stop_loss_pct": -8.0, "min_scanner_score": 25, "max_positions": 15},
        "scanner": {"min_scanner_score": 25},
    }
    assert detect_drift(config) == {}


def test_manual_override_min_rr_and_tier_map(temp_data_dir):
    """v37e+ (16.07.): min_risk_reward_ratio (Skalar) + max_positions_by_capital (Dict)
    via manual_overrides gelockt — Cloud-Restore-Schutz der Rekalibrierungs-Werte."""
    from app.wfo_lock import get_wfo_locked_params, enforce_locks, detect_drift
    _write_overrides(temp_data_dir, {
        "min_risk_reward_ratio": 1.4,
        "max_positions_by_capital": {"3000": 6, "10000": 10, "30000": 15, "999999": 15},
    })
    locked = get_wfo_locked_params()
    assert locked["min_risk_reward_ratio"] == 1.4
    assert locked["max_positions_by_capital"]["999999"] == 15
    # enforce auf eine driftende Config (R/R weg -> Default 2.0; Tier-Map top 20)
    config = {
        "leverage": {"min_risk_reward_ratio": None},
        "portfolio_sizing": {"max_positions_by_capital": {"3000": 6, "10000": 10, "30000": 15, "999999": 20}},
    }
    enforce_locks(config)
    assert config["leverage"]["min_risk_reward_ratio"] == 1.4
    assert config["portfolio_sizing"]["max_positions_by_capital"]["999999"] == 15
    assert detect_drift(config) == {}     # jetzt gematcht -> kein Drift, kein Alert
