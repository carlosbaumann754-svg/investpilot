"""Tests fuer den Date-Range-Pfad im Backtester (Stress-Test-Mode).

Stand 2026-05-06: Hinzugefuegt fuer Stress-Test-2008/2020. Verifiziert dass
der neue start_date/end_date-Pfad die alte years-API nicht bricht.
"""

import inspect
import pytest

from app.backtester import download_history, download_vix_history, run_full_backtest


# ============================================================
# SIGNATUR-TESTS (kein yfinance-Call, keine Network-Dependency)
# ============================================================

def test_download_history_signature_has_date_params():
    """download_history muss start_date + end_date als optionale Parameter haben."""
    sig = inspect.signature(download_history)
    params = sig.parameters
    assert "start_date" in params, "start_date Parameter fehlt"
    assert "end_date" in params, "end_date Parameter fehlt"
    assert params["start_date"].default is None
    assert params["end_date"].default is None


def test_download_vix_history_signature_has_date_params():
    """download_vix_history muss start_date + end_date akzeptieren."""
    sig = inspect.signature(download_vix_history)
    params = sig.parameters
    assert "start_date" in params
    assert "end_date" in params
    assert params["start_date"].default is None
    assert params["end_date"].default is None


def test_run_full_backtest_signature_has_date_params():
    """run_full_backtest muss start_date + end_date durchreichen koennen."""
    sig = inspect.signature(run_full_backtest)
    params = sig.parameters
    assert "start_date" in params
    assert "end_date" in params
    # years bleibt als Backward-Compat-Parameter
    assert "years" in params
    assert params["years"].default == 5


def test_backward_compat_years_only_still_works():
    """Backward-Kompatibilitaet: download_history(symbols, years) ohne Dates ruft years-Pfad auf."""
    # Wir koennen die echte yfinance-API nicht aufrufen ohne Network — aber
    # zumindest darf der Aufruf nicht durch fehlende Parameter scheitern.
    sig = inspect.signature(download_history)
    bound = sig.bind_partial(symbols=["AAPL"], years=2)
    bound.apply_defaults()
    # Wenn beide Defaults None bleiben, ist Backward-Pfad aktiv
    assert bound.arguments["start_date"] is None
    assert bound.arguments["end_date"] is None


# ============================================================
# CLI-PARSER-TESTS (im backtest_runner)
# ============================================================

def test_extract_kwarg_returns_value():
    from app.backtest_runner import _extract_kwarg
    args = ["label", "--start", "2008-06-01", "--end", "2010-12-31"]
    assert _extract_kwarg(args, "--start") == "2008-06-01"
    assert _extract_kwarg(args, "--end") == "2010-12-31"


def test_extract_kwarg_returns_default_when_missing():
    from app.backtest_runner import _extract_kwarg
    args = ["label"]
    assert _extract_kwarg(args, "--start") is None
    assert _extract_kwarg(args, "--start", "fallback") == "fallback"


def test_load_symbols_from_file_strips_comments(tmp_path):
    from app.backtest_runner import _load_symbols_from_file
    f = tmp_path / "syms.txt"
    f.write_text(
        "# header comment\n"
        "AAPL\n"
        "MSFT  # inline comment\n"
        "\n"
        "# Crypto\n"
        "BTC\n"
    )
    syms = _load_symbols_from_file(str(f))
    assert syms == ["AAPL", "MSFT", "BTC"]


def test_load_symbols_from_file_handles_empty_lines(tmp_path):
    from app.backtest_runner import _load_symbols_from_file
    f = tmp_path / "syms.txt"
    f.write_text("\n\nAAPL\n\n\nMSFT\n\n")
    syms = _load_symbols_from_file(str(f))
    assert syms == ["AAPL", "MSFT"]


# ============================================================
# INTEGRATION-TEST (network-dependent, marked slow)
# ============================================================

@pytest.mark.slow
@pytest.mark.network
def test_download_history_date_range_returns_aligned_data():
    """Date-Range-Pfad: 3 Symbole + VIX, erwarte gleiche Anzahl Tage.

    Skip-bar via `pytest -m "not slow"` wenn keine Internetverbindung.
    """
    hists = download_history(
        symbols=["AAPL", "MSFT", "SPY"],
        start_date="2024-01-01",
        end_date="2024-07-31",
    )
    vix = download_vix_history(start_date="2024-01-01", end_date="2024-07-31")

    # Alle 3 Symbole haben Daten
    assert len(hists) == 3
    # Alle Symbole haben dieselbe Anzahl Tage (S&P-Trading-Tage 2024 H1 ~145)
    days = [len(df) for df in hists.values()]
    assert all(d > 100 for d in days), f"Erwarte >100 Tage, got {days}"
    assert all(d == days[0] for d in days), "Symbole sollten gleichviele Tage haben"
    # VIX synchronisiert zu Symbolen
    assert abs(len(vix) - days[0]) <= 2, "VIX sollte ~ gleich viele Tage haben"
