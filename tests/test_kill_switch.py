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
