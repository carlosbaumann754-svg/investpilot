"""Tests fuer R-A50 — Boot-Race-Condition Defense + Sentry-Noise-Filter.

Bug-Anlass: Fr 29.05.2026 ca. 13:30 CEST entdeckte Carlos 2 neue Sentry-
Issues von 11:05 UTC heute morgen:
  - PYTHON-FASTAPI-V "Broker-Healthcheck fehlgeschlagen — Cycle wird ueberspr"
  - PYTHON-FASTAPI-T "completed orders request timed out" (ib_insync.ib)

Wurzel: R-A49-NEU eingefuehrtes ib.reqAllOpenOrders + ib.reqCompletedOrders
feuerten direkt nach E27-Subscribe in der E27-Initialisierung. Bei Container-
Restart braucht IBKR-Connection ~10-30s zum Aufbau. Wenn Recovery in dem
Fenster feuert → TimeoutError → ib_insync.ib loggt ERROR → Sentry sammelt.

Heute Phase 5 Cleanup-Restart 11:04 UTC + 7s Boot = 11:05 UTC erste Recovery
→ Timeouts → Sentry-Events.

R-A50 Fix (Combined-Solution per User-Wahl):
  Teil A: Sentry-Filter (4 neue Patterns in _SENTRY_NOISE_PATTERNS)
    - "completed orders request timed out"
    - "open orders request timed out"
    - "Broker-Healthcheck fehlgeschlagen"
    - "Broker-Healthcheck attempt"
  Teil B: Defensive Recovery
    - Connection-Ready-Check via ib.isConnected() am Anfang
    - TimeoutError-Handling inner + outer (WARNING statt unhandled-ERROR)
    - Skip Recovery cycle wenn Boot-Phase, naechster Cycle laeuft normal
"""

from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Teil A: Sentry-Filter-Patterns
# ---------------------------------------------------------------------------

def test_r_a50_sentry_filter_catches_timeout_patterns():
    """Alle 4 R-A50 Boot-Race-Patterns werden vom Sentry-Filter erkannt."""
    from app.sentry_setup import _is_noise

    assert _is_noise("completed orders request timed out") is True
    assert _is_noise("open orders request timed out") is True
    assert _is_noise("Broker-Healthcheck fehlgeschlagen — Cycle wird uebersprungen") is True
    assert _is_noise("Broker-Healthcheck attempt 1/2 failed (get_equity returned None)") is True


def test_r_a50_sentry_filter_does_not_block_real_errors():
    """R-A50-Filter darf echte Errors nicht blockieren (Regression-Schutz)."""
    from app.sentry_setup import _is_noise

    # Generische Bot-Errors die NICHT gefiltert sein duerfen
    assert _is_noise("TypeError: int() argument must be a string") is False
    assert _is_noise("KeyError: 'symbol'") is False
    assert _is_noise("Order rejected by IBKR: Symbol halted") is False


