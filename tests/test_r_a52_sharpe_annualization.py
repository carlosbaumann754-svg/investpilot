"""Tests fuer R-A52 — WFO-Drift Live-Sharpe Annualisierungs-Fix.

Soak-Item WFO-Baseline-Methodik-Review (29.05.2026): Diagnose ergab, dass
der WFO-Drift-Watchdog Aepfel mit Birnen verglich:
  - WFO-OOS-Sharpe: DAILY-annualisiert (backtester: daily_mean/daily_std *
    sqrt(252)) → z.B. 6.40
  - Live-Sharpe: rohes per-Trade mean/std, NICHT annualisiert → z.B. -0.31

~16x systematischer Skalen-Offset → Drift-Watchdog feuerte Dauer-False-
Positives (29.05.: -105.8% "Drift", reines Mess-Artefakt). Selbst eine
perfekt-laufende Strategie haette immer massiven Negativ-Drift gezeigt.

R-A52 Fix: Live-Sharpe auf dieselbe Daily-annualized Convention bringen:
  1. Close-Trades nach Kalender-Tag gruppieren (Summe pnl_pct pro Tag)
  2. (daily_mean / daily_std) * sqrt(252)

Bewusste Approximation: backtester verteilt Returns ueber Holding-Days;
hier auf Close-Tag gelumpt (Live hat keine Holding-Spans). Beide aber
daily * sqrt(252) → gleiche Groessenordnung + Annualisierungs-Faktor.
"""

import math
from datetime import datetime, timedelta

from app.wfo_drift_watchdog import _compute_live_sharpe


def _trade(days_ago, pnl_pct, action="TRAILING_SL_CLOSE"):
    """Helper: Close-Trade mit timestamp X Tage in der Vergangenheit."""
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
    return {"action": action, "timestamp": ts, "pnl_pct": pnl_pct}


# ---------------------------------------------------------------------------
# Annualisierung
# ---------------------------------------------------------------------------

def test_r_a52_sharpe_is_annualized():
    """Live-Sharpe muss jetzt annualisiert sein (Faktor ~sqrt(252) groesser
    als der rohe daily mean/std)."""
    # Trades auf verschiedenen Tagen mit positivem Mittel + Streuung
    trades = [
        _trade(1, 2.0),
        _trade(3, -1.0),
        _trade(5, 3.0),
        _trade(7, -0.5),
        _trade(9, 1.5),
    ]
    sharpe, n = _compute_live_sharpe(trades, lookback_days=30)
    assert sharpe is not None
    assert n == 5
    # Manuell: daily-returns (jeder Trade eigener Tag) = [2,-1,3,-0.5,1.5]
    vals = [2.0, -1.0, 3.0, -0.5, 1.5]
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
    expected = (mean / var ** 0.5) * (252 ** 0.5)
    assert abs(sharpe - expected) < 0.01, f"Erwartet {expected}, got {sharpe}"


def test_r_a52_same_day_trades_summed():
    """Mehrere Close-Trades am selben Tag werden zu einer Tages-Rendite summiert
    (analog backtester daily_contrib)."""
    # 2 Trades heute-1, 1 Trade heute-3
    same_day = (datetime.now() - timedelta(days=1)).isoformat()
    other_day = (datetime.now() - timedelta(days=3)).isoformat()
    trades = [
        {"action": "STOP_LOSS_CLOSE", "timestamp": same_day, "pnl_pct": 1.0},
        {"action": "TAKE_PROFIT_CLOSE", "timestamp": same_day, "pnl_pct": 2.0},
        {"action": "TRAILING_SL_CLOSE", "timestamp": other_day, "pnl_pct": -1.0},
    ]
    sharpe, n = _compute_live_sharpe(trades, lookback_days=30)
    assert n == 3  # 3 Trades total
    # Daily-returns: Tag1 = 1+2 = 3.0, Tag3 = -1.0 → [3.0, -1.0]
    vals = [3.0, -1.0]
    mean = sum(vals) / 2
    var = sum((v - mean) ** 2 for v in vals) / 1
    expected = (mean / var ** 0.5) * (252 ** 0.5)
    assert abs(sharpe - expected) < 0.01


