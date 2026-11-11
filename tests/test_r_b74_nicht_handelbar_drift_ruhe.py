"""R-B74 (11.11.2026): Nicht handelbare Position (ARI.ESC) + Drift-Alarm-Ruhe.

Anlass: IBKR wandelte am 10.11. 23:38 die 19'133 ARI-Aktien (Abwicklung) in
die Treuhand-Position ARI.ESC um (exchange VALUE, ohne Handelsplatz).
(1) Der E6-Abgleich versuchte alle 5 Min einen Notfall-Stopp zu setzen, IBKR
lehnte jedes Mal ab (Error 200, keine Kontrakt-Definition), das Log meldete
trotzdem "nachgezogen".
(2) Der Abgleich-Cron meldete die Position als PHANTOM_POSITION und schickte
Carlos ab Mitternacht ALLE 30 MIN eine Pushover-Warnung (23 Stueck), weil
maybe_alert jede Drift jedes Mal sendet.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import ibkr_client
from app.ibkr_client import IbkrBroker, _CATASTROPHIC_ORDER_REF
from scripts import ibkr_reconcile


# ── (1) E6: nicht handelbare Kontrakte werden nicht immer wieder versucht ──
class _C:
    def __init__(self, conId, symbol, exchange="SMART"):
        self.conId, self.symbol, self.exchange = conId, symbol, exchange


class _P:
    def __init__(self, conId, symbol, qty, avg, exchange="SMART"):
        self.contract = _C(conId, symbol, exchange)
        self.position = qty
        self.avgCost = avg


class _IB:
    def __init__(self, positions, handelbar):
        self._pos = positions
        self.handelbar = handelbar
        self.placed, self.details_calls = [], []

    def positions(self):
        return self._pos

    def openTrades(self):
        return []

    def reqContractDetails(self, c):
        self.details_calls.append(c.conId)
        return ["details"] if c.conId in self.handelbar else []

    def placeOrder(self, c, o):
        self.placed.append((c.conId, o))


class _StopOrder:
    def __init__(self, action, qty, aux):
        self.action, self.totalQuantity, self.auxPrice = action, qty, aux


@pytest.fixture
def broker(monkeypatch):
    import sys
    import types
    fake_mod = types.SimpleNamespace(StopOrder=_StopOrder)
    monkeypatch.setitem(sys.modules, "ib_insync", fake_mod)
    monkeypatch.setattr(ibkr_client, "_E6_NICHT_HANDELBAR", set())
    b = IbkrBroker(config={"risk_management": {
        "catastrophic_stop": {"enabled": True, "pct": 40}}})
    return b


def test_e6_ueberspringt_treuhand_position(monkeypatch, broker):
    ib = _IB([_P(943998398, "ARI.ESC", 19133, 2.325, "VALUE"),
              _P(29308796, "ACAD", 2172, 19.90)], handelbar={29308796})
    monkeypatch.setattr(broker, "_get_ib", lambda: ib)
    res = broker.reconcile_protective_stops()
    assert [cid for cid, _ in ib.placed] == [29308796]
    assert res["added"] == 1
    # zweiter Zyklus: ARI.ESC wird gar nicht mehr abgefragt
    ib.details_calls.clear()
    broker.reconcile_protective_stops()
    assert 943998398 not in ib.details_calls


class _IBOhneDetails(_IB):
    reqContractDetails = None


def test_e6_ohne_details_methode_bisheriges_verhalten(monkeypatch, broker):
    """Fakes/Gateways ohne reqContractDetails: wie bisher versuchen."""
    ib = _IBOhneDetails([_P(1, "X", 100, 10.0)], handelbar=set())
    monkeypatch.setattr(broker, "_get_ib", lambda: ib)
    broker.reconcile_protective_stops()
    assert [cid for cid, _ in ib.placed] == [1]


# ── (2) Drift-Alarm: einmal melden, dann Ruhe bis Aenderung oder 12 Std ────
@pytest.fixture
def speicher(monkeypatch):
    daten, gesendet = {}, []

    def _load(name):
        v = daten.get(name)
        return dict(v) if isinstance(v, dict) else v

    def _save(name, wert):
        daten[name] = dict(wert)

    monkeypatch.setattr("app.config_manager.load_json", _load)
    monkeypatch.setattr("app.config_manager.save_json", _save)
    monkeypatch.setattr("app.alerts.send_alert",
                        lambda msg, level="INFO", config=None: gesendet.append((level, msg)) or True)
    return daten, gesendet


DRIFT = {"status": "DRIFT_DETECTED", "drifts": [
    {"type": "PHANTOM_POSITION", "symbol": "ARI.ESC", "qty": 19133.0,
     "avg_cost": 2.325, "comment": "x"}]}
OK = {"status": "OK", "drifts": []}
T0 = datetime(2026, 11, 11, 0, 13, tzinfo=timezone.utc)


def _lauf(report, jetzt):
    st = ibkr_reconcile._persist_reconcile_status(report)
    ibkr_reconcile.maybe_alert(report, st=st, jetzt=jetzt)


def test_gleiche_drift_meldet_nur_einmal_dann_alle_12h(speicher):
    _, gesendet = speicher
    for i in range(23):                       # die Nacht vom 10./11.11.
        _lauf(DRIFT, T0 + timedelta(minutes=30 * i))
    assert len(gesendet) == 1
    _lauf(DRIFT, T0 + timedelta(hours=12, minutes=1))
    assert len(gesendet) == 2


def test_neue_drift_meldet_sofort(speicher):
    _, gesendet = speicher
    _lauf(DRIFT, T0)
    andere = {"status": "DRIFT_DETECTED", "drifts": DRIFT["drifts"] + [
        {"type": "MISSED_FILL", "symbol": "KMT", "comment": "y"}]}
    _lauf(andere, T0 + timedelta(minutes=30))
    assert len(gesendet) == 2


def test_entwarnung_wenn_drift_weg(speicher):
    _, gesendet = speicher
    _lauf(DRIFT, T0)
    _lauf(OK, T0 + timedelta(minutes=30))
    assert gesendet[-1][0] == "INFO" and "keine Abweichungen" in gesendet[-1][1]
    _lauf(OK, T0 + timedelta(minutes=60))
    assert len(gesendet) == 2                 # Entwarnung nur einmal


def test_fehlerlauf_dazwischen_vergisst_die_drift_nicht(speicher):
    _, gesendet = speicher
    _lauf(DRIFT, T0)
    ibkr_reconcile._persist_reconcile_status({"status": "ERROR", "error": "Timeout"})
    _lauf(DRIFT, T0 + timedelta(hours=1))
    assert len(gesendet) == 1


def test_ohne_status_bisheriges_verhalten(speicher):
    _, gesendet = speicher
    ibkr_reconcile.maybe_alert(DRIFT)
    ibkr_reconcile.maybe_alert(DRIFT)
    assert len(gesendet) == 2
