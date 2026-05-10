"""
Alpha Vantage API Client — InvestPilot v37h+ (10.05.2026).

Schlanker HTTP-Client fuer Alpha Vantage Free-Tier (5 req/min, 25 req/day).
Komplementaer zu yfinance + Polygon + Finnhub fuer:
  - Quotes / OHLCV (Stocks, ETFs)
  - Forex-Pairs (AV's eigentliche Staerke)
  - Crypto-Quotes
  - Fundamentals (Stocks)

Env-Var:
    ALPHA_VANTAGE_API_KEY   — API-Key (gratis bei alphavantage.co/support/#api-key)

Public API (analog finnhub_client):
  - is_available() -> bool
  - fetch_quote(symbol) -> Optional[float]
  - fetch_intraday(symbol, interval='5min') -> list[dict] or []
  - fetch_daily(symbol) -> list[dict] or []
  - fetch_forex_quote(from_currency, to_currency) -> Optional[float]

Alle Funktionen sind fehlertolerant: bei Fehler/Key-Missing liefern
sie None bzw. leere Listen, niemals Exceptions.

Rate-Limit:
  Free Tier: 5 req/min, 25 req/day. Sehr restriktiv.
  Wir nutzen AV als FALLBACK wenn yfinance fehlt — selten getriggert.
  In-Memory-Cache (15 Min/Symbol) reduziert Calls weiter.

References:
  - https://www.alphavantage.co/documentation/
  - https://www.alphavantage.co/support/#api-key
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger("AlphaVantage")

try:
    import requests
except ImportError:
    requests = None
    log.warning("requests nicht verfuegbar — Alpha-Vantage-Client deaktiviert")

_BASE_URL = "https://www.alphavantage.co/query"
_HTTP_TIMEOUT = 10  # AV ist langsamer als andere APIs

# Free-Tier 5/min = 12s zwischen Calls. Plus Sicherheitsbuffer.
_MIN_INTERVAL_SEC = 13.0
_last_call_ts = 0.0

# Quote-Cache: Symbol -> {"value": float, "fetched_at": float}
_quote_cache: dict[str, dict[str, Any]] = {}
_QUOTE_CACHE_TTL = 15 * 60  # 15 Min

# Daily-OHLCV-Cache: Symbol -> {"data": list, "fetched_at": float}
_daily_cache: dict[str, dict[str, Any]] = {}
_DAILY_CACHE_TTL = 6 * 60 * 60  # 6h


def _get_api_key() -> str:
    return os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()


def is_available() -> bool:
    """True wenn requests + ALPHA_VANTAGE_API_KEY verfuegbar."""
    return requests is not None and bool(_get_api_key())


def _rate_limit_wait() -> None:
    """Free-Tier: max 5 calls/min. Schutz vor 429."""
    global _last_call_ts
    delta = time.time() - _last_call_ts
    if delta < _MIN_INTERVAL_SEC:
        time.sleep(_MIN_INTERVAL_SEC - delta)
    _last_call_ts = time.time()


def _get(params: dict[str, Any]) -> Optional[dict]:
    """Interner GET mit Key-Injection + Fehlerbehandlung."""
    if not is_available():
        return None
    _rate_limit_wait()
    params["apikey"] = _get_api_key()
    try:
        resp = requests.get(_BASE_URL, params=params, timeout=_HTTP_TIMEOUT)
        if resp.status_code != 200:
            log.warning("AV HTTP %s for %s", resp.status_code, params.get("function"))
            return None
        data = resp.json()
        # AV signalisiert Fehler/Rate-Limit per JSON-Body, nicht HTTP-Code!
        if "Error Message" in data:
            log.warning("AV error: %s", data["Error Message"])
            return None
        if "Note" in data and "Thank you" in data.get("Note", ""):
            # Rate-limit hint
            log.warning("AV rate-limit hit: %s", data["Note"][:80])
            return None
        if "Information" in data:
            # Free-Tier-Limit-Notiz
            log.debug("AV info: %s", data["Information"][:80])
            if "premium" in data["Information"].lower():
                return None
        return data
    except Exception as e:
        log.warning("AV request failed: %s", e)
        return None


def fetch_quote(symbol: str) -> Optional[float]:
    """Aktueller Last-Price fuer ein Stock-Symbol.

    Args:
        symbol: e.g. 'AAPL', 'MSFT'

    Returns:
        Float-Preis oder None bei Fehler / Rate-limit / unbekanntes Symbol.
    """
    if not symbol:
        return None
    now = time.time()
    cached = _quote_cache.get(symbol)
    if cached and now - cached["fetched_at"] < _QUOTE_CACHE_TTL:
        return cached["value"]

    data = _get({"function": "GLOBAL_QUOTE", "symbol": symbol})
    if not data:
        return None
    quote = data.get("Global Quote") or {}
    price_str = quote.get("05. price")
    if not price_str:
        return None
    try:
        price = float(price_str)
        _quote_cache[symbol] = {"value": price, "fetched_at": now}
        return price
    except (ValueError, TypeError):
        return None


def fetch_daily(symbol: str, outputsize: str = "compact") -> list[dict]:
    """Daily OHLCV (last 100 trading days mit 'compact', 20+ years mit 'full').

    Args:
        symbol: e.g. 'AAPL'
        outputsize: 'compact' (default, 100 days) oder 'full'

    Returns:
        List of {"date", "open", "high", "low", "close", "volume"} oder [].
    """
    if not symbol:
        return []
    cache_key = f"{symbol}_{outputsize}"
    now = time.time()
    cached = _daily_cache.get(cache_key)
    if cached and now - cached["fetched_at"] < _DAILY_CACHE_TTL:
        return cached["data"]

    data = _get({
        "function": "TIME_SERIES_DAILY",
        "symbol": symbol,
        "outputsize": outputsize,
    })
    if not data:
        return []
    series = data.get("Time Series (Daily)") or {}
    bars = []
    for date_str, ohlcv in series.items():
        try:
            bars.append({
                "date": date_str,
                "open": float(ohlcv["1. open"]),
                "high": float(ohlcv["2. high"]),
                "low": float(ohlcv["3. low"]),
                "close": float(ohlcv["4. close"]),
                "volume": int(ohlcv["5. volume"]),
            })
        except (KeyError, ValueError, TypeError):
            continue
    bars.sort(key=lambda b: b["date"])  # ascending date
    _daily_cache[cache_key] = {"data": bars, "fetched_at": now}
    return bars


def fetch_forex_quote(from_currency: str, to_currency: str) -> Optional[float]:
    """Forex-Quote z.B. EUR -> USD.

    AV's eigentliche Staerke. yfinance hat Forex via 'EURUSD=X' aber
    instabil bei Wochenende. AV liefert auch am Wochenende last-close.
    """
    if not from_currency or not to_currency:
        return None
    cache_key = f"FX_{from_currency}_{to_currency}"
    now = time.time()
    cached = _quote_cache.get(cache_key)
    if cached and now - cached["fetched_at"] < _QUOTE_CACHE_TTL:
        return cached["value"]

    data = _get({
        "function": "CURRENCY_EXCHANGE_RATE",
        "from_currency": from_currency.upper(),
        "to_currency": to_currency.upper(),
    })
    if not data:
        return None
    rate_data = data.get("Realtime Currency Exchange Rate") or {}
    rate_str = rate_data.get("5. Exchange Rate")
    if not rate_str:
        return None
    try:
        rate = float(rate_str)
        _quote_cache[cache_key] = {"value": rate, "fetched_at": now}
        return rate
    except (ValueError, TypeError):
        return None


def _reset_caches_for_tests() -> None:
    """Test-Hook: leere alle Caches."""
    _quote_cache.clear()
    _daily_cache.clear()
    global _last_call_ts
    _last_call_ts = 0.0
