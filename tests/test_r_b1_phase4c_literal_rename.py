"""Tests fuer R-B1 Phase 4c — ASSET_UNIVERSE-Literal-Rename etoro_id->internal_id.

Phase 4c: Die 70 ASSET_UNIVERSE-Literale wurden von "etoro_id" auf
"internal_id" umbenannt (Domain-Modell sauber von eToro-Naming). Der
bidirektionale _ensure_internal_ids-Shim re-ergaenzt etoro_id zur Laufzeit
fuer die ~130 Legacy-Reader + persistierte Daten — null Read-Migration,
null Test-Churn.
"""

from pathlib import Path
from app.market_scanner import ASSET_UNIVERSE, _meta_internal_id


# ---------------------------------------------------------------------------
# Source-Domain-Modell ist sauber
# ---------------------------------------------------------------------------

def test_p4c_source_literals_use_internal_id():
    """Source-Code: ASSET_UNIVERSE-Literale nutzen {"internal_id": ...},
    KEINE {"etoro_id": ...} mehr (Domain-Modell entkoppelt)."""
    src = Path(__file__).parent.parent / "app" / "market_scanner.py"
    body = src.read_text(encoding="utf-8")
    assert '{"etoro_id":' not in body, "Literal-Naming-Leak: {\"etoro_id\": noch im Source"
    assert body.count('{"internal_id":') >= 70, "internal_id-Literale fehlen"


# ---------------------------------------------------------------------------
# Runtime-Backward-Compat via bidirektionalem Shim
# ---------------------------------------------------------------------------

def test_p4c_runtime_has_both_keys():
    """Zur Laufzeit hat jeder Entry BEIDE Keys (Shim re-ergaenzt etoro_id)."""
    for sym in ("AAPL", "MSFT", "TSLA", "SPY", "BTC"):
        meta = ASSET_UNIVERSE[sym]
        assert "internal_id" in meta, f"{sym} fehlt internal_id"
        assert "etoro_id" in meta, f"{sym} fehlt etoro_id (Shim-Compat)"
        assert meta["internal_id"] == meta["etoro_id"], f"{sym} Wert-Mismatch"


def test_p4c_values_unchanged():
    """Werte unveraendert (numerisch, int-safe)."""
    assert ASSET_UNIVERSE["AAPL"]["internal_id"] == 6408
    assert ASSET_UNIVERSE["AAPL"]["etoro_id"] == 6408
    assert ASSET_UNIVERSE["MSFT"]["internal_id"] == 1139


def test_p4c_accessor_works():
    """_meta_internal_id liefert weiter korrekte ids nach Rename."""
    assert _meta_internal_id(ASSET_UNIVERSE["AAPL"]) == 6408
    assert _meta_internal_id(ASSET_UNIVERSE["NVDA"]) == 1518


def test_p4c_legacy_etoro_id_readers_still_work():
    """Legacy-Reader die direkt meta["etoro_id"] lesen funktionieren weiter
    (Shim-Compat) — das ist der ganze Punkt des bidirektionalen Shims."""
    # Simuliert einen Legacy-Reader
    for sym, meta in list(ASSET_UNIVERSE.items())[:20]:
        eid = meta.get("etoro_id")  # alter Zugriff
        assert eid is not None, f"{sym}: etoro_id-Compat fehlt"
        assert eid == meta["internal_id"]
