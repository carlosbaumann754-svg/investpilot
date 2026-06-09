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
