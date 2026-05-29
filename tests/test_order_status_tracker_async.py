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
    # R-A49: KEIN order_id in trade_history (Bot war >24h offline, IBKR session-
    # history gewiped, Bot's eigene trade_history kennt diese Order auch nicht).
    # Mit order_id wuerde R-A49 die Order als 'executed' resolven (anderer Test-Pfad).
    storage["trade_history.json"] = [{"symbol": "OLD"}]
    tracker.register(order_id=999, trade_entry={"symbol": "OLD", "order_id": 999})

    # IBKR weiss nichts von der Order
    ib = MagicMock()
    ib.openTrades.return_value = []
    ib.trades.return_value = []
    ib.completedOrders.return_value = []  # R-A49: completedOrders ebenfalls leer

    # v37e Tag 3 (Strategie B — stale-Marker): Order ist gerade-eben registered,
    # also <48h alt -> NOCH NICHT staled. Eintrag bleibt pending.
    stats = tracker.recover_from_ibkr(ib)
    assert stats["resolved"] == 0
    assert stats["staled"] == 0  # zu jung fuer stale-Marker
    assert stats["still_pending"] == 1

    pending = storage["pending_orders.json"]["pending"]
    assert "999" in pending
    assert pending["999"].get("current_status") in (None, "Submitted", "submitted")


def test_stale_marker_triggers_pushover(stress_tracker, monkeypatch):
    """v37e Tag 6: _mark_stale soll send_pushover() aufrufen.

    Visibility-Loch geschlossen — Stale ist eine echte Anomalie die manuelles
    IBKR-Web-Login erfordert. Carlos's Watchdog-Disziplin verlangt Push,
    nicht nur Log + trade_history-Update.

    Erwartung:
      - send_pushover wird genau 1x aufgerufen pro stale-Marker
      - Message enthaelt Symbol + Order-ID + Aktions-Hinweis
      - Priority=1 (HIGH, ueberbrueckt Quiet-Hours)
    """
    from datetime import datetime, timezone, timedelta

    tracker, storage = stress_tracker
    # R-A49: KEIN order_id in trade_history (Order ist stale, nicht executed)
    storage["trade_history.json"] = [{"symbol": "STALEX"}]

    # Order registrieren, dann registered_at auf 49h backdate (>48h Schwelle)
    tracker.register(order_id=555, trade_entry={"symbol": "STALEX", "order_id": 555})
    backdated = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    tracker._pending["555"]["registered_at"] = backdated
    if storage.get("pending_orders.json", {}).get("pending", {}).get("555"):
        storage["pending_orders.json"]["pending"]["555"]["registered_at"] = backdated

    # Mock send_pushover
    pushover_calls = []

    def fake_pushover(message, *args, **kwargs):
        pushover_calls.append({"message": message, "kwargs": kwargs})
        return True

    monkeypatch.setattr("app.alerts.send_pushover", fake_pushover)

    # IBKR weiss nichts → stale-Marker triggert
    ib = MagicMock()
    ib.openTrades.return_value = []
    ib.trades.return_value = []
    ib.completedOrders.return_value = []  # R-A49

    stats = tracker.recover_from_ibkr(ib, stale_after_hours=48)

    assert stats["staled"] == 1, "Stale-Marker haette greifen muessen (>48h alt + kein IBKR-Match)"
    assert len(pushover_calls) == 1, f"Erwartete genau 1 Pushover-Aufruf, bekam {len(pushover_calls)}"

    call = pushover_calls[0]
    msg = call["message"]
    assert "STALEX" in msg, f"Symbol fehlt in message: {msg}"
    assert "555" in msg, f"Order-ID fehlt in message: {msg}"
    assert "IBKR" in msg, f"IBKR-Hinweis fehlt in message: {msg}"
    assert call["kwargs"].get("priority") == 1, "Priority muss 1 (HIGH) sein"


