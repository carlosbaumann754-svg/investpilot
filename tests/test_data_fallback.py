"""v37h Pre-Cutover (10.05.2026) — Tests fuer Data-Fallback-Layer (Option A).

Verifiziert:
- yfinance-Success skippt alle Fallbacks
- Fallback-Reihenfolge: yfinance > AV > Polygon > Finnhub
- All-fail returnt None ohne Crash
- Logging differenziert success / fallback-trigger / all-fail
- get_active_sources() reflektiert Konfiguration
"""
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def reset_caches():
    from app import alpha_vantage_client as av
    from app import polygon_client as pg
    av._reset_caches_for_tests()
    pg._reset_caches_for_tests()
    yield
    av._reset_caches_for_tests()
    pg._reset_caches_for_tests()


# ============================================================
# Fallback-Chain-Reihenfolge
# ============================================================

def test_yfinance_success_skips_all_fallbacks():
    """yfinance liefert Wert -> keine Fallback-Calls."""
    from app.data_fallback import fetch_quote_with_fallback

    with patch("app.data_fallback._try_yfinance", return_value=150.5) as yf, \
         patch("app.data_fallback._try_alpha_vantage") as av, \
         patch("app.data_fallback._try_polygon") as pg, \
         patch("app.data_fallback._try_finnhub") as fh:
        result = fetch_quote_with_fallback("AAPL")

    assert result == 150.5
    yf.assert_called_once()
    av.assert_not_called()
    pg.assert_not_called()
    fh.assert_not_called()


def test_yfinance_fail_falls_to_alpha_vantage():
    """yfinance None -> Alpha Vantage versucht."""
    from app.data_fallback import fetch_quote_with_fallback

    with patch("app.data_fallback._try_yfinance", return_value=None), \
         patch("app.data_fallback._try_alpha_vantage", return_value=149.8) as av, \
         patch("app.data_fallback._try_polygon") as pg, \
         patch("app.data_fallback._try_finnhub") as fh:
        result = fetch_quote_with_fallback("AAPL")

    assert result == 149.8
    av.assert_called_once()
    pg.assert_not_called()
    fh.assert_not_called()


def test_av_fail_falls_to_polygon():
    from app.data_fallback import fetch_quote_with_fallback

    with patch("app.data_fallback._try_yfinance", return_value=None), \
         patch("app.data_fallback._try_alpha_vantage", return_value=None), \
         patch("app.data_fallback._try_polygon", return_value=151.2) as pg, \
         patch("app.data_fallback._try_finnhub") as fh:
        result = fetch_quote_with_fallback("AAPL")

    assert result == 151.2
    pg.assert_called_once()
    fh.assert_not_called()


def test_polygon_fail_falls_to_finnhub():
    from app.data_fallback import fetch_quote_with_fallback

    with patch("app.data_fallback._try_yfinance", return_value=None), \
         patch("app.data_fallback._try_alpha_vantage", return_value=None), \
         patch("app.data_fallback._try_polygon", return_value=None), \
         patch("app.data_fallback._try_finnhub", return_value=148.5) as fh:
        result = fetch_quote_with_fallback("AAPL")

    assert result == 148.5
    fh.assert_called_once()


def test_all_sources_fail_returns_none(caplog):
    """Wenn ALLE 4 Quellen failen -> None + log.error."""
    import logging
    from app.data_fallback import fetch_quote_with_fallback

    with patch("app.data_fallback._try_yfinance", return_value=None), \
         patch("app.data_fallback._try_alpha_vantage", return_value=None), \
         patch("app.data_fallback._try_polygon", return_value=None), \
         patch("app.data_fallback._try_finnhub", return_value=None):
        with caplog.at_level(logging.ERROR, logger="DataFallback"):
            result = fetch_quote_with_fallback("UNKNOWN")

    assert result is None
    error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("ALLE Quellen failed" in m for m in error_msgs)


# ============================================================
# Edge-Cases
# ============================================================

def test_empty_symbol_returns_none():
    from app.data_fallback import fetch_quote_with_fallback
    assert fetch_quote_with_fallback("") is None
    assert fetch_quote_with_fallback(None) is None


