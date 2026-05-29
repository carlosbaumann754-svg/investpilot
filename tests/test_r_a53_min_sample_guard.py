"""Tests fuer R-A53 — WFO-Drift Min-Clean-Sample-Guard.

Anlass: Nach R-A52 (Annualisierungs-Fix) wurde der Drift valide gemessen:
-175.5% (live=-4.83 vs wfo=6.40). ABER das Sample (116 Trades / 30d) war
durch Regime-HALT auf wenige Tage konzentriert → daily-Sharpe statistisch
unzuverlaessig. Ohne Guard wuerde der Watchdog waehrend der ganzen Soak-
Phase taeglich -175% melden = Cry-Wolf.

Der bestehende min_trades-Guard (10) greift NICHT, weil 116 > 10. Das
Problem ist nicht Trade-Anzahl, sondern DISTINKTE Trading-Tage.

R-A53 Fix:
  1. _count_distinct_trading_days(trade_history, lookback_days) Helper
  2. DEFAULT_MIN_DISTINCT_DAYS = 10 Guard in check_wfo_drift
  3. Wenn distinct_days < min → skip Alert (mit skip_reason, live_sharpe
     trotzdem im Result fuer Dashboard-Visibility)
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from app.wfo_drift_watchdog import _count_distinct_trading_days, check_wfo_drift


def _close(days_ago, pnl=1.0):
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
    return {"action": "TRAILING_SL_CLOSE", "timestamp": ts, "pnl_pct": pnl}


# ---------------------------------------------------------------------------
# _count_distinct_trading_days Helper
# ---------------------------------------------------------------------------

def test_r_a53_counts_distinct_days():
    """Zaehlt distinkte Kalender-Tage, nicht Trades."""
    trades = [_close(1), _close(1), _close(1), _close(3), _close(5)]
    # 3 distinkte Tage (1, 3, 5), obwohl 5 Trades
    assert _count_distinct_trading_days(trades, 30) == 3


def test_r_a53_halt_scenario_many_trades_few_days():
    """Carlos's 29.05.-Szenario: viele Trades, aber wenige Tage."""
    # 40 Trades, aber alle auf nur 4 Tagen (HALT-gestoert)
    trades = []
    for i in range(40):
        trades.append(_close(1 + (i % 4) * 2))  # Tage 1,3,5,7
    assert _count_distinct_trading_days(trades, 30) == 4


def test_r_a53_respects_lookback():
    """Trades ausserhalb lookback zaehlen nicht."""
    trades = [_close(1), _close(3), _close(40)]
    assert _count_distinct_trading_days(trades, 30) == 2


def test_r_a53_empty_returns_zero():
    assert _count_distinct_trading_days([], 30) == 0
    assert _count_distinct_trading_days(None, 30) == 0


def test_r_a53_ignores_non_close_trades():
    """Nur Close-Trades zaehlen (SCANNER_BUY hat keinen pnl-Tag)."""
    buy = {"action": "SCANNER_BUY", "timestamp": (datetime.now() - timedelta(days=2)).isoformat()}
    trades = [_close(1), buy, _close(3)]
    assert _count_distinct_trading_days(trades, 30) == 2


# ---------------------------------------------------------------------------
# check_wfo_drift Integration: Guard verhindert Alert
# ---------------------------------------------------------------------------

def test_r_a53_guard_skips_alert_when_few_distinct_days():
    """Viele Trades + wenige distinkte Tage → kein Drift-Alert (skip_reason)."""
    # 30 Trades auf nur 3 Tagen mit UNTERSCHIEDLICHEN Tages-Summen (sonst
    # daily_var=0 → live_sharpe=None → min_trades-Guard statt R-A53-Guard).
    # Tag1: 10x +1.0 = +10 | Tag2: 10x -3.0 = -30 | Tag3: 10x +0.2 = +2
    # → varianzbehaftet, negativer Sharpe, aber nur 3 distinkte Tage.
    trades = []
    day_pnl = {1: 1.0, 2: -3.0, 3: 0.2}
    for i in range(30):
        day = 1 + (i % 3)
        trades.append(_close(day, pnl=day_pnl[day]))

    cfg = {"wfo_drift_watchdog": {"enabled": True, "min_trades": 10,
                                  "min_distinct_days": 10}}

    with patch("app.wfo_drift_watchdog._get_wfo_target_sharpe", return_value=6.40), \
         patch("app.wfo_drift_watchdog.load_json", return_value=trades, create=True), \
         patch("app.config_manager.load_json", return_value=trades):
        result = check_wfo_drift(config=cfg)

    # Guard muss greifen: skip_reason gesetzt, KEIN Alert
    assert result["alert_triggered"] is False
    assert result["skip_reason"] is not None
    assert "distinkte Trading-Tage" in result["skip_reason"]
    assert result.get("distinct_days") == 3
    # live_sharpe trotzdem im Result fuer Dashboard
    assert result.get("live_sharpe") is not None


def test_r_a53_guard_allows_alert_when_enough_distinct_days():
    """Genug distinkte Tage + echter Drift → Alert darf feuern (Guard greift nicht)."""
    # 15 Trades auf 15 verschiedenen Tagen, negativer Sharpe
    trades = [_close(d, pnl=-2.0 if d % 2 == 0 else 0.5) for d in range(1, 16)]

    cfg = {"wfo_drift_watchdog": {"enabled": True, "min_trades": 10,
                                  "min_distinct_days": 10}}

    with patch("app.wfo_drift_watchdog._get_wfo_target_sharpe", return_value=6.40), \
         patch("app.config_manager.load_json", return_value=trades), \
         patch("app.wfo_drift_watchdog._load_alert_state", return_value={}), \
         patch("app.wfo_drift_watchdog._save_alert_state"), \
         patch("app.wfo_drift_watchdog.send_alert", create=True), \
         patch("app.alerts.send_alert", create=True):
        result = check_wfo_drift(config=cfg)

    # Guard greift NICHT (15 distinkte Tage >= 10) → distinct_days reported,
    # skip_reason ist NICHT der distinct-days-Grund
    assert result.get("distinct_days") == 15
    if result.get("skip_reason"):
        assert "distinkte Trading-Tage" not in result["skip_reason"]


# ---------------------------------------------------------------------------
# Source-Based-Regression
# ---------------------------------------------------------------------------

def test_r_a53_markers_present():
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "wfo_drift_watchdog.py"
    body = src.read_text(encoding="utf-8")
    assert "R-A53" in body
    assert "_count_distinct_trading_days" in body
    assert "DEFAULT_MIN_DISTINCT_DAYS" in body
    assert "min_distinct_days" in body
