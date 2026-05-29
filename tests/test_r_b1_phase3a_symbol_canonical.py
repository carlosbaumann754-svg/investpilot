"""Tests fuer R-B1 Phase 3a — symbol-kanonisches Matching (trader.py).

Phase 3a: Position-Matching + Cooldown werden symbol-primaer via
_canon_instrument_key. Macht alte (etoro_id-Zahl) und neue (Symbol)
Instrument-Referenzen vergleichbar — OHNE persistierte Daten zu migrieren.

NON-DESTRUKTIV: client.buy + was als instrument_id gespeichert wird bleiben
unveraendert (= Phase 3b). Hier nur die Matching/Cooldown-LOGIK.

Kern-Garantien:
  - Symbol und zugehoerige etoro_id normalisieren zum SELBEN Key
  - Alte Cooldown-Keys ("6408") interoperieren mit neuen ("AAPL")
  - Discovery-Assets (etoro_id=-1) bekommen EINDEUTIGE Keys (via Symbol)
    statt alle "-1" zu teilen
"""

from app.trader import _canon_instrument_key
from app.market_scanner import ASSET_UNIVERSE


# ---------------------------------------------------------------------------
# _canon_instrument_key Kern-Verhalten
# ---------------------------------------------------------------------------

def test_p3a_symbol_returns_symbol():
    """Bot-Symbol -> Symbol (direkt, eindeutig)."""
    assert _canon_instrument_key("AAPL") == "AAPL"
    assert _canon_instrument_key("MSFT") == "MSFT"


def test_p3a_etoro_id_maps_to_symbol():
    """etoro_id-Zahl -> zugehoeriges Symbol (Reverse)."""
    assert _canon_instrument_key(6408) == "AAPL"
    assert _canon_instrument_key(1139) == "MSFT"


def test_p3a_symbol_and_id_normalize_equal():
    """KERN: Symbol und seine etoro_id ergeben DENSELBEN Key.
    Das ist die Basis fuer alt<->neu-Interoperabilitaet."""
    assert _canon_instrument_key("AAPL") == _canon_instrument_key(6408)
    assert _canon_instrument_key("MSFT") == _canon_instrument_key(1139)


def test_p3a_numeric_string_maps_to_symbol():
    """Numerischer str (alter Cooldown-Key) -> Symbol."""
    assert _canon_instrument_key("6408") == "AAPL"


def test_p3a_unknown_id_stable_fallback():
    """Unbekannte id -> stabiler str-Fallback, kein Crash."""
    assert _canon_instrument_key(99999999) == "99999999"


# ---------------------------------------------------------------------------
# Discovery-Asset Eindeutigkeit (der Bug den Phase 3a loest)
# ---------------------------------------------------------------------------

def test_p3a_discovery_assets_get_distinct_keys():
    """Discovery-Assets (etoro_id=-1/None) bekommen EINDEUTIGE Keys via Symbol.
    Vorher: alle haetten str(-1)='-1' geteilt → Cooldown-Kollision."""
    ASSET_UNIVERSE["_TEST_SPOT_P3A"] = {"etoro_id": None, "yf": "SPOT", "class": "stocks", "name": "Spotify"}
    ASSET_UNIVERSE["_TEST_MRVL_P3A"] = {"etoro_id": None, "yf": "MRVL", "class": "stocks", "name": "Marvell"}
    try:
        k1 = _canon_instrument_key("_TEST_SPOT_P3A")
        k2 = _canon_instrument_key("_TEST_MRVL_P3A")
        assert k1 == "_TEST_SPOT_P3A"
        assert k2 == "_TEST_MRVL_P3A"
        assert k1 != k2, "Discovery-Assets muessen distinkte Keys haben"
    finally:
        del ASSET_UNIVERSE["_TEST_SPOT_P3A"]
        del ASSET_UNIVERSE["_TEST_MRVL_P3A"]


# ---------------------------------------------------------------------------
# Cooldown-Backward-Compat-Simulation
# ---------------------------------------------------------------------------

def test_p3a_old_cooldown_key_matches_new_symbol_check():
    """Simuliert die Phase-3a-Cooldown-Normalisierung: ein alter Zahlen-Key
    im State matched einen neuen Symbol-basierten Check."""
    # Alter persistierter Cooldown-State (Zahlen-Key)
    old_state = {"6408": {"symbol": "AAPL", "last_attempt": "2026-05-29T10:00:00", "attempts": 1}}
    # Phase-3a-Normalisierung (wie in trader.py)
    canon = {_canon_instrument_key(k): v for k, v in old_state.items()}
    # Neuer symbol-basierter Check
    assert _canon_instrument_key("AAPL") in canon
    assert canon[_canon_instrument_key("AAPL")]["symbol"] == "AAPL"


def test_p3a_position_match_symbol_vs_number():
    """Position-Matching: Position mit Zahl-instrument_id matched Candidate
    mit Symbol."""
    # Position hat instrument_id=6408 (Zahl, aus IBKR/Legacy)
    # Candidate hat symbol="AAPL"
    assert _canon_instrument_key(6408) == _canon_instrument_key("AAPL")


# ---------------------------------------------------------------------------
# Source-Based
# ---------------------------------------------------------------------------

def test_p3a_markers_present():
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "trader.py"
    body = src.read_text(encoding="utf-8")
    assert "R-B1 Phase 3a" in body
    assert "_canon_instrument_key" in body
    # Cooldown-Normalisierung vorhanden
    assert "_canon_cooldown" in body
