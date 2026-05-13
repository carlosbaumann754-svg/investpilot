"""v37h Option-C (13.05.2026) — Tests fuer Per-Asset-Class-Routing.

Carlos's Entscheidung 13.05.2026 Abend: Option-C jetzt umsetzen (statt
nur Option A linear-Fallback). Tests fixieren das Routing-Verhalten
damit Soak-Test 13.-17.05. + Cutover-GO-Decision 19.05. abgesichert
sind.

Patterns:
  stocks/etf  -> yfinance + Polygon parallel (Cross-Validation)
  forex       -> Alpha Vantage primary
  crypto      -> Polygon primary
  commodities -> Alpha Vantage + Polygon
"""
from unittest.mock import patch

import pytest


# ============================================================
# Routing-Map: korrekte Source-Priority pro Klasse
# ============================================================

def test_stocks_route_yfinance_first():
    from app.data_fallback import _ASSET_CLASS_ROUTES
    assert _ASSET_CLASS_ROUTES["stocks"][0] == "yfinance"
    assert _ASSET_CLASS_ROUTES["stocks"][1] == "polygon"


def test_etf_same_as_stocks():
    from app.data_fallback import _ASSET_CLASS_ROUTES
    assert _ASSET_CLASS_ROUTES["etf"] == _ASSET_CLASS_ROUTES["stocks"]


def test_forex_av_primary():
    from app.data_fallback import _ASSET_CLASS_ROUTES
    assert _ASSET_CLASS_ROUTES["forex"][0] == "alpha_vantage"


def test_crypto_polygon_primary():
    """Polygon 24/7 statt yfinance-Wochenend-Probleme."""
    from app.data_fallback import _ASSET_CLASS_ROUTES
    assert _ASSET_CLASS_ROUTES["crypto"][0] == "polygon"


def test_commodities_av_primary():
    from app.data_fallback import _ASSET_CLASS_ROUTES
    assert _ASSET_CLASS_ROUTES["commodities"][0] == "alpha_vantage"


def test_unknown_falls_to_legacy_chain():
    """Fallback: keine asset_class -> Generic Chain wie Option A."""
    from app.data_fallback import _ASSET_CLASS_ROUTES
    assert _ASSET_CLASS_ROUTES["unknown"][0] == "yfinance"


# ============================================================
# Cross-Validation-Logic fuer stocks/etf
# ============================================================

def test_cross_validation_classes():
    from app.data_fallback import _CROSS_VALIDATION_CLASSES
    assert "stocks" in _CROSS_VALIDATION_CLASSES
    assert "etf" in _CROSS_VALIDATION_CLASSES
    assert "forex" not in _CROSS_VALIDATION_CLASSES
    assert "crypto" not in _CROSS_VALIDATION_CLASSES


def test_spread_check_no_anomaly_when_close():
    """Bei <0.5% Spread: kein Alarm."""
    from app.data_fallback import _check_quote_spread
    # AAPL: 200.00 vs 200.50 = 0.25% Spread
    result = _check_quote_spread(200.00, "yfinance",
                                  200.50, "polygon", "AAPL")
    assert result is False  # kein Anomalie-Flag


def test_spread_check_detects_anomaly():
    """Bei >0.5% Spread: Anomalie + Alert (Mock send_alert)."""
    from app.data_fallback import _check_quote_spread, _ANOMALY_ALERT_LAST
    _ANOMALY_ALERT_LAST.clear()
    # AAPL: 200.00 vs 205.00 = 2.5% Spread
    with patch("app.alerts.send_alert") as mock_send:
        result = _check_quote_spread(200.00, "yfinance",
                                      205.00, "polygon", "AAPL")
    assert result is True
    mock_send.assert_called_once()
    _msg, level = mock_send.call_args[0][0], mock_send.call_args[1].get("level") \
        or (mock_send.call_args[0][1] if len(mock_send.call_args[0]) > 1 else None)
    # Level kann positional oder kwarg sein
    assert "QUOTE-SPREAD-ANOMALY" in _msg
    assert "AAPL" in _msg


def test_spread_check_throttle_prevents_spam():
    """Gleicher Symbol-Spread innerhalb 1h: nur 1 Alert."""
    from app.data_fallback import _check_quote_spread, _ANOMALY_ALERT_LAST
    _ANOMALY_ALERT_LAST.clear()
    with patch("app.alerts.send_alert") as mock_send:
        for _ in range(3):
            _check_quote_spread(200.00, "yfinance", 205.00, "polygon", "AAPL")
    # Nur 1 Alert trotz 3 Aufrufen
    assert mock_send.call_count == 1


