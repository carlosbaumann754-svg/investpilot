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


def _is_valid_price(value) -> bool:
    """Defensiv: yfinance kann nan / inf / negative liefern (delisted/halted).

    Code-Review-Fix #2 (Pre-Cutover-Sprint 10.05.2026): explizit gegen
    nan absichern weil fast_info bei delisted-Symbolen nan returnt
    statt None. Ohne diese Pruefung wuerde nan durch float() durchgehen
    und die Fallback-Chain ueberspringen.
    """
    import math
    if value is None:
        return False
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return False
        return v > 0
    except (ValueError, TypeError):
        return False


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
            if _is_valid_price(price):
                return float(price)
        except (KeyError, AttributeError, TypeError):
            pass
        # Fallback: history(period='1d')
        h = ticker.history(period="1d")
        if h is not None and len(h) > 0:
            close = h["Close"].iloc[-1]
            if _is_valid_price(close):
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


# ============================================================
# v37h Option-C (13.05.2026): Per-Asset-Class-Routing
# ============================================================
#
# Wo Option A (Fallback-Chain) jede Asset-Klasse durch die gleiche
# yfinance->AV->Polygon->Finnhub-Sequenz schickt, nutzt Option C die
# Staerken jeder Quelle spezifisch:
#
# - stocks/etf  -> yfinance + Polygon parallel CROSS-VALIDATION
#                  (Anomaly-Alert wenn Spread > 0.5%)
# - forex       -> Alpha Vantage primary (Forex-Spezialist, dann yfinance fallback)
# - crypto      -> Polygon primary (24/7 statt yfinance-WE-Probleme), yfinance fallback
# - commodities -> Alpha Vantage + Polygon dual-source (Futures-Daten-Staerke)
#
# Aktivierung: fetch_quote_for_asset_class(symbol, asset_class) — additiv
# zu fetch_quote_with_fallback (Option A bleibt fuer Legacy-Aufrufer).
#
# Soak-Test-Plan: Mi 13.-So 17.05. — bei 0 Anomaly-Alert-False-Positives
# in Paper-Phase => Bot's Pricing-Pfad migrieren von Option A auf Option C
# am Mo 19.05. nach Cutover-Hard-Gate-Check.


_ASSET_CLASS_ROUTES: dict[str, list[str]] = {
    "stocks":      ["yfinance", "polygon", "alpha_vantage", "finnhub"],
    "etf":         ["yfinance", "polygon", "alpha_vantage", "finnhub"],
    "forex":       ["alpha_vantage", "yfinance", "polygon"],
    "crypto":      ["polygon", "yfinance", "alpha_vantage"],
    "commodities": ["alpha_vantage", "polygon", "yfinance"],
    "unknown":     ["yfinance", "alpha_vantage", "polygon", "finnhub"],
}

# Cross-Validation-Asset-Classes: zwei parallele Quellen, Spread-Check.
_CROSS_VALIDATION_CLASSES = frozenset({"stocks", "etf"})

# Spread-Schwellwert fuer Anomaly-Alert (Prozent).
# v37h Option-C Soak-Test Day-1 (13.05.2026): erhoeht von 0.5% auf 5.0%.
# Begruendung: erster Live-Test zeigte AAPL 1.56% / SPY 0.65% Spreads —
# NICHT Bugs sondern erwartet (yfinance live vs Polygon /prev = Vortag-
# Close auf Free-Tier). 0.5% wuerde permanent waehrend RTH feuern.
# 5.0% = "Symbol-Verwechslung oder echter Daten-Bug"-Schwellwert. Bei
# Polygon Paid-Tier-Upgrade (Live-Quotes) koennte spaeter wieder auf
# 0.5-1.0% gesenkt werden — als Q3-Backlog dokumentiert.
_QUOTE_SPREAD_THRESHOLD_PCT = 5.0

# Pushover-Throttle: gleiche Symbol+Anomaly nicht oefter als 1x pro Stunde.
_ANOMALY_ALERT_LAST: dict[str, float] = {}
_ANOMALY_ALERT_INTERVAL_SEC = 3600


def _fetch_from_source(source: str, symbol: str) -> Optional[float]:
    """Dispatcher: einzelne Quelle aufrufen, valid price zurueck oder None."""
    try:
        if source == "yfinance":
            return _try_yfinance(symbol)
        if source == "alpha_vantage":
            return _try_alpha_vantage(symbol)
        if source == "polygon":
            return _try_polygon(symbol)
        if source == "finnhub":
            return _try_finnhub(symbol)
    except Exception as e:
        log.warning("Source %s failed for %s: %s", source, symbol, e)
    return None


