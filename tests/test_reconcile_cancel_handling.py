"""Tests fuer v37db Reconcile-Cancel-Handling.

Bug-Reproduktion 06.05.2026: Bot's SLV-Limit-Order in Pre-Market wurde von
IBKR cancelled (Limit 70.75 nicht erreicht). Reconcile-Cron meldete alle
30 Min MISSED_FILL — False-Positive weil cancelled != missed.

Fix v37db: cancelled_orders + rejected_orders aus ib.trades() sammeln,
beim MISSED_FILL-Check als zusaetzlicher Filter (analog pending_symbols/v37aa).
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest


# ============================================================
# HELPER: Mock-IBKR-State Bauer
# ============================================================

def make_ibkr_state(
    positions=None,
    executions=None,
    cash=1_000_000.0,
    open_orders=None,
    cancelled_orders=None,
    rejected_orders=None,
):
    return {
        "positions": positions or [],
        "executions": executions or [],
        "cash": cash,
        "equity": cash,
        "open_orders": open_orders or [],
        "cancelled_orders": cancelled_orders or [],
        "rejected_orders": rejected_orders or [],
    }


def make_bot_history_entry(symbol, action="SCANNER_BUY", minutes_ago=30, status="executed"):
    """Generiert einen bot_history-Eintrag mit timestamp."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "timestamp": ts.isoformat(),
        "symbol": symbol,
        "action": action,
        "status": status,
    }


# ============================================================
# CORE TESTS — Cancelled / Rejected nicht als MISSED_FILL
# ============================================================

def test_cancelled_order_does_not_trigger_missed_fill():
    """Bug-Reproduktion: Bot logged SCANNER_BUY, IBKR cancelled Order."""
    from scripts.ibkr_reconcile import reconcile

    bot_history = [make_bot_history_entry("SLV", "SCANNER_BUY", minutes_ago=30)]
    ibkr = make_ibkr_state(
        cancelled_orders=[{"symbol": "SLV", "side": "BOT", "status": "Cancelled"}]
    )

    with patch("scripts.ibkr_reconcile.load_bot_state", return_value=(bot_history, 1_000_000.0)), \
         patch("scripts.ibkr_reconcile.get_ibkr_state", return_value=ibkr):
        result = reconcile()

    missed_fills = [d for d in result["drifts"] if d["type"] == "MISSED_FILL"]
    assert len(missed_fills) == 0, f"Cancelled Order sollte KEIN MISSED_FILL ausloesen: {missed_fills}"


def test_rejected_order_does_not_trigger_missed_fill():
    from scripts.ibkr_reconcile import reconcile

    bot_history = [make_bot_history_entry("AAPL", "SCANNER_BUY", minutes_ago=30)]
    ibkr = make_ibkr_state(
        rejected_orders=[{"symbol": "AAPL", "side": "BOT", "status": "Rejected"}]
    )

    with patch("scripts.ibkr_reconcile.load_bot_state", return_value=(bot_history, 1_000_000.0)), \
         patch("scripts.ibkr_reconcile.get_ibkr_state", return_value=ibkr):
        result = reconcile()

    missed_fills = [d for d in result["drifts"] if d["type"] == "MISSED_FILL"]
    assert len(missed_fills) == 0, f"Rejected Order sollte KEIN MISSED_FILL ausloesen: {missed_fills}"


def test_apicancelled_status_handled():
    """ApiCancelled (von IBKR-API zurueckgezogen) auch als cancelled behandeln."""
    from scripts.ibkr_reconcile import reconcile

    bot_history = [make_bot_history_entry("MSFT", "SCANNER_BUY", minutes_ago=30)]
    ibkr = make_ibkr_state(
        cancelled_orders=[{"symbol": "MSFT", "side": "BOT", "status": "ApiCancelled"}]
    )

    with patch("scripts.ibkr_reconcile.load_bot_state", return_value=(bot_history, 1_000_000.0)), \
         patch("scripts.ibkr_reconcile.get_ibkr_state", return_value=ibkr):
        result = reconcile()

    assert all(d["type"] != "MISSED_FILL" for d in result["drifts"])


# ============================================================
# REGRESSION TESTS — Echte MISSED_FILLs muessen weiterhin feuern
# ============================================================

def test_real_missed_fill_still_detected():
    """Wenn Bot loggte BUY, aber IBKR hat KEIN Cancelled/Rejected/Filled/Pending = echter MISSED_FILL."""
    from scripts.ibkr_reconcile import reconcile

    bot_history = [make_bot_history_entry("NVDA", "SCANNER_BUY", minutes_ago=30)]
    ibkr = make_ibkr_state()

    with patch("scripts.ibkr_reconcile.load_bot_state", return_value=(bot_history, 1_000_000.0)), \
         patch("scripts.ibkr_reconcile.get_ibkr_state", return_value=ibkr):
        result = reconcile()

    missed_fills = [d for d in result["drifts"] if d["type"] == "MISSED_FILL"]
    assert len(missed_fills) == 1, "Echter MISSED_FILL muss weiterhin gemeldet werden"
    assert missed_fills[0]["symbol"] == "NVDA"


