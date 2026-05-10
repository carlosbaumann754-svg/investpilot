"""v37h Pre-Cutover (10.05.2026) — Tests fuer AV + Polygon Clients.

Fokus: Module-Behavior bei verschiedenen API-Antworten ohne echten
Network-Call (alle requests.get gemockt).
"""
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    """Caches + rate-limit-counter zwischen Tests reset."""
    from app import alpha_vantage_client as av
    from app import polygon_client as pg
    av._reset_caches_for_tests()
    pg._reset_caches_for_tests()
    # Auch time.sleep stub damit rate-limit-wait Tests nicht verlangsamt
    monkeypatch.setattr("app.alpha_vantage_client.time.sleep", lambda s: None)
    monkeypatch.setattr("app.polygon_client.time.sleep", lambda s: None)
    yield


# ============================================================
# Alpha Vantage — is_available + fetch_quote
# ============================================================

def test_av_is_available_false_without_key(monkeypatch):
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    from app import alpha_vantage_client as av
    assert av.is_available() is False


def test_av_is_available_true_with_key(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "fake123")
    from app import alpha_vantage_client as av
    assert av.is_available() is True


def test_av_fetch_quote_success(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "fake123")
    from app import alpha_vantage_client as av

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "Global Quote": {
            "01. symbol": "AAPL",
            "05. price": "201.45",
        }
    }
    with patch("app.alpha_vantage_client.requests.get", return_value=fake_resp):
        price = av.fetch_quote("AAPL")
    assert price == 201.45


def test_av_fetch_quote_uses_cache(monkeypatch):
    """Zweiter Call innerhalb Cache-TTL nicht erneut API."""
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "fake123")
    from app import alpha_vantage_client as av

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"Global Quote": {"05. price": "201.45"}}
    with patch("app.alpha_vantage_client.requests.get", return_value=fake_resp) as g:
        av.fetch_quote("AAPL")
        av.fetch_quote("AAPL")  # zweiter Call — sollte cached sein
    assert g.call_count == 1  # nur 1 echter HTTP-Call


def test_av_fetch_quote_handles_rate_limit(monkeypatch):
    """AV signalisiert Rate-Limit per JSON-Body 'Note', nicht HTTP-Code."""
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "fake123")
    from app import alpha_vantage_client as av

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "Note": "Thank you for using Alpha Vantage! Our standard API call frequency is 5/min..."
    }
    with patch("app.alpha_vantage_client.requests.get", return_value=fake_resp):
        price = av.fetch_quote("AAPL")
    assert price is None


def test_av_fetch_forex_quote(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "fake123")
    from app import alpha_vantage_client as av

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "Realtime Currency Exchange Rate": {
            "1. From_Currency Code": "EUR",
            "3. To_Currency Code": "USD",
            "5. Exchange Rate": "1.0892",
        }
    }
    with patch("app.alpha_vantage_client.requests.get", return_value=fake_resp):
        rate = av.fetch_forex_quote("EUR", "USD")
    assert rate == 1.0892


def test_av_fetch_quote_returns_none_on_http_error(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "fake123")
    from app import alpha_vantage_client as av

    fake_resp = MagicMock()
    fake_resp.status_code = 500
    with patch("app.alpha_vantage_client.requests.get", return_value=fake_resp):
        price = av.fetch_quote("AAPL")
    assert price is None


def test_av_fetch_daily_returns_sorted_bars(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "fake123")
    from app import alpha_vantage_client as av

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "Time Series (Daily)": {
            "2026-05-08": {"1. open": "200", "2. high": "202",
                           "3. low": "199", "4. close": "201",
                           "5. volume": "1000000"},
            "2026-05-07": {"1. open": "198", "2. high": "200",
                           "3. low": "197", "4. close": "199",
                           "5. volume": "950000"},
        }
    }
    with patch("app.alpha_vantage_client.requests.get", return_value=fake_resp):
        bars = av.fetch_daily("AAPL")
    assert len(bars) == 2
    # Ascending date sort
    assert bars[0]["date"] < bars[1]["date"]
    assert bars[1]["close"] == 201.0


# ============================================================
# Polygon — is_available + fetch_quote
# ============================================================

def test_polygon_is_available_false_without_key(monkeypatch):
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    from app import polygon_client as pg
    assert pg.is_available() is False


def test_polygon_fetch_quote_success(monkeypatch):
    """v37h Update: /prev endpoint statt /last/trade (Free-Tier-OK)."""
    monkeypatch.setenv("POLYGON_API_KEY", "fakepoly")
    from app import polygon_client as pg

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "status": "OK",
        "results": [
            {"o": 200, "h": 202, "l": 199, "c": 201.45,
             "v": 1000000, "t": 1715000000000},
        ],
    }
    with patch("app.polygon_client.requests.get", return_value=fake_resp):
        price = pg.fetch_quote("AAPL")
    assert price == 201.45  # Close des Vortags


def test_polygon_fetch_quote_429_returns_none(monkeypatch):
    """Polygon Rate-Limit (429) -> None ohne crash."""
    monkeypatch.setenv("POLYGON_API_KEY", "fakepoly")
    from app import polygon_client as pg

    fake_resp = MagicMock()
    fake_resp.status_code = 429
    with patch("app.polygon_client.requests.get", return_value=fake_resp):
        price = pg.fetch_quote("AAPL")
    assert price is None


def test_polygon_fetch_quote_401_invalid_key(monkeypatch):
    """Polygon 401 (invalid key) -> None + warn-log."""
    monkeypatch.setenv("POLYGON_API_KEY", "expired")
    from app import polygon_client as pg

    fake_resp = MagicMock()
    fake_resp.status_code = 401
    with patch("app.polygon_client.requests.get", return_value=fake_resp):
        price = pg.fetch_quote("AAPL")
    assert price is None


def test_polygon_fetch_daily_bars_returns_ohlcv(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "fakepoly")
    from app import polygon_client as pg

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "status": "OK",
        "results": [
            {"o": 200, "h": 202, "l": 199, "c": 201,
             "v": 1000000, "t": 1715040000000},  # 2026-05-07
            {"o": 201, "h": 203, "l": 200, "c": 202,
             "v": 950000, "t": 1715126400000},   # 2026-05-08
        ],
    }
    with patch("app.polygon_client.requests.get", return_value=fake_resp):
        bars = pg.fetch_daily_bars("AAPL", days=2)
    assert len(bars) == 2
    assert bars[0]["close"] == 201.0
    assert bars[1]["volume"] == 950000


def test_polygon_fetch_crypto_quote(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "fakepoly")
    from app import polygon_client as pg

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "status": "success",
        "last": {"price": 67250.5},
    }
    with patch("app.polygon_client.requests.get", return_value=fake_resp):
        price = pg.fetch_crypto_quote("BTC-USD")
    assert price == 67250.5


def test_polygon_fetch_crypto_quote_invalid_pair():
    from app import polygon_client as pg
    assert pg.fetch_crypto_quote("BTCUSD") is None  # missing dash
    assert pg.fetch_crypto_quote("") is None
