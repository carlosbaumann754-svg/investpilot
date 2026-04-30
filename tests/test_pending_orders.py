"""Tests fuer Pending-Orders-Visibility (v37bb, Lite-D)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.pending_orders import get_live_pending_orders, summary


class _FakeContract:
    def __init__(self, symbol="ROKU", conId=290651477):
        self.symbol = symbol
        self.conId = conId


class _FakeOrder:
    def __init__(self, action="SELL", qty=1383, lmt=112.06, oid=70):
        self.orderId = oid
        self.permId = 1813839802
        self.action = action
        self.totalQuantity = qty
        self.lmtPrice = lmt
        self.orderType = "LMT"
        self.tif = "DAY"


class _FakeOrderStatus:
    def __init__(self, status="Submitted", filled=0, remaining=1383):
        self.status = status
        self.filled = filled
        self.remaining = remaining


class _FakeTrade:
    def __init__(self, status="Submitted", action="SELL"):
        self.contract = _FakeContract()
        self.order = _FakeOrder(action=action)
        self.orderStatus = _FakeOrderStatus(status=status)


def test_get_live_pending_returns_pending_only():
    """Nur Submitted/PreSubmitted/PendingSubmit/PendingCancel zaehlen."""
    fake_ib = MagicMock()
    fake_ib.openTrades.return_value = [
        _FakeTrade(status="Submitted"),
        _FakeTrade(status="Filled"),       # nicht pending
        _FakeTrade(status="Cancelled"),    # nicht pending
        _FakeTrade(status="PreSubmitted"),
    ]

    fake_broker = MagicMock()
    fake_broker._get_ib.return_value = fake_ib

    with patch("app.ibkr_client.IbkrBroker", return_value=fake_broker):
        result = get_live_pending_orders()
    assert len(result) == 2
    assert all(r["status"] in ("Submitted", "PreSubmitted") for r in result)


def test_get_live_pending_no_ib_returns_empty():
    """Wenn IbkrBroker fehlschlaegt -> leere Liste, kein Crash."""
    with patch("app.ibkr_client.IbkrBroker", side_effect=ImportError):
        result = get_live_pending_orders()
    assert result == []


def test_get_live_pending_extracts_fields():
    fake_ib = MagicMock()
    fake_ib.openTrades.return_value = [_FakeTrade(status="Submitted", action="SELL")]
    fake_broker = MagicMock()
    fake_broker._get_ib.return_value = fake_ib

    with patch("app.ibkr_client.IbkrBroker", return_value=fake_broker):
        result = get_live_pending_orders()
    assert len(result) == 1
    r = result[0]
    assert r["symbol"] == "ROKU"
    assert r["action"] == "SELL"
    assert r["qty"] == 1383.0
    assert r["limit_price"] == 112.06
    assert r["status"] == "Submitted"


def test_summary_aggregates_by_status_and_action():
    fake_ib = MagicMock()
    fake_ib.openTrades.return_value = [
        _FakeTrade(status="Submitted", action="SELL"),
        _FakeTrade(status="Submitted", action="BUY"),
        _FakeTrade(status="PreSubmitted", action="SELL"),
        _FakeTrade(status="Filled", action="SELL"),  # nicht pending
    ]
    fake_broker = MagicMock()
    fake_broker._get_ib.return_value = fake_ib

    with patch("app.ibkr_client.IbkrBroker", return_value=fake_broker):
        s = summary()
    assert s["total_pending"] == 3
    assert s["by_status"]["Submitted"] == 2
    assert s["by_status"]["PreSubmitted"] == 1
    assert s["by_action"]["SELL"] == 2
    assert s["by_action"]["BUY"] == 1


def test_summary_empty_no_pending():
    fake_ib = MagicMock()
    fake_ib.openTrades.return_value = []
    fake_broker = MagicMock()
    fake_broker._get_ib.return_value = fake_ib

    with patch("app.ibkr_client.IbkrBroker", return_value=fake_broker):
        s = summary()
    assert s["total_pending"] == 0
    assert s["by_status"] == {}


def test_get_live_pending_handles_broken_trade():
    """Trade ohne contract oder order wird uebersprungen, kein Crash."""
    bad_trade = MagicMock(spec=[])
    bad_trade.contract = None
    bad_trade.order = None

    fake_ib = MagicMock()
    fake_ib.openTrades.return_value = [
        bad_trade,
        _FakeTrade(status="Submitted"),
    ]
    fake_broker = MagicMock()
    fake_broker._get_ib.return_value = fake_ib

    with patch("app.ibkr_client.IbkrBroker", return_value=fake_broker):
        result = get_live_pending_orders()
    assert len(result) == 1  # nur das gute trade
