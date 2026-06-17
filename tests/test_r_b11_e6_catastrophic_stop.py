"""R-B11/E6 (07.06.2026) — Broker-seitiger Catastrophic-Stop (Backstop).

Weiter GTC-StopOrder bei IBKR nach Buy-Fill (Ist-Qty), schuetzt offene
Positionen wenn der Bot down ist. Dark-ship (config default OFF). Source-of-
Truth fuer Orphan/Resize-Cleanup ist IBKR selbst (Positions + OpenOrders).
"""
import pytest

from app.ibkr_client import (
    _catastrophic_stop_price, _protective_stop_action, _CATASTROPHIC_ORDER_REF,
    IbkrBroker,
)


# ── Pure: Stop-Preis ────────────────────────────────────────────────────────
def test_stop_price_basic():
    assert _catastrophic_stop_price(100.0, 20) == 80.0
    assert _catastrophic_stop_price(50.0, 20) == 40.0
    assert abs(_catastrophic_stop_price(123.45, 10) - 111.105) < 0.011


def test_stop_price_invalid_returns_none():
    assert _catastrophic_stop_price(None, 20) is None
    assert _catastrophic_stop_price("x", 20) is None
    # 100% Stop -> Preis 0 -> None (kein sinnloser 0-Stop)
    assert _catastrophic_stop_price(100.0, 100) is None


# ── Pure: Reconcile-Entscheidung ────────────────────────────────────────────
def test_protective_action_cancel_when_no_position():
    assert _protective_stop_action(100, 0) == "cancel"   # Oversell-Schutz!


def test_protective_action_resize_when_oversize():
    assert _protective_stop_action(100, 50) == "resize"  # nach Partial-Close


def test_protective_action_keep_when_matched_or_smaller():
    assert _protective_stop_action(100, 100) == "keep"
    assert _protective_stop_action(50, 100) == "keep"


# ── Config dark-ship ────────────────────────────────────────────────────────
def test_dark_ship_default_off():
    b = IbkrBroker(config={"ibkr": {}})
    assert b._cat_stop_enabled is False
    assert b._cat_stop_pct == 20.0


def test_config_enables_and_sets_pct():
    b = IbkrBroker(config={"risk_management": {
        "catastrophic_stop": {"enabled": True, "pct": 15}}})
    assert b._cat_stop_enabled is True
    assert b._cat_stop_pct == 15.0


# ── Reconcile mit Fake-IB ───────────────────────────────────────────────────
class _Contract:
    def __init__(self, conId):
        self.conId = conId


class _Pos:
    def __init__(self, conId, position):
        self.contract = _Contract(conId)
        self.position = position


class _Order:
    def __init__(self, ref, qty, aux=None):
        self.orderRef = ref
        self.totalQuantity = qty
        self.auxPrice = aux


class _Trade:
    def __init__(self, conId, order):
        self.contract = _Contract(conId)
        self.order = order


class _FakeIB:
    def __init__(self, positions, trades):
        self._positions = positions
        self._trades = trades
        self.cancelled = []
        self.placed = []

    def positions(self):
        return self._positions

    def openTrades(self):
        return self._trades

    def cancelOrder(self, o):
        self.cancelled.append(o)

    def placeOrder(self, c, o):
        self.placed.append((c, o))
        return _Trade(getattr(c, "conId", 0), o)


def _broker(monkeypatch, fake_ib):
    b = IbkrBroker(config={"risk_management": {
        "catastrophic_stop": {"enabled": True, "pct": 20}}})
    monkeypatch.setattr(b, "_get_ib", lambda: fake_ib)
    return b


def test_reconcile_cancels_orphan(monkeypatch):
    # Stop auf conId 1, aber KEINE Position -> cancel
    fake = _FakeIB(positions=[], trades=[
        _Trade(1, _Order(_CATASTROPHIC_ORDER_REF, 100, aux=80.0))])
    b = _broker(monkeypatch, fake)
    res = b.reconcile_protective_stops()
    assert res["cancelled"] == 1
    assert len(fake.cancelled) == 1


class _FakeStopOrder:
    def __init__(self, action, qty, aux):
        self.action = action
        self.totalQuantity = qty
        self.auxPrice = aux
        self.tif = None
        self.orderRef = None


def test_reconcile_resizes_oversize(monkeypatch):
    # Position 50, Stop 100 -> resize: cancel + neuer Stop qty 50.
    # ib_insync ist lokal nicht installiert -> Fake-Modul fuer den Resize-Zweig.
    import sys, types
    fake_mod = types.ModuleType("ib_insync")
    fake_mod.StopOrder = _FakeStopOrder
    monkeypatch.setitem(sys.modules, "ib_insync", fake_mod)
    fake = _FakeIB(positions=[_Pos(2, 50)], trades=[
        _Trade(2, _Order(_CATASTROPHIC_ORDER_REF, 100, aux=80.0))])
    b = _broker(monkeypatch, fake)
    res = b.reconcile_protective_stops()
    assert res["resized"] == 1
    assert len(fake.cancelled) == 1
    assert len(fake.placed) == 1
    assert int(fake.placed[0][1].totalQuantity) == 50


def test_reconcile_keeps_matched(monkeypatch):
    fake = _FakeIB(positions=[_Pos(3, 100)], trades=[
        _Trade(3, _Order(_CATASTROPHIC_ORDER_REF, 100, aux=80.0))])
    b = _broker(monkeypatch, fake)
    res = b.reconcile_protective_stops()
    assert res["kept"] == 1
    assert not fake.cancelled


def test_reconcile_ignores_untagged_orders(monkeypatch):
    # Fremde Order (kein E6-Tag) wird NIE angefasst
    fake = _FakeIB(positions=[], trades=[
        _Trade(4, _Order("SOME_OTHER_ORDER", 100, aux=80.0))])
    b = _broker(monkeypatch, fake)
    res = b.reconcile_protective_stops()
    # v37dt: Result-Contract um "added" erweitert (E6 dark-ship legt fehlende
    # Catastrophic-Stops an); Test war stale.
    assert res == {"cancelled": 0, "resized": 0, "kept": 0, "added": 0}
    assert not fake.cancelled


def test_reconcile_noop_when_disabled():
    # Disabled -> kein IB-Zugriff (kein _get_ib noetig)
    b = IbkrBroker(config={"ibkr": {}})
    assert b.reconcile_protective_stops() == {
        "cancelled": 0, "resized": 0, "kept": 0, "added": 0}
