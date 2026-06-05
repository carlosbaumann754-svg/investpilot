"""R-B9 (05.06.2026) — STALE_ORDER-Alert Dedup (Cry-Wolf-Fix).

Symptom: 6 harmlose Pending-Leichen loesten je MEHRERE STALE_ORDER-Pushover aus
(18:18 + 19:00 fuer dieselben Orders = roter Alert-Schwall). Ursache: Bot-Prozess
+ Reconcile-Cron-Prozess (docker exec ... ibkr_reconcile, 2x/h) haben GETRENNTE
In-Memory-Tracker; bis der eine den persistierten 'Stale'-Stand des anderen sieht,
re-staled + re-alarmiert er. Der Z.373-FINAL_STATUSES-Skip greift nur INNERHALB
eines Prozesses (R-B7-Singleton ist per-Prozess).

Fix: PERSISTIERTER stale_alerted-Flag (in pending_orders.json). Jeder Prozess,
der _mark_stale aufruft, sieht 'schon alarmiert' -> genau 1 Pushover pro Order,
prozess-uebergreifend.
"""
from unittest.mock import patch

import pytest


@pytest.fixture
def fresh_tracker(monkeypatch):
    """Tracker mit isolierter pending_orders.json (geteilter storage-Dict)."""
    storage = {}
    monkeypatch.setattr("app.config_manager.save_json",
                        lambda f, d: storage.__setitem__(f, d))
    monkeypatch.setattr("app.config_manager.load_json",
                        lambda f: storage.get(f))
    from app.order_status_tracker import OrderStatusTracker
    tracker = OrderStatusTracker(status_mapper=lambda s: s.lower() if s else None)
    return tracker, storage


def _register(tracker, oid, symbol="UNG"):
    tracker.register(order_id=oid, trade_entry={
        "symbol": symbol, "action": "SELL", "amount_usd": 1000,
        "status": "PendingSubmit", "ibkr_status_raw": "PendingSubmit",
    })


def test_stale_alert_fires_once_despite_multiple_marks(fresh_tracker):
    """Mehrere _mark_stale-Aufrufe (mehrere Recover-Laeufe) -> nur 1 Pushover."""
    tracker, _ = fresh_tracker
    _register(tracker, 999)
    with patch("app.alerts.send_pushover") as mock_push:
        with tracker._lock:
            entry = tracker._pending["999"]
            tracker._mark_stale("999", entry)
            tracker._mark_stale("999", entry)
            tracker._mark_stale("999", entry)
    assert mock_push.call_count == 1
    assert tracker._pending["999"].get("stale_alerted")


def test_stale_dedup_survives_fresh_load_other_process(fresh_tracker):
    """Persistierter Flag -> ein FRISCH geladener Tracker (anderer Prozess)
    re-alarmiert NICHT (prozess-uebergreifender Dedup ueber die geteilte Datei)."""
    tracker, storage = fresh_tracker
    _register(tracker, 888, symbol="ASML")
    with patch("app.alerts.send_pushover") as mock_push1:
        with tracker._lock:
            tracker._mark_stale("888", tracker._pending["888"])
    assert mock_push1.call_count == 1

    # Anderer Prozess: neuer Tracker laedt dieselbe (gemockte) Datei.
    from app.order_status_tracker import OrderStatusTracker
    tracker2 = OrderStatusTracker(status_mapper=lambda s: s)
    assert "888" in tracker2._pending  # noch vor cleanup
    assert tracker2._pending["888"].get("stale_alerted")  # Flag persistiert
    with patch("app.alerts.send_pushover") as mock_push2:
        with tracker2._lock:
            tracker2._mark_stale("888", tracker2._pending["888"])
    assert mock_push2.call_count == 0  # KEIN Re-Alert


def test_stale_still_marks_status_even_when_alert_deduped(fresh_tracker):
    """Dedup betrifft NUR den Pushover — der Stale-Marker (status/resolved_at)
    wird weiter gesetzt."""
    tracker, _ = fresh_tracker
    _register(tracker, 777)
    with patch("app.alerts.send_pushover"):
        with tracker._lock:
            tracker._mark_stale("777", tracker._pending["777"])
            tracker._mark_stale("777", tracker._pending["777"])
    assert tracker._pending["777"]["current_status"] == "Stale"
