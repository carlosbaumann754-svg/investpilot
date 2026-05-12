"""v37h Tab-Audit-Day-2 (12.05.2026) — Tests fuer FAILED-Alert-Bypass.

Carlos's Beobachtung: Telegram-disabled blockte ALLE Trade-Alerts
inkl. _FAILED-Pfade. Pushover war konfiguriert aber wurde nie erreicht
weil _tg_notify_enabled() Telegram-zentriert war.

Fix-Verify: FAILED-Actions umgehen den Telegram-Filter und werden
IMMER per send_alert(level='ERROR') geschickt. Erfolgreiche Trades
bleiben silent (kein Spam fuer Carlos's Workflow).
"""
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def cfg_pushover_only():
    """Realistische Carlos-Config: Telegram aus, Pushover an."""
    return {
        "alerts": {
            "telegram": {"enabled": False, "notify_trades": True,
                         "notify_stop_loss": True},
            "pushover": {"enabled": True, "user_key": "u", "api_token": "t",
                         "priority_map": {"INFO": 0, "WARNING": 0, "TRADE": 0,
                                          "ERROR": 1, "CRITICAL": 2}},
        }
    }


# ============================================================
# Erfolgreiche Trades: keine Pushover (kein Spam)
# ============================================================

def test_successful_buy_does_not_send_alert(cfg_pushover_only):
    """SCANNER_BUY mit Telegram=False sollte KEINEN send_alert ausloesen."""
    from app.alerts import alert_trade_executed
    trade = {"action": "SCANNER_BUY", "symbol": "AAPL", "amount_usd": 5000}
    with patch("app.alerts.send_alert") as mock_send:
        alert_trade_executed(trade, config=cfg_pushover_only)
    mock_send.assert_not_called()


def test_successful_stop_loss_close_does_not_send_alert(cfg_pushover_only):
    """STOP_LOSS_CLOSE (success) mit Telegram=False -> kein Alert (geblockt)."""
    from app.alerts import alert_trade_executed
    trade = {"action": "STOP_LOSS_CLOSE", "symbol": "TSLA", "pnl_pct": -3.2,
             "pnl_usd": -300}
    with patch("app.alerts.send_alert") as mock_send:
        alert_trade_executed(trade, config=cfg_pushover_only)
    mock_send.assert_not_called()


def test_successful_partial_close_does_not_send_alert(cfg_pushover_only):
    """PARTIAL_CLOSE (success) -> silent (kein Spam)."""
    from app.alerts import alert_trade_executed
    trade = {"action": "PARTIAL_CLOSE", "symbol": "NVDA", "pnl_pct": 4.5}
    with patch("app.alerts.send_alert") as mock_send:
        alert_trade_executed(trade, config=cfg_pushover_only)
    mock_send.assert_not_called()


# ============================================================
# FAILED-Trades: IMMER Pushover als ERROR-Level
# ============================================================

def test_partial_close_failed_sends_alert_with_error_level(cfg_pushover_only):
    """PARTIAL_CLOSE_FAILED -> send_alert mit level='ERROR' (Pushover Priority 1)."""
    from app.alerts import alert_trade_executed
    trade = {"action": "PARTIAL_CLOSE_FAILED", "symbol": "AAPL", "pnl_pct": 4.2}
    with patch("app.alerts.send_alert") as mock_send:
        alert_trade_executed(trade, config=cfg_pushover_only)
    mock_send.assert_called_once()
    _msg, level, _cfg = mock_send.call_args[0]
    assert level == "ERROR"


def test_stop_loss_close_failed_bypasses_telegram_filter(cfg_pushover_only):
    """STOP_LOSS_CLOSE_FAILED auch mit Telegram=False -> Alert wird gesendet."""
    from app.alerts import alert_trade_executed
    trade = {"action": "STOP_LOSS_CLOSE_FAILED", "symbol": "META", "pnl_pct": -3.5}
    with patch("app.alerts.send_alert") as mock_send:
        alert_trade_executed(trade, config=cfg_pushover_only)
    mock_send.assert_called_once()


def test_take_profit_close_failed_bypasses_telegram_filter(cfg_pushover_only):
    """TAKE_PROFIT_CLOSE_FAILED analog -> bypass."""
    from app.alerts import alert_trade_executed
    trade = {"action": "TAKE_PROFIT_CLOSE_FAILED", "symbol": "NVDA", "pnl_pct": 15.5}
    with patch("app.alerts.send_alert") as mock_send:
        alert_trade_executed(trade, config=cfg_pushover_only)
    mock_send.assert_called_once()


def test_trailing_sl_close_failed_bypasses_telegram_filter(cfg_pushover_only):
    """TRAILING_SL_CLOSE_FAILED analog -> bypass."""
    from app.alerts import alert_trade_executed
    trade = {"action": "TRAILING_SL_CLOSE_FAILED", "symbol": "TSLA", "pnl_pct": 8.2}
    with patch("app.alerts.send_alert") as mock_send:
        alert_trade_executed(trade, config=cfg_pushover_only)
    mock_send.assert_called_once()