def test_spread_check_zero_or_negative_skipped():
    """Defensiv: bei 0/negative kein Crash."""
    from app.data_fallback import _check_quote_spread
    assert _check_quote_spread(0, "y", 200, "p", "X") is False
    assert _check_quote_spread(200, "y", 0, "p", "X") is False
    assert _check_quote_spread(-1, "y", 200, "p", "X") is False
    assert _check_quote_spread(None, "y", 200, "p", "X") is False


# ============================================================
# Routing-Integration: fetch_quote_for_asset_class
# ============================================================

def test_stocks_uses_yfinance_when_no_anomaly():
    """stocks-Klasse + cross_validate=True: yfinance liefert -> nutzen."""
    from app.data_fallback import fetch_quote_for_asset_class
    with patch("app.data_fallback._try_yfinance", return_value=200.0), \
         patch("app.data_fallback._try_polygon", return_value=200.10):
        result = fetch_quote_for_asset_class("AAPL", "stocks")
    assert result == 200.0  # primary (yfinance) wins


def test_forex_uses_av_first():
    """forex: AV-primary, nicht yfinance."""
    from app.data_fallback import fetch_quote_for_asset_class
    with patch("app.data_fallback._try_alpha_vantage", return_value=1.0850) as av, \
         patch("app.data_fallback._try_yfinance", return_value=1.0900):
        result = fetch_quote_for_asset_class("EUR=X", "forex")
    assert result == 1.0850  # AV wins fuer forex
    av.assert_called_once()


def test_crypto_uses_polygon_first():
    """crypto: Polygon-primary."""
    from app.data_fallback import fetch_quote_for_asset_class
    with patch("app.data_fallback._try_polygon", return_value=60000.0), \
         patch("app.data_fallback._try_yfinance", return_value=60100.0):
        result = fetch_quote_for_asset_class("BTC-USD", "crypto")
    assert result == 60000.0


def test_commodities_uses_av_first():
    """commodities: AV-primary, Polygon-secondary."""
    from app.data_fallback import fetch_quote_for_asset_class
    with patch("app.data_fallback._try_alpha_vantage", return_value=85.0), \
         patch("app.data_fallback._try_polygon", return_value=85.5):
        result = fetch_quote_for_asset_class("CPER", "commodities")
    assert result == 85.0


def test_unknown_class_fallback_chain():
    """Unbekannte asset_class -> Generic Chain wie Option A."""
    from app.data_fallback import fetch_quote_for_asset_class
    with patch("app.data_fallback._try_yfinance", return_value=150.0):
        result = fetch_quote_for_asset_class("XYZ", "unknown",
                                              cross_validate=False)
    assert result == 150.0


def test_all_sources_fail_returns_none():
    """Alle Quellen leer -> None, kein Crash."""
    from app.data_fallback import fetch_quote_for_asset_class
    with patch("app.data_fallback._try_yfinance", return_value=None), \
         patch("app.data_fallback._try_polygon", return_value=None), \
         patch("app.data_fallback._try_alpha_vantage", return_value=None), \
         patch("app.data_fallback._try_finnhub", return_value=None):
        result = fetch_quote_for_asset_class("DEAD", "stocks")
    assert result is None


def test_empty_symbol_returns_none():
    from app.data_fallback import fetch_quote_for_asset_class
    assert fetch_quote_for_asset_class("", "stocks") is None
    assert fetch_quote_for_asset_class(None, "stocks") is None


def test_cross_validate_can_be_disabled():
    """cross_validate=False: nur sequentielle Chain, kein Spread-Check."""
    from app.data_fallback import fetch_quote_for_asset_class
    with patch("app.data_fallback._try_yfinance", return_value=200.0) as yf, \
         patch("app.data_fallback._try_polygon", return_value=200.5) as pg:
        result = fetch_quote_for_asset_class("AAPL", "stocks", cross_validate=False)
    assert result == 200.0
    # Polygon nicht parallel gerufen wenn cross_validate=False
    pg.assert_not_called()


def test_cross_validate_fallback_when_primary_empty():
    """Cross-Val: primary leer -> secondary nutzen."""
    from app.data_fallback import fetch_quote_for_asset_class
    with patch("app.data_fallback._try_yfinance", return_value=None), \
         patch("app.data_fallback._try_polygon", return_value=200.0):
        result = fetch_quote_for_asset_class("AAPL", "stocks")
    assert result == 200.0
