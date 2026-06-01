"""R-B5 (01.06.2026) — WFO-Drift-Watchdog auf PF/Win-Rate-Baseline.

Soak-Investigation #1 ergab: Backtest-Sharpe (mean OOS 6.40) ist ein Artefakt
(Return-Glaettung ueber Haltetage + explosives Position-Sizing-Compounding +
OOS-Sharpe > IS-Sharpe = statistisch unmoeglich). PF (Profit-Faktor) ist
scale-invariant -> von diesen Artefakten NICHT betroffen -> zuverlaessige
Drift-Baseline. Watchdog gated jetzt PRIMAER auf PF-Drift; Sharpe bleibt
informativ; Fallback auf Sharpe nur wenn WFO keine PF-Daten hat.
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


def _trade(pnl, days_ago=1, action="STOP_LOSS_CLOSE"):
    return {
        "action": action,
        "pnl_pct": pnl,
        "timestamp": (datetime.now() - timedelta(days=days_ago)).isoformat(),
    }


# ============================================================
# _get_wfo_target_pf
# ============================================================

def test_wfo_target_pf_mean_from_windows(mock_state):
    from app.wfo_drift_watchdog import _get_wfo_target_pf
    mock_state["wfo_status.json"] = {"windows": [
        {"oos_metrics": {"pf": 2.0, "win_rate": 60.0}},
        {"oos_metrics": {"pf": 3.0, "win_rate": 50.0}},
    ]}
    pf, wr = _get_wfo_target_pf()
    assert abs(pf - 2.5) < 0.01
    assert abs(wr - 55.0) < 0.01


def test_wfo_target_pf_none_when_no_pf(mock_state):
    """Alte wfo_status ohne pf -> (None, None) -> Fallback-Pfad."""
    from app.wfo_drift_watchdog import _get_wfo_target_pf
    mock_state["wfo_status.json"] = {"windows": [{"oos_sharpe": 5.0}]}
    pf, wr = _get_wfo_target_pf()
    assert pf is None


# ============================================================
# _compute_live_pf
# ============================================================

def test_compute_live_pf_basic():
    from app.wfo_drift_watchdog import _compute_live_pf
    # gross_win=3, gross_loss=1 -> PF=3.0; 2/3 wins -> win_rate 66.7%
    trades = [_trade(2.0), _trade(1.0), _trade(-1.0)]
    pf, wr, n = _compute_live_pf(trades, lookback_days=30)
    assert abs(pf - 3.0) < 0.01
    assert abs(wr - (2 / 3 * 100)) < 0.1
    assert n == 3


def test_compute_live_pf_no_losses_capped_healthy():
    from app.wfo_drift_watchdog import _compute_live_pf, _LIVE_PF_CAP
    pf, wr, n = _compute_live_pf([_trade(2.0), _trade(1.0)], lookback_days=30)
    assert pf == _LIVE_PF_CAP
    assert wr == 100.0


def test_compute_live_pf_too_few_trades():
    from app.wfo_drift_watchdog import _compute_live_pf
    pf, wr, n = _compute_live_pf([_trade(1.0)], lookback_days=30)
    assert pf is None
    assert n == 1


# ============================================================
# check_wfo_drift — PF-Gating
# ============================================================

def _spread_trades(win_pnl, loss_pnl, days=12):
    """Pro Tag ein Win + ein Loss -> 'days' distinkte Tage, 2*days Trades."""
    out = []
    for i in range(days):
        out.append(_trade(win_pnl, days_ago=i + 1))
        out.append(_trade(loss_pnl, days_ago=i + 1))
    return out


def test_drift_uses_pf_when_available(mock_state):
    """WFO hat pf -> drift_metric == 'pf' (nicht Sharpe)."""
    from app.wfo_drift_watchdog import check_wfo_drift
    mock_state["wfo_status.json"] = {"windows": [
        {"oos_metrics": {"pf": 2.0, "win_rate": 55, "sharpe": 6.4}}]}
    mock_state["trade_history.json"] = _spread_trades(2.0, -1.0)  # PF=2.0
    with patch("app.alerts.send_alert"):
        r = check_wfo_drift({"wfo_drift_watchdog": {
            "enabled": True, "min_trades": 5, "min_distinct_days": 5,
            "drift_threshold_pct": 30}})
    assert r["drift_metric"] == "pf"
    assert r["wfo_pf"] is not None
    assert r["live_pf"] is not None


def test_drift_alert_on_pf_decay(mock_state):
    """Live-PF << WFO-PF -> PF-basierter Alert."""
    from app.wfo_drift_watchdog import check_wfo_drift
    mock_state["wfo_status.json"] = {"windows": [
        {"oos_metrics": {"pf": 2.5, "win_rate": 60, "sharpe": 6.4}}]}
    # PF = 12/24 = 0.5 -> drift = (0.5-2.5)/2.5 = -80%
    mock_state["trade_history.json"] = _spread_trades(1.0, -2.0)
    with patch("app.alerts.send_alert") as mock_alert:
        r = check_wfo_drift({"wfo_drift_watchdog": {
            "enabled": True, "min_trades": 5, "min_distinct_days": 5,
            "drift_threshold_pct": 30}})
    assert r["drift_metric"] == "pf"
    assert r["pf_drift_pct"] < -30
    assert r["alert_triggered"] is True
    mock_alert.assert_called_once()


def test_drift_healthy_when_pf_matches(mock_state):
    """Live-PF ~ WFO-PF -> kein Alert."""
    from app.wfo_drift_watchdog import check_wfo_drift
    mock_state["wfo_status.json"] = {"windows": [
        {"oos_metrics": {"pf": 2.0, "win_rate": 55, "sharpe": 5.0}}]}
    mock_state["trade_history.json"] = _spread_trades(2.0, -1.0)  # PF=2.0
    with patch("app.alerts.send_alert") as mock_alert:
        r = check_wfo_drift({"wfo_drift_watchdog": {
            "enabled": True, "min_trades": 5, "min_distinct_days": 5,
            "drift_threshold_pct": 30}})
    assert r["drift_metric"] == "pf"
    assert abs(r["pf_drift_pct"]) < 1.0
    assert r["alert_triggered"] is False
    mock_alert.assert_not_called()
