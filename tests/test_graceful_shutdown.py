"""Tests fuer R-A25 Graceful-Shutdown-Hook (Sprint-Tag-9 abend 19.05.2026).

Anlass: Container-Restarts fuer Deploys erzeugten ib_insync-Socket-Abriss-
Errors ('Peer closed connection') weil Bot abrupt gekillt wurde. R-A24
filtert die Errors aus Sentry, R-A25 fixt die Wurzel: bei SIGTERM macht
Bot saubere Disconnect-Sequenz BEVOR Container exit.
"""

import signal
from unittest.mock import patch, MagicMock
import pytest


def test_signal_handler_sets_shutdown_flag():
    """SIGTERM-Handler setzt _SHUTDOWN_REQUESTED auf True."""
    from app import scheduler
    # Reset state
    scheduler._SHUTDOWN_REQUESTED = False
    scheduler._SHUTDOWN_REASON = ""

    scheduler._signal_handler(signal.SIGTERM, None)
    assert scheduler._SHUTDOWN_REQUESTED is True
    assert "SIGTERM" in scheduler._SHUTDOWN_REASON


def test_signal_handler_sets_reason_for_sigint():
    """SIGINT (Ctrl+C) wird auch korrekt erfasst."""
    from app import scheduler
    scheduler._SHUTDOWN_REQUESTED = False
    scheduler._SHUTDOWN_REASON = ""

    scheduler._signal_handler(signal.SIGINT, None)
    assert scheduler._SHUTDOWN_REQUESTED is True
    assert "SIGINT" in scheduler._SHUTDOWN_REASON


def test_double_signal_triggers_force_exit():
    """Zweites SIGTERM (impatient user) -> os._exit(1) Force-Exit."""
    from app import scheduler
    scheduler._SHUTDOWN_REQUESTED = True  # bereits ein SIGTERM empfangen

    with patch("os._exit") as mock_exit:
        scheduler._signal_handler(signal.SIGTERM, None)
        mock_exit.assert_called_once_with(1)


def test_graceful_shutdown_calls_ib_disconnect():
    """Graceful-Shutdown ruft ib.disconnect() auf wenn connected."""
    from app import scheduler

    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    mock_broker = MagicMock()
    mock_broker._ib = mock_ib

    with patch("app.broker_base.get_broker", return_value=mock_broker), \
         patch("app.config_manager.load_json", return_value={}), \
         patch("app.config_manager.save_json"), \
         patch("app.alerts.send_pushover"):
        scheduler._graceful_shutdown()

    mock_ib.disconnect.assert_called_once()


def test_graceful_shutdown_skips_disconnect_if_not_connected():
    """Wenn ib bereits disconnected -> kein crash."""
    from app import scheduler

    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = False
    mock_broker = MagicMock()
    mock_broker._ib = mock_ib

    with patch("app.broker_base.get_broker", return_value=mock_broker), \
         patch("app.config_manager.load_json", return_value={}), \
         patch("app.config_manager.save_json"), \
         patch("app.alerts.send_pushover"):
        # Sollte nicht crashen
        scheduler._graceful_shutdown()

    mock_ib.disconnect.assert_not_called()


def test_graceful_shutdown_persists_brain_state():
    """Brain-State wird beim Shutdown gespeichert mit audit_log Eintrag."""
    from app import scheduler

    saved_state = {}

    def fake_save(filename, data):
        saved_state[filename] = data

    with patch("app.broker_base.get_broker", return_value=MagicMock(_ib=None)), \
         patch("app.config_manager.load_json", return_value={"existing": "data"}), \
         patch("app.config_manager.save_json", side_effect=fake_save), \
         patch("app.alerts.send_pushover"):
        scheduler._graceful_shutdown()

    assert "brain_state.json" in saved_state
    brain = saved_state["brain_state.json"]
    assert "audit_log" in brain
    assert len(brain["audit_log"]) >= 1
    last = brain["audit_log"][-1]
    assert last["event"] == "graceful_shutdown"
    assert "timestamp" in last


def test_graceful_shutdown_resilient_to_disconnect_error():
    """Wenn IBKR-Disconnect crashed, geht restlicher Shutdown weiter."""
    from app import scheduler

    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.disconnect.side_effect = RuntimeError("simulated network error")
    mock_broker = MagicMock()
    mock_broker._ib = mock_ib

    saved_state = {}

    with patch("app.broker_base.get_broker", return_value=mock_broker), \
         patch("app.config_manager.load_json", return_value={"existing": "data"}), \
         patch("app.config_manager.save_json", side_effect=lambda fn, d: saved_state.update({fn: d})), \
         patch("app.alerts.send_pushover"):
        # Sollte nicht crashen trotz Disconnect-Error
        scheduler._graceful_shutdown()

    # Brain-State sollte trotzdem geflusht sein
    assert "brain_state.json" in saved_state


def test_graceful_shutdown_resilient_to_save_error():
    """Wenn save_json crashed, geht restlicher Shutdown weiter."""
    from app import scheduler

    with patch("app.broker_base.get_broker", return_value=MagicMock(_ib=None)), \
         patch("app.config_manager.load_json", return_value={}), \
         patch("app.config_manager.save_json", side_effect=IOError("disk full")), \
         patch("app.alerts.send_pushover") as mock_push:
        # Sollte nicht crashen trotz save-Error
        scheduler._graceful_shutdown()

    # Pushover sollte trotzdem versucht werden
    mock_push.assert_called_once()
