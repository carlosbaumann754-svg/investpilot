"""
Polygon.io API Client — InvestPilot v37h+ (10.05.2026).

Schlanker HTTP-Client fuer Polygon.io. Free-Tier hat aktuell:
  - 5 calls/min Rate-Limit
  - End-of-Day-Daten (delayed 15 Min fuer Stocks; T+1 EOD-Bars gratis)
  - Stocks, ETFs, Options, Forex, Crypto

Komplementaer zu yfinance (Hauptquelle) + Alpha Vantage (Forex-Spezialist) +
Finnhub (News-Spezialist). Polygon's Staerke: zuverlaessige US-Equity-Daten
mit minimaler Latenz, plus Crypto-Quotes 24/7.

Env-Var:
    POLYGON_API_KEY   — API-Key (gratis bei polygon.io/dashboard)

Public API (analog finnhub_client / alpha_vantage_client):
  - is_available() -> bool
  - fetch_quote(symbol) -> Optional[float]              # Last-Price (delayed)
  - fetch_daily_bars(symbol, days=30) -> list[dict]     # OHLCV
  - fetch_crypto_quote(crypto_pair) -> Optional[float]  # e.g. 'BTC-USD'

Alle Funktionen sind fehlertolerant.

Rate-Limit:
  Free Tier: 5/min. Wir nutzen Polygon als FALLBACK-Layer wenn yfinance
  und AV nicht verfuegbar — selten getriggert. In-Memory-Cache 15 Min.

References:
  - https://polygon.io/docs/stocks/getting-started
  - https://polygon.io/dashboard
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("Polygon")

try:
    import requests
except ImportError:
    requests = None
    log.warning("requests nicht verfuegbar — Polygon-Client deaktiviert")

_BASE_URL = "https://api.polygon.io"
_HTTP_TIMEOUT = 10

# Free-Tier 5/min = 12s zwischen Calls.
_MIN_INTERVAL_SEC = 13.0
_last_call_ts = 0.0

# Caches
_quote_cache: dict[str, dict[str, Any]] = {}
_QUOTE_CACHE_TTL = 15 * 60

_bars_cache: dict[str, dict[str, Any]] = {}
_BARS_CACHE_TTL = 6 * 60 * 60


def _get_api_key() -> str:
    return os.environ.get("POLYGON_API_KEY", "").strip()


def is_available() -> bool:
    """True wenn requests + POLYGON_API_KEY verfuegbar."""
    return requests is not None and bool(_get_api_key())


def _rate_limit_wait() -> None:
    global _last_call_ts
    delta = time.time() - _last_call_ts
    if delta < _MIN_INTERVAL_SEC:
        time.sleep(_MIN_INTERVAL_SEC - delta)
    _last_call_ts = time.time()


def _get(path: str, params: Optional[dict[str, Any]] = None) -> Optional[dict]:
    """Interner GET mit Key-Injection."""
    if not is_available():
        return None
    _rate_limit_wait()
    params = params or {}
    params["apiKey"] = _get_api_key()
    try:
        resp = requests.get(f"{_BASE_URL}{path}", params=params, timeout=_HTTP_TIMEOUT)
        if resp.status_code == 429:
            log.warning("Polygon rate-limit hit (429)")
            return None
        if resp.status_code == 401:
            log.warning("Polygon 401 — API-Key invalid/expired")
            return None
        if resp.status_code != 200:
            log.warning("Polygon HTTP %s for %s", resp.status_code, path)
            return None
        data = resp.json()
        if data.get("status") == "ERROR":
            log.warning("Polygon error: %s", data.get("error", "?"))
            return None
        return data
    except Exception as e:
        log.warning("Polygon request failed: %s", e)
        return None


def fetch_quote(symbol: str) -> Optional[float]:
    """Vortag-Close-Price (backward-compat wrapper).

    v37h Option-C Phase 1: Wrapper um fetch_quote_with_meta() der nur
    den Preis zurueckgibt (Legacy-Signatur). Neuer Code sollte
    fetch_quote_with_meta() nutzen fuer Staleness-Awareness.
    """
    result = fetch_quote_with_meta(symbol)
    return result[0] if result else None


def fetch_quote_with_meta(symbol: str) -> Optional[tuple[float, dict]]:
    """v37h Option-C Phase 1 (13.05.2026): Vortag-Close-Price MIT Metadata.

    Returnt (price, metadata)-Tuple statt nur price, damit Caller
    Staleness erkennen kann. Bei Polygon Free-Tier ist /prev der Vortag-
    Close — fuer Live-Trading-Decisions ist das stale wenn der heutige
    Markt offen ist. Metadata bringt 'is_stale'-Flag + 'as_of_date'
    sodass Trade-Path entscheiden kann ob Cross-Validation gegen
    yfinance/AV nuetzlich ist.

    Args:
        symbol: e.g. 'AAPL'

    Returns:
        (price, metadata) tuple oder None.
        metadata-keys:
          'source': 'polygon'
          'endpoint': '/v2/aggs/prev'
          'as_of_date': YYYY-MM-DD string (Vortag, vom Bar)
          'is_stale': bool — True wenn Markt heute offen ist und Bar
                       von gestern ist (Live-Decision-relevant)
          'volume': int (Vortag-Volume, hilfreich fuer Liquidity-Check)
    """
    if not symbol:
        return None
    now = time.time()
    cached = _quote_cache.get(symbol)
    if cached and now - cached["fetched_at"] < _QUOTE_CACHE_TTL:
        # Cache enthaelt jetzt {value, meta, fetched_at}
        if "meta" in cached:
            return (cached["value"], cached["meta"])
        # Legacy-Cache-Eintrag ohne meta -> neu fetchen
        pass

    data = _get(f"/v2/aggs/ticker/{symbol.upper()}/prev",
                {"adjusted": "true"})
    if not data:
        return None
    results = data.get("results") or []
    if not results:
        return None
    last_bar = results[0]
    price = last_bar.get("c")  # 'c' = close
    if price is None:
        return None
    try:
        price_f = float(price)
        # Metadata sammeln
        from datetime import datetime, timezone
        bar_ts_ms = last_bar.get("t")  # Unix-ms timestamp
        as_of_date = ""
        if bar_ts_ms:
            try:
                as_of_date = datetime.fromtimestamp(
                    bar_ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                pass
        # is_stale: wenn as_of_date != heute UTC, ist es stale
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        is_stale = bool(as_of_date and as_of_date != today)
        meta = {
            "source": "polygon",
            "endpoint": "/v2/aggs/prev",
            "as_of_date": as_of_date,
            "is_stale": is_stale,
            "volume": int(last_bar.get("v") or 0),
        }
        _quote_cache[symbol] = {
            "value": price_f, "meta": meta, "fetched_at": now,
        }
        return (price_f, meta)
    except (ValueError, TypeError):
        return None


def fetch_daily_bars(symbol: str, days: int = 30) -> list[dict]:
    """Daily OHLCV-Bars fuer letzte N Tage.

    Args:
        symbol: e.g. 'AAPL'
        days: Anzahl Tage zurueck (max 5 Jahre auf Free-Tier)

    Returns:
        List of {"date", "open", "high", "low", "close", "volume"} oder [].
    """
    if not symbol or days <= 0:
        return []
    cache_key = f"{symbol}_{days}"
    now = time.time()
    cached = _bars_cache.get(cache_key)
    if cached and now - cached["fetched_at"] < _BARS_CACHE_TTL:
        return cached["data"]

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)
    path = (
        f"/v2/aggs/ticker/{symbol.upper()}/range/1/day/"
        f"{start_date.isoformat()}/{end_date.isoformat()}"
    )
    data = _get(path, {"adjusted": "true", "sort": "asc"})
    if not data:
        return []
    results = data.get("results") or []
    bars = []
    for r in results:
        try:
            # Polygon liefert timestamps als ms-since-epoch
            ts_ms = r.get("t", 0)
            date_str = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
            bars.append({
                "date": date_str,
                "open": float(r.get("o", 0)),
                "high": float(r.get("h", 0)),
                "low": float(r.get("l", 0)),
                "close": float(r.get("c", 0)),
                "volume": int(r.get("v", 0)),
            })
        except (KeyError, ValueError, TypeError):
            continue
    _bars_cache[cache_key] = {"data": bars, "fetched_at": now}
    return bars


def fetch_crypto_quote(pair: str) -> Optional[float]:
    """Crypto-Quote, z.B. 'BTC-USD' -> last price.

    Polygon hat 24/7 Crypto-Daten (yfinance hat Wochenende-Probleme).
    """
    if not pair or "-" not in pair:
        return None
    base, quote = pair.upper().split("-", 1)
    cache_key = f"CRYPTO_{base}_{quote}"
    now = time.time()
    cached = _quote_cache.get(cache_key)
    if cached and now - cached["fetched_at"] < _QUOTE_CACHE_TTL:
        return cached["value"]

    # Polygon-Crypto-Endpoint
    path = f"/v1/last/crypto/{base}/{quote}"
    data = _get(path)
    if not data:
        return None
    last = data.get("last") or {}
    price = last.get("price")
    if price is None:
        return None
    try:
        price_f = float(price)
        _quote_cache[cache_key] = {"value": price_f, "fetched_at": now}
        return price_f
    except (ValueError, TypeError):
        return None


def _reset_caches_for_tests() -> None:
    _quote_cache.clear()
    _bars_cache.clear()
    global _last_call_ts
    _last_call_ts = 0.0
