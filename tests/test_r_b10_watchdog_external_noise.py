"""R-B10 (07.06.2026) — Bot-Watchdog flaggt abgefangene externe API-Hiccups nicht.

Symptom: BOT-GESUNDHEIT-Watchdog-Karte zeigte (transient) ERROR "1 wiederholtes
Fehlermuster (3x)". Ursache: macro_signals loggte abgefangene externe Fetch-
Timeouts (FRED Yield-Curve, Credit-Spread HYG/IEF, Marktbreite SPY/RSP) mit
exc_info=True (voller Traceback) bzw. auf ERROR-Level. Der Watchdog
(_check_error_patterns) zaehlt jede [ERROR]/Traceback-Zeile -> 3 externe
Timeouts = Cry-Wolf-ERROR, obwohl der Bot graceful degradiert (Score 0).

Fix (R-B10): die 4 abgefangenen Makro-Fetch-Fehler loggen jetzt auf WARNING
OHNE Traceback. Diese Tests sichern den Watchdog-Vertrag, auf dem der Fix
beruht: WARNING-ohne-Traceback wird NICHT geflaggt, echte [ERROR]/Traceback
WEITERHIN (keine Ueber-Unterdrueckung).
"""
from app.watchdog import _check_error_patterns


def test_handled_warning_lines_not_flagged():
    """3x abgefangene WARNING (kein [ERROR], kein Traceback) -> Watchdog 'ok'."""
    line = ("2026-06-07 12:00:00 [WARNING] FRED Yield-Curve Fetch "
            "fehlgeschlagen: HTTPSConnectionPool(host='fred.stlouisfed.org') "
            "Read timed out")
    res = _check_error_patterns([line, line, line])
    assert res["status"] == "ok", res


def test_real_error_lines_still_flagged():
    """3x [ERROR] -> weiterhin geflaggt (echte Fehler NICHT versteckt)."""
    line = "2026-06-07 12:00:00 [ERROR] Etwas wirklich Kaputtes ist passiert"
    res = _check_error_patterns([line, line, line])
    assert res["status"] == "error"


def test_traceback_lines_still_flagged():
    """3x Traceback -> weiterhin geflaggt."""
    res = _check_error_patterns(["Traceback (most recent call last):"] * 3)
    assert res["status"] == "error"


def test_two_handled_warnings_below_threshold_ok():
    """<3x -> ok (Threshold bleibt bei 3)."""
    line = "[WARNING] Marktbreite Fetch fehlgeschlagen: timeout"
    res = _check_error_patterns([line, line])
    assert res["status"] == "ok"
