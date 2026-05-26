"""Tests fuer R-A47 — yfinance_freshness Holiday-aware Refactor.

Bug-Anlass: Di 26.05.2026 10:14 CEST (Sprint-Tag-15) zeigte das Dashboard
Self-Test 15/16 mit yfinance_freshness FAIL: "SPY-Last-Bar vor 4.2d
(>4d Toleranz)". Diagnose: kein echter Bug, sondern False-Positive durch
Memorial Day Mo 25.05. + Wochenende. Letzter US-Trading-Day-Close war
Fr 22.05. -> 4.2d Lücke beim Di-Morgen, knapp über harter 4d-Schwelle.

Gleiche False-Positives waeren entstanden bei:
  - Independence Day Long-Weekend (~4d)
  - Labor Day Long-Weekend (~4d)
  - Thanksgiving Fr-Morgen (~4.5d)  ← SCHLIMMSTER FALL
  - Christmas/New-Year Holiday-Pause (~4d)

R-A47 Fix: statt harter 4d-Schwelle wird last_market_close_utc() aus
market_calendar.py genutzt (Holiday-aware), Toleranz 24h fuer yfinance-
Reporting-Lag.

Tests verifizieren:
  1. Memorial-Day-Morning-Szenario (war FAIL, jetzt OK)
  2. Thanksgiving-Friday-Szenario (worst-case 4.5d, war FAIL, jetzt OK)
  3. Echter Stale-Bug (>1 Trading-Day Lücke) wird WEITERHIN als FAIL erkannt
  4. Normal-Operation (Mi-Vormittag nach Di-Close) ist GREEN
"""

from datetime import datetime, timezone

from app.self_test import _yfinance_bar_age_acceptable


# ---------------------------------------------------------------------------
# False-Positive Szenarien — diese MUESSEN jetzt GREEN sein
# ---------------------------------------------------------------------------

def test_r_a47_memorial_day_morning_no_false_positive():
    """Di 26.05.2026 10:14 CEST (= 08:14 UTC) nach Memorial Day.
    Letzte SPY-Bar Fr 22.05.2026 20:00 UTC (16:00 ET Close).
    Lücke: 4.2 Tage Kalendertage, ABER nur ~12h hinter expected
    last_market_close (Fr 16:00 ET).
    Erwartung: OK (war FAIL bei harter 4d-Schwelle).
    """
    now = datetime(2026, 5, 26, 8, 14, tzinfo=timezone.utc)
    last_bar = datetime(2026, 5, 22, 20, 0, tzinfo=timezone.utc)
    ok, msg = _yfinance_bar_age_acceptable(last_bar, now_utc=now)
    assert ok, f"Memorial-Day-Morning sollte fresh sein, nicht stale: {msg}"
    assert "fresh" in msg.lower()


def test_r_a47_thanksgiving_friday_worst_case():
    """Fr 27.11.2026 09:00 CEST = 08:00 UTC, Thanksgiving war Do 26.11.
    Letzte Bar Mi 25.11.2026 21:00 UTC (16:00 ET Close).
    Lücke: ~1.5 Tage Kalendertage, ~13h hinter expected last_close.
    Erwartung: OK.
    """
    now = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)
    last_bar = datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc)
    ok, msg = _yfinance_bar_age_acceptable(last_bar, now_utc=now)
    assert ok, f"Thanksgiving-Fri-Morning sollte fresh sein: {msg}"


def test_r_a47_long_weekend_monday_after_thanksgiving():
    """Worst-Case-Szenario aus Carlos's Analyse: Mo 30.11.2026 09:00 CEST.
    Letzter Trading-Tag war Wed 25.11. (Thanksgiving Do, Fr verkuerzt).
    Lücke ~4.5 Kalendertage, aber Fr 27.11. war auch Trading-Day (verkuerzt
    bis 13:00 ET = 18:00 UTC). expected_last_close = Fr 27.11. 21:00 UTC
    (Standard-16:00-ET-Close). Wenn yfinance Fr-Bar geliefert hat -> OK.
    """
    now = datetime(2026, 11, 30, 8, 0, tzinfo=timezone.utc)
    # yfinance liefert Fr 27.11. close-Bar
    last_bar = datetime(2026, 11, 27, 21, 0, tzinfo=timezone.utc)
    ok, msg = _yfinance_bar_age_acceptable(last_bar, now_utc=now)
    assert ok, f"Mo nach Thanksgiving-Long-Weekend sollte fresh sein: {msg}"