def test_stale_marker_pushover_failure_does_not_break(stress_tracker, monkeypatch):
    """v37e Tag 6: Wenn Pushover failt, soll Stale-Marker trotzdem persistieren.

    Resilience-Pattern: Pushover ist Best-Effort, nicht Required-Path.
    Stale-Status MUSS in trade_history landen, auch wenn Push-API down.
    """
    from datetime import datetime, timezone, timedelta

    tracker, storage = stress_tracker
    # R-A49: KEIN order_id in trade_history (Stale-Test-Setup)
    storage["trade_history.json"] = [{"symbol": "PUSHFAIL"}]
    tracker.register(order_id=777, trade_entry={"symbol": "PUSHFAIL", "order_id": 777})
    backdated = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    tracker._pending["777"]["registered_at"] = backdated

    # Pushover wirft Exception
    def broken_pushover(*args, **kwargs):
        raise RuntimeError("Pushover API down")

    monkeypatch.setattr("app.alerts.send_pushover", broken_pushover)

    ib = MagicMock()
    ib.openTrades.return_value = []
    ib.trades.return_value = []
    ib.completedOrders.return_value = []  # R-A49

    # Soll NICHT crashen
    stats = tracker.recover_from_ibkr(ib, stale_after_hours=48)
    assert stats["staled"] == 1

    # R-A49: Resilience-Pattern — Stale-Status persistiert in pending_orders.json
    # auch wenn Pushover failed. (Vor R-A49 wurde gegen trade_history.json
    # gecheckt, aber das setzt voraus dass order_id im trade_history-Setup
    # existiert — was R-A49-Helper als 'executed' faelschlich resolven wuerde.
    # Semantisch klarer: Stale-Marker MUSS in pending_orders.json sichtbar sein
    # damit Bot's nuechster Cycle (Tracker2) den Status erkennt.)
    pending = storage.get("pending_orders.json", {}).get("pending", {})
    assert pending.get("777", {}).get("current_status") == "Stale", \
        "Stale-Status muss in pending_orders persistieren auch bei Pushover-Failure"


def test_stale_marker_persists_across_instances_no_pushover_spam(monkeypatch):
    """v37h Bugfix 10.05.2026 (Carlos's Pushover-Spam-Vorfall):

    Vor dem Fix war _mark_stale nicht persistierend — nur in-memory.
    Folge: Bei Container-Restart oder neuer IbkrBroker-Instanz (Reconcile-
    Cron, API-Endpoints) wurde pending_orders.json frisch geladen mit
    current_status='PendingSubmit'. Stale-Check feuerte erneut. Pushover-
    Spam alle 1-3 Min.

    Test verifiziert Cross-Instance-Idempotenz:
      Tracker1 stales order -> persists status='Stale' to pending_orders.json
      Tracker2 (fresh load) sees status='Stale' (in IBKR_FINAL_STATUSES)
      Tracker2.recover_from_ibkr -> skips line 227 -> KEIN zweiter Pushover.
    """
    from datetime import datetime, timezone, timedelta

    # Shared storage simuliert pending_orders.json auf Disk
    storage = {}
    monkeypatch.setattr("app.config_manager.save_json",
                        lambda fn, data: storage.__setitem__(fn, data))
    monkeypatch.setattr("app.config_manager.load_json",
                        lambda fn: storage.get(fn))
    # R-A49: KEIN order_id in trade_history (Cross-Instance Stale-Test)
    storage["trade_history.json"] = [{"symbol": "STUCK"}]

    pushover_calls = []
    monkeypatch.setattr("app.alerts.send_pushover",
                        lambda msg, *a, **kw: pushover_calls.append(msg))

    # === Tracker1 — entdeckt + markiert stale ===
    t1 = OrderStatusTracker(status_mapper=lambda s: s.lower() if s else None)
    t1.register(order_id=122, trade_entry={"symbol": "STUCK", "order_id": 122})
    backdated = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    t1._pending["122"]["registered_at"] = backdated

    ib = MagicMock()
    ib.openTrades.return_value = []
    ib.trades.return_value = []
    ib.completedOrders.return_value = []  # R-A49

    stats1 = t1.recover_from_ibkr(ib, stale_after_hours=48)
    assert stats1["staled"] == 1, "Tracker1: Stale-Marker haette greifen muessen"
    assert len(pushover_calls) == 1, "Tracker1: genau 1 Pushover beim ersten Marker"

    # KRITISCH: storage["pending_orders.json"] muss jetzt status='Stale' enthalten
    persisted = storage.get("pending_orders.json", {}).get("pending", {}).get("122", {})
    assert persisted.get("current_status") == "Stale", \
        f"Bugfix-Verifikation: pending_orders.json muss Stale-Status persistieren, " \
        f"aber sah: {persisted.get('current_status')!r}"

    # === Tracker2 — neue Instanz (z.B. Reconcile-Cron, Container-Restart) ===
    t2 = OrderStatusTracker(status_mapper=lambda s: s.lower() if s else None)
    # Constructor liest pending_orders.json -> Order 122 mit Stale geladen
    assert t2._pending.get("122", {}).get("current_status") == "Stale", \
        "Tracker2 muss Stale-Status aus persistiertem File laden"

    stats2 = t2.recover_from_ibkr(ib, stale_after_hours=48)

    # IDEMPOTENZ: kein zweiter Pushover, kein erneutes Stalen
    assert stats2["staled"] == 0, \
        f"Tracker2 darf NICHT erneut stalen (war: {stats2['staled']})"
    assert len(pushover_calls) == 1, \
        f"Cross-Instance-Idempotenz verletzt: {len(pushover_calls)} Pushover " \
        f"(erwartet: genau 1 vom ersten Tracker)"


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


