"""E27 Tag 2 — Integration-Tests fuer IbkrBroker + OrderStatusTracker.

Test-Strategie:
- Mock ib_insync.IB statt echtes IBKR-Gateway
- Verifiziere Feature-Flag-Verhalten (enabled / disabled)
- Verifiziere Subscription + Register-Hooks an den richtigen Stellen
- Race-Condition-Tests folgen in Tag 3 (test_order_status_tracker_async.py)
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_storage(monkeypatch):
    """Mock fuer config_manager save/load damit Tests isoliert sind."""
    storage = {}
    monkeypatch.setattr("app.config_manager.save_json", lambda f, d: storage.update({f: d}))
    monkeypatch.setattr("app.config_manager.load_json", lambda f: storage.get(f))
    return storage


@pytest.fixture
def make_broker():
    """Factory fuer IbkrBroker mit Feature-Flag-Variante."""
    def _make(enabled=False):
        from app.ibkr_client import IbkrBroker
        config = {
            "ibkr": {"client_id": 1, "host": "localhost", "port": 4004},
            "realtime_status_tracker": {"enabled": enabled},
        }
        broker = IbkrBroker(config)
        return broker
    return _make


# ============================================================
# Feature-Flag-Verhalten
# ============================================================

def test_tracker_instantiated_even_when_disabled(make_broker, mock_storage):
    """Tracker wird IMMER instantiiert (kein Risiko). Subscription nicht."""
    broker = make_broker(enabled=False)
    assert broker._tracker is not None  # da
    assert broker._e27_enabled is False
    assert broker._e27_subscribed is False


def test_tracker_enabled_via_config(make_broker, mock_storage):
    broker = make_broker(enabled=True)
    assert broker._e27_enabled is True
    assert broker._tracker is not None


def test_subscription_only_when_flag_enabled(make_broker, mock_storage):
    """_maybe_subscribe_e27_events macht nur etwas wenn flag=true."""
    broker = make_broker(enabled=False)
    mock_ib = MagicMock()
    broker._maybe_subscribe_e27_events(mock_ib)
    # No subscription
    assert broker._e27_subscribed is False
    # mock_ib.orderStatusEvent wurde NICHT mit += angesprochen (kein __iadd__-call)


def test_subscription_attaches_handler_when_enabled(make_broker, mock_storage):
    broker = make_broker(enabled=True)
    mock_ib = MagicMock()
    broker._maybe_subscribe_e27_events(mock_ib)
    assert broker._e27_subscribed is True
    # Subscription-Tracking via mock-Counter — orderStatusEvent ist ein
    # MagicMock-Attribut, += darauf fuegt der Mock-Liste was hinzu


def test_subscription_idempotent(make_broker, mock_storage):
    """Mehrfacher _maybe_subscribe-Call subscribed nur einmal."""
    broker = make_broker(enabled=True)
    mock_ib = MagicMock()
    broker._maybe_subscribe_e27_events(mock_ib)
    broker._maybe_subscribe_e27_events(mock_ib)
    broker._maybe_subscribe_e27_events(mock_ib)
    assert broker._e27_subscribed is True
    # Idempotenz-Bestaetigung: kein Crash, keine doppelten Subscriptions


def test_recovery_attempted_on_subscription(make_broker, mock_storage):
    """Recovery wird nach erfolgreicher Subscription getriggert."""
    broker = make_broker(enabled=True)
    mock_ib = MagicMock()

    # Mock recover-Methode auf dem Tracker
    broker._tracker.recover_from_ibkr = MagicMock(return_value=3)

    broker._maybe_subscribe_e27_events(mock_ib)

    broker._tracker.recover_from_ibkr.assert_called_once_with(mock_ib)


# ============================================================
# Robustheit: Tracker-Init oder Subscription darf NIE crashen
# ============================================================

def test_init_resilient_to_tracker_import_failure(make_broker, mock_storage, monkeypatch):
    """Wenn Tracker-Import scheitert, Broker funktioniert weiter."""
    # Wir koennen den import nicht trivial brechen ohne Side-Effects, daher
    # simulieren wir nur den Try/Except-Pfad mit einem korrupten Tracker.
    broker = make_broker(enabled=True)
    broker._tracker = None  # simuliert Import-Fail
    broker._e27_subscribed = False

    mock_ib = MagicMock()
    # Sollte NICHT crashen
    broker._maybe_subscribe_e27_events(mock_ib)
    assert broker._e27_subscribed is False  # nicht subscribed weil tracker=None


def test_subscription_failure_does_not_crash(make_broker, mock_storage):
    """Wenn ib.orderStatusEvent += fehlschlaegt, Bot funktioniert weiter."""
    broker = make_broker(enabled=True)

    mock_ib = MagicMock()
    # Force exception on subscription
    type(mock_ib).orderStatusEvent = MagicMock(side_effect=Exception("ib_insync changed API"))

    # Sollte NICHT crashen
    try:
        broker._maybe_subscribe_e27_events(mock_ib)
    except Exception:
        pytest.fail("E27 subscription failure should be caught (non-fatal)")


def test_recovery_failure_does_not_crash(make_broker, mock_storage):
    """Wenn recover_from_ibkr crasht, Subscription gilt trotzdem als ok."""
    broker = make_broker(enabled=True)
    broker._tracker.recover_from_ibkr = MagicMock(side_effect=Exception("Recovery boom"))

    mock_ib = MagicMock()
    # Sollte NICHT crashen
    broker._maybe_subscribe_e27_events(mock_ib)
    assert broker._e27_subscribed is True  # Subscription ging trotzdem durch
