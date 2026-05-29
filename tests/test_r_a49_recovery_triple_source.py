"""Tests fuer R-A49 — Order-Status-Recovery Triple-Source-Fix.

Bug-Anlass: Carlos meldete Fr 29.05.2026 ca. 09:50 CEST Dashboard mit 9
'Pending Orders' Status PendingSubmit aus 27.-28.05.2026. Diagnose:
recover_from_ibkr() in order_status_tracker.py checkte nur:
  - ib.openTrades() (aktive Orders)
  - ib.trades() (Trade-Objekte)
NICHT:
  - ib.completedOrders() (Filled-Orders die IBKR aus active-list verschoben hat)
  - Bot's eigene trade_history.json (zweite Wahrheitsquelle)

Resultat: 10 Orders die laengst executed waren (laut Bot-trade_history)
hingen tagelang als 'PendingSubmit' im Dashboard. E27 Daily-Maintenance
loeste sie nicht auf (resolved=0) weil openTrades() leer.

R-A40 (22.05.) fixte den Subscribe-Lifecycle (per-ib-Instanz Re-Subscribe
nach Reconnect). Das war richtig — aber der Recovery-Fallback war nur
Single-Source. Wenn ein orderStatusEvent zwischen Fill und Reconnect
verloren ging, blieb Order stale bis >48h alt.

R-A49 Fix (~75 LoC + 7 Tests):
  1. Pure-Function _executed_order_ids_from_trade_history(trade_history)
  2. recover_from_ibkr erweitert:
     - Stufe 1: openTrades + trades (wie bisher)
     - Stufe 2 (NEU): completedOrders (IBKR archived Filled-Orders)
     - Stufe 3 (NEU): trade_history.json cross-ref → mark Filled
     - Stufe 4: stale-Marker (wie bisher)
  3. Bei trade_history-Resolve: _save_state() damit Status-Update persistent
"""

import json
import tempfile
from pathlib import Path

from app.order_status_tracker import _executed_order_ids_from_trade_history


# ---------------------------------------------------------------------------
# Pure-Function Helper Tests
# ---------------------------------------------------------------------------

def test_r_a49_extracts_order_ids_from_list_format():
    """Trade-History als list[dict] (eldest format)."""
    history = [
        {"order_id": 199, "symbol": "TSLA", "action": "SCANNER_BUY"},
        {"order_id": 201, "symbol": "ASML", "action": "SCANNER_BUY"},
        {"order_id": 203, "symbol": "ASML", "action": "SCANNER_BUY"},
    ]
    ids = _executed_order_ids_from_trade_history(history)
    assert ids == {"199", "201", "203"}


def test_r_a49_extracts_order_ids_from_dict_format():
    """Trade-History als dict mit 'trades'-Key (newer format)."""
    history = {
        "trades": [
            {"order_id": 199, "symbol": "TSLA"},
            {"order_id": 201, "symbol": "ASML"},
        ],
        "version": 2,
    }
    ids = _executed_order_ids_from_trade_history(history)
    assert ids == {"199", "201"}


def test_r_a49_skips_entries_without_order_id():
    """Trades ohne order_id (z.B. CLOSE-Trades mit nur Symbol) werden ignoriert."""
    history = [
        {"order_id": 199, "symbol": "TSLA"},
        {"symbol": "TSLA", "action": "TRAILING_SL_CLOSE", "pnl_pct": 1.73},  # kein oid
        {"order_id": "-", "symbol": "ASML"},  # placeholder "-"
        {"order_id": "", "symbol": "ASML"},  # empty string
    ]
    ids = _executed_order_ids_from_trade_history(history)
    assert ids == {"199"}


def test_r_a49_handles_alternative_id_field():
    """Trades koennten 'id' statt 'order_id' nutzen (legacy)."""
    history = [
        {"id": 100, "symbol": "TSLA"},
        {"order_id": 199, "symbol": "ASML"},
    ]
    ids = _executed_order_ids_from_trade_history(history)
    assert ids == {"100", "199"}


