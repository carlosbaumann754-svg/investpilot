"""Tests fuer R-B1 Phase 2 — resolve_contract symbol-aware (additiv).

Phase 2: resolve_contract akzeptiert jetzt ein Bot-Symbol (str) zusaetzlich
zur numerischen instrument_id. Symbol wird zur kanonischen id normalisiert,
danach laeuft der bestehende kritische Pfad unveraendert.

NON-BREAKING: numerischer Input + symbol-mit-echter-id laufen denselben Pfad.
Discovery-Assets ohne Broker-id (symbol-direkt) sind bewusst deferred
(NotImplementedError) — kommt in Phase 3+.

Tests nutzen Mock-IB (kein echter IBKR-Connect), fokussieren auf die
Symbol-Normalisierungs-Logik vor dem Qualify.
"""

import sys
import pytest
from unittest.mock import MagicMock


class _FakeContract:
    def __init__(self, secType=None, conId=0, symbol=None, exchange=None, currency=None):
        self.secType = secType
        self.conId = conId
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency


def _install_fake_ib_insync():
    """ib_insync ist lokal nicht installiert (nur VPS) — fake einsetzen."""
    fake = MagicMock()
    fake.IB = MagicMock
    fake.Contract = _FakeContract
    fake.Stock = lambda symbol, exchange, currency: _FakeContract(
        secType="STK", symbol=symbol, exchange=exchange, currency=currency)
    fake.Crypto = lambda symbol, exchange, currency: _FakeContract(
        secType="CRYPTO", symbol=symbol, exchange=exchange, currency=currency)
    fake.Forex = lambda pair: _FakeContract(secType="CASH", symbol=pair[:3])
    sys.modules["ib_insync"] = fake


_install_fake_ib_insync()

import app.ibkr_contract_resolver as resolver


# ---------------------------------------------------------------------------
# Symbol-Normalisierung
# ---------------------------------------------------------------------------

def test_rb1_p2_symbol_normalizes_to_cache_path(monkeypatch):
    """Symbol 'AAPL' wird zu id 6408 normalisiert → trifft denselben Cache-
    Pfad wie wenn 6408 direkt uebergeben wuerde."""
    # Cache mit 6408-Eintrag → Cache-Hit-Pfad, kein echter qualify noetig
    fake_cache = {
        "6408": {"secType": "STK", "conId": 265598, "symbol": "AAPL",
                 "exchange": "SMART", "currency": "USD"}
    }
    monkeypatch.setattr(resolver, "_load_cache", lambda: fake_cache)

    ib = MagicMock()
    # Symbol-Input
    c_sym = resolver.resolve_contract(ib, "AAPL")
    # id-Input (gleicher Pfad)
    c_id = resolver.resolve_contract(ib, 6408)

    assert c_sym.conId == 265598 == c_id.conId
    assert c_sym.symbol == "AAPL" == c_id.symbol


def test_rb1_p2_numeric_input_unchanged(monkeypatch):
    """Numerischer Input verhaelt sich exakt wie vorher (Backward-Compat)."""
    fake_cache = {
        "1139": {"secType": "STK", "conId": 272093, "symbol": "MSFT",
                 "exchange": "SMART", "currency": "USD"}
    }
    monkeypatch.setattr(resolver, "_load_cache", lambda: fake_cache)
    ib = MagicMock()
    c = resolver.resolve_contract(ib, 1139)
    assert c.conId == 272093
    assert c.symbol == "MSFT"


def test_rb1_p2_numeric_string_still_works(monkeypatch):
    """Numerischer String '6408' (digit) wird NICHT als Symbol behandelt."""
    fake_cache = {
        "6408": {"secType": "STK", "conId": 265598, "symbol": "AAPL",
                 "exchange": "SMART", "currency": "USD"}
    }
    monkeypatch.setattr(resolver, "_load_cache", lambda: fake_cache)
    ib = MagicMock()
    c = resolver.resolve_contract(ib, "6408")  # numeric string
    assert c.symbol == "AAPL"


# ---------------------------------------------------------------------------
# Fehler-Faelle
# ---------------------------------------------------------------------------

def test_rb1_p2_unknown_symbol_raises_valueerror():
    """Unbekanntes Symbol → ValueError mit klarer Meldung."""
    ib = MagicMock()
    with pytest.raises(ValueError, match="nicht in ASSET_UNIVERSE"):
        resolver.resolve_contract(ib, "TOTALLY_UNKNOWN_XYZ")


def test_rb1_p2_discovery_asset_symbol_deferred(monkeypatch):
    """Discovery-Asset-Symbol (etoro_id=-1) → NotImplementedError (Phase 3+)."""
    from app.market_scanner import ASSET_UNIVERSE
    ASSET_UNIVERSE["_TEST_DISCOVERY_P2"] = {
        "etoro_id": None, "yf": "X", "class": "stocks", "name": "T"
    }
    ib = MagicMock()
    try:
        with pytest.raises(NotImplementedError, match="Discovery-Asset"):
            resolver.resolve_contract(ib, "_TEST_DISCOVERY_P2")
    finally:
        del ASSET_UNIVERSE["_TEST_DISCOVERY_P2"]


# ---------------------------------------------------------------------------
# Source-Based
# ---------------------------------------------------------------------------

def test_rb1_p2_marker_present():
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "ibkr_contract_resolver.py"
    body = src.read_text(encoding="utf-8")
    assert "R-B1 Phase 2" in body
    assert "instrument_id_for_symbol" in body
