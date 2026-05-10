"""v37h Pre-Cutover (10.05.2026) — Tests fuer yfinance-Weekend-Log-Filter."""
import logging
from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def reset_filter():
    """Filter zwischen Tests sauber abbauen."""
    from app import yfinance_log_filter
    yfinance_log_filter._reset_for_tests()
    yield
    yfinance_log_filter._reset_for_tests()


# ============================================================
# _is_weekend_utc — Datums-Klassifikator
# ============================================================

def test_weekday_returns_false():
    """Mo-Fr -> False (kein Wochenende)."""
    from app.yfinance_log_filter import _is_weekend_utc
    monday = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)  # Mon
    friday = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)  # Fri
    assert _is_weekend_utc(monday) is False
    assert _is_weekend_utc(friday) is False


def test_saturday_returns_true():
    from app.yfinance_log_filter import _is_weekend_utc
    saturday = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
    assert _is_weekend_utc(saturday) is True


def test_sunday_returns_true():
    from app.yfinance_log_filter import _is_weekend_utc
    sunday = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    assert _is_weekend_utc(sunday) is True


# ============================================================
# Filter-Logik: Werktag = pass, Wochenende = drop bei Pattern
# ============================================================

def test_filter_passes_message_on_weekday():
    """Werktag: ALLE Messages durchlassen (auch 'possibly delisted')."""
    from app.yfinance_log_filter import YFinanceWeekendNoiseFilter
    flt = YFinanceWeekendNoiseFilter(weekend_check=lambda: False)
    rec = logging.LogRecord(
        "yfinance", logging.ERROR, "x.py", 1,
        "$HG=F: possibly delisted; no price data found  (period=1d)",
        args=None, exc_info=None,
    )
    assert flt.filter(rec) is True


def test_filter_drops_delisted_message_on_weekend():
    from app.yfinance_log_filter import YFinanceWeekendNoiseFilter
    flt = YFinanceWeekendNoiseFilter(weekend_check=lambda: True)
    rec = logging.LogRecord(
        "yfinance", logging.ERROR, "x.py", 1,
        "$HG=F: possibly delisted; no price data found  (period=1d)",
        args=None, exc_info=None,
    )
    assert flt.filter(rec) is False  # drop


def test_filter_drops_no_price_data_message_on_weekend():
    from app.yfinance_log_filter import YFinanceWeekendNoiseFilter
    flt = YFinanceWeekendNoiseFilter(weekend_check=lambda: True)
    rec = logging.LogRecord(
        "yfinance", logging.ERROR, "x.py", 1,
        "$GC=F: no price data found",
        args=None, exc_info=None,
    )
    assert flt.filter(rec) is False


def test_filter_passes_other_yfinance_errors_on_weekend():
    """Wochenende: andere yfinance-Errors NICHT droppen (echte Probleme)."""
    from app.yfinance_log_filter import YFinanceWeekendNoiseFilter
    flt = YFinanceWeekendNoiseFilter(weekend_check=lambda: True)
    rec = logging.LogRecord(
        "yfinance", logging.ERROR, "x.py", 1,
        "Connection refused — yfinance API unreachable",
        args=None, exc_info=None,
    )
    assert flt.filter(rec) is True  # pass through (kein Match)


# ============================================================
# Install-Hook: idempotent + tatsaechlich am yfinance-logger
# ============================================================

def test_install_attaches_to_yfinance_logger():
    from app.yfinance_log_filter import (
        install_yfinance_weekend_filter, YFinanceWeekendNoiseFilter
    )
    install_yfinance_weekend_filter()
    yf_log = logging.getLogger("yfinance")
    assert any(isinstance(f, YFinanceWeekendNoiseFilter) for f in yf_log.filters)


def test_install_is_idempotent():
    from app.yfinance_log_filter import install_yfinance_weekend_filter
    first = install_yfinance_weekend_filter()
    second = install_yfinance_weekend_filter()
    assert first is True
    assert second is False  # zweiter call macht nichts


def test_filter_does_not_crash_on_format_error():
    """Defensiv: wenn record.getMessage() raised, pass-through statt crash."""
    from app.yfinance_log_filter import YFinanceWeekendNoiseFilter
    flt = YFinanceWeekendNoiseFilter(weekend_check=lambda: True)

    class BrokenRecord:
        levelno = logging.ERROR
        def getMessage(self):
            raise ValueError("broken format")

    assert flt.filter(BrokenRecord()) is True  # pass through


# ============================================================
# Integration: Filter unterdrueckt yfinance-Logs end-to-end
# ============================================================

def test_end_to_end_yfinance_logger_drops_at_weekend(caplog):
    from app.yfinance_log_filter import (
        install_yfinance_weekend_filter, YFinanceWeekendNoiseFilter
    )
    # Filter installieren (mit forcierter weekend=True)
    install_yfinance_weekend_filter()
    # Patch instanziierten Filter auf weekend=True
    yf_log = logging.getLogger("yfinance")
    for f in yf_log.filters:
        if isinstance(f, YFinanceWeekendNoiseFilter):
            f._is_weekend = lambda: True

    with caplog.at_level(logging.ERROR, logger="yfinance"):
        yf_log.error("$HG=F: possibly delisted; no price data found  (period=1d)")
        yf_log.error("Connection refused — yfinance API unreachable")

    msgs = [r.getMessage() for r in caplog.records]
    assert not any("possibly delisted" in m for m in msgs), \
        "Wochenende-Noise sollte unterdrueckt sein"
    assert any("Connection refused" in m for m in msgs), \
        "Echte yfinance-Errors muessen weiter sichtbar sein"
