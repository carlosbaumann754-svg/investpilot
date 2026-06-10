"""Preis-Provider — einheitliche Schnittstelle mit Quellen-Kette (v37dm, 10.06.2026).

Entkoppelt den Scanner von EINER einzelnen Preisquelle. Liefert pro Symbol
``(price_now, price_ref_21d)`` — derselbe Vertrag wie zuvor
``signal_stack_runner.fetch_recent_prices`` (drop-in).

ARCHITEKTUR (Quellen-Kette, Stufe 1)
====================================
  1. yfinance (Bulk-Download, schnell)        <- PRIMAER
  2. IBKR     (reqHistoricalData, read-only)  <- FALLBACK fuer verfehlte Symbole

Warum diese Schnittstelle der saubere Weg ist: WELCHE Quelle primaer ist, ist
nur noch eine Reihenfolge-Entscheidung in EINER Funktion. Stufe 2 (nach dem
Soak, wenn IBKR-Batching+Cache getestet sind) flippt die Prioritaet auf
IBKR-primaer — OHNE Umbau, nur die Reihenfolge unten drehen.

EIGENSCHAFTEN
=============
- **Success-Path unveraendert**: laeuft yfinance normal, ist das Ergebnis
  identisch zu vorher (Soak-Experiment unberuehrt). Der IBKR-Fallback feuert
  NUR fuer Symbole, die yfinance verfehlt.
- **Kein Single-Point-of-Failure mehr**: ein yfinance-Ausfall kann den
  Live-Handel (Selektion) nicht mehr blockieren — IBKR (Ausfuehrungs-Quelle)
  springt ein.
- **Bounded**: der IBKR-Fallback ist auf ``_IBKR_FALLBACK_CAP`` Symbole
  gedeckelt (Pacing-Schutz). Was darueber liegt, wird GELOGGT (kein stilles
  Abschneiden) — Stufe 2 mit Cache+Batching hebt den Cap.
- **Non-fatal**: jeder Fehler -> die betroffenen Symbole fehlen einfach im
  Ergebnis (wie das alte Verhalten), nie eine Exception nach oben.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

AUDIT_METADATA = {
    "purpose": "Einheitlicher Preis-Provider mit Quellen-Kette (yfinance primaer + IBKR-Fallback) — entkoppelt den Scanner von einer einzelnen Preisquelle, damit ein yfinance-Ausfall den Live-Handel nicht blockiert",
    "config_section": "signal_stack",
    "state_files": [],
    "self_tests": ["tc_price_provider"],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "v37dm (yfinance-Entkopplung Stufe 1 — Scanner-Preise)",
}

_REF_OFFSET = 21          # Handelstage fuer Reversal (~1 Monat)
_IBKR_FALLBACK_CLIENT_ID = 88   # eigene clientId (Bot=1, Reconcile=99) — kein Konflikt
_IBKR_FALLBACK_CAP = 80   # max Symbole pro Scan ueber den IBKR-Fallback (Pacing-Schutz)


def _extract_now_ref(closes: list, ref_offset: int = _REF_OFFSET) -> tuple:
    """(letzter Close, Close ~ref_offset Handelstage zuvor). (None,None) wenn zu kurz."""
    if not closes or len(closes) < ref_offset + 1:
        return None, None
    return closes[-1], closes[-1 - ref_offset]


def _fetch_via_yfinance(symbols: list, lookback_days: int) -> dict:
    """{symbol: (now, ref_21d)} via yfinance-Bulk. {} wenn yfinance fehlt/leer."""
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance nicht verfuegbar — Primaerquelle leer")
        return {}
    try:
        data = yf.download(symbols, period="%dd" % lookback_days, interval="1d",
                           group_by="ticker", auto_adjust=True, threads=True,
                           progress=False)
    except Exception as e:
        log.warning("yfinance-Bulk-Download fehlgeschlagen: %s", e)
        return {}
    out = {}
    multi = len(symbols) > 1
    for s in symbols:
        try:
            df = data[s] if multi else data
            closes = df["Close"].dropna().values.tolist()
        except Exception:
            continue
        pn, pr = _extract_now_ref(closes)
        if pn is not None and pr is not None:
            out[s] = (pn, pr)
    return out


def _fetch_via_ibkr(symbols: list, lookback_days: int) -> dict:
    """{symbol: (now, ref_21d)} via IBKR reqHistoricalData (eigene clientId).

    Read-only-Verbindung, sequentiell (Pacing). Gedeckelt auf _IBKR_FALLBACK_CAP.
    Non-fatal: jeder Fehler -> Symbol fehlt im Ergebnis.
    """
    if not symbols:
        return {}
    capped = symbols[:_IBKR_FALLBACK_CAP]
    if len(symbols) > _IBKR_FALLBACK_CAP:
        log.warning("IBKR-Fallback: %d Symbole angefragt, aber Cap=%d — %d NICHT "
                    "nachgeholt (Pacing-Schutz; Stufe-2-Cache hebt das): %s",
                    len(symbols), _IBKR_FALLBACK_CAP, len(symbols) - _IBKR_FALLBACK_CAP,
                    symbols[_IBKR_FALLBACK_CAP:_IBKR_FALLBACK_CAP + 10])
    out = {}
    broker = None
    try:
        from app.ibkr_client import IbkrBroker
        broker = IbkrBroker({"ibkr": {"client_id": _IBKR_FALLBACK_CLIENT_ID,
                                      "readonly": True, "timeout": 60}})
        for sym in capped:
            closes = broker.get_recent_daily_closes(sym, lookback_days)
            pn, pr = _extract_now_ref(closes)
            if pn is not None and pr is not None:
                out[sym] = (pn, pr)
    except Exception as e:
        log.warning("IBKR-Fallback insgesamt fehlgeschlagen (non-fatal): %s", e)
    finally:
        if broker is not None:
            try:
                broker.disconnect()
            except Exception:
                pass
    return out


def fetch_recent_prices(symbols: list, lookback_days: int = 45,
                        allow_ibkr_fallback: bool = True) -> dict:
    """{symbol: (price_now, price_ref_21d)} — yfinance primaer, IBKR-Fallback.

    Drop-in-Ersatz fuer das alte signal_stack_runner.fetch_recent_prices.

    Args:
        symbols: Liste Ticker.
        lookback_days: Kalendertage Historie (>=~31 fuer den 21-Handelstag-Ref).
        allow_ibkr_fallback: False -> nur yfinance (z.B. Tests / Offline).
    """
    prices = _fetch_via_yfinance(symbols, lookback_days)
    missing = [s for s in symbols if s not in prices]
    if missing and allow_ibkr_fallback:
        log.warning("yfinance verfehlte %d/%d Symbole -> IBKR-Fallback (clientId %d)",
                    len(missing), len(symbols), _IBKR_FALLBACK_CLIENT_ID)
        ibkr_prices = _fetch_via_ibkr(missing, lookback_days)
        if ibkr_prices:
            prices.update(ibkr_prices)
            log.info("IBKR-Fallback lieferte %d/%d verfehlte Symbole nach",
                     len(ibkr_prices), len(missing))
        else:
            log.warning("IBKR-Fallback lieferte 0 Symbole nach (IBG down / Pacing?)")
    return prices
