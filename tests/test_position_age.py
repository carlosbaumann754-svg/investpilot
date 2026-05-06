"""Tests fuer v37dd Position-Age-Fix.

Bug 06.05.2026: Positionen-Tab im Dashboard zeigte ALTER-Spalte fuer
TSLA/AAPL/EEM mit "--" obwohl Bot 12+ BUY-Eintraege in trade_history hat.
Root-Cause: brain.py:90-99 Snapshot-Persistence droppt position_id +
open_time. Frontend liest aus Cache → _find_position_open_time(None, None)
→ age_days=None → "--".

Fix v37dd:
1. brain.py: position_id + open_time im Snapshot-Mapping mit-persistieren.
2. _find_position_open_time: Symbol-Fallback fuer alte Cache-Snapshots ohne
   position_id (Backward-Compat fuer existierende brain_state-Daten).
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest


# ============================================================
# CORE TESTS — Symbol-Fallback im _find_position_open_time
# ============================================================

def test_symbol_fallback_finds_latest_buy():
    """Wenn position_id=None aber symbol gesetzt: nimmt letzten BUY fuer Symbol."""
    from app.trader import _find_position_open_time

    older = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    newer = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    mock_history = [
        {"symbol": "TSLA", "action": "SCANNER_BUY", "timestamp": older,
         "status": "executed", "position_id": "X1"},
        {"symbol": "TSLA", "action": "SCANNER_BUY", "timestamp": newer,
         "status": "executed", "position_id": "X2"},
        {"symbol": "AAPL", "action": "SCANNER_BUY", "timestamp": newer,
         "status": "executed", "position_id": "Y1"},
    ]
    with patch("app.trader.load_json", return_value=mock_history):
        # Ohne position_id, mit symbol="TSLA" → nimmt newer-Eintrag
        dt, age = _find_position_open_time(None, None, symbol="TSLA")

    assert dt is not None, "Symbol-Fallback sollte Datum finden"
    assert age is not None
    assert 1.5 < age < 2.5, f"Erwarte ~2 Tage (newer), got {age}"


def test_symbol_fallback_skips_cancelled_status():
    """Symbol-Fallback ignoriert cancelled/rejected Status."""
    from app.trader import _find_position_open_time

    cancelled_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    executed_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    mock_history = [
        {"symbol": "SLV", "action": "SCANNER_BUY", "timestamp": cancelled_ts,
         "status": "cancelled", "position_id": "C1"},
        {"symbol": "SLV", "action": "SCANNER_BUY", "timestamp": executed_ts,
         "status": "executed", "position_id": "E1"},
    ]
    with patch("app.trader.load_json", return_value=mock_history):
        dt, age = _find_position_open_time(None, None, symbol="SLV")

    assert dt is not None
    # Sollte den 5-Tage-alten executed nehmen, nicht den 1-Tag-alten cancelled
    assert age > 4, f"Cancelled sollte ignoriert werden, got age={age}"


def test_position_id_priority_over_symbol():
    """Wenn position_id matcht, nimmt diesen — nicht den Symbol-Fallback."""
    from app.trader import _find_position_open_time

    pid_ts = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    sym_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    mock_history = [
        {"symbol": "AAPL", "action": "SCANNER_BUY", "timestamp": pid_ts,
         "status": "executed", "position_id": "MATCH"},
        {"symbol": "AAPL", "action": "SCANNER_BUY", "timestamp": sym_ts,
         "status": "executed", "position_id": "OTHER"},
    ]
    with patch("app.trader.load_json", return_value=mock_history):
        dt, age = _find_position_open_time("MATCH", None, symbol="AAPL")

    # position_id-Match-Logic nimmt FIRST match
    assert age > 6, f"position_id-Match sollte 7-Tage-Eintrag finden, got {age}"


def test_returns_none_when_neither_id_nor_symbol_match():
    """Kein Match → (None, None)."""
    from app.trader import _find_position_open_time

    with patch("app.trader.load_json", return_value=[]):
        dt, age = _find_position_open_time(None, None, symbol="NOTFOUND")

    assert dt is None
    assert age is None


def test_signature_has_symbol_param():
    """Signatur muss optional symbol-Parameter haben."""
    import inspect
    from app.trader import _find_position_open_time

    sig = inspect.signature(_find_position_open_time)
    assert "symbol" in sig.parameters
    assert sig.parameters["symbol"].default is None


# ============================================================
# brain.py Snapshot-Persistence
# ============================================================

def test_brain_snapshot_includes_position_id_and_open_time():
    """brain.record_snapshot speichert position_id + open_time im Snapshot.

    Ohne diese Felder kann Frontend kein age_days berechnen (war v37dd-Bug).
    """
    import inspect
    import app.brain as brain_module

    src = inspect.getsource(brain_module)
    # Suche nach dem Snapshot-Position-Mapping
    assert "\"position_id\": p.get(\"position_id\")" in src, \
        "brain.py Snapshot-Persistence muss position_id mit speichern"
    assert "\"open_time\": p.get(\"open_time\")" in src, \
        "brain.py Snapshot-Persistence muss open_time mit speichern"
