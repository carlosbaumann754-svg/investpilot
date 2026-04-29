"""
Market-Calendar (W3 Cutover-Hard-Gate, vorgezogen 29.04.2026)
==============================================================

Liefert die Boersen-Feiertagsliste (full-day closures) fuer NYSE/NASDAQ
2026-2028. Wird von ``app.asset_classes.TradingSession`` konsumiert,
damit ``is_asset_class_tradeable()`` an Holidays korrekt False zurueckgibt
und der Off-Hours-Guard im Trader greift.

Warum nicht pandas_market_calendars?
-----------------------------------
Hardcoded ist robuster (kein Network, keine extra Dependency, kein
Schema-Drift) und Boersen-Holidays aendern sich quasi nie. Die Quellen
fuer 2026-2028 stammen direkt von der NYSE-Website (Stand April 2026).

Wann muss diese Datei erweitert werden?
---------------------------------------
- Spaetestens Q4/2027: 2029-Liste hinzufuegen.
- Bei einer Sonder-Schliessung (Hurricane, Tod eines Praesidenten, etc.)
  sofort den entsprechenden Datum-Eintrag mit Kommentar ergaenzen.

Half-Days (Day after Thanksgiving, Christmas Eve etc.) werden bewusst
NICHT abgebildet — der Bot trade dort einfach 6.5h statt 6.5h, kein
relevanter Edge-Case fuer Off-Hours-Spam.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, Optional

# ============================================================
# US/NYSE FULL-DAY CLOSURES
# ============================================================

US_MARKET_HOLIDAYS: frozenset[date] = frozenset({
    # ---- 2026 ----
    date(2026, 1, 1),    # New Year's Day (Thu)
    date(2026, 1, 19),   # Martin Luther King Jr. Day (Mon, 3rd Mon Jan)
    date(2026, 2, 16),   # Presidents Day (Mon, 3rd Mon Feb)
    date(2026, 4, 3),    # Good Friday (Easter 2026 = 5 Apr)
    date(2026, 5, 25),   # Memorial Day (Mon, last Mon May) -- 3 Tage VOR Cutover 28.05.!
    date(2026, 6, 19),   # Juneteenth (Fri)
    date(2026, 7, 3),    # Independence Day OBSERVED (Fri, weil 4 Jul = Sa)
    date(2026, 9, 7),    # Labor Day (Mon, 1st Mon Sep)
    date(2026, 11, 26),  # Thanksgiving (Thu, 4th Thu Nov)
    date(2026, 12, 25),  # Christmas (Fri)

    # ---- 2027 ----
    date(2027, 1, 1),    # New Year's Day (Fri)
    date(2027, 1, 18),   # MLK Day (Mon)
    date(2027, 2, 15),   # Presidents Day (Mon)
    date(2027, 3, 26),   # Good Friday (Easter 2027 = 28 Mar)
    date(2027, 5, 31),   # Memorial Day (Mon)
    date(2027, 6, 18),   # Juneteenth OBSERVED (Fri, weil 19 Jun = Sa)
    date(2027, 7, 5),    # Independence Day OBSERVED (Mon, weil 4 Jul = So)
    date(2027, 9, 6),    # Labor Day (Mon)
    date(2027, 11, 25),  # Thanksgiving (Thu)
    date(2027, 12, 24),  # Christmas OBSERVED (Fri, weil 25 Dec = Sa)

    # ---- 2028 ----
    # 1 Jan 2028 ist Samstag -> NYSE-Policy: NUR fuer New Year's Day KEINE
    # Friday-Observance (vermeidet Vorjahres-Gap). Kein Eintrag.
    date(2028, 1, 17),   # MLK Day (Mon)
    date(2028, 2, 21),   # Presidents Day (Mon)
    date(2028, 4, 14),   # Good Friday (Easter 2028 = 16 Apr)
    date(2028, 5, 29),   # Memorial Day (Mon)
    date(2028, 6, 19),   # Juneteenth (Mon)
    date(2028, 7, 4),    # Independence Day (Tue)
    date(2028, 9, 4),    # Labor Day (Mon)
    date(2028, 11, 23),  # Thanksgiving (Thu)
    date(2028, 12, 25),  # Christmas (Mon)
})


# ============================================================
# REGION-DISPATCH (fuer spaetere Erweiterung EU/UK/CH)
# ============================================================

_HOLIDAYS_BY_REGION: dict[str, frozenset[date]] = {
    "US": US_MARKET_HOLIDAYS,
    # Spaeter: "EU": EU_MARKET_HOLIDAYS, "UK": UK_MARKET_HOLIDAYS, ...
}


# ============================================================
# PUBLIC API
# ============================================================

def is_market_holiday(check_date: date, region: str = "US") -> bool:
    """True wenn ``check_date`` in der Region ein Full-Day-Closure ist."""
    holidays = _HOLIDAYS_BY_REGION.get(region.upper())
    if not holidays:
        return False
    return check_date in holidays


def is_market_holiday_at(now_utc: Optional[datetime] = None,
                         region: str = "US",
                         iana_timezone: str = "America/New_York") -> bool:
    """True wenn JETZT (in der angegebenen TZ) ein Holiday ist.

    Wir koppeln das Datum an die LOKALE Boersen-TZ — UTC-Date allein
    wuerde am Sonntag 22:00 UTC bereits Montag werfen, obwohl es in NY
    noch Sonntag-Abend ist.
    """
    from zoneinfo import ZoneInfo
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(ZoneInfo(iana_timezone))
    return is_market_holiday(local.date(), region=region)


def upcoming_holidays(start: Optional[date] = None,
                      region: str = "US",
                      n: int = 5) -> list[date]:
    """Naechsten n Holidays ab ``start`` (heute wenn None). Sortiert."""
    if start is None:
        start = datetime.now(timezone.utc).date()
    holidays = _HOLIDAYS_BY_REGION.get(region.upper(), frozenset())
    future = sorted(d for d in holidays if d >= start)
    return future[:n]


def all_holidays(region: str = "US") -> Iterable[date]:
    """Alle bekannten Holidays fuer eine Region (sortiert)."""
    return sorted(_HOLIDAYS_BY_REGION.get(region.upper(), frozenset()))
