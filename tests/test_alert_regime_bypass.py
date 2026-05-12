"""v37h Tab-Audit-Day-2 (12.05.2026) — Tests fuer Regime-Alert-Bypass.

Carlos's Bug-Hunt 12.05.2026 entdeckte: 5 weitere Alert-Funktionen
nutzen _tg_notify_enabled-Filter, der bei Carlos's telegram.enabled=
False ALLES blockt. Pattern A1 vom Bug-Hunt-Report.

Praferenz Carlos: 'nur negative Sachen' Pushover. Daher:
  - Regime-Halt (negativ: Bot pausiert) -> bypass mit WARNING
  - Regime-Resumed (positiv: Bot wieder live, aber Follow-up nach
    negativem Event) -> bypass mit INFO
  - Weekly Report / Optimizer-Completed / Daily Summary (positiv/info)
    -> bleiben auf Telegram-Filter (bei Carlos silent — gewollt)
"""
from unittest.mock import patch

import pytest


@pytest.fixture
def cfg_pushover_only():
    """Carlos-Config: Telegram aus, Pushover an."""
    return {
        "alerts": {
            "telegram": {"enabled": False, "notify_regime_change": True,
                         "notify_weekly_report": True, "notify_optimizer": True,
                         "notify_daily_summary": True},
            "pushover": {"enabled": True, "user_key": "u", "api_token": "t",
                         "priority_map": {"INFO": 0, "WARNING": 0, "ERROR": 1}},
        }
    }


# ============================================================
# NEGATIV-EVENTS — IMMER eskaliert (Bypass Telegram-Filter)
# ============================================================

def test_regime_halt_bypasses_telegram_filter(cfg_pushover_only):
    """Regime-Halt (Bot pausiert) sollte IMMER Pushover ausloesen."""
    from app.alerts import alert_regime_halt
    with patch("app.alerts.send_alert") as mock_send:
        alert_regime_halt("Bear-Markt detected", {"vix": 28.5}, config=cfg_pushover_only)
    mock_send.assert_called_once()
    msg, level, _ = mock_send.call_args[0]
    assert level == "WARNING"
    assert "REGIME HALT" in msg
    assert "Bear-Markt" in msg


def test_regime_resumed_bypasses_telegram_filter(cfg_pushover_only):
    """Regime-Resumed (Bot wieder live) — Follow-up nach Halt, auch immer Pushover."""
    from app.alerts import alert_regime_resumed
    with patch("app.alerts.send_alert") as mock_send:
        alert_regime_resumed(config=cfg_pushover_only)
    mock_send.assert_called_once()
    msg, level, _ = mock_send.call_args[0]
    assert level == "INFO"
    assert "REGIME HALT AUFGEHOBEN" in msg


def test_regime_halt_includes_diagnostics(cfg_pushover_only):
    """Message muss VIX/Fear-Greed/Regime-Daten enthalten."""
    from app.alerts import alert_regime_halt
    regime_data = {"vix": 31.2, "fear_greed": 18, "regime": "bear"}
    with patch("app.alerts.send_alert") as mock_send:
        alert_regime_halt("VIX-Spike", regime_data, config=cfg_pushover_only)
    msg = mock_send.call_args[0][0]
    assert "31.2" in msg
    assert "18" in msg
    assert "bear" in msg


# ============================================================
# INFO-EVENTS — bleiben Telegram-gefiltert (bei Carlos silent — gewollt)
# ============================================================

def test_weekly_report_stays_silent_for_pushover_only_user(cfg_pushover_only):
    """Carlos's Praeferenz: keine Pushover bei Weekly Report (Info-Event)."""
    from app.alerts import alert_weekly_report
    report = {"performance": {"total_return_pct": 2.5, "portfolio_value": 1040000},
              "weekly_trades": {"total_trades": 12, "buys": 5, "sells": 7}}
    with patch("app.alerts.send_alert") as mock_send:
        alert_weekly_report(report, config=cfg_pushover_only)
    # Telegram-Filter blockiert da telegram.enabled=False
    mock_send.assert_not_called()


def test_optimizer_completed_stays_silent_for_pushover_only_user(cfg_pushover_only):
    """Optimizer-Completed = Info-Event, kein Pushover noetig."""
    from app.alerts import alert_optimizer_completed
    result = {"action": "no_change"}
    with patch("app.alerts.send_alert") as mock_send:
        alert_optimizer_completed(result, config=cfg_pushover_only)
    mock_send.assert_not_called()


# ============================================================
# Backward-Compat: wenn Telegram-User wieder enabled, alles funktional
# ============================================================

def test_regime_halt_still_works_when_telegram_enabled():
    """Wenn Carlos Telegram spaeter aktiviert: kein Regression."""
    from app.alerts import alert_regime_halt
    cfg_both = {
        "alerts": {
            "telegram": {"enabled": True, "notify_regime_change": True},
            "pushover": {"enabled": True, "user_key": "u", "api_token": "t",
                         "priority_map": {"WARNING": 0}},
        }
    }
    with patch("app.alerts.send_alert") as mock_send:
        alert_regime_halt("Test", config=cfg_both)
    mock_send.assert_called_once()


def test_weekly_report_works_when_telegram_enabled():
    """Wenn Telegram aktiviert, Weekly Report durchlaufen lassen."""
    from app.alerts import alert_weekly_report
    cfg_tg_on = {
        "alerts": {
            "telegram": {"enabled": True, "notify_weekly_report": True,
                         "bot_token": "x", "chat_id": "y"},
            "pushover": {"enabled": False},
        }
    }
    report = {"performance": {"total_return_pct": 2.5, "portfolio_value": 1040000},
              "weekly_trades": {"total_trades": 12}}
    with patch("app.alerts.send_alert") as mock_send:
        alert_weekly_report(report, config=cfg_tg_on)
    mock_send.assert_called_once()


def test_weekly_report_blocked_via_notify_weekly_report_flag():
    """Granularer Opt-out: notify_weekly_report=False blockt selbst bei
    enabled Telegram."""
    from app.alerts import alert_weekly_report
    cfg = {
        "alerts": {
            "telegram": {"enabled": True, "notify_weekly_report": False,
                         "bot_token": "x", "chat_id": "y"},
            "pushover": {"enabled": False},
        }
    }
    report = {"performance": {"total_return_pct": 2.5}, "weekly_trades": {}}
    with patch("app.alerts.send_alert") as mock_send:
        alert_weekly_report(report, config=cfg)
    mock_send.assert_not_called()
