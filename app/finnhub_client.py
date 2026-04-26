"""
Finnhub API Client — InvestPilot v12+.

Schlanker HTTP-Client fuer Finnhub.io Free-Tier (60 req/min).
Liefert News + Wirtschaftskalender als Upgrade zu yfinance.

Env-Var:
    FINNHUB_API_KEY   — API-Key (gratis, nur E-Mail noetig)

Public API:
  - is_available() -> bool
  - fetch_company_news(symbol, days=7) -> list[str]
  - fetch_general_market_news(category="general") -> list[str]
  - fetch_news_sentiment(symbol) -> dict or None
  - fetch_economic_calendar(days_ahead=1) -> list[dict]

Alle Funktionen sind fehlertolerant: bei Fehler/Key-Missing liefern
sie leere Listen bzw. None, niemals Exceptions.

Caching:
  - Company-News: 4h pro Symbol (in-memory)
  - Economic Calendar: 6h global (in-memory)
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger("Finnhub")

try:
    import requests
except ImportError:
    requests = None
    log.warning("requests nicht verfuegbar — Finnhub-Client deaktiviert")

_BASE_URL = "https://finnhub.io/api/v1"
_HTTP_TIMEOUT = 8  # sec

# Simple rate-limit guard: Finnhub Free = 60/min. Wir zielen auf <= 30/min.
_MIN_INTERVAL_SEC = 2.0
_last_call_ts = 0.0

# Caches
_news_cache: dict[str, dict[str, Any]] = {}
_NEWS_CACHE_TTL = 4 * 60 * 60  # 4h

_sentiment_cache: dict[str, dict[str, Any]] = {}
_SENTIMENT_CACHE_TTL = 4 * 60 * 60

_calendar_cache: dict[str, Any] = {"fetched_at": 0.0, "events": []}
_CALENDAR_CACHE_TTL = 6 * 60 * 60  # 6h

_general_news_cache: dict[str, Any] = {"fetched_at": 0.0, "headlines": []}
_GENERAL_CACHE_TTL = 2 * 60 * 60  # 2h


def _get_api_key() -> str:
    return os.environ.get("FINNHUB_API_KEY", "").strip()


def is_available() -> bool:
    """True wenn requests + FINNHUB_API_KEY verfuegbar sind."""
    return requests is not None and bool(_get_api_key())


def _rate_limit_wait() -> None:
    """Schlichter Gate um Finnhub-Ratelimit nicht zu reissen."""
    global _last_call_ts
    delta = time.time() - _last_call_ts
    if delta < _MIN_INTERVAL_SEC:
        time.sleep(_MIN_INTERVAL_SEC - delta)
    _last_call_ts = time.time()


def _get(path: str, params: dict[str, Any]) -> Any:
    """Interner GET mit Key-Injection + Fehlerbehandlung."""
    if not is_available():
        return None
    params = dict(params)
    params["token"] = _get_api_key()
    url = f"{_BASE_URL}{path}"
    try:
        _rate_limit_wait()
        r = requests.get(url, params=params, timeout=_HTTP_TIMEOUT)
        if r.status_code == 429:
            log.warning("Finnhub Rate-Limit erreicht (429)")
            return None
        if r.status_code != 200:
            log.debug(f"Finnhub {path} HTTP {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        log.debug(f"Finnhub {path} Fehler: {e}")
        return None


# ============================================================
# Company News
# ============================================================

def fetch_company_news(symbol: str, days: int = 7) -> list[str]:
    """
    Liefert Liste von Headline-Strings (title + summary combined) fuer ein
    Symbol ueber die letzten `days` Tage.  Leer bei Fehler.
    """
    if not symbol:
        return []

    now = time.time()
    cached = _news_cache.get(symbol)
    if cached and now - cached["fetched_at"] < _NEWS_CACHE_TTL:
        return cached["headlines"]

    to_date = datetime.utcnow().date()
    from_date = to_date - timedelta(days=max(1, days))
    data = _get("/company-news", {
        "symbol": symbol,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
    })

    headlines: list[str] = []
    if isinstance(data, list):
        for article in data[:30]:  # Cap: max 30 Artikel pro Symbol
            if not isinstance(article, dict):
                continue
            title = (article.get("headline") or "").strip()
            summary = (article.get("summary") or "").strip()
            combined = f"{title}. {summary}".strip(". ")
            if combined:
                headlines.append(combined)

    _news_cache[symbol] = {"headlines": headlines, "fetched_at": now}
    return headlines


def fetch_general_market_news(category: str = "general") -> list[str]:
    """
    Allgemeine Markt-News (SPY-Feed-artig).  Cached 2h.
    Categories: general, forex, crypto, merger
    """
    now = time.time()
    if now - _general_news_cache["fetched_at"] < _GENERAL_CACHE_TTL:
        return _general_news_cache["headlines"]

    data = _get("/news", {"category": category})
    headlines: list[str] = []
    if isinstance(data, list):
        for article in data[:30]:
            if not isinstance(article, dict):
                continue
            title = (article.get("headline") or "").strip()
            summary = (article.get("summary") or "").strip()
            combined = f"{title}. {summary}".strip(". ")
            if combined:
                headlines.append(combined)

    _general_news_cache["headlines"] = headlines
    _general_news_cache["fetched_at"] = now
    return headlines


# ============================================================
# News Sentiment (Finnhub-internes Scoring)
# ============================================================

def fetch_news_sentiment(symbol: str) -> dict | None:
    """
    Finnhub's eigenes News-Sentiment pro Symbol.
    Returns dict {score, buzz_weekly, source: "finnhub"} oder None.

    Score-Normalisierung: Finnhub liefert `companyNewsScore` in 0..1.
    Wir mappen auf -1..+1 (0.5 = neutral).
    """
    if not symbol:
        return None

    now = time.time()
    cached = _sentiment_cache.get(symbol)
    if cached and now - cached["fetched_at"] < _SENTIMENT_CACHE_TTL:
        return cached["result"]

    data = _get("/news-sentiment", {"symbol": symbol})
    if not isinstance(data, dict):
        _sentiment_cache[symbol] = {"result": None, "fetched_at": now}
        return None

    raw_score = data.get("companyNewsScore")
    if raw_score is None:
        _sentiment_cache[symbol] = {"result": None, "fetched_at": now}
        return None

    try:
        # 0..1 -> -1..+1
        score = max(-1.0, min(1.0, (float(raw_score) - 0.5) * 2.0))
    except (TypeError, ValueError):
        _sentiment_cache[symbol] = {"result": None, "fetched_at": now}
        return None

    buzz = data.get("buzz") or {}
    weekly_articles = 0
    try:
        weekly_articles = int(buzz.get("articlesInLastWeek") or 0)
    except (TypeError, ValueError):
        pass

    result = {
        "score": round(score, 3),
        "buzz_weekly": weekly_articles,
        "source": "finnhub",
    }
    _sentiment_cache[symbol] = {"result": result, "fetched_at": now}
    return result


# ============================================================
# Economic Calendar
# ============================================================

def fetch_economic_calendar(days_ahead: int = 1) -> list[dict]:
    """
    Wirtschaftskalender fuer heute + `days_ahead` Tage.
    Liefert Liste von dicts {name, description, impact, time, country}
    Impact wird auf low/medium/high gemappt (Finnhub: 1..3 bzw. low/medium/high).
    """
    now = time.time()
    if now - _calendar_cache["fetched_at"] < _CALENDAR_CACHE_TTL:
        return _calendar_cache["events"]

    today = datetime.utcnow().date()
    end = today + timedelta(days=max(0, days_ahead))
    data = _get("/calendar/economic", {
        "from": today.isoformat(),
        "to": end.isoformat(),
    })

    events: list[dict] = []
    if isinstance(data, dict):
        raw_list = data.get("economicCalendar") or []
        if isinstance(raw_list, list):
            for ev in raw_list:
                if not isinstance(ev, dict):
                    continue
                name = ev.get("event") or ""
                country = ev.get("country") or ""
                # Finnhub: "high"/"medium"/"low" oder numerisch
                impact_raw = ev.get("impact") or ""
                if isinstance(impact_raw, (int, float)):
                    impact = "high" if impact_raw >= 3 else ("medium" if impact_raw >= 2 else "low")
                else:
                    impact = str(impact_raw).lower() or "low"
                if impact not in ("low", "medium", "high"):
                    impact = "low"
                time_str = ev.get("time") or ""
                events.append({
                    "name": name,
                    "description": f"{country}: {name}".strip(": "),
                    "impact": impact,
                    "time": time_str,
                    "country": country,
                    "source": "finnhub",
                })

    _calendar_cache["events"] = events
    _calendar_cache["fetched_at"] = now
    return events


# ============================================================
# Insider Transactions (SEC Form 4 via Finnhub)
# ============================================================
# Im Free-Tier verfuegbar (verifiziert 2026-04-26: 735 NVDA-Records).
# Liefert das Roh-CEOWatcher-Aequivalent gratis.

_insider_cache: dict[str, dict[str, Any]] = {}
_INSIDER_CACHE_TTL = 6 * 60 * 60  # 6h


def fetch_insider_transactions(symbol: str) -> list[dict]:
    """
    Liefert die letzten Insider-Transaktionen (SEC Form 4 Filings) als Liste.

    Jede Transaction: {name, transactionDate, share (post-trade-holdings),
                       change (signed: +Buy / -Sell), transactionPrice, currency}
    Cache 6h pro Symbol — Form 4 Filings sind taeglich, schnellere Refreshes
    bringen nichts und brennen API-Quota.
    """
    if not symbol:
        return []
    now = time.time()
    cached = _insider_cache.get(symbol)
    if cached and now - cached["fetched_at"] < _INSIDER_CACHE_TTL:
        return cached["transactions"]

    data = _get("/stock/insider-transactions", {"symbol": symbol})
    transactions: list[dict] = []
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        for tx in data["data"]:
            if not isinstance(tx, dict):
                continue
            transactions.append({
                "name": tx.get("name", ""),
                "transactionDate": tx.get("transactionDate", ""),
                "filingDate": tx.get("filingDate", ""),
                "change": tx.get("change", 0),
                "share": tx.get("share", 0),
                "transactionPrice": tx.get("transactionPrice", 0.0),
                "transactionCode": tx.get("transactionCode", ""),
                "currency": tx.get("currency", "USD"),
            })

    _insider_cache[symbol] = {"fetched_at": now, "transactions": transactions}
    return transactions
