"""R-B7 (03.06.2026) — OrderStatusTracker als Prozess-Singleton.

ROOT CAUSE (systematic-debugging, 03.06.): Phantom-"PendingSubmit"-Leichen
in pending_orders.json, obwohl Orders bei IBKR gefuellt waren (3 Tage lang
beobachtet: 01.06. 8 Stk, 03.06. 17 Stk).

Mechanismus:
  - get_broker() erzeugt PRO Cycle einen NEUEN IbkrBroker -> NEUEN
    OrderStatusTracker mit eigener in-memory _pending-Map.
  - Der Connection-Pool ist aber ein SINGLETON (ueber Cycles wiederverwendet).
  - ib.orderStatusEvent += tracker.handle_status_event wird NUR im Fresh-
    Connect-Pfad gesetzt (ibkr_client _get_ib Z.497), NICHT im Pool-Hit-Pfad
    (Z.442). -> Subscription haengt am Tracker des ERSTEN (Boot-)Brokers.
  - Spaetere Zyklus-Broker registrieren Orders auf IHREN Trackern (Datei),
    aber der subscribte Boot-Tracker hat eine in-memory _pending von __init__-
    Zeit -> kennt diese Orders nie -> handle_status_event skippt sie als
    "unbekannte Order" -> KEIN Update -> stale bis 48h-Recover.

FIX: Tracker als Prozess-Singleton (wie der Connection-Pool). Alle Broker
teilen EINE _pending-Map -> register() + handle_status_event kohaerent.
"""
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Storage isolieren + Singleton vor JEDEM Test zuruecksetzen."""
    storage = {}
    monkeypatch.setattr("app.config_manager.save_json",
                        lambda f, d: storage.__setitem__(f, d))
    monkeypatch.setattr("app.config_manager.load_json",
                        lambda f: storage.get(f))
    from app.order_status_tracker import _reset_shared_tracker_for_tests
    _reset_shared_tracker_for_tests()
    yield storage
    _reset_shared_tracker_for_tests()


def _make_trade(order_id, status, symbol="UNG", filled=0, avg_fill=0):
    trade = MagicMock()
    trade.order.orderId = order_id
    trade.orderStatus.status = status
    trade.orderStatus.filled = filled
    trade.orderStatus.avgFillPrice = avg_fill
    trade.contract.symbol = symbol
    return trade


def test_get_shared_tracker_is_singleton():
    from app.order_status_tracker import get_shared_tracker
    assert get_shared_tracker() is get_shared_tracker()


def test_status_event_applies_across_references():
    """DER Bug: Order via Referenz A registriert, Status-Event via Referenz B
    (die subscribte) muss sie updaten. Geht nur wenn A is B (Singleton)."""
    from app.order_status_tracker import get_shared_tracker

    ref_cycle = get_shared_tracker(status_mapper=lambda s: s)   # registriert
    ref_subscribed = get_shared_tracker()                       # subscribed (Boot)

    ref_cycle.register(999, {
        "symbol": "UNG", "action": "SELL", "amount_usd": 1000,
        "status": "PendingSubmit", "ibkr_status_raw": "PendingSubmit",
    })
    # Filled-Event kommt auf der subscribten Referenz an:
    ref_subscribed.handle_status_event(_make_trade(999, "Filled", filled=4204, avg_fill=11.53))

    # Order muss aufgeloest sein (Final-Status -> aus pending entfernt ODER Filled),
    # NICHT als PendingSubmit haengen:
    entry = ref_cycle._pending.get("999")
    assert entry is None or entry.get("current_status") == "Filled"


def test_ibkr_brokers_share_tracker_singleton():
    """Integrations-Punkt: zwei IbkrBroker (per-cycle) teilen den Tracker."""
    from app.ibkr_client import IbkrBroker
    cfg = {"realtime_status_tracker": {"enabled": True}, "ibkr": {"client_id": 1}}
    b1 = IbkrBroker(cfg)
    b2 = IbkrBroker(cfg)
    assert b1._tracker is not None
    assert b1._tracker is b2._tracker
