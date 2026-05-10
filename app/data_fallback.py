"""
Data-Fallback-Layer — InvestPilot v37h+ (10.05.2026).

Robustifiziert Quote-Fetching gegen yfinance-Aussetzer (typisch:
Wochenende fuer Futures, Pre-Market-Hangs, Symbol-Drift).

ARCHITEKTUR (Option A — Fallback-Chain)
=======================================
Primary:    yfinance (yf.Ticker(symbol).history bzw. .fast_info)
Fallback 1: Alpha Vantage (Stocks + Forex)
Fallback 2: Polygon.io (Stocks + Crypto)
Fallback 3: Finnhub (Stocks via /quote endpoint)

Wichtige Eigenschaften:
- **Success-Path unveraendert**: yfinance funktioniert wie heute. Fallbacks
  nur bei yfinance-Fehler/Empty.
- **Backwards-compat**: fetch_quote_with_fallback(symbol) ist drop-in fuer
  yf.Ticker(symbol).fast_info["lastPrice"].
- **Conservative**: bei allen 4 Sources Fehler -> None (Caller entscheidet).
- **Logging**: jeder Fallback-Trigger loggt warn (Visibility welche Quelle
  wirklich performt).

POST-CUTOVER OPTION C (geplant 14.-17.05.2026)
=============================================
Per-Asset-Class-Routing wo jede Quelle ihre Staerke ausspielt:
  - stocks/etf -> yfinance + Polygon parallel cross-validation
  - forex      -> Alpha Vantage primary
  - crypto     -> Polygon (24/7) primary
  - commodities-> AV + Polygon (Futures-Staerke)
Wird als separate Funktion eingebaut, ohne diese Fallback-Chain zu
ersetzen. Caller entscheiden was sie wollen.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("DataFallback")


def _try_yfinance(symbol: str) -> Optional[float]:
    """yfinance-Quote (primary path)."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        # fast_info ist instant, history() ist robuster aber langsamer.
        try:
            info = ticker.fast_info
            price = info.get("lastPrice") if hasattr(info, "get") else None
            if price is None:
                # fast_info wirft KeyError statt None — defensiv
                price = info["lastPrice"]
            if price and price > 0:
                return float(price)
        except (KeyError, AttributeError, TypeError):
            pass
        # Fallback: history(period='1d')
        h = ticker.history(period="1d")
        if h is not None and len(h) > 0:
            close = h["Close"].iloc[-1]
            if close and close > 0:
                return float(close)
    except Exception as e:
        log.debug("yfinance quote failed for %s: %s", symbol, e)
    return None


def _try_alpha_vantage(symbol: str) -> Optional[float]:
    try:
        from app import alpha_vantage_client as av
        if av.is_available():
            return av.fetch_quote(symbol)
    except Exception as e:
        log.debug("alpha_vantage quote failed for %s: %s", symbol, e)
    return None


def _try_polygon(symbol: str) -> Optional[float]:
    try:
        from app import polygon_client as pg
        if pg.is_available():
            return pg.fetch_quote(symbol)
    except Exception as e:
        log.debug("polygon quote failed for %s: %s", symbol, e)
    return None


def _try_finnhub(symbol: str) -> Optional[float]:
    try:
        from app import finnhub_client as fh
        if fh.is_available():
            # Finnhub /quote endpoint: c = current, o = open, h = high, l = low
            quote = fh._get("/quote", {"symbol": symbol}) if hasattr(fh, "_get") else None
            if quote and quote.get("c"):
                return float(quote["c"])
    except Exception as e:
        log.debug("finnhub quote failed for %s: %s", symbol, e)
    return None


def fetch_quote_with_fallback(symbol: str) -> Optional[float]:
    """Quote-Fetcher mit 4-stufigem Fallback (yfinance > AV > Polygon > Finnhub).

    Args:
        symbol: Ticker (yfinance-Format, z.B. 'AAPL', 'BTC-USD', 'EURUSD=X')

    Returns:
        Float-Last-Price oder None wenn ALLE Quellen failen.

    Logging-Verhalten:
        - Success bei yfinance: silent (success-path)
        - Fallback triggert: log.warning mit erfolgreich gewordener Quelle
        - All-Fail: log.error (echte Anomalie)

    PERFORMANCE
    Im Normalfall ~50-200 ms (yfinance fast_info). Bei Fallback je nach
    Quelle bis 2-5s zusaetzlich. Caller sollte timeout-aware sein.
    """
    if not symbol:
        return None

    # Layer 1: yfinance (Standard-Pfad)
    price = _try_yfinance(symbol)
    if price is not None and price > 0:
        return price

    # Layer 2: Alpha Vantage
    price = _try_alpha_vantage(symbol)
    if price is not None and price > 0:
        log.warning("Quote-Fallback aktiv fuer %s: Alpha Vantage geliefert", symbol)
        return price

    # Layer 3: Polygon
    price = _try_polygon(symbol)
    if price is not None and price > 0:
        log.warning("Quote-Fallback aktiv fuer %s: Polygon geliefert", symbol)
        return price

    # Layer 4: Finnhub
    price = _try_finnhub(symbol)
    if price is not None and price > 0:
        log.warning("Quote-Fallback aktiv fuer %s: Finnhub geliefert", symbol)
        return price

    # Alle 4 Quellen failed
    log.error(
        "Quote-Fallback ALLE Quellen failed fuer %s — yfinance + AV + "
        "Polygon + Finnhub liefern keinen Preis. Echte Anomalie.", symbol,
    )
    return None


def get_active_sources() -> dict[str, bool]:
    """Diagnose: welche Daten-Quellen sind konfiguriert?

    Wird vom /api/news-sources Endpoint genutzt fuer Dashboard-Status.
    """
    sources = {"yfinance": False, "alpha_vantage": False,
               "polygon": False, "finnhub": False}
    try:
        import yfinance as _yf
        sources["yfinance"] = True
    except ImportError:
        pass
    try:
        from app import alpha_vantage_client as av
        sources["alpha_vantage"] = av.is_available()
    except Exception:
        pass
    try:
        from app import polygon_client as pg
        sources["polygon"] = pg.is_available()
    except Exception:
        pass
    try:
        from app import finnhub_client as fh
        sources["finnhub"] = fh.is_available()
    except Exception:
        pass
    return sources
