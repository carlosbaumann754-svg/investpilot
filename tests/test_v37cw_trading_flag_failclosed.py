"""Tests fuer v37cw — Trading-Flag fail-closed + Cancel-on-Off + Pre-Submit-Hours-Guard.

Episode 05.05.2026: Bot submittete ueber Nacht 6 SCANNER_BUY-Limits fuer AAPL+TSLA
- Trading-Flag stand auf TRUE (Default fail-OPEN bei fehlender Datei)
- US-Markt war zu (ET 00:03–02:22) → Limits liefen pending → MISSED_FILL
- Cancel-on-Trading-Off-Transition fehlte → AMZN-Cover-Order fillte unintended
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch
import pytest


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("INVESTPILOT_DATA_DIR", str(tmp_path))
    import importlib
    from app import config_manager
    importlib.reload(config_manager)
    yield tmp_path


def test_is_trading_enabled_failclosed_when_flag_missing(temp_data_dir):
    """v37cw KERN: fehlende Flag-Datei => Trading DEAKTIVIERT (frueher: True)."""
    import importlib
    from app import scheduler
    importlib.reload(scheduler)
    assert scheduler.is_trading_enabled() is False


def test_is_trading_enabled_true_with_explicit_true(temp_data_dir):
    import importlib
    from app import scheduler
    importlib.reload(scheduler)
    scheduler.TRADING_FLAG.write_text("true")
    assert scheduler.is_trading_enabled() is True


def test_is_trading_enabled_false_with_explicit_false(temp_data_dir):
    import importlib
    from app import scheduler
    importlib.reload(scheduler)
    scheduler.TRADING_FLAG.write_text("false")
    assert scheduler.is_trading_enabled() is False


def test_is_trading_enabled_failclosed_on_empty_file(temp_data_dir):
    """Leere Datei darf NICHT als True interpretiert werden."""
    import importlib
    from app import scheduler
    importlib.reload(scheduler)
    scheduler.TRADING_FLAG.write_text("")
    assert scheduler.is_trading_enabled() is False


def test_data_access_get_trading_status_failclosed(temp_data_dir):
    """Web-UI Data-Access: konsistent fail-closed mit Scheduler."""
    from web.data_access import get_trading_status
    status = get_trading_status()
    assert status["enabled"] is False  # frueher: True


def test_data_access_get_trading_status_empty_file_failclosed(temp_data_dir):
    from app.config_manager import get_data_path
    flag = get_data_path("trading_enabled.flag")
    flag.write_text("")
    from web.data_access import get_trading_status
    assert get_trading_status()["enabled"] is False


def test_ensure_trading_flag_initialized_creates_false_file(temp_data_dir):
    """Boot-Init schreibt 'false' wenn Datei fehlt."""
    import importlib
    from app import scheduler
    importlib.reload(scheduler)
    assert not scheduler.TRADING_FLAG.exists()
    scheduler._ensure_trading_flag_initialized()
    assert scheduler.TRADING_FLAG.exists()
    assert scheduler.TRADING_FLAG.read_text().strip() == "false"


def test_ensure_trading_flag_initialized_idempotent_with_true(temp_data_dir):
    """Wenn Datei schon mit 'true' existiert: NICHT ueberschreiben."""
    import importlib
    from app import scheduler
    importlib.reload(scheduler)
    scheduler.TRADING_FLAG.parent.mkdir(parents=True, exist_ok=True)
    scheduler.TRADING_FLAG.write_text("true")
    scheduler._ensure_trading_flag_initialized()
    assert scheduler.TRADING_FLAG.read_text().strip() == "true"


def test_cancel_all_pending_orders_no_trades(temp_data_dir):
    """Wenn keine offenen IBKR-Trades: returnt 0 ohne Crash."""
    from app import scheduler
    fake_ib = MagicMock()
    fake_ib.openTrades.return_value = []
    fake_broker = MagicMock()
    fake_broker._get_ib.return_value = fake_ib
    with patch("app.ibkr_client.IbkrBroker", return_value=fake_broker):
        n = scheduler.cancel_all_pending_orders(reason="test")
    assert n == 0


def test_cancel_all_pending_orders_cancels_each_open_trade(temp_data_dir):
    from app import scheduler
    t1, t2 = MagicMock(), MagicMock()
    t1.contract.symbol = "AAPL"; t1.order.action = "BUY"
    t1.order.totalQuantity = 100; t1.orderStatus.status = "Submitted"
    t2.contract.symbol = "TSLA"; t2.order.action = "BUY"
    t2.order.totalQuantity = 50; t2.orderStatus.status = "PreSubmitted"
    fake_ib = MagicMock()
    fake_ib.openTrades.return_value = [t1, t2]
    fake_broker = MagicMock()
    fake_broker._get_ib.return_value = fake_ib
    with patch("app.ibkr_client.IbkrBroker", return_value=fake_broker):
        n = scheduler.cancel_all_pending_orders(reason="trading_flag_off")
    assert n == 2
    assert fake_ib.cancelOrder.call_count == 2
