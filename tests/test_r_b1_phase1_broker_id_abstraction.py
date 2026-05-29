"""Tests fuer R-B1 Phase 1 — Broker-ID-Abstraktion Foundation.

R-B1 (Soak-Phase 29.05.2026): Migration-Schuld eToro->IBKR aufloesen.
Symbol als logischer Primary-Key, Broker-IDs als Adapter-Layer.

PHASE 1 (ADDITIV, zero behavior change): zentrale Mapping-Helfer einfuehren
OHNE bestehende Aufrufer zu aendern. Werte bleiben identisch (AAPL=6408) →
keine Migration persistierter Daten.

Diese Tests verifizieren:
  - instrument_id_for_symbol / symbol_for_instrument_id Konsistenz mit
    ASSET_UNIVERSE
  - Defensive gegen etoro_id=None (R-A46 SPOT/MRVL/STX/MU)
  - build_broker_id_map Vollstaendigkeit
  - Round-Trip symbol -> id -> symbol
"""

from app.market_scanner import (
    ASSET_UNIVERSE,
    NO_INSTRUMENT_ID,
    instrument_id_for_symbol,
    symbol_for_instrument_id,
    build_broker_id_map,
)


# ---------------------------------------------------------------------------
# instrument_id_for_symbol
# ---------------------------------------------------------------------------

def test_rb1_known_symbol_returns_etoro_id():
    """Bekanntes Symbol -> die historische etoro_id (Wert unveraendert)."""
    assert instrument_id_for_symbol("AAPL") == 6408
    assert instrument_id_for_symbol("MSFT") == 1139


def test_rb1_unknown_symbol_returns_sentinel():
    """Unbekanntes Symbol -> NO_INSTRUMENT_ID (-1), kein Crash."""
    assert instrument_id_for_symbol("DOESNOTEXIST_XYZ") == NO_INSTRUMENT_ID


def test_rb1_none_etoro_id_returns_sentinel():
    """R-A46-Kompat: Asset mit etoro_id=None -> Sentinel (-1), kein Crash."""
    # Temporaer ein None-Asset einfuegen
    ASSET_UNIVERSE["_TEST_NONE_RB1"] = {
        "etoro_id": None, "yf": "X", "class": "stocks", "name": "T"
    }
    try:
        assert instrument_id_for_symbol("_TEST_NONE_RB1") == NO_INSTRUMENT_ID
    finally:
        del ASSET_UNIVERSE["_TEST_NONE_RB1"]


def test_rb1_return_type_always_int():
    """instrument_id_for_symbol gibt immer int (nie None) — sicher fuer int()-Casts."""
    assert isinstance(instrument_id_for_symbol("AAPL"), int)
    assert isinstance(instrument_id_for_symbol("NOPE"), int)


# ---------------------------------------------------------------------------
# symbol_for_instrument_id (Reverse)
# ---------------------------------------------------------------------------

def test_rb1_reverse_lookup_known_id():
    """Bekannte instrument_id -> Symbol."""
    assert symbol_for_instrument_id(6408) == "AAPL"
    assert symbol_for_instrument_id(1139) == "MSFT"


def test_rb1_reverse_lookup_accepts_str():
    """instrument_id als str (z.B. aus Cooldown-Key) wird auch aufgeloest."""
    assert symbol_for_instrument_id("6408") == "AAPL"


def test_rb1_reverse_lookup_unknown_returns_none():
    assert symbol_for_instrument_id(99999999) is None


def test_rb1_reverse_lookup_sentinel_returns_none():
    """Sentinel -1 ist nicht eindeutig (mehrere Discovery-Assets) -> None."""
    assert symbol_for_instrument_id(-1) is None
    assert symbol_for_instrument_id(NO_INSTRUMENT_ID) is None


def test_rb1_reverse_lookup_invalid_returns_none():
    """Nicht-numerische Eingabe -> None, kein Crash."""
    assert symbol_for_instrument_id("not_a_number") is None
    assert symbol_for_instrument_id(None) is None


# ---------------------------------------------------------------------------
# Round-Trip + build_broker_id_map
# ---------------------------------------------------------------------------

def test_rb1_round_trip_symbol_id_symbol():
    """symbol -> id -> symbol ergibt Original (fuer Assets mit echter ID)."""
    for sym in ("AAPL", "MSFT", "TSLA", "NVDA"):
        iid = instrument_id_for_symbol(sym)
        assert iid > 0, f"{sym} sollte echte ID haben"
        assert symbol_for_instrument_id(iid) == sym


def test_rb1_broker_id_map_covers_all_universe():
    """build_broker_id_map enthaelt jedes ASSET_UNIVERSE-Symbol."""
    m = build_broker_id_map()
    assert set(m.keys()) == set(ASSET_UNIVERSE.keys())
    # Alle Werte sind int
    assert all(isinstance(v, int) for v in m.values())


def test_rb1_broker_id_map_matches_helper():
    """build_broker_id_map konsistent mit instrument_id_for_symbol."""
    m = build_broker_id_map()
    for sym in list(ASSET_UNIVERSE.keys())[:10]:
        assert m[sym] == instrument_id_for_symbol(sym)


# ---------------------------------------------------------------------------
# Phase-1-Garantie: ZERO behavior change (additiv)
# ---------------------------------------------------------------------------

def test_rb1_phase1_asset_universe_unchanged():
    """Phase 1 ist additiv: ASSET_UNIVERSE-Eintraege haben WEITERHIN etoro_id
    (noch nicht umbenannt — das kommt in spaeterer Phase mit Compat)."""
    assert "etoro_id" in ASSET_UNIVERSE["AAPL"]
    assert ASSET_UNIVERSE["AAPL"]["etoro_id"] == 6408
