"""v37h Tab-Audit-Day-2 (12.05.2026) — Tests fuer last_market_close_utc().

Hintergrund (Carlos 12.05.2026 Mittag): Bot zeigte HEUTE +CHF 445, IBKR
sagte -CHF 5'553. Diff 6k weil Bot's "HEUTE" rolling-24h berechnete
statt seit Marktschluss. Diese Tests fixieren die Edge-Cases damit
der calendar-based Fix nicht in 2 Wochen wieder driftet.

Tests:
  1. Werktag 14:00 UTC vor Marktschluss -> gestern 20:00 UTC
  2. Werktag 22:00 UTC nach Marktschluss -> heute 20:00 UTC
  3. Samstag Mittag -> Freitag 20:00 UTC
  4. Sonntag Mittag -> Freitag 20:00 UTC
  5. Memorial Day Di Mittag -> Freitag 20:00 UTC (Mo = Holiday)
  6. Thanksgiving Fr Mittag -> Mi 20:00 UTC (Do = Holiday)
  7. Winter (Januar) -> 21:00 UTC statt 20:00 UTC (DST-Wechsel)
  8. Christmas-Tuesday-Holiday-mit-Wochenende-davor: 26.12.2027 (So)
"""
from datetime import datetime, timezone

import pytest

from app.market_calendar import last_market_close_utc


def _at(year, month, day, hour, minute=0):
    """UTC-aware datetime helper."""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ============================================================
# Standard-Werktag
# ============================================================

def test_weekday_before_close_returns_yesterday():
    """Di 12.05.2026 14:00 UTC = 10:00 ET -> Markt noch nicht zu -> Mo 20:00 UTC."""
    now = _at(2026, 5, 12, 14, 0)
    result = last_market_close_utc(now_utc=now)
    # Mo 11.05.2026 16:00 ET = 20:00 UTC (Sommer)
    expected = datetime(2026, 5, 11, 20, 0)
    assert result == expected, f"Erwartet {expected}, bekam {result}"


def test_weekday_after_close_returns_today():
    """Di 12.05.2026 22:00 UTC = 18:00 ET -> Markt heute zu -> heute 20:00 UTC."""
    now = _at(2026, 5, 12, 22, 0)
    result = last_market_close_utc(now_utc=now)
    expected = datetime(2026, 5, 12, 20, 0)
    assert result == expected


def test_weekday_exactly_at_close():
    """Di 16:00 ET exakt = 20:00 UTC -> heute (>= close gilt als zu)."""
    now = _at(2026, 5, 12, 20, 0)
    result = last_market_close_utc(now_utc=now)
    expected = datetime(2026, 5, 12, 20, 0)
    assert result == expected


# ============================================================
# Wochenende
# ============================================================

def test_saturday_returns_friday():
    """Sa 16.05.2026 Mittag -> Fr 15.05. 20:00 UTC."""
    now = _at(2026, 5, 16, 12, 0)
    result = last_market_close_utc(now_utc=now)
    expected = datetime(2026, 5, 15, 20, 0)
    assert result == expected


def test_sunday_returns_friday():
    """So 17.05.2026 Mittag -> Fr 15.05. 20:00 UTC."""
    now = _at(2026, 5, 17, 12, 0)
    result = last_market_close_utc(now_utc=now)
    expected = datetime(2026, 5, 15, 20, 0)
    assert result == expected


# ============================================================
# US-Holidays
# ============================================================

def test_memorial_day_tuesday_returns_previous_friday():
    """Memorial Day = Mo 25.05.2026 (Holiday). Di 26.05. -> Fr 22.05. 20:00 UTC."""
    now = _at(2026, 5, 26, 14, 0)  # Di nach Memorial Day
    result = last_market_close_utc(now_utc=now)
    expected = datetime(2026, 5, 22, 20, 0)  # letzter Trading-Tag vor MemDay
    assert result == expected