def test_filled_order_does_not_trigger_missed_fill():
    """Wenn IBKR die Execution hat, kein MISSED_FILL."""
    from scripts.ibkr_reconcile import reconcile

    bot_history = [make_bot_history_entry("GOOGL", "SCANNER_BUY", minutes_ago=30)]
    ibkr = make_ibkr_state(
        executions=[{"exec_id": "1", "symbol": "GOOGL", "side": "BOT",
                     "qty": 10, "price": 150.0, "time": "2026-05-06T12:00:00"}]
    )

    with patch("scripts.ibkr_reconcile.load_bot_state", return_value=(bot_history, 1_000_000.0)), \
         patch("scripts.ibkr_reconcile.get_ibkr_state", return_value=ibkr):
        result = reconcile()

    assert all(d["type"] != "MISSED_FILL" for d in result["drifts"])


def test_pending_order_does_not_trigger_missed_fill():
    """v37aa-Pfad: pending Order = kein MISSED_FILL (Regression)."""
    from scripts.ibkr_reconcile import reconcile

    bot_history = [make_bot_history_entry("TSLA", "SCANNER_BUY", minutes_ago=30)]
    ibkr = make_ibkr_state(
        open_orders=[{"symbol": "TSLA", "side": "BOT", "status": "Submitted"}]
    )

    with patch("scripts.ibkr_reconcile.load_bot_state", return_value=(bot_history, 1_000_000.0)), \
         patch("scripts.ibkr_reconcile.get_ibkr_state", return_value=ibkr):
        result = reconcile()

    assert all(d["type"] != "MISSED_FILL" for d in result["drifts"])


# ============================================================
# EDGE-CASES
# ============================================================

def test_cancelled_and_rejected_combined():
    """Beide Filter-Sets werden korrekt UNIONED."""
    from scripts.ibkr_reconcile import reconcile

    bot_history = [
        make_bot_history_entry("SLV", "SCANNER_BUY", minutes_ago=30),
        make_bot_history_entry("AAPL", "SCANNER_BUY", minutes_ago=30),
    ]
    ibkr = make_ibkr_state(
        cancelled_orders=[{"symbol": "SLV", "side": "BOT", "status": "Cancelled"}],
        rejected_orders=[{"symbol": "AAPL", "side": "BOT", "status": "Rejected"}],
    )

    with patch("scripts.ibkr_reconcile.load_bot_state", return_value=(bot_history, 1_000_000.0)), \
         patch("scripts.ibkr_reconcile.get_ibkr_state", return_value=ibkr):
        result = reconcile()

    assert all(d["type"] != "MISSED_FILL" for d in result["drifts"])


def test_bot_status_cancelled_filter():
    """v37db Backup-Pfad: Bot's Status-Field auf 'cancelled' = Reconcile akzeptiert ohne MISSED_FILL.

    Robusteste Variante weil Symbol-Mapping (Bot-internal 'SILVER' vs IBKR-Ticker 'SLV')
    den ib.trades()-Filter unzuverlaessig macht. Bot-Status ist Single-Source-of-Truth.
    """
    from scripts.ibkr_reconcile import reconcile

    bot_history = [make_bot_history_entry("SILVER", "SCANNER_BUY", minutes_ago=30, status="cancelled")]
    ibkr = make_ibkr_state()  # IBKR sieht nichts — wuerde normalerweise MISSED_FILL ausloesen

    with patch("scripts.ibkr_reconcile.load_bot_state", return_value=(bot_history, 1_000_000.0)), \
         patch("scripts.ibkr_reconcile.get_ibkr_state", return_value=ibkr):
        result = reconcile()

    assert all(d["type"] != "MISSED_FILL" for d in result["drifts"]), \
        "Bot-Status='cancelled' soll MISSED_FILL unterdruecken"


def test_bot_status_rejected_filter():
    from scripts.ibkr_reconcile import reconcile

    bot_history = [make_bot_history_entry("AAPL", "SCANNER_BUY", minutes_ago=30, status="rejected")]
    ibkr = make_ibkr_state()

    with patch("scripts.ibkr_reconcile.load_bot_state", return_value=(bot_history, 1_000_000.0)), \
         patch("scripts.ibkr_reconcile.get_ibkr_state", return_value=ibkr):
        result = reconcile()

    assert all(d["type"] != "MISSED_FILL" for d in result["drifts"])


def test_partial_match_only_cancelled_side():
    """Side-Match muss exakt sein (BOT vs SLD)."""
    from scripts.ibkr_reconcile import reconcile

    bot_history = [make_bot_history_entry("SLV", "SCANNER_BUY", minutes_ago=30)]
    ibkr = make_ibkr_state(
        cancelled_orders=[{"symbol": "SLV", "side": "SLD", "status": "Cancelled"}]
    )

    with patch("scripts.ibkr_reconcile.load_bot_state", return_value=(bot_history, 1_000_000.0)), \
         patch("scripts.ibkr_reconcile.get_ibkr_state", return_value=ibkr):
        result = reconcile()

    missed_fills = [d for d in result["drifts"] if d["type"] == "MISSED_FILL"]
    assert len(missed_fills) == 1, "Side-Match muss exakt sein (BOT vs SLD)"
