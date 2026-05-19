"""Tests fuer R-A27 Brain-Score-Reconstruct-from-History (Sprint-Tag-9 abend
19.05.2026).

Anlass: Dashboard-Card 'Instrument Scores' zeigte nur 4 Symbols obwohl
trade_history.json 600+ Trades ueber 16+ Symbols hatte. Wurzel: brain.py
nutzte nur live performance_snapshots als Datenquelle, geclosede Symbols
ohne Snapshot waren unsichtbar.

Fix: _collect_realized_pnls_from_history() scannt komplette History, sammelt
realized PnL pro Symbol (via Symbol-Feld direkt ODER ASSET_UNIVERSE Reverse-
Lookup ueber instrument_id ODER State-Machine durch BUY->CLOSE Tracking).
"""

import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def mock_load_json():
    """Patcht load_json fuer kontrolliertes Trade-History-Mocking."""
    state = {"history": []}

    def fake_load(filename):
        if filename == "trade_history.json":
            return state["history"]
        return None

    with patch("app.brain.load_json", side_effect=fake_load):
        yield state


def test_realized_pnls_from_close_with_explicit_symbol(mock_load_json):
    """CLOSE-Trade mit symbol-Feld direkt verwendet."""
    from app.brain import _collect_realized_pnls_from_history

    mock_load_json["history"] = [
        {"timestamp": "2026-05-01T10:00", "action": "SCANNER_BUY",
         "symbol": "AAPL", "instrument_id": 265598, "status": "executed"},
        {"timestamp": "2026-05-02T10:00", "action": "TIME_STOP_CLOSE",
         "symbol": "AAPL", "instrument_id": 265598,
         "pnl_pct": 1.5, "status": "executed"},
    ]
    realized, symbol_map = _collect_realized_pnls_from_history()
    assert "265598" in realized
    assert realized["265598"] == [1.5]
    assert symbol_map["265598"] == "AAPL"


def test_realized_pnls_from_close_via_buy_tracker(mock_load_json):
    """CLOSE-Trade OHNE symbol-Feld → State-Machine findet Symbol via vorherigem BUY."""
    from app.brain import _collect_realized_pnls_from_history

    mock_load_json["history"] = [
        # BUY mit Symbol, gleiche instrument_id wie spaeterer CLOSE
        {"timestamp": "2026-05-01T10:00", "action": "SCANNER_BUY",
         "symbol": "ROKU", "instrument_id": 8150, "status": "executed"},
        # CLOSE OHNE symbol-Feld (alter Trade-Stil), aber gleiche instrument_id
        {"timestamp": "2026-05-02T10:00", "action": "TRAILING_SL_CLOSE",
         "instrument_id": 8150, "pnl_pct": -1.15, "status": "executed"},
    ]
    realized, symbol_map = _collect_realized_pnls_from_history()
    assert "8150" in realized
    assert realized["8150"] == [-1.15]
    assert symbol_map["8150"] == "ROKU"


def test_multiple_closes_aggregate_per_symbol(mock_load_json):
    """Mehrere Closes auf dasselbe Symbol → Liste sammelt alle PnLs."""
    from app.brain import _collect_realized_pnls_from_history

    mock_load_json["history"] = [
        {"timestamp": "2026-05-01", "action": "SCANNER_BUY",
         "symbol": "AAPL", "instrument_id": 265598, "status": "executed"},
        {"timestamp": "2026-05-02", "action": "STOP_LOSS_CLOSE",
         "instrument_id": 265598, "pnl_pct": -2.0, "status": "executed"},
        {"timestamp": "2026-05-03", "action": "SCANNER_BUY",
         "symbol": "AAPL", "instrument_id": 265598, "status": "executed"},
        {"timestamp": "2026-05-05", "action": "TIME_STOP_CLOSE",
         "symbol": "AAPL", "instrument_id": 265598, "pnl_pct": 3.5, "status": "executed"},
    ]
    realized, _ = _collect_realized_pnls_from_history()
    assert sorted(realized["265598"]) == [-2.0, 3.5]