def test_r_a47_independence_day_long_weekend():
    """Sa 04.07.2026 = Independence Day Observed Fr 03.07.2026.
    Annahme: Carlos checked Mo 06.07.2026 09:00 CEST = 07:00 UTC.
    Letzter Trading-Tag = Do 02.07.2026 (Fr ist US-Holiday).
    Erwartung: OK wenn yfinance Do-Bar hat.
    """
    now = datetime(2026, 7, 6, 7, 0, tzinfo=timezone.utc)
    last_bar = datetime(2026, 7, 2, 20, 0, tzinfo=timezone.utc)  # Do close
    ok, msg = _yfinance_bar_age_acceptable(last_bar, now_utc=now)
    assert ok, f"Independence-Day-Long-Weekend Mo sollte fresh sein: {msg}"


# ---------------------------------------------------------------------------
# True-Positive Szenarien — echte stale Daten MUESSEN weiterhin als FAIL erkannt werden
# ---------------------------------------------------------------------------

def test_r_a47_real_stale_data_still_fails():
    """Di 26.05.2026 08:14 UTC, aber yfinance liefert SPY-Bar von Fr 15.05.2026.
    Das sind echte stale Daten (>1 Woche). MUSS FAIL bleiben.
    """
    now = datetime(2026, 5, 26, 8, 14, tzinfo=timezone.utc)
    last_bar = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)  # 1 Woche alt
    ok, msg = _yfinance_bar_age_acceptable(last_bar, now_utc=now)
    assert not ok, f"1 Woche alte Bar muss als stale erkannt werden: {msg}"
    assert "stale" in msg.lower()


def test_r_a47_two_day_lag_fails():
    """yfinance reporting-lag > 1 Trading-Day muss FAIL.
    Di 26.05.2026 08:14 UTC, expected_last_close = Fr 22.05. 20:00 UTC
    (Memorial Day Mo filter). last_bar von Do 21.05. 14:00 UTC = 30h
    hinter expected -> stale.
    """
    now = datetime(2026, 5, 26, 8, 14, tzinfo=timezone.utc)
    last_bar = datetime(2026, 5, 21, 14, 0, tzinfo=timezone.utc)
    ok, msg = _yfinance_bar_age_acceptable(last_bar, now_utc=now)
    assert not ok, f"30h-alte Bar bei Di-Check sollte stale sein: {msg}"


# ---------------------------------------------------------------------------
# Normal-Betrieb Szenarien
# ---------------------------------------------------------------------------

def test_r_a47_normal_midweek_check_is_fresh():
    """Mi-Vormittag, last_bar von Di-Close = ~13h hinter expected_close.
    Klassischer Normal-Fall."""
    now = datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc)  # Mi 10:00 CEST
    last_bar = datetime(2026, 5, 12, 20, 0, tzinfo=timezone.utc)  # Di 22:00 CEST close
    ok, msg = _yfinance_bar_age_acceptable(last_bar, now_utc=now)
    assert ok, f"Normal Mi-Morgen sollte fresh sein: {msg}"


def test_r_a47_sunday_morning_is_fresh():
    """So-Morgen check, last_bar von Fr-Close. Normaler Wochenend-Fall."""
    now = datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc)  # So 12:00 CEST
    last_bar = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)  # Fr close
    ok, msg = _yfinance_bar_age_acceptable(last_bar, now_utc=now)
    assert ok, f"So-Morgen mit Fr-Bar sollte fresh sein: {msg}"


# ---------------------------------------------------------------------------
# Regression-Schutz: alter buggy Code-Pfad darf nicht zurueckkommen
# ---------------------------------------------------------------------------

def test_r_a47_old_hardcoded_4d_threshold_gone():
    """Regression: alter 'age_days > 4' Pattern darf nicht mehr in
    tc_yfinance_freshness sein."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "self_test.py"
    body = src.read_text(encoding="utf-8")
    # Lookup nur die tc_yfinance_freshness-Funktion
    fn_start = body.index("def tc_yfinance_freshness")
    fn_end = body.index("\ndef ", fn_start + 50)
    fn_body = body[fn_start:fn_end]
    assert "age_days > 4" not in fn_body, (
        "R-A47 REGRESSION: alte harte 4d-Schwelle ist zurueck"
    )
    assert "_yfinance_bar_age_acceptable" in fn_body, (
        "R-A47: Holiday-aware Helper muss in tc_yfinance_freshness genutzt werden"
    )


def test_r_a47_uses_market_calendar():
    """R-A47 Helper MUSS last_market_close_utc nutzen (sonst nicht holiday-aware)."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "self_test.py"
    body = src.read_text(encoding="utf-8")
    fn_start = body.index("def _yfinance_bar_age_acceptable")
    fn_end = body.index("\ndef ", fn_start + 50)
    fn_body = body[fn_start:fn_end]
    assert "last_market_close_utc" in fn_body, (
        "R-A47 Helper muss last_market_close_utc importieren/nutzen"
    )
