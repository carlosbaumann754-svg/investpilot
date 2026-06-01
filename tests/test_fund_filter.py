"""R-B3 (01.06.2026) — Fonds-Filter fuer das Trading-Universum.

Hintergrund: Die Wochen-Discovery sammelte VITAX (Vanguard Information
Technology Index Fund, ein MUTUAL FUND) als Kandidaten ein. Mutual Funds
liefern Tages-NAV (bestehen also den Tages-History-Check in
analyze_single_asset), haben aber KEIN Boersen-/Intraday-Handelsdaten ->
im Live-Scan 15x/48h "possibly delisted; no price data found".

Fix: analyze_single_asset filtert nicht-handelbare yfinance-instrumentType-
Werte (MUTUALFUND/INDEX/MONEYMARKET) heraus -> sie kommen weder in den
Scanner noch (ueber die Discovery, die analyze_single_asset nutzt) ins
ASSET_UNIVERSE. Konservativ: bei unbekanntem Typ NICHT filtern (fail-open),
damit handelbare Klassen (EQUITY/ETF/CRYPTOCURRENCY/CURRENCY/FUTURE) nie
faelschlich ausgeschlossen werden.
"""
from unittest.mock import MagicMock, patch

import pandas as pd


def _fake_hist(rows=60):
    """Valide Tages-History mit genug Zeilen fuer alle Indikatoren."""
    idx = pd.date_range("2026-01-01", periods=rows, freq="D")
    base = [100.0 + i * 0.2 for i in range(rows)]
    return pd.DataFrame(
        {
            "Open": base,
            "High": [c + 1.0 for c in base],
            "Low": [c - 1.0 for c in base],
            "Close": base,
            "Volume": [1_000_000 for _ in range(rows)],
        },
        index=idx,
    )


def _fake_yf_module(instrument_type, hist=None):
    """Fake yf-Modul: Ticker() -> Objekt mit history() + history_metadata.

    Wird per patch.object(market_scanner, 'yf', ...) eingesetzt — funktioniert
    unabhaengig davon, ob yfinance lokal installiert ist (yf koennte None sein).
    """
    ticker = MagicMock()
    ticker.history.return_value = hist if hist is not None else _fake_hist()
    ticker.history_metadata = (
        {"instrumentType": instrument_type} if instrument_type is not None else {}
    )
    fake_yf = MagicMock()
    fake_yf.Ticker.return_value = ticker
    return fake_yf


_ASSET = {"yf": "TEST", "name": "Test Asset", "class": "stocks"}


def test_mutualfund_is_filtered_out():
    """MUTUALFUND (z.B. VITAX) -> None, trotz valider Tages-History."""
    from app import market_scanner

    with patch.object(market_scanner, "yf", _fake_yf_module("MUTUALFUND")):
        result = market_scanner.analyze_single_asset("VITAX", _ASSET)

    assert result is None


def test_index_is_filtered_out():
    """INDEX-Instrumente sind nicht handelbar -> None."""
    from app import market_scanner

    with patch.object(market_scanner, "yf", _fake_yf_module("INDEX")):
        result = market_scanner.analyze_single_asset("^GSPC", _ASSET)

    assert result is None


def test_equity_is_not_filtered():
    """EQUITY -> normale Analyse (Regression: handelbare Assets bleiben)."""
    from app import market_scanner

    with patch.object(market_scanner, "yf", _fake_yf_module("EQUITY")):
        result = market_scanner.analyze_single_asset("AAPL", _ASSET)

    assert result is not None
    assert "rsi" in result


def test_missing_metadata_fails_open():
    """Kein instrumentType bekannt -> handelbar lassen (konservativ)."""
    from app import market_scanner

    with patch.object(market_scanner, "yf", _fake_yf_module(None)):
        result = market_scanner.analyze_single_asset("EURUSD=X", _ASSET)

    assert result is not None
