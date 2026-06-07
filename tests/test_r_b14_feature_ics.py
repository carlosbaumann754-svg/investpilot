"""R-B14 (07.06.2026) Phase 0 — Per-Feature-IC-Harness.

Misst pro Einzel-Feature (Score-Komponenten + Roh-Features) den Spearman-IC vs
pnl_net_pct, durch Re-Berechnung der Entry-Features aus den histories (kein
Eingriff in die Sim). Beantwortet: hat IRGENDEIN vorhandenes Feature Signal?
"""
import pandas as pd

from app.backtester import _compute_feature_ics


def _hist(closes):
    n = len(closes)
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"Close": closes, "High": [c * 1.01 for c in closes],
         "Volume": [1000 + i for i in range(n)]},
        index=dates)


def test_feature_ics_structure_and_runs():
    closes = [100 + i * 0.3 for i in range(90)]  # steigend, 90 Bars
    histories = {"AAA": _hist(closes)}
    dates = pd.date_range("2024-01-01", periods=90, freq="D")
    trades = []
    for k in range(25):
        idx = 60 + k  # idx 60..84 (>= lookback 60)
        ed = str(dates[idx])[:10]
        trades.append({"symbol": "AAA", "entry_date": ed,
                       "pnl_net_pct": float(k % 7 - 3)})
    res = _compute_feature_ics(trades, histories)
    # alle erwarteten Feature-Keys vorhanden
    for fn in ["score", "rsi", "momentum_5d", "momentum_20d",
               "volatility", "mr_strength"]:
        assert fn in res, fn
    # mind. ein Feature hat genug Samples -> ein Spearman-Wert in [-1,1]
    vals = [v["ic_spearman"] for v in res.values() if v.get("ic_spearman") is not None]
    assert vals, "kein Feature mit IC berechnet"
    assert all(-1.0 <= v <= 1.0 for v in vals)


def test_feature_ics_missing_history_safe():
    trades = [{"symbol": "ZZZ", "entry_date": "2024-03-01", "pnl_net_pct": 1.0}] * 25
    res = _compute_feature_ics(trades, {})  # keine histories
    for v in res.values():
        assert v["ic_spearman"] is None


def test_feature_ics_too_few_trades():
    histories = {"AAA": _hist([100 + i * 0.3 for i in range(90)])}
    dates = pd.date_range("2024-01-01", periods=90, freq="D")
    trades = [{"symbol": "AAA", "entry_date": str(dates[70])[:10],
               "pnl_net_pct": 1.0}] * 5  # nur 5 < 20
    res = _compute_feature_ics(trades, histories)
    for v in res.values():
        assert v["ic_spearman"] is None


def test_feature_ics_ignores_trades_without_pnl_or_date():
    histories = {"AAA": _hist([100 + i * 0.3 for i in range(90)])}
    trades = [{"symbol": "AAA"}, {"entry_date": "2024-03-01"},
              {"symbol": "AAA", "entry_date": "2024-03-01"}]  # alle unvollstaendig
    res = _compute_feature_ics(trades, histories)
    for v in res.values():
        assert v["n"] == 0