def test_failed_close_ignored(mock_load_json):
    """STOP_LOSS_CLOSE_FAILED → kein realisierter PnL gezaehlt."""
    from app.brain import _collect_realized_pnls_from_history

    mock_load_json["history"] = [
        {"timestamp": "2026-05-01", "action": "SCANNER_BUY",
         "symbol": "TSLA", "instrument_id": 5001, "status": "executed"},
        {"timestamp": "2026-05-02", "action": "STOP_LOSS_CLOSE_FAILED",
         "instrument_id": 5001, "pnl_pct": -1.0, "status": "executed"},
        {"timestamp": "2026-05-03", "action": "TRAILING_SL_CLOSE",
         "instrument_id": 5001, "pnl_pct": 2.0, "status": "executed"},
    ]
    realized, _ = _collect_realized_pnls_from_history()
    # Nur der erfolgreiche TRAILING-Close zaehlt
    assert realized.get("5001") == [2.0]


def test_status_close_failed_ignored(mock_load_json):
    """status='close_failed' → Trade nicht als realisiert gewertet."""
    from app.brain import _collect_realized_pnls_from_history

    mock_load_json["history"] = [
        {"timestamp": "2026-05-01", "action": "SCANNER_BUY",
         "symbol": "MSFT", "instrument_id": 7000, "status": "executed"},
        {"timestamp": "2026-05-02", "action": "TRAILING_SL_CLOSE",
         "instrument_id": 7000, "pnl_pct": -3.0, "status": "close_failed"},
    ]
    realized, _ = _collect_realized_pnls_from_history()
    assert "7000" not in realized


def test_close_without_pnl_ignored(mock_load_json):
    """CLOSE-Trade ohne pnl_pct → kein Beitrag zum Score."""
    from app.brain import _collect_realized_pnls_from_history

    mock_load_json["history"] = [
        {"timestamp": "2026-05-01", "action": "SCANNER_BUY",
         "symbol": "NVDA", "instrument_id": 9000, "status": "executed"},
        {"timestamp": "2026-05-02", "action": "TIME_STOP_CLOSE",
         "instrument_id": 9000, "status": "executed"},  # pnl_pct fehlt
    ]
    realized, _ = _collect_realized_pnls_from_history()
    assert "9000" not in realized


def test_close_without_any_symbol_reference_ignored(mock_load_json):
    """CLOSE-Trade ohne symbol-Feld + kein BUY vorher + nicht in ASSET_UNIVERSE → skip."""
    from app.brain import _collect_realized_pnls_from_history

    mock_load_json["history"] = [
        # Nur CLOSE, kein vorheriger BUY für diese instrument_id
        {"timestamp": "2026-05-02", "action": "TIME_STOP_CLOSE",
         "instrument_id": 99999999, "pnl_pct": 1.5, "status": "executed"},
    ]
    realized, _ = _collect_realized_pnls_from_history()
    # 99999999 ist nicht im ASSET_UNIVERSE -> kein Mapping -> skip
    # (Symbol-bestimmung scheitert)
    assert "99999999" not in realized


def test_empty_history(mock_load_json):
    """Empty history → empty dicts."""
    from app.brain import _collect_realized_pnls_from_history
    mock_load_json["history"] = []
    realized, symbol_map = _collect_realized_pnls_from_history()
    assert realized == {}
    assert symbol_map == {}


def test_chronological_order_independent(mock_load_json):
    """Function sortiert intern → unsortierter Input gibt korrektes Ergebnis."""
    from app.brain import _collect_realized_pnls_from_history

    mock_load_json["history"] = [
        # In falsch-Reihenfolge: CLOSE vor BUY
        {"timestamp": "2026-05-02", "action": "TIME_STOP_CLOSE",
         "instrument_id": 100, "pnl_pct": 0.5, "status": "executed"},
        {"timestamp": "2026-05-01", "action": "SCANNER_BUY",
         "symbol": "X", "instrument_id": 100, "status": "executed"},
    ]
    realized, symbol_map = _collect_realized_pnls_from_history()
    assert "100" in realized
    assert symbol_map.get("100") == "X"
