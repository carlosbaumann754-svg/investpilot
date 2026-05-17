"""v37h+2 (17.05.2026) — Tests fuer WFO-Drift-Watchdog (Option C).

Verifiziert:
  - Sharpe-Berechnung aus trade_history.json (Edge-Cases)
  - Drift-Threshold-Trigger (Pushover bei > 30% Decay)
  - Throttle (max 1 Alert pro 24h)
  - Skip-Logik (zu wenig Trades, kein WFO-Status, disabled)
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


@pytest.fixture
def mock_state():
    state = {}

    def fake_load(filename):
        return state.get(filename)

    def fake_save(filename, data):
        state[filename] = data

    with patch("app.wfo_drift_watchdog.load_json", side_effect=fake_load,
               create=True), \
         patch("app.wfo_drift_watchdog.save_json", side_effect=fake_save,
               create=True), \
         patch("app.config_manager.load_json", side_effect=fake_load), \
         patch("app.config_manager.save_json", side_effect=fake_save), \
         patch("app.config_manager.load_config", return_value={}):
        yield state


def _trade(action="STOP_LOSS_CLOSE", pnl=1.0, days_ago=1):
    return {
        "action": action,
        "pnl_pct": pnl,
        "timestamp": (datetime.now() - timedelta(days=days_ago)).isoformat(),
    }


def test_drift_skip_when_no_wfo_data(mock_state):
    """Ohne wfo_status.json -> Skip."""
    from app.wfo_drift_watchdog import check_wfo_drift
    r = check_wfo_drift({"wfo_drift_watchdog": {"enabled": True}})
    assert r["wfo_sharpe"] is None
    assert "Keine WFO-Daten" in r["skip_reason"]


def test_drift_skip_when_disabled(mock_state):
    """Feature disabled -> Skip."""
    from app.wfo_drift_watchdog import check_wfo_drift
    mock_state["wfo_status.json"] = {"windows": [{"oos_sharpe": 5.0}]}
    r = check_wfo_drift({"wfo_drift_watchdog": {"enabled": False}})
    assert "disabled" in r["skip_reason"].lower()


def test_drift_skip_too_few_trades(mock_state):
    """< min_trades -> Skip."""
    from app.wfo_drift_watchdog import check_wfo_drift
    mock_state["wfo_status.json"] = {"windows": [{"oos_sharpe": 5.0}]}
    mock_state["trade_history.json"] = [
        _trade(pnl=1.0, days_ago=i) for i in range(5)  # nur 5, min=10
    ]
    r = check_wfo_drift({"wfo_drift_watchdog": {"enabled": True, "min_trades": 10}})
    assert r["live_sharpe"] is None
    assert "Zu wenig Trades" in r["skip_reason"]


def test_drift_healthy_no_alert(mock_state):
    """Live-Sharpe nahe WFO-Sharpe -> kein Alert."""
    from app.wfo_drift_watchdog import check_wfo_drift
    mock_state["wfo_status.json"] = {"windows": [{"oos_sharpe": 5.0}]}
    # Konstruiere 20 Trades mit mean=1, std=0.2 -> Sharpe=5.0 (= WFO)
    pnls = [1.0, 1.2, 0.8, 1.1, 0.9, 1.0, 1.2, 0.8, 1.0, 1.1,
            0.9, 1.0, 1.2, 0.8, 1.0, 1.1, 0.9, 1.0, 1.2, 0.8]
    mock_state["trade_history.json"] = [
        _trade(pnl=p, days_ago=i+1) for i, p in enumerate(pnls)
    ]
    with patch("app.alerts.send_alert") as mock_alert:
        r = check_wfo_drift({"wfo_drift_watchdog": {"enabled": True, "min_trades": 10}})
    assert r["alert_triggered"] is False
    mock_alert.assert_not_called()


def test_drift_alert_on_severe_decay(mock_state):
    """Live-Sharpe << WFO-Sharpe -> Alert."""
    from app.wfo_drift_watchdog import check_wfo_drift
    mock_state["wfo_status.json"] = {"windows": [{"oos_sharpe": 5.0}]}
    # Konstruiere 20 negative Trades -> Live-Sharpe negativ
    pnls = [-0.5, -1.0, -0.3, -0.8, -0.6, -0.4, -0.9, -0.5, -0.7, -0.6,
            -0.4, -0.8, -0.5, -0.6, -0.7, -0.5, -0.4, -0.9, -0.6, -0.5]
    mock_state["trade_history.json"] = [
        _trade(pnl=p, days_ago=i+1) for i, p in enumerate(pnls)
    ]
    with patch("app.wfo_drift_watchdog.send_alert", create=True) as mock_alert, \
         patch("app.alerts.send_alert") as mock_alert2:
        r = check_wfo_drift({"wfo_drift_watchdog": {
            "enabled": True, "min_trades": 10, "drift_threshold_pct": 30,
        }})
    assert r["live_sharpe"] is not None
    assert r["live_sharpe"] < 0
    assert r["drift_pct"] < -100  # extremer Decay
    assert r["alert_triggered"] is True


def test_drift_alert_throttle(mock_state):
    """Zweiter Drift innerhalb 24h -> kein zweiter Alert."""
    from app.wfo_drift_watchdog import check_wfo_drift
    # State so setzen dass letzter Alert vor 1h war
    mock_state["wfo_drift_alert_state.json"] = {
        "last_alert_at": (datetime.now() - timedelta(hours=1)).isoformat(),
    }
    mock_state["wfo_status.json"] = {"windows": [{"oos_sharpe": 5.0}]}
    # 20 Trades mit Varianz aber negativem Mean -> Drift > 30%
    pnls = [-0.5, -0.8, -0.3, -0.7, -0.4, -0.6, -0.9, -0.2, -0.5, -0.8,
            -0.4, -0.6, -0.3, -0.7, -0.5, -0.8, -0.4, -0.6, -0.5, -0.7]
    mock_state["trade_history.json"] = [
        _trade(pnl=p, days_ago=i+1) for i, p in enumerate(pnls)
    ]
    with patch("app.alerts.send_alert") as mock_alert:
        r = check_wfo_drift({"wfo_drift_watchdog": {
            "enabled": True, "min_trades": 10,
            "drift_threshold_pct": 30, "alert_throttle_hours": 24,
        }})
    assert r["alert_triggered"] is False
    assert "gethrottlet" in r["skip_reason"]
    mock_alert.assert_not_called()


def test_drift_alert_after_throttle_expires(mock_state):
    """Letzter Alert > 24h her -> wieder erlaubt."""
    from app.wfo_drift_watchdog import check_wfo_drift
    mock_state["wfo_drift_alert_state.json"] = {
        "last_alert_at": (datetime.now() - timedelta(hours=30)).isoformat(),
    }
    mock_state["wfo_status.json"] = {"windows": [{"oos_sharpe": 5.0}]}
    pnls = [-0.5, -0.8, -0.3, -0.7, -0.4, -0.6, -0.9, -0.2, -0.5, -0.8,
            -0.4, -0.6, -0.3, -0.7, -0.5, -0.8, -0.4, -0.6, -0.5, -0.7]
    mock_state["trade_history.json"] = [
        _trade(pnl=p, days_ago=i+1) for i, p in enumerate(pnls)
    ]
    with patch("app.alerts.send_alert") as mock_alert:
        r = check_wfo_drift({"wfo_drift_watchdog": {
            "enabled": True, "min_trades": 10,
            "drift_threshold_pct": 30, "alert_throttle_hours": 24,
        }})
    assert r["alert_triggered"] is True


def test_drift_only_counts_close_trades(mock_state):
    """SCANNER_BUY-Trades werden ignoriert (kein realisierter PnL)."""
    from app.wfo_drift_watchdog import check_wfo_drift
    mock_state["wfo_status.json"] = {"windows": [{"oos_sharpe": 5.0}]}
    # 15 BUYs + 12 CLOSEs
    trades = [_trade("SCANNER_BUY", 0, i) for i in range(15)]
    trades += [_trade("STOP_LOSS_CLOSE", 1.0, i+1) for i in range(12)]
    mock_state["trade_history.json"] = trades
    r = check_wfo_drift({"wfo_drift_watchdog": {"enabled": True, "min_trades": 10}})
    # n=12 (nur Closes gezaehlt)
    assert r["n_trades"] == 12


def test_drift_only_counts_recent_trades(mock_state):
    """Trades aelter als lookback_days werden ignoriert."""
    from app.wfo_drift_watchdog import check_wfo_drift
    mock_state["wfo_status.json"] = {"windows": [{"oos_sharpe": 5.0}]}
    # 5 frische + 20 alte
    recent = [_trade("STOP_LOSS_CLOSE", 1.0, i+1) for i in range(5)]
    old = [_trade("STOP_LOSS_CLOSE", 1.0, 100 + i) for i in range(20)]
    mock_state["trade_history.json"] = recent + old
    r = check_wfo_drift({"wfo_drift_watchdog": {
        "enabled": True, "min_trades": 10, "lookback_days": 30
    }})
    # 5 frische < min_trades=10 -> Skip
    assert r["n_trades"] == 5
    assert "Zu wenig" in r["skip_reason"]
