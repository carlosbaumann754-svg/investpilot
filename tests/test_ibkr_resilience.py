"""v37h Task 2d (10.05.2026) — IB-Gateway-Resilience Tests.

Hintergrund: Daily-Restart-Cron (03:00 UTC) handhabt geplante Restarts.
Mid-day Socket-Drops (Network-Blip, IBG-Garbage-Collection-Pause, IBKR-
Server-Restarts in Quiet-Hours) sind aber NICHT abgefangen — Trader-Cycle
crasht oder bleibt stehen.

Spec _ensure_connected(max_retries, backoff_base_s):
  - Wraps _get_ib() mit exponential backoff
  - Default 3 Retries (5s, 15s, 45s)
  - Returns True bei healthy connection, False bei Final-Failure
  - log.error bei Final-Failure -> automatisch Sentry-Alert (08.05.-Setup)

Tests verifizieren:
1. _get_ib() OK first try -> True, kein retry, kein sleep
2. _get_ib() raises 2x, succeeds 3x -> True, 2 sleeps mit 5s+15s
3. _get_ib() raises alle Versuche -> False, log.error
4. _get_ib() liefert disconnected ib -> retry
5. Backoff-Werte: 5s, 15s, 45s (3^n exponential)
6. Custom max_retries=0 -> ein Versuch, kein retry
"""
from unittest.mock import MagicMock, patch
import pytest


def _make_broker():
    from app.ibkr_client import IbkrBroker
    return IbkrBroker({"ibkr": {"client_id": 1}})


# ============================================================
# Happy-Path: erste Verbindung healthy
# ============================================================

def test_first_attempt_succeeds_no_sleep():
    """_get_ib() returnt connected ib first try -> kein retry, kein sleep."""
    broker = _make_broker()
    fake_ib = MagicMock()
    fake_ib.isConnected.return_value = True
    broker._get_ib = MagicMock(return_value=fake_ib)

    with patch("time.sleep") as mock_sleep:
        result = broker._ensure_connected()

    assert result is True
    broker._get_ib.assert_called_once()
    mock_sleep.assert_not_called()


# ============================================================
# Retry-Path: ein paar Failures, dann success
# ============================================================

def test_two_failures_then_success():
    """_get_ib() raises 2x, klappt 3x -> True, 2 sleeps."""
    broker = _make_broker()
    fake_ib = MagicMock()
    fake_ib.isConnected.return_value = True
    broker._get_ib = MagicMock(side_effect=[
        ConnectionError("blip 1"),
        ConnectionError("blip 2"),
        fake_ib,
    ])

    with patch("time.sleep") as mock_sleep:
        result = broker._ensure_connected(backoff_base_s=5.0)

    assert result is True
    assert broker._get_ib.call_count == 3
    # Sleep calls: backoff_base * 3^0 = 5, dann 5 * 3^1 = 15
    sleep_args = [c.args[0] for c in mock_sleep.call_args_list]
    assert sleep_args == [5.0, 15.0]


def test_disconnected_ib_triggers_retry():
    """_get_ib returnt ib aber isConnected=False -> retry."""
    broker = _make_broker()
    dead_ib = MagicMock()
    dead_ib.isConnected.return_value = False
    live_ib = MagicMock()
    live_ib.isConnected.return_value = True
    broker._get_ib = MagicMock(side_effect=[dead_ib, live_ib])

    with patch("time.sleep"):
        result = broker._ensure_connected()

    assert result is True
    assert broker._get_ib.call_count == 2


# ============================================================
# Final-Fail: alle Retries gehen schief
# ============================================================

def test_all_retries_fail_returns_false(caplog):
    """3 retries fail -> return False, log.error."""
    import logging
    broker = _make_broker()
    broker._get_ib = MagicMock(side_effect=ConnectionError("ibg down"))

    with patch("time.sleep"):
        with caplog.at_level(logging.ERROR, logger="app.ibkr_client"):
            result = broker._ensure_connected(max_retries=3)

    assert result is False
    # 1 initial + 3 retries = 4 calls
    assert broker._get_ib.call_count == 4
    error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("FINAL" in m or "Safe-Mode" in m for m in error_msgs)


def test_zero_retries_one_attempt():
    """max_retries=0 -> nur ein Versuch."""
    broker = _make_broker()
    broker._get_ib = MagicMock(side_effect=ConnectionError("nope"))

    with patch("time.sleep") as mock_sleep:
        result = broker._ensure_connected(max_retries=0)

    assert result is False
    assert broker._get_ib.call_count == 1
    mock_sleep.assert_not_called()


# ============================================================
# Backoff-Sequence-Verifikation (3^n)
# ============================================================

def test_backoff_sequence_5_15_45():
    """Default backoff: 5s, 15s, 45s — 3^n exponential."""
    broker = _make_broker()
    broker._get_ib = MagicMock(side_effect=ConnectionError("x"))

    with patch("time.sleep") as mock_sleep:
        broker._ensure_connected(max_retries=3, backoff_base_s=5.0)

    # 3 retries -> 3 sleeps zwischen den Versuchen
    sleep_args = [c.args[0] for c in mock_sleep.call_args_list]
    assert sleep_args == [5.0, 15.0, 45.0]


def test_custom_backoff_base():
    """backoff_base_s=2.0 -> 2, 6, 18."""
    broker = _make_broker()
    broker._get_ib = MagicMock(side_effect=ConnectionError("x"))

    with patch("time.sleep") as mock_sleep:
        broker._ensure_connected(max_retries=3, backoff_base_s=2.0)

    sleep_args = [c.args[0] for c in mock_sleep.call_args_list]
    assert sleep_args == [2.0, 6.0, 18.0]


# ============================================================
# Recovery-Logging
# ============================================================

def test_recovery_logs_attempts(caplog):
    """Erfolgreicher Reconnect nach Fail loggt 'Reconnect erfolgreich'."""
    import logging
    broker = _make_broker()
    fake_ib = MagicMock()
    fake_ib.isConnected.return_value = True
    broker._get_ib = MagicMock(side_effect=[ConnectionError("blip"), fake_ib])

    with patch("time.sleep"):
        with caplog.at_level(logging.INFO, logger="app.ibkr_client"):
            broker._ensure_connected()

    info_msgs = [r.message for r in caplog.records]
    # Mind. eine Recovery-Meldung
    assert any("Reconnect" in m or "erfolgreich" in m for m in info_msgs)


def test_warn_log_on_each_failed_attempt(caplog):
    """Jeder failed-Versuch loggt warn (nicht info, damit sichtbar)."""
    import logging
    broker = _make_broker()
    broker._get_ib = MagicMock(side_effect=ConnectionError("x"))

    with patch("time.sleep"):
        with caplog.at_level(logging.WARNING, logger="app.ibkr_client"):
            broker._ensure_connected(max_retries=2)

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    # 1 initial + 2 retries = 3 attempts -> mind. 3 WARN-Messages
    assert len(warns) >= 3
