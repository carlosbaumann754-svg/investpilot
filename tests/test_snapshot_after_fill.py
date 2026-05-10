"""v37h Task 2 (10.05.2026) — Snapshot-After-Fill Tests.

Hintergrund: 08.05.2026 Reconcile-Drift-Vorfall — Brain-Snapshot war stale
zwischen Fill und naechstem Trader-Cycle (5-15 Min Luecke). Reconcile-Cron
(alle 30 Min) konnte zwischen Fill und Cycle reinfallen -> Phantom-Drift.

v37f hat das Schema-Problem gefixt aber NICHT den Timing-Gap. Diese Task
schliesst den Gap: direkt nach 'Filled'-Status wird Brain-Snapshot eager
geschrieben. Reconcile sieht damit jederzeit aktuelles Cash + Positionen.

Tests verifizieren:
1. status='Filled' triggert record_snapshot mit live get_portfolio()
2. status='Cancelled'/'Rejected'/'Submitted' triggert NICHT
3. get_portfolio() returnt None -> kein Crash, kein Snapshot
4. record_snapshot raised Exception -> kein Crash, return False
5. Helper isoliert testbar ohne ib_insync (analog Sa-Task-1 _resolve_order_settings)
6. Idempotenz: zweimal aufrufen mit selbem status macht 2 Snapshots (kein
   internal-state, jeder Fill bekommt einen)
"""
from unittest.mock import MagicMock, patch
import pytest


# ============================================================
# Helper: minimal-Broker ohne ib_insync
# ============================================================

def _make_broker(config=None):
    """IbkrBroker mit minimal-config (kein ib_insync nötig)."""
    from app.ibkr_client import IbkrBroker
    return IbkrBroker(config or {"ibkr": {"client_id": 1}})


# ============================================================
# _snapshot_after_fill_safely — Status-Filtering
# ============================================================

def test_filled_triggers_snapshot():
    """status='Filled' ruft get_portfolio + record_snapshot."""
    broker = _make_broker()
    fake_portfolio = {"credit": 100000, "positions": [], "_equity": 100000}
    broker.get_portfolio = MagicMock(return_value=fake_portfolio)

    with patch("app.brain.record_snapshot") as mock_record:
        result = broker._snapshot_after_fill_safely("Filled", "AAPL")

    assert result is True
    broker.get_portfolio.assert_called_once()
    mock_record.assert_called_once_with(fake_portfolio)


def test_cancelled_does_not_trigger_snapshot():
    """status='Cancelled' loest KEINEN Snapshot aus."""
    broker = _make_broker()
    broker.get_portfolio = MagicMock()

    with patch("app.brain.record_snapshot") as mock_record:
        result = broker._snapshot_after_fill_safely("Cancelled", "AAPL")

    assert result is False
    broker.get_portfolio.assert_not_called()
    mock_record.assert_not_called()


def test_rejected_does_not_trigger():
    broker = _make_broker()
    broker.get_portfolio = MagicMock()
    with patch("app.brain.record_snapshot") as mock_record:
        result = broker._snapshot_after_fill_safely("Rejected", "AAPL")
    assert result is False
    mock_record.assert_not_called()


def test_submitted_does_not_trigger():
    """Submitted = Order eingereicht aber noch nicht gefuellt."""
    broker = _make_broker()
    broker.get_portfolio = MagicMock()
    with patch("app.brain.record_snapshot") as mock_record:
        result = broker._snapshot_after_fill_safely("Submitted", "AAPL")
    assert result is False
    mock_record.assert_not_called()


def test_partially_filled_triggers():
    """PartiallyFilled = Teil-Fill -> Cash hat sich geaendert -> Snapshot wichtig."""
    broker = _make_broker()
    fake_portfolio = {"credit": 95000, "positions": [{"x": 1}]}
    broker.get_portfolio = MagicMock(return_value=fake_portfolio)
    with patch("app.brain.record_snapshot") as mock_record:
        result = broker._snapshot_after_fill_safely("PartiallyFilled", "AAPL")
    assert result is True
    mock_record.assert_called_once_with(fake_portfolio)


def test_empty_status_does_not_trigger():
    """Leerer status (z.B. wenn trade.orderStatus None war)."""
    broker = _make_broker()
    broker.get_portfolio = MagicMock()
    with patch("app.brain.record_snapshot") as mock_record:
        result = broker._snapshot_after_fill_safely("", "AAPL")
    assert result is False


# ============================================================
# Defensive: Errors duerfen Order-Flow nicht brechen
# ============================================================

def test_get_portfolio_returns_none_no_crash():
    """get_portfolio() returnt None -> kein Snapshot, kein Crash."""
    broker = _make_broker()
    broker.get_portfolio = MagicMock(return_value=None)

    with patch("app.brain.record_snapshot") as mock_record:
        result = broker._snapshot_after_fill_safely("Filled", "AAPL")

    assert result is False
    mock_record.assert_not_called()


def test_record_snapshot_raises_no_crash():
    """record_snapshot raised Exception -> defensiv gefangen, return False."""
    broker = _make_broker()
    broker.get_portfolio = MagicMock(return_value={"credit": 1, "positions": []})

    with patch("app.brain.record_snapshot", side_effect=RuntimeError("disk full")):
        # WICHTIG: Test darf KEIN raise hochkommen
        result = broker._snapshot_after_fill_safely("Filled", "AAPL")

    assert result is False  # Failure-Mode aber kein Crash


def test_get_portfolio_raises_no_crash():
    """get_portfolio raised (z.B. IB-Disconnect) -> defensiv."""
    broker = _make_broker()
    broker.get_portfolio = MagicMock(side_effect=ConnectionError("ib disconnected"))

    with patch("app.brain.record_snapshot") as mock_record:
        result = broker._snapshot_after_fill_safely("Filled", "AAPL")

    assert result is False
    mock_record.assert_not_called()


# ============================================================
# Idempotenz / Re-call-Semantik
# ============================================================

def test_two_fills_two_snapshots():
    """Helper hat keinen internal state — 2x Filled = 2x Snapshot."""
    broker = _make_broker()
    broker.get_portfolio = MagicMock(return_value={"credit": 1, "positions": []})

    with patch("app.brain.record_snapshot") as mock_record:
        broker._snapshot_after_fill_safely("Filled", "AAPL")
        broker._snapshot_after_fill_safely("Filled", "TSLA")

    assert mock_record.call_count == 2


# ============================================================
# Symbol-Logging — fuer Postmortem-Debugging
# ============================================================

def test_logs_symbol_on_success(caplog):
    """Erfolgs-Log enthaelt Symbol fuer Audit-Trail."""
    import logging
    broker = _make_broker()
    broker.get_portfolio = MagicMock(return_value={"credit": 1, "positions": []})

    with patch("app.brain.record_snapshot"):
        with caplog.at_level(logging.INFO, logger="app.ibkr_client"):
            broker._snapshot_after_fill_safely("Filled", "AAPL")

    assert any("AAPL" in r.message for r in caplog.records)


def test_logs_symbol_on_failure(caplog):
    """Failure-Log enthaelt Symbol fuer Postmortem."""
    import logging
    broker = _make_broker()
    broker.get_portfolio = MagicMock(return_value={"credit": 1, "positions": []})

    with patch("app.brain.record_snapshot", side_effect=RuntimeError("boom")):
        with caplog.at_level(logging.WARNING, logger="app.ibkr_client"):
            broker._snapshot_after_fill_safely("Filled", "TSLA")

    assert any("TSLA" in r.message for r in caplog.records)
