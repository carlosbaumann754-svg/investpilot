"""Tests fuer R-A15 External-Quote-Fallback (Sprint-Tag-9, 19.05.2026).

Anlass: Sentry erfasste 19.05. um ~11:00 CEST mehrfach "Kein Quote fuer
instrument_id=5001 (USO)" in app.ibkr_client. yfinance-Coverage fuer USO
ist fluktuierend (gestern OK, heute fail). Bisher: harter Order-Abort.

Fix R-A15: get_quote() nutzt data_fallback.fetch_quote_with_fallback()
wenn IBKR keinen Quote liefert (yfinance > Alpha Vantage > Polygon > Finnhub).
"""

from unittest.mock import MagicMock, patch


def test_external_fallback_returns_price_when_ibkr_none():
    """Hauptfall: IBKR liefert None, external fallback liefert Preis."""
    from app.ibkr_contract_resolver import _try_external_fallback

    with patch("app.data_fallback.fetch_quote_with_fallback",
               return_value=78.42) as mock_fb:
        price = _try_external_fallback("USO")
    assert price == 78.42
    mock_fb.assert_called_once_with("USO")


def test_external_fallback_returns_none_for_empty_symbol():
    """Defensive: leerer/None symbol -> None ohne crash."""
    from app.ibkr_contract_resolver import _try_external_fallback
    assert _try_external_fallback(None) is None
    assert _try_external_fallback("") is None


def test_external_fallback_returns_none_when_all_sources_fail():
    """Wenn alle 4 Sources None liefern, return None."""
    from app.ibkr_contract_resolver import _try_external_fallback

    with patch("app.data_fallback.fetch_quote_with_fallback",
               return_value=None):
        price = _try_external_fallback("USO")
    assert price is None


def test_external_fallback_rejects_zero_or_negative():
    """data_fallback liefert 0 oder -1 -> treat as None (Defensive)."""
    from app.ibkr_contract_resolver import _try_external_fallback

    with patch("app.data_fallback.fetch_quote_with_fallback",
               return_value=0):
        assert _try_external_fallback("USO") is None
    with patch("app.data_fallback.fetch_quote_with_fallback",
               return_value=-1.5):
        assert _try_external_fallback("USO") is None


def test_external_fallback_exception_does_not_crash():
    """Wenn data_fallback eine Exception wirft -> None statt re-raise."""
    from app.ibkr_contract_resolver import _try_external_fallback

    with patch("app.data_fallback.fetch_quote_with_fallback",
               side_effect=RuntimeError("api down")):
        price = _try_external_fallback("USO")
    assert price is None


def test_get_quote_uses_external_fallback_when_ibkr_timeout():
    """Integration: get_quote() ruft external fallback wenn IBKR Timeout."""
    from app.ibkr_contract_resolver import get_quote

    # Mock IB-Instance + Contract — IBKR liefert nichts
    fake_ib = MagicMock()
    fake_ticker = MagicMock()
    # Alle attrs sind None oder 0 -> kein Quote von IBKR
    fake_ticker.last = None
    fake_ticker.bid = None
    fake_ticker.ask = None
    fake_ticker.marketPrice = None
    fake_ticker.close = None
    fake_ib.reqMktData.return_value = fake_ticker

    fake_contract = MagicMock()
    fake_contract.symbol = "USO"

    with patch("app.data_fallback.fetch_quote_with_fallback",
               return_value=82.15):
        # Mit kurzem Timeout damit der Test nicht 5s braucht
        price = get_quote(fake_ib, fake_contract, timeout=0.2)

    assert price == 82.15


def test_get_quote_ignores_fallback_when_ibkr_provides_price():
    """Success-Path bleibt unangetastet: IBKR liefert Quote -> kein Fallback."""
    from app.ibkr_contract_resolver import get_quote

    fake_ib = MagicMock()
    fake_ticker = MagicMock()
    fake_ticker.last = 100.0  # IBKR liefert Last
    fake_ib.reqMktData.return_value = fake_ticker

    fake_contract = MagicMock()
    fake_contract.symbol = "AAPL"

    with patch("app.data_fallback.fetch_quote_with_fallback") as mock_fb:
        price = get_quote(fake_ib, fake_contract, timeout=0.2)

    assert price == 100.0
    mock_fb.assert_not_called()  # external fallback NICHT aufgerufen
