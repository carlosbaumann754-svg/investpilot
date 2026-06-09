"""Tests fuer app/sp600_universe.py — Universum-Integritaet."""
from app import sp600_universe as u


def test_universe_size_reasonable():
    syms = u.get_symbols()
    assert 300 <= len(syms) <= 600  # validierte ~330er-Teilmenge


def test_no_duplicates():
    assert len(u.SP600_SYMBOLS) == len(set(u.SP600_SYMBOLS))


def test_symbols_are_clean_tickers():
    for s in u.get_symbols():
        assert s == s.upper()
        assert s.isalpha() or all(c.isalnum() or c in "." for c in s)
        assert 1 <= len(s) <= 6


def test_get_symbols_sorted_and_copy():
    syms = u.get_symbols()
    assert syms == sorted(syms)
    syms.append("ZZZZ")  # darf das Modul nicht veraendern
    assert "ZZZZ" not in u.get_symbols()


def test_universe_entries_format():
    entries = u.universe_entries()
    assert len(entries) == len(u.get_symbols())
    sample = entries["AAP"]
    assert sample["yf"] == "AAP"
    assert sample["class"] == "stocks"
    # alle Eintraege haben die Pflicht-Felder
    for sym, meta in entries.items():
        assert meta["yf"] == sym
        assert meta["class"] == "stocks"
        assert "internal_id" in meta


def test_synthetic_ids_unique_and_noncolliding():
    ids = [m["internal_id"] for m in u.universe_entries().values()]
    assert len(ids) == len(set(ids))            # eindeutig
    assert all(i > 900000 for i in ids)         # kollisionsfrei zu ASSET_UNIVERSE (~1k-11k)


def test_id_maps_roundtrip():
    s2i = u.symbol_to_id()
    i2s = u.id_to_symbol()
    for sym in u.get_symbols():
        assert i2s[s2i[sym]] == sym             # roundtrip symbol->id->symbol


def test_ids_stable_across_calls():
    assert u.symbol_to_id() == u.symbol_to_id()  # deterministisch


def test_is_sp600_id():
    a_id = u.symbol_to_id()["AAP"]
    assert u.is_sp600_id(a_id) is True
    assert u.is_sp600_id(6408) is False          # AAPL (ASSET_UNIVERSE) ist kein sp600
    assert u.is_sp600_id(None) is False
    assert u.is_sp600_id("xyz") is False


def test_market_scanner_sp600_id_mapping_additive():
    """Integration: market_scanner-ID-Mapping erkennt sp600 UND die 51 (additiv,
    keine Regression fuer das bestehende Universum)."""
    from app import market_scanner as ms
    cid = ms.instrument_id_for_symbol("CALM")           # sp600
    assert cid > 900000
    assert ms.symbol_for_instrument_id(cid) == "CALM"   # roundtrip
    assert ms.instrument_id_for_symbol("AAPL") == 6408  # 51-Universe unveraendert
    assert ms.symbol_for_instrument_id(6408) == "AAPL"
