"""v37h Pre-Cutover (10.05.2026) — Weekend-Filter fuer yfinance-ERROR-Noise.

PROBLEM
yfinance loggt ERROR fuer Symbole ohne Daten am angefragten Tag:
  "$HG=F: possibly delisted; no price data found (period=1d)"

Am Sonntag/Samstag haben Commodity-Futures (HG=F, GC=F, SI=F, NG=F) und
einige Forex-Paare keine 'period=1d'-Daten weil die Maerkte zu sind. Bot
fragt aber trotzdem an (Scanner-Cycles, MTF-Confluence, Cost-Calibrator,
ML-Training).

Folge: am Wochenende kommen Dutzende ERROR-Logs die KEINE echten
Anomalien sind. Klassischer Cry-Wolf-Effekt — wenn Mo wirklich ein
Symbol delistet wird, geht der Alert in der Sonntag-Flut unter.

LOESUNG
Logging-Filter der ERROR-Messages mit 'possibly delisted' /
'no price data found' am Wochenende auf DEBUG runterdrueckt. Werktags:
unveraendert (echte Delistings sollen dann sichtbar bleiben).

Eintrittspunkt: install_yfinance_weekend_filter() wird beim Bot-Boot
aufgerufen (z.B. aus scheduler.py oder app/__init__.py).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone


# Patterns die typische Wochenend-yfinance-Errors matchen.
# Conservative: nur diese exakten Sub-Strings unterdruecken, sonst passiert
# auch echtes Delisting durch.
_WEEKEND_NOISE_PATTERNS = [
    re.compile(r"possibly delisted", re.IGNORECASE),
    re.compile(r"no price data found", re.IGNORECASE),
]


def _is_weekend_utc(now_utc: datetime | None = None) -> bool:
    """True wenn Samstag oder Sonntag UTC.

    Args:
        now_utc: Optional injectable datetime fuer Tests.
    """
    now = now_utc or datetime.now(timezone.utc)
    return now.weekday() in (5, 6)  # Mon=0 ... Sat=5, Sun=6


class YFinanceWeekendNoiseFilter(logging.Filter):
    """Filtert "possibly delisted"-ERRORs am Wochenende auf DEBUG-Level.

    Pattern: yfinance-Logger emittiert WARNING/ERROR mit dem Text. Filter
    dropt diese am Wochenende komplett (return False = unterdruecken).
    Werktags pass-through (return True).

    Bewusst NICHT level-rewrite (record.levelno = DEBUG) weil das andere
    Handler verwirren koennte. Kompletter Drop ist sauberer.
    """

    def __init__(self, weekend_check=_is_weekend_utc):
        super().__init__()
        self._is_weekend = weekend_check

    def filter(self, record: logging.LogRecord) -> bool:
        # Werktag: pass through
        if not self._is_weekend():
            return True
        # Wochenende: nur Messages die unsere Pattern matchen droppen
        try:
            msg = record.getMessage()
        except Exception:
            return True  # Defensive: pass through bei Format-Errors
        for pat in _WEEKEND_NOISE_PATTERNS:
            if pat.search(msg):
                return False  # drop
        return True  # andere yfinance-Errors weiter zeigen


_FILTER_INSTALLED = False


def install_yfinance_weekend_filter() -> bool:
    """Installiere den Filter idempotent auf den yfinance-Logger.

    Returns:
        True bei Erst-Install, False wenn schon installiert.
    """
    global _FILTER_INSTALLED
    if _FILTER_INSTALLED:
        return False

    yf_logger = logging.getLogger("yfinance")
    # Plus optional auf root logger weil yfinance >= 0.2.50 manchmal direkt
    # ueber root loggt. Mehrere Loggers abdecken ist defensiv.
    target_loggers = [
        yf_logger,
        logging.getLogger("yfinance.utils"),
        logging.getLogger("yfinance.data"),
    ]
    flt = YFinanceWeekendNoiseFilter()
    for lg in target_loggers:
        lg.addFilter(flt)
    _FILTER_INSTALLED = True
    return True


def _reset_for_tests() -> None:
    """Test-Hook — entfernt Filter + reset Flag fuer fresh-install in tests."""
    global _FILTER_INSTALLED
    for name in ("yfinance", "yfinance.utils", "yfinance.data"):
        lg = logging.getLogger(name)
        lg.filters = [f for f in lg.filters
                      if not isinstance(f, YFinanceWeekendNoiseFilter)]
    _FILTER_INSTALLED = False
