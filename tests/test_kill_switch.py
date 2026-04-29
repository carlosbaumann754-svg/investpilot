"""Tests fuer den Kill-Switch (v37l Drill-Bug-Fix).

Wichtige Eigenschaft: das Trading-Flag MUSS auf false gehen, auch wenn
die Position-Schliessung fehlschlaegt (Broker-Disconnect, Quote-Errors,
leeres Portfolio, etc.). Das ist die Cutover-kritische Garantie.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.risk_manager import emergency_close_all


_SENTINEL = object()


class _FakeBroker:
    def __init__(self, portfolio_response=None, close_response=_SENTINEL):
        self._pf = portfolio_response
        # Sentinel-Trick: _SENTINEL = "default", None = "explizit Fehler simulieren"
        self._close = (
            {"orderForOpen": {"orderID": "X1"}}
            if close_response is _SENTINEL else close_response
        )
        self.close_calls = []

    def get_portfolio(self):
        return self._pf

    def close_position(self, position_id, instrument_id=None):
        self.close_calls.append((position_id, instrument_id))
        return self._close


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    """Isoliertes data/ Verzeichnis pro Test."""
    monkeypatch.setenv("INVESTPILOT_DATA_DIR", str(tmp_path))
    # Reload config_manager so es das neue ENV sieht
    import importlib
    from app import config_manager
    importlib.reload(config_manager)
    yield tmp_path


# ============================================================
# WURZEL-CHECK: Flag wird IMMER gesetzt
# ============================================================

def test_flag_set_even_when_portfolio_returns_none(temp_data_dir):
    """Drill-Bug-Reproduktion: Portfolio leer -> Flag muss trotzdem auf false."""
    broker = _FakeBroker(portfolio_response=None)
    res = emergency_close_all(broker, reason="Test")

    assert res["trading_flag_set"] is True
    assert res["closed"] == 0
    assert res["portfolio_error"] is not None  # Portfolio-Fehler dokumentiert
    flag_file = temp_data_dir / "trading_enabled.flag"
    assert flag_file.exists()
    assert flag_file.read_text().strip() == "false"


def test_flag_set_when_get_portfolio_raises(temp_data_dir):
    """Wenn der Broker eine Exception wirft -> Flag trotzdem auf false."""
    broker = MagicMock()
    broker.get_portfolio.side_effect = ConnectionError("Broker disconnected")
    res = emergency_close_all(broker, reason="Test")

    assert res["trading_flag_set"] is True
    assert "Broker disconnected" in (res["portfolio_error"] or "")
    flag_file = temp_data_dir / "trading_enabled.flag"
    assert flag_file.read_text().strip() == "false"


def test_flag_set_when_close_position_fails(temp_data_dir):
    """Wenn close_position immer fehlschlaegt -> Flag trotzdem auf false."""
    portfolio = {
        "positions": [
            {"positionID": "1", "instrumentID": 100, "amount": 1000, "isBuy": True,
             "openRate": 100.0, "currentRate": 95.0,
             "unrealizedPnL": {"pnL": -50}},
        ],
    }
    broker = _FakeBroker(portfolio_response=portfolio, close_response=None)  # close-fail
    res = emergency_close_all(broker, reason="Test")

    assert res["trading_flag_set"] is True
    assert res["failed"] >= 1
    flag_file = temp_data_dir / "trading_enabled.flag"
    assert flag_file.read_text().strip() == "false"


# ============================================================
# Erfolgs-Pfad
# ============================================================

def test_happy_path_closes_all_positions(temp_data_dir):
    """Normaler Pfad: 2 Positionen, beide werden geschlossen."""
    portfolio = {
        "positions": [
            {"positionID": "1", "instrumentID": 100, "amount": 1000, "isBuy": True,
             "openRate": 100.0, "currentRate": 105.0,
             "unrealizedPnL": {"pnL": 50}},
            {"positionID": "2", "instrumentID": 200, "amount": 500, "isBuy": True,
             "openRate": 50.0, "currentRate": 48.0,
             "unrealizedPnL": {"pnL": -10}},
        ],
    }
    broker = _FakeBroker(portfolio_response=portfolio)
    res = emergency_close_all(broker, reason="Test")

    assert res["trading_flag_set"] is True
    assert res["closed"] == 2
    assert res["failed"] == 0
    assert len(broker.close_calls) == 2


def test_empty_portfolio_returns_zero(temp_data_dir):
    """Keine offenen Positionen -> closed=0, aber Flag trotzdem gesetzt."""
    broker = _FakeBroker(portfolio_response={"positions": []})
    res = emergency_close_all(broker, reason="Test")

    assert res["trading_flag_set"] is True
    assert res["closed"] == 0
    assert res["failed"] == 0


# ============================================================
# Resultat-Schema
# ============================================================

def test_result_has_complete_schema(temp_data_dir):
    """Resultat-Dict muss alle erwarteten Keys haben."""
    broker = _FakeBroker(portfolio_response=None)
    res = emergency_close_all(broker, reason="UnitTest")

    expected_keys = {"closed", "failed", "trading_flag_set", "pause_set",
                     "portfolio_error", "reason"}
    assert expected_keys.issubset(res.keys())
    assert res["reason"] == "UnitTest"


# ============================================================
# IBKR-Fallback (v37o): wenn get_portfolio leer aber ib.positions() Daten hat
# ============================================================

class _FakeIb:
    """Mock fuer ib_insync.IB mit positions() + reqPositions() + sleep()."""
    def __init__(self, positions_list):
        self._positions = positions_list
        self.req_positions_called = False

    def positions(self):
        return list(self._positions)

    def reqPositions(self):
        self.req_positions_called = True

    def sleep(self, seconds):
        pass


class _FakeContract:
    def __init__(self, conId, symbol):
        self.conId = conId
        self.symbol = symbol


class _FakePosition:
    def __init__(self, conId, symbol, qty, avg_cost):
        self.contract = _FakeContract(conId, symbol)
        self.position = qty
        self.avgCost = avg_cost


class _FakeIbkrBroker:
    """Mock IbkrBroker mit get_portfolio() + _get_ib() + close_position."""
    def __init__(self, portfolio_response, ib_positions):
        self._pf = portfolio_response
        self._ib = _FakeIb(ib_positions)
        self.close_calls = []

    def get_portfolio(self):
        return self._pf

    def _get_ib(self):
        return self._ib

    def close_position(self, position_id, instrument_id=None):
        self.close_calls.append((position_id, instrument_id))
        return {"orderForOpen": {"orderID": "X1", "avgFillPrice": 100.0}}


def test_ibkr_direct_fallback_when_get_portfolio_empty(temp_data_dir):
    """v37o: get_portfolio leer -> ib.positions() liefert Backup-Daten."""
    # Standard-Pfad leer (Cache nicht populated), aber ib.positions() voll
    portfolio_empty = {"positions": []}
    ib_positions = [
        _FakePosition(290651477, "ROKU", qty=100, avg_cost=115.0),
        _FakePosition(365207014, "UBER", qty=50, avg_cost=76.0),
    ]
    broker = _FakeIbkrBroker(portfolio_empty, ib_positions)
    res = emergency_close_all(broker, reason="Test IBKR-Fallback")

    assert res["trading_flag_set"] is True
    assert res["closed"] == 2
    assert res["failed"] == 0
    assert len(broker.close_calls) == 2
    # Symbole-Identitaet check: position_ids sind conIds als string
    pids = {c[0] for c in broker.close_calls}
    assert pids == {"290651477", "365207014"}


def test_force_sync_when_initial_positions_empty(temp_data_dir):
    """v37o: ib.positions() initial leer -> reqPositions() + Retry."""
    portfolio_empty = {"positions": []}
    ib_initially_empty = []  # Stage 1+2: leer
    broker = _FakeIbkrBroker(portfolio_empty, ib_initially_empty)

    # Stage 3: ib.positions() liefert nach reqPositions Daten — wir patchen das
    original_positions = broker._ib.positions
    call_count = {"n": 0}
    def positions_after_sync():
        call_count["n"] += 1
        if call_count["n"] >= 2:  # nach reqPositions
            return [_FakePosition(290651477, "ROKU", qty=100, avg_cost=115.0)]
        return []
    broker._ib.positions = positions_after_sync

    res = emergency_close_all(broker, reason="Test Force-Sync")

    assert res["trading_flag_set"] is True
    assert broker._ib.req_positions_called is True
    assert res["closed"] == 1


def test_all_three_stages_empty_still_sets_flag(temp_data_dir):
    """Wenn ALLE 3 Fallback-Stufen leer: Flag trotzdem auf false."""
    portfolio_empty = {"positions": []}
    ib_empty: list = []
    broker = _FakeIbkrBroker(portfolio_empty, ib_empty)

    res = emergency_close_all(broker, reason="Test all empty")

    assert res["trading_flag_set"] is True
    assert res["closed"] == 0
    assert res["failed"] == 0
    assert res["portfolio_error"] is not None
