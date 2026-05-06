"""Tests fuer v37de Symbol-Mapping (Bot-internal <-> IBKR-Ticker).

Bug 06.05.2026: SLV/SILVER Pushover-Drift-Spam — Bot's Universum-Names
(SILVER, GOLD, OIL) matchten nicht mit IBKR-ETF-Tickers (SLV, GLD, USO).
3 Drift-Typen pro Commodity-Trade (PHANTOM_POSITION + MISSED_FILL +
CASH_DRIFT). Cutover-Blocker.

Fix v37de: Zentrale Translation-Helper in market_scanner.py + Reconcile-
Code expandiert Match-Sets bidirektional.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest


# ============================================================
# Translation-Helper (market_scanner.py)
# ============================================================

def test_bot_symbol_to_ibkr_ticker_known_aliases():
    """Bot-internal Names werden zu IBKR-ETF-Tickers gemapped."""
    from app.market_scanner import bot_symbol_to_ibkr_ticker

    assert bot_symbol_to_ibkr_ticker("SILVER") == "SLV"
    assert bot_symbol_to_ibkr_ticker("GOLD") == "GLD"
    assert bot_symbol_to_ibkr_ticker("OIL") == "USO"
    assert bot_symbol_to_ibkr_ticker("NGAS") == "UNG"
    assert bot_symbol_to_ibkr_ticker("COPPER") == "CPER"


def test_bot_symbol_passthrough_for_stocks():
    """Stocks/ETFs ohne Override: Symbol bleibt unveraendert."""
    from app.market_scanner import bot_symbol_to_ibkr_ticker

    assert bot_symbol_to_ibkr_ticker("AAPL") == "AAPL"
    assert bot_symbol_to_ibkr_ticker("TSLA") == "TSLA"
    assert bot_symbol_to_ibkr_ticker("SPY") == "SPY"


def test_ibkr_ticker_to_bot_symbol_reverse():
    """IBKR-ETF-Ticker -> Bot-Universum-Name (oder unveraendert wenn beide existieren).

    Hinweis: SLV/GLD/USO/UNG/CPER sind teilweise AUCH direkte ASSET_UNIVERSE-Keys
    (= Bot-Symbol identisch mit IBKR-Ticker). In dem Fall returnt die Funktion
    den Ticker selbst — beides ist valide. Wichtig fuer Reconcile ist dass
    expand_symbol_for_match() BEIDE Variants zurueckgibt (siehe separater Test).
    """
    from app.market_scanner import ibkr_ticker_to_bot_symbol, ASSET_UNIVERSE

    # Bei direktem Match: Funktion returnt Ticker selbst (kein Bug — andere
    # Funktion expand_symbol_for_match liefert dann beide Variants).
    result = ibkr_ticker_to_bot_symbol("SLV")
    assert result in ("SILVER", "SLV"), f"Erwarte SILVER oder SLV, got {result}"


def test_ibkr_ticker_passthrough_for_known_bot_symbols():
    """Wenn IBKR-Ticker direkt im ASSET_UNIVERSE ist (z.B. AAPL): unveraendert."""
    from app.market_scanner import ibkr_ticker_to_bot_symbol

    assert ibkr_ticker_to_bot_symbol("AAPL") == "AAPL"
    assert ibkr_ticker_to_bot_symbol("TSLA") == "TSLA"


def test_expand_symbol_for_match_commodity():
    """Commodity-Symbol expandiert auf BEIDE Variants."""
    from app.market_scanner import expand_symbol_for_match

    silver_set = expand_symbol_for_match("SILVER")
    assert "SILVER" in silver_set
    assert "SLV" in silver_set

    slv_set = expand_symbol_for_match("SLV")
    assert "SILVER" in slv_set
    assert "SLV" in slv_set


def test_expand_symbol_for_match_stock():
    """Stock-Symbol ohne Override hat nur einen Eintrag."""
    from app.market_scanner import expand_symbol_for_match

    aapl_set = expand_symbol_for_match("AAPL")
    assert aapl_set == {"AAPL"}


def test_expand_symbol_handles_empty():
    """None oder leerer String returnt leeres Set."""
    from app.market_scanner import expand_symbol_for_match

    assert expand_symbol_for_match("") == set()
    assert expand_symbol_for_match(None) == set()


# ============================================================
# Reconcile-Integration (echter Bug-Fix)
# ============================================================

def make_ibkr_state(
    positions=None,
    executions=None,
    cash=1_000_000.0,
    open_orders=None,
    cancelled_orders=None,
    rejected_orders=None,
):
    return {
        "positions": positions or [],
        "executions": executions or [],
        "cash": cash,
        "equity": cash,
        "open_orders": open_orders or [],
        "cancelled_orders": cancelled_orders or [],
        "rejected_orders": rejected_orders or [],
    }


def make_bot_history_entry(symbol, action="SCANNER_BUY", minutes_ago=30, status="executed"):
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "timestamp": ts.isoformat(),
        "symbol": symbol,
        "action": action,
        "status": status,
    }


def test_reconcile_bot_silver_matches_ibkr_slv():
    """Bug-Reproduktion 06.05.: Bot loggt SILVER, IBKR hat SLV-Position.

    Mit Symbol-Translation sollte KEIN Phantom + KEIN MissedFill mehr.
    """
    from scripts.ibkr_reconcile import reconcile

    bot_history = [make_bot_history_entry("SILVER", "SCANNER_BUY", minutes_ago=120)]
    ibkr = make_ibkr_state(
        positions=[{"symbol": "SLV", "conId": 99, "qty": 597, "avg_cost": 70.17}],
        executions=[{"exec_id": "1", "symbol": "SLV", "side": "BOT",
                     "qty": 597, "price": 70.17, "time": "2026-05-06T16:54:00"}],
    )

    with patch("scripts.ibkr_reconcile.load_bot_state", return_value=(bot_history, 1_000_000.0)), \
         patch("scripts.ibkr_reconcile.get_ibkr_state", return_value=ibkr):
        result = reconcile()

    drift_types = [d["type"] for d in result["drifts"]]
    assert "PHANTOM_POSITION" not in drift_types, \
        "SILVER bot-log + SLV ibkr-pos sollte KEIN Phantom ausloesen (Symbol-Translation)"
    assert "MISSED_FILL" not in drift_types, \
        "SILVER bot-log + SLV ibkr-execution sollte KEIN MissedFill ausloesen"


def test_reconcile_real_phantom_still_detected():
    """Regression: Echte unbekannte IBKR-Position triggert weiterhin PHANTOM."""
    from scripts.ibkr_reconcile import reconcile

    bot_history = []  # Bot kennt nichts
    ibkr = make_ibkr_state(
        positions=[{"symbol": "NVDA", "conId": 5, "qty": 50, "avg_cost": 700}],
    )
    with patch("scripts.ibkr_reconcile.load_bot_state", return_value=(bot_history, 1_000_000.0)), \
         patch("scripts.ibkr_reconcile.get_ibkr_state", return_value=ibkr):
        result = reconcile()

    drift_types = [d["type"] for d in result["drifts"]]
    assert "PHANTOM_POSITION" in drift_types