def test_earnings_blackout_close_failed_bypasses_filter(cfg_pushover_only):
    """EARNINGS_BLACKOUT_CLOSE_FAILED analog."""
    from app.alerts import alert_trade_executed
    trade = {"action": "EARNINGS_BLACKOUT_CLOSE_FAILED", "symbol": "ROKU",
             "pnl_pct": 12.5}
    with patch("app.alerts.send_alert") as mock_send:
        alert_trade_executed(trade, config=cfg_pushover_only)
    mock_send.assert_called_once()


# ============================================================
# Message-Format-Verify
# ============================================================

def test_failed_message_contains_action_and_symbol(cfg_pushover_only):
    """Message muss action + symbol enthalten fuer Pushover-Klarheit."""
    from app.alerts import alert_trade_executed
    trade = {"action": "PARTIAL_CLOSE_FAILED", "symbol": "AAPL", "pnl_pct": 4.2}
    with patch("app.alerts.send_alert") as mock_send:
        alert_trade_executed(trade, config=cfg_pushover_only)
    msg = mock_send.call_args[0][0]
    assert "PARTIAL_CLOSE_FAILED" in msg
    assert "AAPL" in msg
    assert "OFFEN" in msg  # Carlos sieht sofort: muss handeln


def test_failed_message_includes_tranche_info_for_partial(cfg_pushover_only):
    """Bei PARTIAL_CLOSE_FAILED zusaetzlich Tranche-Info im Push."""
    from app.alerts import alert_trade_executed
    trade = {
        "action": "PARTIAL_CLOSE_FAILED", "symbol": "TSLA", "pnl_pct": 8.5,
        "tranche_close_pct": 30, "tranche_target_pct": 8,
    }
    with patch("app.alerts.send_alert") as mock_send:
        alert_trade_executed(trade, config=cfg_pushover_only)
    msg = mock_send.call_args[0][0]
    assert "30%" in msg
    assert "+8%" in msg


def test_failed_message_includes_reason_if_provided(cfg_pushover_only):
    """reason-Feld vom Caller wird in Message uebernommen."""
    from app.alerts import alert_trade_executed
    trade = {
        "action": "PARTIAL_CLOSE_FAILED", "symbol": "EEM", "pnl_pct": 4.5,
        "reason": "IBKR Liquidity-Halt fuer EEM",
    }
    with patch("app.alerts.send_alert") as mock_send:
        alert_trade_executed(trade, config=cfg_pushover_only)
    msg = mock_send.call_args[0][0]
    assert "IBKR Liquidity-Halt" in msg


# ============================================================
# Edge: Telegram enabled bleibt funktional (backwards-compat)
# ============================================================

def test_telegram_enabled_still_sends_success_trades():
    """Wenn Carlos Telegram irgendwann aktiviert: erfolgreiche Trades
    sollten wieder durchschalten (kein Regression)."""
    from app.alerts import alert_trade_executed
    cfg_both = {
        "alerts": {
            "telegram": {"enabled": True, "notify_trades": True,
                         "notify_stop_loss": True},
            "pushover": {"enabled": True, "user_key": "u", "api_token": "t",
                         "priority_map": {"TRADE": 0, "ERROR": 1}},
        }
    }
    trade = {"action": "SCANNER_BUY", "symbol": "AAPL", "amount_usd": 5000}
    with patch("app.alerts.send_alert") as mock_send:
        alert_trade_executed(trade, config=cfg_both)
    mock_send.assert_called_once()


def test_telegram_disabled_failed_action_still_alerts():
    """Wenn beide channels enabled aber notify_trades=False fuer Telegram:
    FAILED-Action muss trotzdem gesendet werden."""
    from app.alerts import alert_trade_executed
    cfg = {
        "alerts": {
            "telegram": {"enabled": True, "notify_trades": False,
                         "notify_stop_loss": False},
            "pushover": {"enabled": True, "user_key": "u", "api_token": "t",
                         "priority_map": {"ERROR": 1}},
        }
    }
    # Erfolgreicher Trade: blockiert weil notify_trades=False
    trade_ok = {"action": "SCANNER_BUY", "symbol": "AAPL"}
    with patch("app.alerts.send_alert") as mock_send:
        alert_trade_executed(trade_ok, config=cfg)
    mock_send.assert_not_called()

    # FAILED-Trade: durchschalten trotz notify_trades=False
    trade_fail = {"action": "PARTIAL_CLOSE_FAILED", "symbol": "AAPL"}
    with patch("app.alerts.send_alert") as mock_send:
        alert_trade_executed(trade_fail, config=cfg)
    mock_send.assert_called_once()
