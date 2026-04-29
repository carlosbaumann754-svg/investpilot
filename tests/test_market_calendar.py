"""Tests fuer den Holiday-Calendar (v37j W3 Cutover-Hard-Gate)."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from app.market_calendar import (
    is_market_holiday,
    is_market_holiday_at,
    upcoming_holidays,
    all_holidays,
    US_MARKET_HOLIDAYS,
)
from app.asset_classes import is_asset_class_tradeable


# ============================================================
# WURZEL-CHECK: Memorial Day 25.05.2026 (3 Tage vor Cutover!)
# ============================================================

def test_memorial_day_2026_is_holiday():
    """Memorial Day 2026 = Mo 25.05. — der Cutover-kritischste Holiday."""
    assert is_market_holiday(date(2026, 5, 25)) is True


def test_memorial_day_2026_blocks_us_stocks():
    """is_asset_class_tradeable('stocks') muss am Memorial Day False zurueckgeben.

    Pruefung um 14:00 UTC = 10:00 EDT (waere normalerweise mitten in RTH).
    Trotzdem False weil Holiday.
    """
    # 14:00 UTC am 25.05.2026 = 10:00 EDT (Memorial Day)
    memorial_day_rth = datetime(2026, 5, 25, 14, 0, tzinfo=timezone.utc)
    assert is_asset_class_tradeable("stocks", now_utc=memorial_day_rth) is False
    assert is_asset_class_tradeable("etf", now_utc=memorial_day_rth) is False


def test_day_before_memorial_day_2026_is_normal():
    """Friday 22.05.2026 = normaler Handelstag, RTH muss aktiv sein."""
    # 14:00 UTC am 22.05.2026 = 10:00 EDT (Fri)
    friday_before = datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc)
    assert is_market_holiday(date(2026, 5, 22)) is False
    assert is_asset_class_tradeable("stocks", now_utc=friday_before) is True


def test_day_after_memorial_day_2026_is_normal():
    """Tuesday 26.05.2026 = normaler Handelstag (Cutover-Tag wuerde der 28.05. sein!)."""
    tuesday_after = datetime(2026, 5, 26, 14, 0, tzinfo=timezone.utc)
    assert is_market_holiday(date(2026, 5, 26)) is False
    assert is_asset_class_tradeable("stocks", now_utc=tuesday_after) is True


# ============================================================
# Cutover-Tag selbst (28.05.2026 = Donnerstag)
# ============================================================

def test_cutover_day_2026_is_normal_trading():
    """Real-Money Cutover = Do 28.05.2026 — muss normaler Handelstag sein."""
    cutover = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)  # 10:00 EDT
    assert is_market_holiday(date(2026, 5, 28)) is False
    assert is_asset_class_tradeable("stocks", now_utc=cutover) is True


# ============================================================
# Alle 10 NYSE-Holidays 2026
# ============================================================

@pytest.mark.parametrize("d", [
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day observed (4 Jul = Sa)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
])
def test_all_2026_holidays_recognized(d):
    assert is_market_holiday(d) is True


def test_2026_holiday_count():
    """2026 hat genau 10 Full-Day-Closures."""
    holidays_2026 = [d for d in US_MARKET_HOLIDAYS if d.year == 2026]
    assert len(holidays_2026) == 10


# ============================================================
# 2027 spotty checks
# ============================================================

def test_observance_rules_2027():
    """2027: Independence Day (4 Jul = Sun) -> Mon 5 Jul observed.
       Christmas (25 Dec = Sat) -> Fri 24 Dec observed."""
    assert is_market_holiday(date(2027, 7, 5)) is True
    assert is_market_holiday(date(2027, 12, 24)) is True
    # Original days NOT in calendar (since they fall on weekend)
    assert is_market_holiday(date(2027, 7, 4)) is False  # Sunday
    assert is_market_holiday(date(2027, 12, 25)) is False  # Saturday


# ============================================================
# Region + TZ-Edge-Cases
# ============================================================

def test_unknown_region_returns_false():
    assert is_market_holiday(date(2026, 5, 25), region="JP") is False


def test_holiday_at_respects_local_tz():
    """Sonntag 22:00 UTC = Sonntag 18:00 EDT — noch NICHT MLK-Day-Montag in NY.
    Holiday-Detection darf nicht naiv UTC-date verwenden."""
    sun_evening = datetime(2026, 1, 18, 22, 0, tzinfo=timezone.utc)  # Sun
    # Lokal in NY: Sonntag 17:00 EST -> nicht MLK-Day
    assert is_market_holiday_at(sun_evening) is False


# ============================================================
# Helpers
# ============================================================

def test_upcoming_holidays_from_today():
    """upcoming_holidays liefert sortierte zukuenftige Daten."""
    res = upcoming_holidays(start=date(2026, 5, 1), n=3)
    assert res == [date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3)]


def test_all_holidays_sorted():
    seq = list(all_holidays())
    assert seq == sorted(seq)


# ============================================================
# Crypto: 24/7, KEINE Holiday-Blockierung
# ============================================================

def test_crypto_tradeable_on_memorial_day():
    """Crypto soll AUCH am Memorial Day tradeable sein (hat keine NYSE-TZ)."""
    memorial_day = datetime(2026, 5, 25, 14, 0, tzinfo=timezone.utc)
    # Crypto kann an IBKR-Wartung blockiert sein, aber nicht durch Memorial Day.
    # 14 UTC am Mo ist nicht IBKR-Wartung -> tradeable
    assert is_asset_class_tradeable("crypto", now_utc=memorial_day) is True
