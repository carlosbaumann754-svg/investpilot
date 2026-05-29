"""Tests fuer R-B1 Phase 4a — internal_id Dual-Key-Transition.

Phase 4: Naming-Cleanup etoro_id -> internal_id (semantisch korrekt, Bot
handelt rein ueber IBKR). Strangler-Transition: Entries tragen BEIDE Keys
(gleicher Wert), neuer Code liest internal_id via _meta_internal_id, alter
Code liest weiter etoro_id. System an jedem Zwischenstand lauffaehig.

Phase 4a: Dual-Key-Shim + Accessor + Helper-Umstellung. Werte bleiben
numerisch (int-safe — kritisch, da 4+ Stellen int(instrument_id) machen).
"""

from app.market_scanner import (
    ASSET_UNIVERSE,
    NO_INSTRUMENT_ID,
    _meta_internal_id,
    _ensure_internal_ids,
    instrument_id_for_symbol,
    symbol_for_instrument_id,
    build_broker_id_map,
)


# ---------------------------------------------------------------------------
# Dual-Key-Shim: alle Entries haben internal_id
# ---------------------------------------------------------------------------

def test_p4a_all_entries_have_internal_id():
    """Nach _ensure_internal_ids (laeuft bei Import) hat jeder Entry mit
    etoro_id auch internal_id mit GLEICHEM Wert."""
    for sym, meta in ASSET_UNIVERSE.items():
        if "etoro_id" in meta:
            assert "internal_id" in meta, f"{sym} fehlt internal_id"
            assert meta["internal_id"] == meta["etoro_id"], f"{sym} Wert-Mismatch"


def test_p4a_internal_id_is_numeric():
    """KRITISCH: internal_id bleibt numerisch (int-safe). Ein String wuerde
    die int(instrument_id)-Casts in ibkr_client/market_scanner crashen."""
    assert isinstance(ASSET_UNIVERSE["AAPL"]["internal_id"], int)
    assert ASSET_UNIVERSE["AAPL"]["internal_id"] == 6408


def test_p4a_ensure_idempotent():
    """_ensure_internal_ids ist idempotent — zweiter Lauf ergaenzt 0."""
    _ensure_internal_ids(ASSET_UNIVERSE)  # 1. (lief schon bei Import)
    added = _ensure_internal_ids(ASSET_UNIVERSE)  # 2.
    assert added == 0


def test_p4a_ensure_adds_to_legacy_entry():
    """_ensure_internal_ids ergaenzt internal_id fuer einen Entry der nur
    etoro_id hat (simuliert frisch gemergte Discovery)."""
    u = {"_TEST_LEGACY": {"etoro_id": 12345, "yf": "X", "class": "stocks"}}
    added = _ensure_internal_ids(u)
    assert added == 1
    assert u["_TEST_LEGACY"]["internal_id"] == 12345


# ---------------------------------------------------------------------------
# _meta_internal_id Accessor
# ---------------------------------------------------------------------------

def test_p4a_accessor_prefers_internal_id():
    """internal_id wird bevorzugt wenn vorhanden."""
    assert _meta_internal_id({"internal_id": 100, "etoro_id": 999}) == 100


def test_p4a_accessor_falls_back_to_etoro_id():
    """Nur etoro_id (Legacy/persistiert) -> Fallback."""
    assert _meta_internal_id({"etoro_id": 6408}) == 6408


def test_p4a_accessor_none_defensive():
    """None/leer/Falsy -> Sentinel (R-A46-Kompat)."""
    assert _meta_internal_id({}) == NO_INSTRUMENT_ID
    assert _meta_internal_id(None) == NO_INSTRUMENT_ID
    assert _meta_internal_id({"internal_id": None, "etoro_id": None}) == NO_INSTRUMENT_ID


def test_p4a_accessor_internal_none_falls_to_etoro():
    """internal_id=None aber etoro_id gesetzt -> etoro_id (Falsy-or-Kette)."""
    assert _meta_internal_id({"internal_id": None, "etoro_id": 6408}) == 6408


# ---------------------------------------------------------------------------
# Helper-Konsistenz nach Umstellung auf Accessor
# ---------------------------------------------------------------------------

def test_p4a_helpers_still_consistent():
    """instrument_id_for_symbol / symbol_for_instrument_id / build_broker_id_map
    funktionieren weiter nach Accessor-Umstellung."""
    assert instrument_id_for_symbol("AAPL") == 6408
    assert symbol_for_instrument_id(6408) == "AAPL"
    m = build_broker_id_map()
    assert m["AAPL"] == 6408
    assert m["MSFT"] == 1139


def test_p4a_roundtrip_via_internal_id():
    """Round-Trip funktioniert ueber den internal_id-Accessor."""
    for sym in ("AAPL", "MSFT", "TSLA", "NVDA"):
        iid = instrument_id_for_symbol(sym)
        assert symbol_for_instrument_id(iid) == sym


# ---------------------------------------------------------------------------
# Source-Based
# ---------------------------------------------------------------------------

def test_p4a_markers_present():
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "market_scanner.py"
    body = src.read_text(encoding="utf-8")
    assert "R-B1 Phase 4" in body
    assert "_meta_internal_id" in body
    assert "_ensure_internal_ids" in body
