"""Tests fuer R-A21 Phantom-Check robust gegen Long-Hold-Positions.

Carlos's Bug-Spot 19.05.2026: "Wenn Bot Position laenger als Lookback-
Window haelt, loest Reconcile-Cron permanente Phantom-Position-Alerts aus".

Bisherige Logik: bot_known_symbols nur aus recent_bot (time-filtered).
-> False-Positive bei Long-Hold.

Fix R-A21: _compute_currently_open_symbols scannt KOMPLETTE history,
state-machine pro Symbol (open/closed via BUY/FULL-CLOSE-Actions).
PARTIAL_CLOSE / *_FAILED bleiben unverändert.
"""

import pytest


# ============================================================
# 1. Long-Hold Position — KEIN Phantom mehr
# ============================================================

def test_long_hold_position_recognized_as_open():
    """Bot kaufte AAPL vor 60 Tagen, haelt noch -> bekannt, kein Phantom."""
    from scripts.ibkr_reconcile import _compute_currently_open_symbols
    history = [
        {"timestamp": "2026-03-20T10:00:00", "action": "SCANNER_BUY", "symbol": "AAPL"},
        # ... 60 Tage spaeter noch immer offen
    ]
    open_syms = _compute_currently_open_symbols(history)
    assert "AAPL" in open_syms


def test_closed_position_not_in_open_set():
    """Bot kaufte+verkaufte komplett -> Position ist closed, NICHT in Set."""
    from scripts.ibkr_reconcile import _compute_currently_open_symbols
    history = [
        {"timestamp": "2026-03-20T10:00:00", "action": "SCANNER_BUY", "symbol": "AAPL"},
        {"timestamp": "2026-04-15T10:00:00", "action": "STOP_LOSS_CLOSE", "symbol": "AAPL"},
    ]
    open_syms = _compute_currently_open_symbols(history)
    assert "AAPL" not in open_syms


def test_buy_close_buy_again_is_open():
    """Bot kauft -> closed -> kauft erneut -> aktuell open."""
    from scripts.ibkr_reconcile import _compute_currently_open_symbols
    history = [
        {"timestamp": "2026-01-01T10:00:00", "action": "SCANNER_BUY", "symbol": "AAPL"},
        {"timestamp": "2026-02-01T10:00:00", "action": "TRAILING_SL_CLOSE", "symbol": "AAPL"},
        {"timestamp": "2026-03-01T10:00:00", "action": "SCANNER_BUY", "symbol": "AAPL"},
    ]
    open_syms = _compute_currently_open_symbols(history)
    assert "AAPL" in open_syms


# ============================================================
# 2. PARTIAL_CLOSE — Position bleibt offen
# ============================================================

def test_partial_close_keeps_position_open():
    """Bot kauft 100 Shares, verkauft 30 partial -> 70 noch offen."""
    from scripts.ibkr_reconcile import _compute_currently_open_symbols
    history = [
        {"timestamp": "2026-01-01T10:00:00", "action": "SCANNER_BUY", "symbol": "KO"},
        {"timestamp": "2026-02-01T10:00:00", "action": "PARTIAL_CLOSE", "symbol": "KO"},
    ]
    open_syms = _compute_currently_open_symbols(history)
    assert "KO" in open_syms


def test_partial_signal_does_not_close():
    """PARTIAL_SIGNAL ist kein echter Trade -> kein State-Change."""
    from scripts.ibkr_reconcile import _compute_currently_open_symbols
    history = [
        {"timestamp": "2026-01-01T10:00:00", "action": "SCANNER_BUY", "symbol": "XLE"},
        {"timestamp": "2026-02-01T10:00:00", "action": "PARTIAL_SIGNAL", "symbol": "XLE"},
    ]
    open_syms = _compute_currently_open_symbols(history)
    assert "XLE" in open_syms


# ============================================================
# 3. Failed Closes — Position bleibt offen
# ============================================================

def test_failed_close_does_not_close_position():
    """STOP_LOSS_CLOSE_FAILED -> Try-Fail, Position bleibt offen."""
    from scripts.ibkr_reconcile import _compute_currently_open_symbols
    history = [
        {"timestamp": "2026-01-01T10:00:00", "action": "SCANNER_BUY", "symbol": "MSFT"},
        {"timestamp": "2026-02-01T10:00:00", "action": "STOP_LOSS_CLOSE_FAILED", "symbol": "MSFT"},
    ]
    open_syms = _compute_currently_open_symbols(history)
    assert "MSFT" in open_syms