# ============================================================
# Tag 4: run_periodic_maintenance Helper (Scheduler-Hook)
# ============================================================

def test_maintenance_skipped_when_feature_disabled(stress_tracker):
    """Wenn _e27_enabled=False, run_periodic_maintenance macht nichts."""
    from app.order_status_tracker import run_periodic_maintenance

    broker = MagicMock()
    broker._e27_enabled = False
    broker._tracker = MagicMock()

    result = run_periodic_maintenance(broker)
    assert result == {"enabled": False, "recovery": None, "cleanup_deleted": None}
    broker._tracker.recover_from_ibkr.assert_not_called()
    broker._tracker.cleanup_resolved.assert_not_called()


def test_maintenance_calls_recovery_and_cleanup(stress_tracker):
    """Wenn enabled=True: Recovery + Cleanup beide aufgerufen."""
    from app.order_status_tracker import run_periodic_maintenance

    tracker, _ = stress_tracker
    broker = MagicMock()
    broker._e27_enabled = True
    broker._tracker = tracker
    broker._get_ib.return_value = MagicMock(
        openTrades=lambda: [], trades=lambda: [],
    )

    result = run_periodic_maintenance(broker, max_age_hours=24, stale_after_hours=48)
    assert result["enabled"] is True
    assert isinstance(result["recovery"], dict)
    assert isinstance(result["cleanup_deleted"], int)


def test_maintenance_resilient_to_recovery_failure(stress_tracker):
    """Wenn Recovery crasht: Cleanup wird trotzdem versucht."""
    from app.order_status_tracker import run_periodic_maintenance

    broker = MagicMock()
    broker._e27_enabled = True
    broker._tracker = MagicMock()
    broker._tracker.recover_from_ibkr.side_effect = Exception("IBKR boom")
    broker._tracker.cleanup_resolved.return_value = 5

    result = run_periodic_maintenance(broker)
    assert result["enabled"] is True
    assert result["recovery"] is None  # failed
    assert result["cleanup_deleted"] == 5  # ran trotzdem


def test_maintenance_resilient_to_no_tracker(stress_tracker):
    """Wenn _tracker is None: enabled=False, kein Crash."""
    from app.order_status_tracker import run_periodic_maintenance

    broker = MagicMock()
    broker._e27_enabled = True
    broker._tracker = None

    result = run_periodic_maintenance(broker)
    assert result["enabled"] is False