def _check_quote_spread(price_a: float, source_a: str,
                       price_b: float, source_b: str,
                       symbol: str,
                       threshold_pct: float = _QUOTE_SPREAD_THRESHOLD_PCT) -> bool:
    """Pruefe Cross-Validation-Spread. True wenn ANOMALIE detectiert.

    Spread berechnet als prozentuale Abweichung vs Mittelwert. Bei
    Anomaly: log.warning + Pushover-Alert via send_alert (Throttle 1h
    pro Symbol).
    """
    if not (price_a and price_b and price_a > 0 and price_b > 0):
        return False
    avg = (price_a + price_b) / 2
    spread_pct = abs(price_a - price_b) / avg * 100
    if spread_pct <= threshold_pct:
        return False
    # ANOMALIE
    log.warning(
        "QUOTE-ANOMALY %s: %s=$%.4f vs %s=$%.4f (spread %.2f%% > threshold %.2f%%)",
        symbol, source_a, price_a, source_b, price_b, spread_pct, threshold_pct,
    )
    # Pushover-Alert mit Throttle
    import time as _t
    now = _t.time()
    last = _ANOMALY_ALERT_LAST.get(symbol, 0)
    if now - last >= _ANOMALY_ALERT_INTERVAL_SEC:
        _ANOMALY_ALERT_LAST[symbol] = now
        try:
            from app.alerts import send_alert
            msg = (f"⚠️ <b>QUOTE-SPREAD-ANOMALY</b>: {symbol}\n"
                   f"{source_a}: ${price_a:.4f}\n"
                   f"{source_b}: ${price_b:.4f}\n"
                   f"Spread: {spread_pct:.2f}% (Threshold {threshold_pct:.2f}%)\n"
                   f"Trade-Path sollte vorsichtshalber blocken bis Spread schliesst.")
            send_alert(msg, level="WARNING")
        except Exception as e:
            log.debug("send_alert (quote-anomaly) failed: %s", e)
    return True


def fetch_quote_for_asset_class(symbol: str, asset_class: Optional[str] = None,
                                cross_validate: bool = True) -> Optional[float]:
    """v37h Option-C (13.05.2026): Per-Asset-Class-Routing fuer Quotes.

    Args:
        symbol: Ticker (z.B. 'AAPL', 'EUR=X', 'BTC-USD').
        asset_class: 'stocks' / 'etf' / 'forex' / 'crypto' / 'commodities'.
                     None oder 'unknown' -> Generic Fallback-Chain.
        cross_validate: bei stocks/etf eine zweite Quelle parallel
                        aufrufen + Anomaly-Spread-Check (Default True).

    Returns:
        Float-Preis oder None wenn alle Quellen failed.

    Aktivierung-Strategie: additiv zu fetch_quote_with_fallback (Option
    A bleibt fuer Legacy-Aufrufer). Bot-Code muss EXPLIZIT auf
    fetch_quote_for_asset_class umschalten — daher Soak-Test-Phase
    13.-19.05. ohne Live-Trade-Impact.
    """
    if not symbol:
        return None

    asset_class = (asset_class or "unknown").lower()
    route = _ASSET_CLASS_ROUTES.get(asset_class, _ASSET_CLASS_ROUTES["unknown"])

    # Cross-Validation-Modus fuer stocks/etf
    if cross_validate and asset_class in _CROSS_VALIDATION_CLASSES and len(route) >= 2:
        primary_src = route[0]
        secondary_src = route[1]
        primary_price = _fetch_from_source(primary_src, symbol)
        secondary_price = _fetch_from_source(secondary_src, symbol)
        if _is_valid_price(primary_price) and _is_valid_price(secondary_price):
            # Beide Quellen liefern -> Cross-Validation-Check
            _check_quote_spread(
                primary_price, primary_src,
                secondary_price, secondary_src,
                symbol,
            )
            # Primary nutzen (yfinance) auch bei Anomalie — Alert ist
            # Sichtbarkeit, Block-Entscheidung im Trade-Path (separater Layer).
            return float(primary_price)
        # Eine Quelle leer -> auf die andere zurueckfallen
        if _is_valid_price(primary_price):
            return float(primary_price)
        if _is_valid_price(secondary_price):
            log.info("Cross-Val %s: primary %s leer, nutze secondary %s",
                     symbol, primary_src, secondary_src)
            return float(secondary_price)
        # Beide leer -> tiefer in die Chain
        remaining = route[2:]
    else:
        remaining = route

    # Sequential Fallback durch verbleibende Quellen
    for src in remaining:
        price = _fetch_from_source(src, symbol)
        if _is_valid_price(price):
            return float(price)

    log.error("fetch_quote_for_asset_class %s (class=%s): ALLE Quellen failed",
              symbol, asset_class)
    return None
