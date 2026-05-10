"""v37h Pre-Cutover-Task 1 (10.05.2026) — Tests fuer Rolling-Window-WFO.

Validiert dass walk_forward_validate_rolling() korrekt N Walks erzeugt
und die OOS-Metriken aggregiert (statt single 80/20-Split).
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


def _make_history_df(start_date, n_days, base_price=100.0):
    """Erzeuge minimal-DataFrame mit n_days OHLCV-Zeilen."""
    import pandas as pd
    dates = pd.date_range(start=start_date, periods=n_days, freq="D")
    df = pd.DataFrame({
        "Open": [base_price] * n_days,
        "High": [base_price * 1.01] * n_days,
        "Low": [base_price * 0.99] * n_days,
        "Close": [base_price] * n_days,
        "Volume": [1_000_000] * n_days,
    }, index=dates)
    return df


@pytest.fixture
def histories_5y():
    """5 Jahre = 1825 Tage, 1 Symbol fuer Tests."""
    return {
        "AAPL": _make_history_df("2021-05-01", 1825, 150.0),
    }


@pytest.fixture
def histories_short():
    """Nur 1 Jahr — zu wenig fuer Rolling."""
    return {
        "AAPL": _make_history_df("2025-05-01", 365, 150.0),
    }


# ============================================================
# Happy-Path: 5y History gibt mehrere Walks
# ============================================================

def test_5y_history_produces_multiple_walks(histories_5y):
    """5y / 24Mo Train + 6Mo Test + 6Mo Step -> ~5 Walks."""
    from app.backtester import walk_forward_validate_rolling

    fake_metrics = {"sharpe_ratio": 2.5, "total_return_pct": 25.0,
                    "max_drawdown_pct": 5.0, "total_trades": 100}
    with patch("app.backtester.simulate_trades", return_value=[]), \
         patch("app.backtester.calculate_metrics", return_value=fake_metrics), \
         patch("app.backtester._build_position_sizing_from_config",
               return_value={"kelly_fraction": 0.5}):
        result = walk_forward_validate_rolling(
            histories_5y, config={},
            train_months=24, test_months=6, step_months=6,
            min_walks=2,
        )

    assert result is not None
    assert "walks" in result
    assert "aggregate" in result
    assert len(result["walks"]) >= 4, f"Erwartete >=4 Walks, bekam {len(result['walks'])}"


def test_aggregate_metrics_correct(histories_5y):
    """Aggregate berechnet mean OOS Sharpe / Decay korrekt."""
    from app.backtester import walk_forward_validate_rolling

    # Train Sharpe = 4.0, OOS Sharpe = 2.0 -> Decay = 50%
    train_metrics = {"sharpe_ratio": 4.0, "total_return_pct": 50.0,
                     "max_drawdown_pct": 8.0, "total_trades": 200}
    test_metrics = {"sharpe_ratio": 2.0, "total_return_pct": 12.0,
                    "max_drawdown_pct": 4.0, "total_trades": 50}

    call_count = {"i": 0}
    def fake_metrics_side(trades, **kwargs):
        # Erste Call ist Train, zweiter Test, alternierend
        call_count["i"] += 1
        return train_metrics if call_count["i"] % 2 == 1 else test_metrics

    with patch("app.backtester.simulate_trades", return_value=[]), \
         patch("app.backtester.calculate_metrics", side_effect=fake_metrics_side), \
         patch("app.backtester._build_position_sizing_from_config",
               return_value={"kelly_fraction": 0.5}):
        result = walk_forward_validate_rolling(
            histories_5y, config={},
            train_months=24, test_months=6, step_months=6,
        )

    agg = result["aggregate"]
    assert agg["mean_oos_sharpe"] == 2.0
    assert agg["mean_is_sharpe"] == 4.0
    assert agg["sharpe_decay_pct"] == 50.0
    assert agg["mean_oos_return_pct"] == 12.0
    assert agg["oos_walks"] >= 4


# ============================================================
# Edge: zu kurze History -> None (Caller fallback)
# ============================================================

def test_too_short_history_returns_none(histories_short):
    """1y-History reicht nicht fuer 24Mo Train -> None."""
    from app.backtester import walk_forward_validate_rolling
    result = walk_forward_validate_rolling(
        histories_short, config={},
        train_months=24, test_months=6, step_months=6,
        min_walks=2,
    )
    assert result is None


def test_min_walks_threshold(histories_5y):
    """Mit min_walks=10 (zu hoch fuer 5y / 6Mo Step) -> None."""
    from app.backtester import walk_forward_validate_rolling
    fake_metrics = {"sharpe_ratio": 2.5, "total_return_pct": 25.0,
                    "max_drawdown_pct": 5.0, "total_trades": 100}
    with patch("app.backtester.simulate_trades", return_value=[]), \
         patch("app.backtester.calculate_metrics", return_value=fake_metrics), \
         patch("app.backtester._build_position_sizing_from_config",
               return_value={"kelly_fraction": 0.5}):
        result = walk_forward_validate_rolling(
            histories_5y, config={},
            train_months=24, test_months=6, step_months=6,
            min_walks=10,  # zu hoch
        )
    assert result is None


def test_empty_histories_returns_none():
    from app.backtester import walk_forward_validate_rolling
    result = walk_forward_validate_rolling({}, config={})
    assert result is None


# ============================================================
# Walk-Detail: jeder Walk hat eigene Train/Test-Periods
# ============================================================

def test_walks_have_distinct_periods(histories_5y):
    """Jeder Walk hat eigene period_train + period_test."""
    from app.backtester import walk_forward_validate_rolling
    fake = {"sharpe_ratio": 2.5, "total_return_pct": 25.0,
            "max_drawdown_pct": 5.0, "total_trades": 100}
    with patch("app.backtester.simulate_trades", return_value=[]), \
         patch("app.backtester.calculate_metrics", return_value=fake), \
         patch("app.backtester._build_position_sizing_from_config",
               return_value={"kelly_fraction": 0.5}):
        result = walk_forward_validate_rolling(
            histories_5y, config={},
            train_months=24, test_months=6, step_months=6,
        )

    walks = result["walks"]
    # Periods muessen monoton fortschreiten
    test_starts = [w["period_test"].split("—")[0] for w in walks]
    assert test_starts == sorted(test_starts)
    # Distinct
    assert len(set(test_starts)) == len(test_starts)


# ============================================================
# Hard-Gate-Trigger-Werte plausibel
# ============================================================

def test_aggregate_includes_hard_gate_signals(histories_5y):
    """aggregate enthaelt mean_oos_sharpe und sharpe_decay_pct fuer Gates."""
    from app.backtester import walk_forward_validate_rolling
    fake = {"sharpe_ratio": 3.0, "total_return_pct": 30.0,
            "max_drawdown_pct": 6.0, "total_trades": 100}
    with patch("app.backtester.simulate_trades", return_value=[]), \
         patch("app.backtester.calculate_metrics", return_value=fake), \
         patch("app.backtester._build_position_sizing_from_config",
               return_value={"kelly_fraction": 0.5}):
        result = walk_forward_validate_rolling(histories_5y, config={})

    agg = result["aggregate"]
    # Diese Felder sind Hard-Gate-relevant — muessen vorhanden sein
    for key in ("mean_oos_sharpe", "median_oos_sharpe", "mean_is_sharpe",
                "sharpe_decay_pct", "oos_walks", "total_oos_trades"):
        assert key in agg, f"agg fehlt Pflicht-Key {key}"