def test_r_a50_patterns_present_in_source():
    """Source-Based-Regression: R-A50-Patterns muessen in sentry_setup.py existieren."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "sentry_setup.py"
    body = src.read_text(encoding="utf-8")
    assert "completed orders request timed out" in body
    assert "open orders request timed out" in body
    assert "Broker-Healthcheck fehlgeschlagen" in body
    assert "R-A50" in body, "R-A50-Marker muss in sentry_setup.py existieren"


# ---------------------------------------------------------------------------
# Teil B: Defensive Recovery — Connection-Ready-Check
# ---------------------------------------------------------------------------

def test_r_a50_recover_skips_when_not_connected():
    """Wenn ib.isConnected() = False: Recovery skippt, kein TimeoutError-Risiko."""
    from app.order_status_tracker import OrderStatusTracker

    tracker = OrderStatusTracker(status_mapper=lambda s: s.lower() if s else None)
    ib = MagicMock()
    ib.isConnected.return_value = False

    stats = tracker.recover_from_ibkr(ib)
    assert stats["resolved"] == 0
    assert stats["staled"] == 0
    # isConnected wurde gerufen, andere Methoden NICHT
    ib.isConnected.assert_called()
    ib.reqAllOpenOrders.assert_not_called()
    ib.reqCompletedOrders.assert_not_called()
    ib.openTrades.assert_not_called()


def test_r_a50_recover_proceeds_when_connected():
    """Wenn ib.isConnected() = True: Recovery laeuft normal weiter."""
    from app.order_status_tracker import OrderStatusTracker

    tracker = OrderStatusTracker(status_mapper=lambda s: s.lower() if s else None)
    ib = MagicMock()
    ib.isConnected.return_value = True
    ib.openTrades.return_value = []
    ib.trades.return_value = []
    ib.completedOrders.return_value = []

    stats = tracker.recover_from_ibkr(ib)
    # Connection-Check + normaler Recovery-Pfad ausgefuehrt
    ib.isConnected.assert_called()
    ib.reqAllOpenOrders.assert_called()
    assert stats["resolved"] == 0  # keine pending, kein resolve


def test_r_a50_recover_handles_missing_isConnected_method():
    """Mock-IB-Objekte ohne isConnected (z.B. alte Tests): fall-through, kein Crash."""
    from app.order_status_tracker import OrderStatusTracker

    tracker = OrderStatusTracker(status_mapper=lambda s: s.lower() if s else None)
    # Mock OHNE isConnected
    ib = MagicMock(spec=["openTrades", "trades", "reqAllOpenOrders", "reqCompletedOrders", "sleep"])
    ib.openTrades.return_value = []
    ib.trades.return_value = []

    stats = tracker.recover_from_ibkr(ib)
    # Skip-Path nicht getriggert (hasattr=False), Recovery laeuft
    ib.reqAllOpenOrders.assert_called()


# ---------------------------------------------------------------------------
# Teil B: Defensive Recovery — TimeoutError-Handling
# ---------------------------------------------------------------------------

def test_r_a50_recover_handles_inner_timeout_gracefully():
    """completedOrders TimeoutError → WARNING-Log, kein Crash, kein ERROR-Sentry."""
    from app.order_status_tracker import OrderStatusTracker

    tracker = OrderStatusTracker(status_mapper=lambda s: s.lower() if s else None)
    ib = MagicMock()
    ib.isConnected.return_value = True
    ib.openTrades.return_value = []
    ib.trades.return_value = []
    # reqCompletedOrders wirft TimeoutError (Boot-Race-Simulation)
    ib.reqCompletedOrders.side_effect = TimeoutError("completed orders request timed out")

    # Soll NICHT crashen — exception wird gefangen
    stats = tracker.recover_from_ibkr(ib)
    assert stats["resolved"] == 0  # nichts zu resolven aber kein Crash


def test_r_a50_recover_handles_outer_timeout_gracefully():
    """reqAllOpenOrders TimeoutError im outer try: Skip-Recovery, kein Crash."""
    from app.order_status_tracker import OrderStatusTracker

    tracker = OrderStatusTracker(status_mapper=lambda s: s.lower() if s else None)
    ib = MagicMock()
    ib.isConnected.return_value = True
    # reqAllOpenOrders wirft TimeoutError (Boot-Race im outer)
    ib.reqAllOpenOrders.side_effect = TimeoutError("API connection failed: TimeoutError()")

    stats = tracker.recover_from_ibkr(ib)
    assert stats["resolved"] == 0
    assert stats["staled"] == 0
    # still_pending = aktueller Tracker-State (0 weil keine pending)
    assert "still_pending" in stats


def test_r_a50_recover_handles_isConnected_exception():
    """isConnected() wirft Exception (z.B. broken mock): Skip-Path greift."""
    from app.order_status_tracker import OrderStatusTracker

    tracker = OrderStatusTracker(status_mapper=lambda s: s.lower() if s else None)
    ib = MagicMock()
    ib.isConnected.side_effect = AttributeError("internal state corrupt")

    stats = tracker.recover_from_ibkr(ib)
    assert stats["resolved"] == 0
    # Defensive: kein nachfolgender Recovery-Call
    ib.reqAllOpenOrders.assert_not_called()


# ---------------------------------------------------------------------------
# Source-Based-Regression: R-A50 Code-Marker
# ---------------------------------------------------------------------------

def test_r_a50_defensive_markers_in_tracker():
    """order_status_tracker.py MUSS R-A50-Logic enthalten."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "order_status_tracker.py"
    body = src.read_text(encoding="utf-8")
    assert "R-A50" in body, "R-A50-Marker muss in order_status_tracker.py existieren"
    assert "isConnected" in body, "R-A50 Connection-Ready-Check via isConnected()"
    assert "except TimeoutError" in body, "R-A50 TimeoutError-Handling"
