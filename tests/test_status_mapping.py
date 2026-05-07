"""Tests fuer v37df _map_ibkr_status_to_bot_status.

Behebt 'intent vs reality'-Logging: Bot's trade_history.status wird jetzt
aus IBKR's orderForOpen.statusID abgeleitet, nicht hardcoded "executed".
"""

from app.trader import _map_ibkr_status_to_bot_status


def test_filled_returns_executed():
    assert _map_ibkr_status_to_bot_status("Filled") == "executed"


def test_submitted_returns_submitted():
    for s in ("Submitted", "PreSubmitted", "PendingSubmit", "PendingCancel", "ApiPending"):
        assert _map_ibkr_status_to_bot_status(s) == "submitted", f"failed for {s}"


def test_cancelled_returns_cancelled():
    for s in ("Cancelled", "ApiCancelled", "Inactive"):
        assert _map_ibkr_status_to_bot_status(s) == "cancelled", f"failed for {s}"


def test_rejected_returns_rejected():
    assert _map_ibkr_status_to_bot_status("Rejected") == "rejected"


def test_partially_filled_returns_partial():
    assert _map_ibkr_status_to_bot_status("PartiallyFilled") == "partial"


def test_empty_or_none_falls_back_to_executed():
    """Backward-Compat fuer eToro (kein statusID-Feld)."""
    assert _map_ibkr_status_to_bot_status("") == "executed"
    assert _map_ibkr_status_to_bot_status(None) == "executed"


def test_unknown_status_falls_back_to_executed():
    """Sicherheitsnetz fuer kuenftige IBKR-Status-Werte."""
    assert _map_ibkr_status_to_bot_status("SomeFutureStatus") == "executed"


def test_case_sensitive():
    """IBKR-Statuses sind capitalized — leicht-anders sollte nicht matchen."""
    # 'filled' (klein) sollte NICHT als executed gemapped werden (Bug-Schutz)
    assert _map_ibkr_status_to_bot_status("filled") == "executed"  # falls back
    # Whitespace wird gestrippt
    assert _map_ibkr_status_to_bot_status("  Filled  ") == "executed"