def test_status_close_failed_keeps_open():
    """status='close_failed' -> kein echter Close, Position offen."""
    from scripts.ibkr_reconcile import _compute_currently_open_symbols
    history = [
        {"timestamp": "2026-01-01T10:00:00", "action": "SCANNER_BUY", "symbol": "NVDA"},
        {"timestamp": "2026-02-01T10:00:00", "action": "TRAILING_SL_CLOSE",
         "symbol": "NVDA", "status": "close_failed"},
    ]
    open_syms = _compute_currently_open_symbols(history)
    assert "NVDA" in open_syms


# ============================================================
# 4. Verschiedene Close-Types alle als Close erkannt
# ============================================================

@pytest.mark.parametrize("close_action", [
    "TRAILING_SL_CLOSE",
    "STOP_LOSS_CLOSE",
    "TIME_STOP_CLOSE",
    "EARNINGS_BLACKOUT_CLOSE",
    "SCANNER_SELL",
    "MANUAL_SELL",
])
def test_all_full_close_actions_recognized(close_action):
    """Alle Full-Close-Actions setzen State auf closed."""
    from scripts.ibkr_reconcile import _compute_currently_open_symbols
    history = [
        {"timestamp": "2026-01-01T10:00:00", "action": "SCANNER_BUY", "symbol": "TSLA"},
        {"timestamp": "2026-02-01T10:00:00", "action": close_action, "symbol": "TSLA"},
    ]
    open_syms = _compute_currently_open_symbols(history)
    assert "TSLA" not in open_syms, f"{close_action} sollte Position closed setzen"


# ============================================================
# 5. Edge-Cases
# ============================================================

def test_empty_history_returns_empty_set():
    from scripts.ibkr_reconcile import _compute_currently_open_symbols
    assert _compute_currently_open_symbols([]) == set()
    assert _compute_currently_open_symbols(None) == set()


def test_no_symbol_in_trade_ignored():
    """Trades ohne Symbol -> defensive ignoriert."""
    from scripts.ibkr_reconcile import _compute_currently_open_symbols
    history = [
        {"timestamp": "2026-01-01T10:00:00", "action": "SCANNER_BUY"},  # kein symbol
        {"timestamp": "2026-02-01T10:00:00", "action": "SCANNER_BUY", "symbol": "QQQ"},
    ]
    open_syms = _compute_currently_open_symbols(history)
    assert "QQQ" in open_syms
    assert len(open_syms) == 1  # nur QQQ, kein Crash


def test_unsorted_history_handled_correctly():
    """History in unsorted order — Funktion soll trotzdem korrekt sortieren."""
    from scripts.ibkr_reconcile import _compute_currently_open_symbols
    history = [
        # In falsch-Reihenfolge:
        {"timestamp": "2026-02-01T10:00:00", "action": "STOP_LOSS_CLOSE", "symbol": "GOOGL"},
        {"timestamp": "2026-01-01T10:00:00", "action": "SCANNER_BUY", "symbol": "GOOGL"},
    ]
    open_syms = _compute_currently_open_symbols(history)
    assert "GOOGL" not in open_syms  # nach Sortierung: BUY zuerst, dann CLOSE -> closed


def test_multiple_symbols_independent_state():
    """Mehrere Symbols mit unterschiedlichen States."""
    from scripts.ibkr_reconcile import _compute_currently_open_symbols
    history = [
        {"timestamp": "2026-01-01T10:00:00", "action": "SCANNER_BUY", "symbol": "AAPL"},
        {"timestamp": "2026-01-02T10:00:00", "action": "SCANNER_BUY", "symbol": "KO"},
        {"timestamp": "2026-01-03T10:00:00", "action": "SCANNER_BUY", "symbol": "XLE"},
        {"timestamp": "2026-02-01T10:00:00", "action": "TIME_STOP_CLOSE", "symbol": "AAPL"},
    ]
    open_syms = _compute_currently_open_symbols(history)
    assert "KO" in open_syms
    assert "XLE" in open_syms
    assert "AAPL" not in open_syms  # closed


def test_carlos_xle_ko_scenario():
    """Direkt Carlos's Scenario: XLE + KO jahrelang gehalten + heute KO partial."""
    from scripts.ibkr_reconcile import _compute_currently_open_symbols
    history = [
        # XLE 60 Tage alt — kein BUY in 30d-Lookback aber HISTORIE hat ihn
        {"timestamp": "2026-03-20T10:00:00", "action": "SCANNER_BUY", "symbol": "XLE"},
        # KO ebenfalls alt
        {"timestamp": "2026-02-15T10:00:00", "action": "SCANNER_BUY", "symbol": "KO"},
        # Heute Partial-Close auf KO (reduziert qty aber State open)
        {"timestamp": "2026-05-19T15:43:00", "action": "PARTIAL_CLOSE", "symbol": "KO"},
    ]
    open_syms = _compute_currently_open_symbols(history)
    # BEIDE muessen als open erkannt werden -> KEIN Phantom-Alert mehr
    assert "XLE" in open_syms
    assert "KO" in open_syms