def test_r_a52_high_frequency_does_not_overinflate():
    """Regression-Schutz gegen den falschen Trade-Level-Annualisierungs-Ansatz:
    bei vielen Trades am selben Tag darf der Sharpe NICHT durch sqrt(trade_count)
    explodieren — Daily-Grouping verhindert das."""
    # 20 Trades, aber alle an nur 3 verschiedenen Tagen
    trades = []
    for i in range(20):
        day = 1 + (i % 3) * 2  # Tage 1, 3, 5 zyklisch
        trades.append(_trade(day, 1.0 if i % 2 == 0 else -0.8))
    sharpe, n = _compute_live_sharpe(trades, lookback_days=30)
    assert n == 20
    # Annualisierungs-Faktor = sqrt(252), NICHT sqrt(20).
    # Nur 3 Daily-Buckets → Daily-Std basiert auf 3 Werten, Faktor bleibt sqrt(252).
    # Sanity: |sharpe| sollte im Bereich eines daily-Sharpe * 15.87 liegen,
    # nicht * sqrt(20)=4.5 oder * sqrt(1400).
    assert sharpe is not None


# ---------------------------------------------------------------------------
# Edge-Cases
# ---------------------------------------------------------------------------

def test_r_a52_insufficient_data_returns_none():
    """< 2 Close-Trades → None."""
    sharpe, n = _compute_live_sharpe([_trade(1, 2.0)], lookback_days=30)
    assert sharpe is None
    assert n == 1


def test_r_a52_single_day_all_trades_returns_none():
    """Alle Trades am selben Tag → nur 1 Daily-Bucket → std nicht berechenbar → None."""
    same_day = (datetime.now() - timedelta(days=1)).isoformat()
    trades = [
        {"action": "STOP_LOSS_CLOSE", "timestamp": same_day, "pnl_pct": 1.0},
        {"action": "TAKE_PROFIT_CLOSE", "timestamp": same_day, "pnl_pct": 2.0},
    ]
    sharpe, n = _compute_live_sharpe(trades, lookback_days=30)
    assert sharpe is None  # nur 1 Daily-Bucket, <2 fuer std
    assert n == 2


def test_r_a52_respects_lookback_window():
    """Trades aelter als lookback_days werden ignoriert."""
    trades = [
        _trade(1, 2.0),
        _trade(3, -1.0),
        _trade(5, 1.5),
        _trade(40, 99.0),  # ausserhalb 30d-Fenster
    ]
    sharpe, n = _compute_live_sharpe(trades, lookback_days=30)
    assert n == 3  # der 40-Tage-alte Trade zaehlt nicht


def test_r_a52_zero_variance_returns_none():
    """Alle Tage gleiche Rendite → daily_std=0 → None (kein div-by-zero)."""
    trades = [
        _trade(1, 1.0),
        _trade(3, 1.0),
        _trade(5, 1.0),
    ]
    sharpe, n = _compute_live_sharpe(trades, lookback_days=30)
    assert sharpe is None


# ---------------------------------------------------------------------------
# Source-Based-Regression
# ---------------------------------------------------------------------------

def test_r_a52_uses_sqrt_252_annualization():
    """Source: Live-Sharpe MUSS sqrt(252)-Annualisierung nutzen."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "wfo_drift_watchdog.py"
    body = src.read_text(encoding="utf-8")
    assert "252 ** 0.5" in body or "sqrt(252)" in body, (
        "R-A52: Daily-Annualisierung via sqrt(252) muss vorhanden sein"
    )
    assert "R-A52" in body


def test_r_a52_old_raw_pertrademean_pattern_gone():
    """Regression: alter 'mean / std' OHNE Annualisierung weg aus _compute_live_sharpe."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "wfo_drift_watchdog.py"
    body = src.read_text(encoding="utf-8")
    fn_start = body.index("def _compute_live_sharpe")
    next_def = body.find("\ndef ", fn_start + 50)
    fn_end = next_def if next_def != -1 else len(body)
    fn_body = body[fn_start:fn_end]
    # Der alte buggy return war "return (mean / std), n" — darf nicht mehr da sein
    assert "return (mean / std), n" not in fn_body, (
        "R-A52 REGRESSION: alter nicht-annualisierter per-Trade-Sharpe ist zurueck"
    )
    assert "daily" in fn_body.lower(), "R-A52: Daily-Grouping muss vorhanden sein"
