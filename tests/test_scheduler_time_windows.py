"""
Tests fuer die Cron-Trigger-Fenster:
- asset_discovery.is_friday_discovery_time (Fr 17:00-17:05 UTC)
- weekly_report.is_friday_evening (Fr 18:00-18:05 UTC)
- optimizer.is_sunday_optimization_time (So 02:00-06:00 UTC, Intervall-Guard)

Stellt sicher, dass alle Trigger UTC nutzen (nicht Container-Lokalzeit) und
damit deckungsgleich mit den GitHub-Action-Crons (auch UTC) bleiben — DST-frei.
"""

from datetime import datetime, timezone
from unittest.mock import patch


def _utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ---- asset_discovery.is_friday_discovery_time ----

def test_friday_discovery_hits_at_17_utc():
    from app import asset_discovery
    # 2026-05-01 ist ein Freitag
    with patch("app.asset_discovery.datetime") as dt:
        dt.now.return_value = _utc(2026, 5, 1, 17, 2)
        dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert asset_discovery.is_friday_discovery_time() is True


def test_friday_discovery_misses_at_16_utc():
    from app import asset_discovery
    with patch("app.asset_discovery.datetime") as dt:
        dt.now.return_value = _utc(2026, 5, 1, 16, 30)
        assert asset_discovery.is_friday_discovery_time() is False


def test_friday_discovery_misses_after_window():
    from app import asset_discovery
    with patch("app.asset_discovery.datetime") as dt:
        dt.now.return_value = _utc(2026, 5, 1, 17, 6)
        assert asset_discovery.is_friday_discovery_time() is False


def test_friday_discovery_misses_on_thursday():
    from app import asset_discovery
    with patch("app.asset_discovery.datetime") as dt:
        # 2026-04-30 ist Donnerstag
        dt.now.return_value = _utc(2026, 4, 30, 17, 2)
        assert asset_discovery.is_friday_discovery_time() is False


def test_friday_discovery_uses_utc_not_local():
    """Smoke-Test: Funktion ruft datetime.now(tz=...) mit UTC auf."""
    from app import asset_discovery
    with patch("app.asset_discovery.datetime") as dt:
        dt.now.return_value = _utc(2026, 5, 1, 17, 0)
        asset_discovery.is_friday_discovery_time()
        # Sicherstellen: tz-Argument war timezone.utc
        args, kwargs = dt.now.call_args
        tz = args[0] if args else kwargs.get("tz")
        assert tz is timezone.utc


# ---- weekly_report.is_friday_evening ----

def test_friday_evening_hits_at_18_utc():
    from app import weekly_report
    with patch("app.weekly_report.datetime") as dt:
        dt.now.return_value = _utc(2026, 5, 1, 18, 3)
        assert weekly_report.is_friday_evening() is True


def test_friday_evening_misses_outside_window():
    from app import weekly_report
    with patch("app.weekly_report.datetime") as dt:
        dt.now.return_value = _utc(2026, 5, 1, 17, 30)
        assert weekly_report.is_friday_evening() is False


def test_friday_evening_uses_utc():
    from app import weekly_report
    with patch("app.weekly_report.datetime") as dt:
        dt.now.return_value = _utc(2026, 5, 1, 18, 0)
        weekly_report.is_friday_evening()
        args, kwargs = dt.now.call_args
        tz = args[0] if args else kwargs.get("tz")
        assert tz is timezone.utc


# ---- optimizer.is_sunday_optimization_time (Stundenfenster-Anteil) ----

def test_sunday_optimization_outside_hour_window_false():
    from app import optimizer
    with patch("app.optimizer.datetime") as dt:
        # Sonntag 2026-05-03 06:30 UTC -> ausserhalb [02..06)
        dt.now.return_value = _utc(2026, 5, 3, 6, 30)
        assert optimizer.is_sunday_optimization_time() is False


def test_sunday_optimization_wrong_weekday_false():
    from app import optimizer
    with patch("app.optimizer.datetime") as dt:
        # Samstag 03:00 UTC
        dt.now.return_value = _utc(2026, 5, 2, 3, 0)
        assert optimizer.is_sunday_optimization_time() is False


def test_sunday_optimization_uses_utc():
    from app import optimizer
    with patch("app.optimizer.datetime") as dt:
        dt.now.return_value = _utc(2026, 5, 3, 6, 30)  # forces early return
        optimizer.is_sunday_optimization_time()
        args, kwargs = dt.now.call_args
        tz = args[0] if args else kwargs.get("tz")
        assert tz is timezone.utc
