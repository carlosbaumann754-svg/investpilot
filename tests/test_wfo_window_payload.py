"""R-B4 (01.06.2026) — WFO per-Window oos_sharpe Visibility-Fix.

Hintergrund (Soak-Investigation #2): wfo_status.json windows[] exponierte
`oos_sharpe` nicht -> Konsumenten (Dashboard / Drift-Watchdog), die
window["oos_sharpe"] lesen, bekamen None. Die Daten lagen nur nested in
oos_metrics["sharpe"]. Fix: oos_sharpe explizit im Window-Payload surfacen.
"""
import pandas as pd

from app.walk_forward_optimizer import Window, _window_to_payload


def _make_window(sharpe):
    return Window(
        idx=0,
        train_start=pd.Timestamp("2024-01-01"),
        train_end=pd.Timestamp("2024-12-31"),
        test_start=pd.Timestamp("2024-12-31"),
        test_end=pd.Timestamp("2025-06-30"),
        best_params={"stop_loss_pct": -3},
        is_score=2.0,
        oos_score=sharpe,
        oos_trades=42,
        oos_metrics=({"sharpe": sharpe, "trades": 42, "pf": 1.8}
                     if sharpe is not None else {}),
    )


def test_window_payload_surfaces_oos_sharpe():
    """oos_sharpe wird aus oos_metrics.sharpe als Top-Level-Feld exponiert."""
    payload = _window_to_payload(_make_window(3.5))
    assert payload["oos_sharpe"] == 3.5


def test_window_payload_oos_sharpe_none_when_no_metrics():
    """Kein sharpe in oos_metrics (z.B. Error-Window) -> None, kein Crash."""
    payload = _window_to_payload(_make_window(None))
    assert payload["oos_sharpe"] is None


def test_window_payload_keeps_existing_fields():
    """Regression: alle bisherigen Felder bleiben erhalten."""
    payload = _window_to_payload(_make_window(3.5))
    for key in ("idx", "train_start", "train_end", "test_start", "test_end",
                "best_params", "is_score", "oos_score", "oos_trades",
                "oos_metrics"):
        assert key in payload
    assert payload["train_start"] == "2024-01-01"
    assert payload["oos_trades"] == 42