def test_day_after_thanksgiving_morning():
    """Thanksgiving = Do 26.11.2026 (Holiday). Fr 27.11.2026 morgens -> Mi 20:00 UTC."""
    now = _at(2026, 11, 27, 10, 0)  # Fr morgens nach Thanksgiving
    result = last_market_close_utc(now_utc=now)
    # NYSE schliesst Mi 25.11. 16:00 ET. Im November ist EST (UTC-5) -> 21:00 UTC
    expected = datetime(2026, 11, 25, 21, 0)
    assert result == expected


def test_christmas_in_holiday_period():
    """Christmas = Fr 25.12.2026 (Holiday). Sa 26.12. -> Do 24.12. 20:00 UTC?
    Aber 24.12.2026 = Donnerstag, kein Holiday in unserer Liste -> Do 21:00 UTC.
    (Half-day-trading wird ignoriert, wir behandeln nur Full-Day-Closures.)
    """
    now = _at(2026, 12, 26, 14, 0)  # Sa nach Christmas
    result = last_market_close_utc(now_utc=now)
    # 24.12.2026 = Do, kein Full-Day-Holiday in unserer Liste -> 21:00 UTC
    expected = datetime(2026, 12, 24, 21, 0)
    assert result == expected


# ============================================================
# DST (Daylight Saving Time)
# ============================================================

def test_winter_returns_2100_utc():
    """Januar = Winterzeit (EST = UTC-5). Marktschluss 16:00 ET = 21:00 UTC."""
    now = _at(2026, 1, 7, 14, 0)  # Mi 07.01.2026, vor Marktschluss
    result = last_market_close_utc(now_utc=now)
    # Di 06.01.2026 ist Trading-Tag, Winterzeit -> 21:00 UTC
    expected = datetime(2026, 1, 6, 21, 0)
    assert result == expected


def test_summer_returns_2000_utc():
    """Mai = Sommerzeit (EDT = UTC-4). Marktschluss 16:00 ET = 20:00 UTC."""
    now = _at(2026, 5, 6, 14, 0)  # Mi 06.05.2026, vor Marktschluss
    result = last_market_close_utc(now_utc=now)
    expected = datetime(2026, 5, 5, 20, 0)  # Sommerzeit, 20:00 UTC
    assert result == expected


def test_dst_transition_march_2026():
    """DST-Wechsel: 8.3.2026 So um 02:00 ET -> 03:00 EDT (Spring forward).
    Mo 9.3. 14:00 UTC -> Fr 6.3. 20:00 UTC (Sommerzeit ab Mi 11.3.? Nein, ab So 8.3.).
    Fr 6.3. war noch Winterzeit -> Fr 21:00 UTC. Mo 9.3. ist Sommerzeit aber
    wir suchen Fr's Schluss -> Fr 21:00 UTC.
    """
    now = _at(2026, 3, 9, 14, 0)  # Mo 09.03.2026 - kurz nach DST-Wechsel
    result = last_market_close_utc(now_utc=now)
    # Letzter Trading-Tag = Fr 06.03., damals noch Winterzeit -> 21:00 UTC
    expected = datetime(2026, 3, 6, 21, 0)
    assert result == expected


# ============================================================
# Robustheit
# ============================================================

def test_returns_naive_datetime():
    """Helper returnt naive UTC datetime (kompatibel mit Bot-Snapshots)."""
    now = _at(2026, 5, 12, 14, 0)
    result = last_market_close_utc(now_utc=now)
    assert result.tzinfo is None


def test_now_utc_defaults_to_actual_now(monkeypatch):
    """now_utc=None -> nutzt datetime.now(timezone.utc)."""
    result = last_market_close_utc(now_utc=None)
    # Sollte irgendein Wert in der Vergangenheit sein
    assert result < datetime.utcnow()
    # Und mind. 1 Tag zurueck (selbst direkt nach Marktschluss)
    assert (datetime.utcnow() - result).total_seconds() < 7 * 86400  # max 1 Woche
