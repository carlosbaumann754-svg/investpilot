"""E27 Tag 3 — Async-Mock-Tests + Stress + Edge-Cases.

Strategie:
- ib_insync's orderStatusEvent ist synchron im Event-Loop (kein asyncio).
  Daher koennen wir direkt MagicMock-Events triggern statt echtes Async-Mock.
- Race-Conditions: threading.Thread mit 1000+ parallel Events
- Edge-Cases: Multi-Status-Sequenzen pro Trade (real-world IBKR-Verhalten)
- Stress: pending_orders.json bleibt konsistent unter Last
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

from app.order_status_tracker import OrderStatusTracker, IBKR_FINAL_STATUSES


@pytest.fixture
def stress_tracker(monkeypatch):
    """Tracker mit isolated storage fuer Stress-Tests."""
    storage = {}

    def fake_save(filename, data):
        storage[filename] = data

    def fake_load(filename):
        return storage.get(filename)

    monkeypatch.setattr("app.config_manager.save_json", fake_save)
    monkeypatch.setattr("app.config_manager.load_json", fake_load)

    tracker = OrderStatusTracker(status_mapper=lambda s: s.lower() if s else None)
    return tracker, storage


def _trade(order_id, status, filled=0, avg_fill=0):
    t = MagicMock()
    t.order.orderId = order_id
    t.orderStatus.status = status
    t.orderStatus.filled = filled
    t.orderStatus.avgFillPrice = avg_fill
    return t


# ============================================================
# EDGE-CASES — Real-World IBKR Status-Sequences
# ============================================================

def test_multi_status_change_submitted_to_partially_to_filled(stress_tracker):
    """IBKR-Realitaet: Order durchlaeuft Status-Kette.

    Submitted -> PreSubmitted -> Submitted (re-confirm) -> PartiallyFilled
    -> Filled. Tracker muss jeden Schritt loggen, Final-State korrekt setzen.
    """
    tracker, storage = stress_tracker
    storage["trade_history.json"] = [{"symbol": "AAPL", "order_id": 100}]

    tracker.register(order_id=100, trade_entry={"symbol": "AAPL", "order_id": 100})

    # Status-Sequenz wie real IBKR
    statuses = [
        ("PreSubmitted", 0, 0),
        ("Submitted", 0, 0),
        ("PartiallyFilled", 50, 285.0),
        ("PartiallyFilled", 80, 285.5),
        ("Filled", 100, 285.7),
    ]
    for status, filled, avg in statuses:
        tracker.handle_status_event(_trade(100, status, filled, avg))

    # Final State
    history_entry = storage["trade_history.json"][0]
    assert history_entry["status"] == "filled"  # via identity-mapper
    assert history_entry["ibkr_status_raw"] == "Filled"
    assert history_entry["filled_qty"] == 100
    assert history_entry["avg_fill_price"] == 285.7


def test_status_sequence_ending_in_cancellation(stress_tracker):
    """Submitted -> PartiallyFilled (50) -> Cancelled.

    Order-Cancellation nach Teilfill. Status sollte "cancelled" sein,
    aber filled_qty bleibt (50 Stueck wurden gehandelt).
    """
    tracker, storage = stress_tracker
    storage["trade_history.json"] = [{"symbol": "GOLD", "order_id": 200}]
    tracker.register(order_id=200, trade_entry={"symbol": "GOLD", "order_id": 200})

    tracker.handle_status_event(_trade(200, "Submitted", 0, 0))
    tracker.handle_status_event(_trade(200, "PartiallyFilled", 50, 70.5))
    tracker.handle_status_event(_trade(200, "Cancelled", 50, 70.5))

    entry = storage["trade_history.json"][0]
    assert entry["status"] == "cancelled"
    assert entry["filled_qty"] == 50  # Teilfill bleibt!
    assert entry["avg_fill_price"] == 70.5


def test_idempotent_same_status_repeated(stress_tracker):
    """IBKR sendet manchmal selben Status mehrfach. Sollte idempotent sein."""
    tracker, storage = stress_tracker
    storage["trade_history.json"] = [{"symbol": "X", "order_id": 300}]
    tracker.register(order_id=300, trade_entry={"symbol": "X", "order_id": 300})

    # 5x Submitted-Event
    for _ in range(5):
        tracker.handle_status_event(_trade(300, "Submitted"))

    # Kein Crash, current_status korrekt
    pending = storage["pending_orders.json"]["pending"]
    assert pending["300"]["current_status"] == "Submitted"


def test_status_regress_pending_after_partial_fill(stress_tracker):
    """Edge: PartiallyFilled -> wieder PreSubmitted (z.B. Order-Modify).

    Tracker sollte den neuen Status reflektieren ohne resolved-Flag.
    """
    tracker, storage = stress_tracker
    storage["trade_history.json"] = [{"symbol": "Y", "order_id": 400}]
    tracker.register(order_id=400, trade_entry={"symbol": "Y", "order_id": 400})

    tracker.handle_status_event(_trade(400, "PartiallyFilled", 30, 100))
    tracker.handle_status_event(_trade(400, "PreSubmitted"))

    pending = storage["pending_orders.json"]["pending"]
    assert pending["400"]["current_status"] == "PreSubmitted"
    assert "resolved_at" not in pending["400"]


def test_multiple_orders_for_same_symbol(stress_tracker):
    """Bot kauft Symbol X (filled), spaeter erneut (rejected)."""
    tracker, storage = stress_tracker
    storage["trade_history.json"] = [
        {"symbol": "AAPL", "order_id": 1, "status": "submitted"},
        {"symbol": "AAPL", "order_id": 2, "status": "submitted"},
    ]
    tracker.register(order_id=1, trade_entry={"symbol": "AAPL", "order_id": 1})
    tracker.register(order_id=2, trade_entry={"symbol": "AAPL", "order_id": 2})

    tracker.handle_status_event(_trade(1, "Filled", 100, 285))
    tracker.handle_status_event(_trade(2, "Rejected"))

    history = storage["trade_history.json"]
    assert history[0]["status"] == "filled"
    assert history[1]["status"] == "rejected"


def test_register_with_existing_order_id_overwrites(stress_tracker):
    """Wenn order_id schon existiert (sollte nicht passieren in real, aber Edge):
    register ersetzt Eintrag (latest wins).
    """
    tracker, storage = stress_tracker
    tracker.register(order_id=500, trade_entry={"symbol": "OLD", "order_id": 500})
    tracker.register(order_id=500, trade_entry={"symbol": "NEW", "order_id": 500})
    pending = storage["pending_orders.json"]["pending"]
    assert pending["500"]["symbol"] == "NEW"


# ============================================================
# HEAVY STRESS — 1000+ parallele Events
# ============================================================

def test_stress_1000_parallel_status_events(stress_tracker):
    """1000 Orders, jede 5 Status-Events, alle parallel."""
    tracker, storage = stress_tracker
    n_orders = 1000
    statuses_per_order = ["PreSubmitted", "Submitted", "PartiallyFilled", "PartiallyFilled", "Filled"]

    storage["trade_history.json"] = [
        {"symbol": f"S{i}", "order_id": i} for i in range(n_orders)
    ]

    # Erst alle registern (single-thread, kein Konflikt)
    for i in range(n_orders):
        tracker.register(order_id=i, trade_entry={"symbol": f"S{i}", "order_id": i})

    # Jetzt parallele Events
    def worker(order_id):
        for status in statuses_per_order:
            tracker.handle_status_event(_trade(order_id, status, 100 if status == "Filled" else 50))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_orders)]
    start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    duration = time.time() - start

    # Alle Orders haben Final-Status "filled" + resolved_at
    pending = storage["pending_orders.json"]["pending"]
    final_count = sum(1 for p in pending.values() if p.get("current_status") == "Filled")
    assert final_count == n_orders, f"Expected {n_orders} filled, got {final_count}"

    print(f"\nStress-Test-Stats: {n_orders} orders × {len(statuses_per_order)} events = "
          f"{n_orders * len(statuses_per_order)} updates in {duration:.2f}s "
          f"({n_orders * len(statuses_per_order) / duration:.0f} updates/sec)")


def test_stress_concurrent_register_and_events(stress_tracker):
    """Realistisches Pattern: Bot registert continuously waehrend Events fluten."""
    tracker, storage = stress_tracker
    n = 500
    storage["trade_history.json"] = [{"symbol": f"X{i}", "order_id": i} for i in range(n)]

    barrier = threading.Barrier(3)  # registrar + filler + canceler

    def registrar():
        barrier.wait()
        for i in range(n):
            tracker.register(order_id=i, trade_entry={"symbol": f"X{i}", "order_id": i})

    def filler():
        barrier.wait()
        time.sleep(0.005)  # leicht spaeter
        for i in range(0, n, 2):  # gerade order_ids -> Filled
            tracker.handle_status_event(_trade(i, "Filled", 100, 50))

    def canceler():
        barrier.wait()
        time.sleep(0.005)
        for i in range(1, n, 2):  # ungerade order_ids -> Cancelled
            tracker.handle_status_event(_trade(i, "Cancelled"))

    ts = [threading.Thread(target=fn) for fn in [registrar, filler, canceler]]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    # Alle Orders haben Final-Status (Filled or Cancelled)
    pending = storage["pending_orders.json"]["pending"]
    final = sum(1 for p in pending.values()
                if p.get("current_status") in IBKR_FINAL_STATUSES)
    assert final == n, f"Expected all {n} resolved, got {final}"


# ============================================================
# RECOVERY — verschiedene Strategien testen (fuer Carlos's Entscheidung)
# ============================================================

def test_recovery_pending_order_NOT_in_ibkr_history(stress_tracker):
    """Edge-Case der zur Strategie-Entscheidung A/B/C fuehrt:

    Pending Order ist in pending_orders.json, aber IBKR's openTrades + trades()
    zeigen sie NICHT (Bot war >24h offline, IBKR hat Session-History gewiped).

    Aktuelles Verhalten: Eintrag bleibt in pending (kein resolve).
    Strategien zur Wahl:
      A) Eintrag bleibt pending (status quo)
      B) Status auf 'stale' setzen + cleanup-faehig
      C) Status auf 'cancelled' annehmen (konservativ) + cleanup-faehig
    """
    tracker, storage = stress_tracker
    storage["trade_history.json"] = [{"symbol": "OLD", "order_id": 999}]
    tracker.register(order_id=999, trade_entry={"symbol": "OLD", "order_id": 999})

    # IBKR weiss nichts von der Order
    ib = MagicMock()
    ib.openTrades.return_value = []
    ib.trades.return_value = []

    # v37e Tag 3 (Strategie B — stale-Marker): Order ist gerade-eben registered,
    # also <48h alt -> NOCH NICHT staled. Eintrag bleibt pending.
    stats = tracker.recover_from_ibkr(ib)
    assert stats["resolved"] == 0
    assert stats["staled"] == 0  # zu jung fuer stale-Marker
    assert stats["still_pending"] == 1

    pending = storage["pending_orders.json"]["pending"]
    assert "999" in pending
    assert pending["999"].get("current_status") in (None, "Submitted", "submitted")


def test_recovery_with_filled_order_in_ibkr(stress_tracker):
    """Happy-Path: Order war pending, beim Restart von IBKR als Filled."""
    tracker, storage = stress_tracker
    storage["trade_history.json"] = [{"symbol": "X", "order_id": 700, "status": "submitted"}]
    tracker.register(order_id=700, trade_entry={"symbol": "X", "order_id": 700})

    ib = MagicMock()
    ib.openTrades.return_value = []
    ib.trades.return_value = [_trade(700, "Filled", 100, 50)]

    stats = tracker.recover_from_ibkr(ib)
    assert stats["resolved"] == 1
    assert storage["trade_history.json"][0]["status"] == "filled"