def test_r_a49_handles_empty_input():
    """Leere/None Input → empty set, kein Crash."""
    assert _executed_order_ids_from_trade_history([]) == set()
    assert _executed_order_ids_from_trade_history({"trades": []}) == set()
    assert _executed_order_ids_from_trade_history(None) == set()
    assert _executed_order_ids_from_trade_history("invalid") == set()


def test_r_a49_normalizes_int_and_str_ids():
    """Order-IDs koennen int oder str sein, output ist immer str fuer
    konsistenten Lookup gegen pending_orders.json keys (auch str)."""
    history = [
        {"order_id": 199, "symbol": "T"},      # int
        {"order_id": "201", "symbol": "A"},    # str
        {"order_id": 203, "symbol": "B"},      # int
    ]
    ids = _executed_order_ids_from_trade_history(history)
    assert ids == {"199", "201", "203"}
    # All values are str
    for v in ids:
        assert isinstance(v, str)


# ---------------------------------------------------------------------------
# Carlos's 29.05.2026 Real-World Scenario
# ---------------------------------------------------------------------------

def test_r_a49_carlos_scenario_10_stale_recovered():
    """Carlos's real-world Pending-Order-Stau am 29.05.2026:
    10 Order-IDs sind in trade_history als executed, sollten alle
    durch R-A49 als 'executed' erkannt werden."""
    history = [
        {"order_id": 185, "symbol": "ASML", "action": "SCANNER_SELL"},
        {"order_id": 187, "symbol": "ASML", "action": "SCANNER_BUY"},
        {"order_id": 189, "symbol": "XLF", "action": "SCANNER_BUY"},
        {"order_id": 194, "symbol": "XLE", "action": "STOP_LOSS_CLOSE"},
        {"order_id": 196, "symbol": "ASML", "action": "TRAILING_SL_CLOSE"},
        {"order_id": 199, "symbol": "TSLA", "action": "SCANNER_BUY"},
        {"order_id": 201, "symbol": "ASML", "action": "SCANNER_BUY"},
        {"order_id": 203, "symbol": "ASML", "action": "SCANNER_BUY"},
        {"order_id": 205, "symbol": "TSLA", "action": "SCANNER_BUY"},
        {"order_id": 210, "symbol": "ASML", "action": "TRAILING_SL_CLOSE"},
        # #197 und #209 (UNCLEAR) sind NICHT in History → bleiben pending
    ]
    pending_ids = {"185", "187", "189", "194", "196", "197", "199", "201",
                   "203", "205", "209", "210"}
    executed = _executed_order_ids_from_trade_history(history)
    # 10 von 12 pending sind in history als executed verzeichnet
    recovered = pending_ids & executed
    still_pending = pending_ids - executed
    assert len(recovered) == 10
    assert still_pending == {"197", "209"}


# ---------------------------------------------------------------------------
# Source-Based Regression
# ---------------------------------------------------------------------------

def test_r_a49_recover_uses_completedOrders():
    """R-A49 MUSS completedOrders() in recover_from_ibkr abfragen."""
    src = Path(__file__).parent.parent / "app" / "order_status_tracker.py"
    body = src.read_text(encoding="utf-8")
    fn_start = body.index("def recover_from_ibkr")
    fn_end = body.index("\n    def ", fn_start + 50)
    fn_body = body[fn_start:fn_end]
    assert "completedOrders" in fn_body, (
        "R-A49: recover_from_ibkr MUSS completedOrders() abfragen"
    )
    assert "_executed_order_ids_from_trade_history" in fn_body, (
        "R-A49: recover_from_ibkr MUSS trade_history-Cross-Ref nutzen"
    )


def test_r_a49_helper_present():
    """R-A49 Helper-Funktion MUSS im Modul existieren."""
    from app.order_status_tracker import _executed_order_ids_from_trade_history
    # Pure-Function check: signature aufrufbar mit list-Input
    result = _executed_order_ids_from_trade_history([])
    assert isinstance(result, set)
