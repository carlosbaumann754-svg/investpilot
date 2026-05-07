"""Tests fuer E27 OrderStatusTracker.

Tag 1: Unit-Tests ohne IBKR-Connection.
Tag 3: Async-Mock-Tests + Race-Conditions (siehe test_order_status_tracker_async.py)
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.order_status_tracker import (
    OrderStatusTracker,
    IBKR_FINAL_STATUSES,
    IBKR_PENDING_STATUSES,
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def fresh_tracker(tmp_path, monkeypatch):
    """Tracker mit isolierter pending_orders.json + mocked trade_history."""
    # Mock config_manager.save_json + load_json damit wir nicht real /data/ touchen
    fake_storage = {}

    def fake_save(filename, data):
        fake_storage[filename] = data

    def fake_load(filename):
        return fake_storage.get(filename)

    monkeypatch.setattr("app.config_manager.save_json", fake_save)
    monkeypatch.setattr("app.config_manager.load_json", fake_load)

    # Identity status-mapper damit Tests deterministisch sind
    tracker = OrderStatusTracker(status_mapper=lambda s: s.lower() if s else None)
    return tracker, fake_storage


def _make_trade(order_id, status, symbol="SLV", action="BUY", filled=0, avg_fill=0):
    """Helper: ib_insync-Trade-Objekt mocken."""
    trade = MagicMock()
    trade.order.orderId = order_id
    trade.orderStatus.status = status
    trade.orderStatus.filled = filled
    trade.orderStatus.avgFillPrice = avg_fill
    trade.contract.symbol = symbol
    return trade


# ============================================================
# Core: register + handle_status_event
# ============================================================

def test_register_creates_pending_entry(fresh_tracker):
    tracker, storage = fresh_tracker
    tracker.register(order_id=123, trade_entry={
        "symbol": "SLV", "action": "SCANNER_BUY", "amount_usd": 5000,
        "status": "submitted", "ibkr_status_raw": "Submitted",
    })
    assert tracker.get_pending_count() == 1
    assert "pending_orders.json" in storage
    pending = storage["pending_orders.json"]["pending"]
    assert "123" in pending
    assert pending["123"]["symbol"] == "SLV"


def test_register_with_none_order_id_is_silent(fresh_tracker):
    tracker, _ = fresh_tracker
    tracker.register(order_id=None, trade_entry={"symbol": "X"})
    assert tracker.get_pending_count() == 0


def test_handle_status_event_filled_updates_status(fresh_tracker):
    tracker, storage = fresh_tracker
    storage["trade_history.json"] = [
        {"symbol": "SLV", "action": "SCANNER_BUY", "order_id": 123,
         "status": "submitted", "ibkr_status_raw": "Submitted"}
    ]
    tracker.register(order_id=123, trade_entry={
        "symbol": "SLV", "order_id": 123,
        "status": "submitted", "ibkr_status_raw": "Submitted",
    })

    trade = _make_trade(123, "Filled", filled=100, avg_fill=70.5)
    tracker.handle_status_event(trade)

    history = storage["trade_history.json"]
    assert history[0]["status"] == "filled"  # via identity-mapper
    assert history[0]["ibkr_status_raw"] == "Filled"
    assert history[0]["filled_qty"] == 100
    assert history[0]["avg_fill_price"] == 70.5


def test_handle_status_event_cancelled(fresh_tracker):
    tracker, storage = fresh_tracker
    storage["trade_history.json"] = [
        {"symbol": "GOLD", "action": "SCANNER_BUY", "order_id": 456,
         "status": "submitted", "ibkr_status_raw": "Submitted"}
    ]
    tracker.register(order_id=456, trade_entry={
        "symbol": "GOLD", "order_id": 456,
        "status": "submitted", "ibkr_status_raw": "Submitted",
    })

    trade = _make_trade(456, "Cancelled")
    tracker.handle_status_event(trade)

    assert storage["trade_history.json"][0]["status"] == "cancelled"
    # Entry hat jetzt resolved_at
    pending = storage["pending_orders.json"]["pending"]
    assert "resolved_at" in pending["456"]


def test_handle_status_event_unknown_order_id_skips(fresh_tracker):
    """Ord-ID nicht von uns registriert (z.B. manueller Trade in IBKR-App)."""
    tracker, storage = fresh_tracker
    storage["trade_history.json"] = []

    trade = _make_trade(999, "Filled")
    tracker.handle_status_event(trade)

    # No crash, no update
    assert tracker.get_pending_count() == 0


def test_handle_status_event_partially_filled(fresh_tracker):
    tracker, storage = fresh_tracker
    storage["trade_history.json"] = [
        {"symbol": "OIL", "order_id": 789,
         "status": "submitted", "ibkr_status_raw": "Submitted"}
    ]
    tracker.register(order_id=789, trade_entry={
        "symbol": "OIL", "order_id": 789, "status": "submitted",
    })

    trade = _make_trade(789, "PartiallyFilled", filled=50, avg_fill=70.0)
    tracker.handle_status_event(trade)

    # PartiallyFilled = nicht final, bleibt pending
    pending = storage["pending_orders.json"]["pending"]
    assert "resolved_at" not in pending["789"]
    assert pending["789"]["filled_qty"] == 50


# ============================================================
# Persistence
# ============================================================

def test_persistence_survives_reload(fresh_tracker, monkeypatch):
    tracker, storage = fresh_tracker
    tracker.register(order_id=111, trade_entry={
        "symbol": "AAPL", "status": "submitted", "ibkr_status_raw": "Submitted",
    })
    assert tracker.get_pending_count() == 1

    # Neuer Tracker (simuliert Bot-Restart) mit gleichen storage
    tracker2 = OrderStatusTracker(status_mapper=lambda s: s.lower() if s else None)
    assert tracker2.get_pending_count() == 1


# ============================================================
# Cleanup
# ============================================================

def test_cleanup_removes_old_resolved_entries(fresh_tracker):
    tracker, storage = fresh_tracker
    storage["trade_history.json"] = [
        {"symbol": "X", "order_id": 1}, {"symbol": "Y", "order_id": 2},
    ]
    # 2 orders, beide filled
    tracker.register(order_id=1, trade_entry={"symbol": "X", "order_id": 1})
    tracker.register(order_id=2, trade_entry={"symbol": "Y", "order_id": 2})
    tracker.handle_status_event(_make_trade(1, "Filled"))
    tracker.handle_status_event(_make_trade(2, "Filled"))

    # Manipuliere resolved_at fuer order 1 auf "alt"
    pending = storage["pending_orders.json"]["pending"]
    pending["1"]["resolved_at"] = "2020-01-01T00:00:00+00:00"
    # In tracker laden
    tracker._pending = pending

    deleted = tracker.cleanup_resolved(max_age_hours=24)
    assert deleted == 1
    assert "1" not in tracker._pending
    assert "2" in tracker._pending  # younger, bleibt


def test_cleanup_keeps_pending_entries(fresh_tracker):
    """Cleanup darf KEINE pending (= nicht-final) Eintraege loeschen."""
    tracker, _ = fresh_tracker
    tracker.register(order_id=99, trade_entry={"symbol": "X"})
    deleted = tracker.cleanup_resolved(max_age_hours=0)  # ALLES alt
    assert deleted == 0  # pending bleibt
    assert tracker.get_pending_count() == 1


# ============================================================
# Thread-Safety
# ============================================================

def test_thread_safety_concurrent_registers(fresh_tracker):
    """10 Threads × 100 Registers gleichzeitig — keine verlorenen Eintraege."""
    tracker, _ = fresh_tracker
    n_threads = 10
    n_per_thread = 100

    def worker(thread_id):
        for i in range(n_per_thread):
            tracker.register(
                order_id=thread_id * 1000 + i,
                trade_entry={"symbol": f"T{thread_id}", "order_id": thread_id * 1000 + i},
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert tracker.get_pending_count() == n_threads * n_per_thread


def test_thread_safety_status_event_during_register(fresh_tracker):
    """Race: register + handle_status_event parallel — keine Inconsistencies."""
    tracker, storage = fresh_tracker
    storage["trade_history.json"] = [
        {"symbol": f"S{i}", "order_id": i} for i in range(100)
    ]
    n = 100
    barrier = threading.Barrier(2)

    def registrar():
        barrier.wait()
        for i in range(n):
            tracker.register(order_id=i, trade_entry={
                "symbol": f"S{i}", "order_id": i,
                "status": "submitted",
            })

    def updater():
        barrier.wait()
        time.sleep(0.001)  # leicht spaeter starten
        for i in range(n):
            tracker.handle_status_event(_make_trade(i, "Filled"))

    t1 = threading.Thread(target=registrar)
    t2 = threading.Thread(target=updater)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Kein Crash; Anzahl pending OK (alle die registered + nicht final = 0,
    # alle die updated gewesen sind = filled = final)
    pending = tracker._pending
    assert all(p.get("current_status") in ("Filled", "submitted")
               for p in pending.values())


# ============================================================
# Recover from IBKR (Bot-Restart-Recovery)
# ============================================================

def test_recover_from_ibkr_resolves_pending(fresh_tracker):
    tracker, storage = fresh_tracker
    storage["trade_history.json"] = [
        {"symbol": "SLV", "order_id": 555, "status": "submitted"}
    ]
    tracker.register(order_id=555, trade_entry={
        "symbol": "SLV", "order_id": 555, "status": "submitted",
    })

    # Mock IBKR
    ib = MagicMock()
    ib.openTrades.return_value = []
    ib.trades.return_value = [_make_trade(555, "Cancelled")]

    resolved = tracker.recover_from_ibkr(ib)
    assert resolved == 1
    assert storage["trade_history.json"][0]["status"] == "cancelled"


def test_recover_with_no_ib_returns_0(fresh_tracker):
    tracker, _ = fresh_tracker
    assert tracker.recover_from_ibkr(None) == 0


def test_recover_skips_already_final(fresh_tracker):
    tracker, storage = fresh_tracker
    storage["trade_history.json"] = [{"symbol": "X", "order_id": 7}]
    tracker.register(order_id=7, trade_entry={"symbol": "X", "order_id": 7})
    tracker.handle_status_event(_make_trade(7, "Filled"))

    ib = MagicMock()
    ib.openTrades.return_value = []
    ib.trades.return_value = [_make_trade(7, "Cancelled")]  # Sollte ignoriert werden

    resolved = tracker.recover_from_ibkr(ib)
    assert resolved == 0  # Final-Status wird nicht ueberschrieben
    assert storage["trade_history.json"][0]["status"] == "filled"
