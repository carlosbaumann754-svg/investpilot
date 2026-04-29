"""Tests fuer Meta-Labeler auto_activate-Schalter (v37q)."""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("INVESTPILOT_DATA_DIR", str(tmp_path))
    import importlib
    from app import config_manager
    importlib.reload(config_manager)
    yield tmp_path


def _build_matured_log(temp_data_dir, n_takes: int, n_profitable: int):
    """Schreibt einen Shadow-Log + matching trade_history mit gewuenschter Precision."""
    from app.config_manager import save_json

    shadow_log = []
    trade_history = []
    for i in range(n_takes):
        pid = f"P{i}"
        shadow_log.append({"position_id": pid, "decision": "shadow_take"})
        # Outcome via CLOSE-Eintrag mit pnl_pct
        pnl = 5.0 if i < n_profitable else -3.0
        trade_history.append({
            "position_id": pid,
            "action": "STOP_LOSS_CLOSE",
            "pnl_pct": pnl,
        })

    save_json("meta_labeling_shadow.json", shadow_log)
    save_json("trade_history.json", trade_history)


def test_threshold_reached_but_auto_activate_false_does_not_flip(temp_data_dir):
    """Schwelle erreicht (50 trades, 70% Precision), aber auto_activate=false
    -> shadow_mode bleibt True, kein save_config-Call."""
    from app.meta_labeler import check_and_maybe_activate
    _build_matured_log(temp_data_dir, n_takes=50, n_profitable=35)  # 70% Precision

    config = {
        "meta_labeling": {
            "enabled": True,
            "shadow_mode": True,
            "auto_activate": False,
            "min_trades_to_activate": 50,
            "min_precision_to_activate": 0.65,
        }
    }
    with patch("app.config_manager.save_config") as mock_save:
        result = check_and_maybe_activate(config)
        assert result is False
        # Wichtig: Config wurde NICHT geflippt
        assert config["meta_labeling"]["shadow_mode"] is True
        # save_config darf nicht aufgerufen werden
        mock_save.assert_not_called()


def test_threshold_reached_and_auto_activate_true_flips_to_live(temp_data_dir):
    """Schwelle erreicht UND auto_activate=true -> shadow_mode -> False."""
    from app.meta_labeler import check_and_maybe_activate
    _build_matured_log(temp_data_dir, n_takes=50, n_profitable=35)  # 70% Precision

    config = {
        "meta_labeling": {
            "enabled": True,
            "shadow_mode": True,
            "auto_activate": True,
            "min_trades_to_activate": 50,
            "min_precision_to_activate": 0.65,
        }
    }
    with patch("app.config_manager.save_config") as mock_save:
        result = check_and_maybe_activate(config)
        assert result is True
        assert config["meta_labeling"]["shadow_mode"] is False
        mock_save.assert_called_once()


def test_below_threshold_returns_false_regardless(temp_data_dir):
    """Wenn Precision < threshold, kein Flip — egal welcher auto_activate-Wert."""
    from app.meta_labeler import check_and_maybe_activate
    _build_matured_log(temp_data_dir, n_takes=50, n_profitable=20)  # 40% Precision

    for auto_act in (True, False):
        config = {
            "meta_labeling": {
                "enabled": True,
                "shadow_mode": True,
                "auto_activate": auto_act,
                "min_trades_to_activate": 50,
                "min_precision_to_activate": 0.65,
            }
        }
        result = check_and_maybe_activate(config)
        assert result is False
        assert config["meta_labeling"]["shadow_mode"] is True


def test_too_few_matured_returns_false(temp_data_dir):
    """Wenn weniger als min_trades matured sind, kein Flip moeglich."""
    from app.meta_labeler import check_and_maybe_activate
    _build_matured_log(temp_data_dir, n_takes=10, n_profitable=10)  # 100% aber nur 10

    config = {
        "meta_labeling": {
            "enabled": True,
            "shadow_mode": True,
            "auto_activate": True,
            "min_trades_to_activate": 50,
            "min_precision_to_activate": 0.65,
        }
    }
    result = check_and_maybe_activate(config)
    assert result is False