def test_zero_or_negative_quote_treated_as_invalid():
    """yfinance gibt manchmal 0.0 oder negative zurueck — als invalid behandeln."""
    from app.data_fallback import fetch_quote_with_fallback

    with patch("app.data_fallback._try_yfinance", return_value=0.0), \
         patch("app.data_fallback._try_alpha_vantage", return_value=150.5) as av:
        result = fetch_quote_with_fallback("AAPL")

    assert result == 150.5  # AV-fallback aktiv weil yfinance 0.0
    av.assert_called_once()


def test_is_valid_price_rejects_nan_inf_negative():
    """Code-Review-Fix #2: yfinance fast_info kann nan/inf liefern bei
    delisted/halted Symbolen. Ohne Guard wuerde nan durch float() durchgehen."""
    import math
    from app.data_fallback import _is_valid_price

    # Valid
    assert _is_valid_price(100.5) is True
    assert _is_valid_price(0.001) is True

    # Invalid
    assert _is_valid_price(None) is False
    assert _is_valid_price(0) is False
    assert _is_valid_price(0.0) is False
    assert _is_valid_price(-5.0) is False
    assert _is_valid_price(float("nan")) is False
    assert _is_valid_price(math.nan) is False
    assert _is_valid_price(float("inf")) is False
    assert _is_valid_price(float("-inf")) is False
    assert _is_valid_price("not_a_number") is False


def test_yfinance_nan_falls_to_av(monkeypatch):
    """yfinance liefert nan (delisted symbol) -> AV-Fallback aktiv."""
    import math
    from app.data_fallback import fetch_quote_with_fallback

    fake_yf = type("FakeYF", (), {})()
    fake_ticker = type("FakeTicker", (), {})()
    fake_info = {"lastPrice": math.nan}
    fake_ticker.fast_info = fake_info
    # history liefert leeres DataFrame
    class FakeHist:
        def __len__(self): return 0
    fake_ticker.history = lambda **kw: FakeHist()
    fake_yf.Ticker = lambda sym: fake_ticker

    with patch.dict("sys.modules", {"yfinance": fake_yf}), \
         patch("app.data_fallback._try_alpha_vantage", return_value=150.5) as av:
        result = fetch_quote_with_fallback("DELISTED")

    assert result == 150.5
    av.assert_called_once()


def test_fallback_logs_warning_on_trigger(caplog):
    import logging
    from app.data_fallback import fetch_quote_with_fallback

    with patch("app.data_fallback._try_yfinance", return_value=None), \
         patch("app.data_fallback._try_alpha_vantage", return_value=149.8):
        with caplog.at_level(logging.WARNING, logger="DataFallback"):
            fetch_quote_with_fallback("AAPL")

    warns = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Alpha Vantage geliefert" in m for m in warns)


# ============================================================
# get_active_sources() Diagnose
# ============================================================

def test_get_active_sources_returns_dict():
    from app.data_fallback import get_active_sources
    sources = get_active_sources()
    assert isinstance(sources, dict)
    for key in ("yfinance", "alpha_vantage", "polygon", "finnhub"):
        assert key in sources
        assert isinstance(sources[key], bool)


def test_get_active_sources_yfinance_always_true_when_lib_installed():
    """yfinance ist in requirements.txt -> sollte immer True sein."""
    from app.data_fallback import get_active_sources
    sources = get_active_sources()
    assert sources["yfinance"] is True


def test_get_active_sources_av_polygon_finnhub_depend_on_env(monkeypatch):
    """AV/Polygon/Finnhub-Status haengt von API-Key in env ab."""
    from app.data_fallback import get_active_sources
    from app import alpha_vantage_client as av
    from app import polygon_client as pg
    from app import finnhub_client as fh

    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    sources = get_active_sources()
    assert sources["alpha_vantage"] is False
    assert sources["polygon"] is False
    assert sources["finnhub"] is False

    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "fakekey123")
    monkeypatch.setenv("POLYGON_API_KEY", "fakepoly456")
    sources = get_active_sources()
    assert sources["alpha_vantage"] is True
    assert sources["polygon"] is True
