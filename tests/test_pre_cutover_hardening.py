"""v37h+2 R-A1 + R-A2 + R-A6 (15.05.2026) — Pre-Cutover-Hardening.

Letzte 3 RISK-Items aus Audit-Phase-2 vor Cutover-Wochenende 23.-25.05.

R-A1: paused_until tz-aware mit ZoneInfo('Europe/Zurich'). Verhindert
  Daily-Drawdown-Pause-Drift wenn risk_state.json zwischen VPS (UTC) und
  Local-Box (CEST) synct.

R-A2: detect_cash_deposit Currency-Mismatch-Reset. Verhindert Phantom-
  DCA-Plan wenn IBKR mal USD-Cash mal BASE-Cash reportet (FX-Reval).

R-A6: Earnings-Exit Defensive-Mode bei yfinance/Finnhub-Outage. In-
  Memory-Cache liefert letzte bekannte Earnings-Daten + bei Cache-Hit
  + Earnings imminent → defensive Close (ROKU-30.04.-Failure-Pattern).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


# ============================================================
# R-A1: paused_until tz-aware
# ============================================================

def test_now_local_returns_zurich_tz():
    """_now_local liefert tz-aware datetime in Europe/Zurich."""
    from app.risk_manager import _now_local, _LOCAL_TZ
    dt = _now_local()
    assert dt.tzinfo is not None
    # Vergleich gegen erwartete TZ — ZoneInfo oder utc-Fallback
    assert dt.tzinfo == _LOCAL_TZ


def test_parse_paused_until_tz_aware_input():
    """tz-aware ISO-String wird korrekt geparsed."""
    from app.risk_manager import _parse_paused_until
    # ISO mit +02:00 Offset (CEST)
    dt = _parse_paused_until("2026-05-20T15:30:00+02:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_paused_until_naive_input_legacy():
    """Naive ISO-Eintraege (Legacy) werden als Local-TZ interpretiert."""
    from app.risk_manager import _parse_paused_until, _LOCAL_TZ
    dt = _parse_paused_until("2026-05-20T15:30:00")
    assert dt is not None
    assert dt.tzinfo == _LOCAL_TZ


def test_parse_paused_until_utc_z_suffix():
    """Z-Suffix wird zu UTC-Offset konvertiert + auf Local-TZ umgerechnet."""
    from app.risk_manager import _parse_paused_until
    dt = _parse_paused_until("2026-05-20T13:30:00Z")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_paused_until_garbage_returns_none():
    from app.risk_manager import _parse_paused_until
    assert _parse_paused_until(None) is None
    assert _parse_paused_until("") is None
    assert _parse_paused_until("nope") is None


def test_parse_paused_until_no_drift_across_tz():
    """Regression: gleicher absoluter Zeitpunkt in CEST und UTC parsen identisch."""
    from app.risk_manager import _parse_paused_until
    # 15:30 CEST = 13:30 UTC
    a = _parse_paused_until("2026-05-20T15:30:00+02:00")
    b = _parse_paused_until("2026-05-20T13:30:00+00:00")
    # Sollten denselben Moment darstellen
    assert (a - b).total_seconds() == 0


# ============================================================
# R-A2: detect_cash_deposit Currency-Mismatch
# ============================================================

@pytest.fixture
def mock_dca_state():
    state = {}

    def fake_load(filename):
        return state.get(filename)

    def fake_save(filename, data):
        state[filename] = data

    with patch("app.risk_manager.load_json", side_effect=fake_load), \
         patch("app.risk_manager.save_json", side_effect=fake_save):
        yield state


def test_dca_currency_match_works_normally(mock_dca_state):
    """Gleiche Currency in zwei Cycles -> normales DCA-Verhalten."""
    from app.risk_manager import detect_cash_deposit
    mock_dca_state["cash_dca_state.json"] = {
        "last_seen_cash_usd": 1000.0,
        "last_seen_currency": "USD",
    }
    cfg = {"deposit_handling": {"dca_on_new_cash": True,
                                  "min_new_cash_trigger_usd": 500,
                                  "dca_spread_cycles": 5}}
    # +1000 cash in USD waehrend gespeichert USD -> normaler DCA-Trigger
    result = detect_cash_deposit(2000.0, cfg, currency="USD")
    assert result["dca_active"] is True


def test_dca_currency_mismatch_triggers_state_reset(mock_dca_state):
    """R-A2 Kern: Currency wechselt von USD zu CHF -> State-Reset, kein DCA."""
    from app.risk_manager import detect_cash_deposit
    mock_dca_state["cash_dca_state.json"] = {
        "last_seen_cash_usd": 1000.0,
        "last_seen_currency": "USD",
    }
    cfg = {"deposit_handling": {"dca_on_new_cash": True,
                                  "min_new_cash_trigger_usd": 500,
                                  "dca_spread_cycles": 5}}
    # current_cash 2000 in CHF (FX-Reval, nicht Einzahlung) -> kein DCA!
    result = detect_cash_deposit(2000.0, cfg, currency="CHF")
    assert result["dca_active"] is False
    # State wurde reset
    assert mock_dca_state["cash_dca_state.json"]["last_seen_currency"] == "CHF"
    assert mock_dca_state["cash_dca_state.json"]["active_plan"] is None


def test_dca_no_currency_hint_backward_compat(mock_dca_state):
    """currency=None (alter Caller-Style) -> kein Mismatch-Check, normales DCA."""
    from app.risk_manager import detect_cash_deposit
    mock_dca_state["cash_dca_state.json"] = {
        "last_seen_cash_usd": 1000.0,
        "last_seen_currency": "USD",
    }
    cfg = {"deposit_handling": {"dca_on_new_cash": True,
                                  "min_new_cash_trigger_usd": 500,
                                  "dca_spread_cycles": 5}}
    # currency=None -> alter Code-Pfad
    result = detect_cash_deposit(2000.0, cfg)
    # Normales DCA-Verhalten (Delta +1000 >= 500)
    assert result["dca_active"] is True


def test_dca_currency_persisted_on_state_save(mock_dca_state):
    """currency wird im State persistiert fuer naechsten Cycle."""
    from app.risk_manager import detect_cash_deposit
    cfg = {"deposit_handling": {"dca_on_new_cash": True,
                                  "min_new_cash_trigger_usd": 500,
                                  "dca_spread_cycles": 5}}
    detect_cash_deposit(2000.0, cfg, currency="CHF")
    assert mock_dca_state["cash_dca_state.json"]["last_seen_currency"] == "CHF"


# ============================================================
# R-A6: Earnings-Exit Defensive-Mode
# ============================================================

@pytest.fixture(autouse=True)
def _clear_earnings_cache():
    """Cache zwischen Tests leeren."""
    from app.earnings_exit import _earnings_date_cache
    _earnings_date_cache.clear()
    yield
    _earnings_date_cache.clear()


def test_fetch_with_cache_fresh_api_updates_cache():
    """Erfolgreicher API-Call -> Cache wird geupdatet."""
    from app.earnings_exit import _fetch_earnings_date_with_cache, _earnings_date_cache
    fake_dt = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
    with patch("app.events_calendar._fetch_earnings_date", return_value=fake_dt):
        dt, src = _fetch_earnings_date_with_cache("AAPL")
    assert dt == fake_dt
    assert src == "fresh"
    assert "AAPL" in _earnings_date_cache


def test_fetch_with_cache_api_down_returns_stale_cache():
    """R-A6 Kern: API None aber Cache-Hit -> stale_cache zurueck."""
    from app.earnings_exit import _fetch_earnings_date_with_cache, _earnings_date_cache
    # Cache vorbefuellen wie nach einem fruheren Cycle
    fake_dt = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
    _earnings_date_cache["AAPL"] = (fake_dt, datetime.now(timezone.utc).timestamp())
    # API liefert None
    with patch("app.events_calendar._fetch_earnings_date", return_value=None):
        dt, src = _fetch_earnings_date_with_cache("AAPL")
    assert dt == fake_dt
    assert src == "stale_cache"


def test_fetch_with_cache_api_down_no_cache_returns_none():
    """API None + kein Cache -> no_data."""
    from app.earnings_exit import _fetch_earnings_date_with_cache
    with patch("app.events_calendar._fetch_earnings_date", return_value=None):
        dt, src = _fetch_earnings_date_with_cache("UNKNOWN")
    assert dt is None
    assert src == "no_data"


def test_fetch_with_cache_old_cache_not_trusted():
    """Cache > 72h alt -> nicht mehr trusted, no_data."""
    from app.earnings_exit import _fetch_earnings_date_with_cache, _earnings_date_cache
    fake_dt = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
    old_ts = datetime.now(timezone.utc).timestamp() - 100 * 3600  # 100h alt
    _earnings_date_cache["AAPL"] = (fake_dt, old_ts)
    with patch("app.events_calendar._fetch_earnings_date", return_value=None):
        dt, src = _fetch_earnings_date_with_cache("AAPL")
    assert dt is None
    assert src == "no_data"


def test_fetch_with_cache_api_exception_uses_cache():
    """API wirft Exception -> Cache-Fallback greift."""
    from app.earnings_exit import _fetch_earnings_date_with_cache, _earnings_date_cache
    fake_dt = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
    _earnings_date_cache["AAPL"] = (fake_dt, datetime.now(timezone.utc).timestamp())
    with patch("app.events_calendar._fetch_earnings_date",
               side_effect=Exception("yfinance timeout")):
        dt, src = _fetch_earnings_date_with_cache("AAPL")
    assert dt == fake_dt
    assert src == "stale_cache"


def test_check_earnings_exit_defensive_close_on_stale_cache():
    """R-A6 KRITISCH: check_earnings_exit triggert Defensive-Close bei stale-Cache."""
    from app.earnings_exit import check_earnings_exit, _earnings_date_cache
    # Cache: Earnings morgen (1 Tag entfernt)
    earnings_tomorrow = datetime.now() + timedelta(days=1)
    _earnings_date_cache["ROKU"] = (
        earnings_tomorrow, datetime.now(timezone.utc).timestamp()
    )
    # API ist down
    with patch("app.events_calendar._fetch_earnings_date", return_value=None):
        should_exit, reason = check_earnings_exit(
            "ROKU",
            position_value_usd=5000,  # nur 5% Portfolio (sub-Trigger)
            portfolio_value_usd=100000,
            config={"market_context": {"earnings_exit_enabled": True}},
        )
    assert should_exit is True
    assert "Defensive-Close" in reason
    assert "yfinance/Finnhub API down" in reason


def test_check_earnings_exit_no_action_when_no_cache_and_api_down():
    """API down + kein Cache -> kein Trigger (kann nicht entscheiden)."""
    from app.earnings_exit import check_earnings_exit
    with patch("app.events_calendar._fetch_earnings_date", return_value=None):
        should_exit, reason = check_earnings_exit(
            "UNKNOWN", 5000, 100000,
            config={"market_context": {"earnings_exit_enabled": True}},
        )
    assert should_exit is False


def test_check_earnings_exit_normal_path_unaffected():
    """Negativ-Test: API liefert valides Date -> normaler Trigger-Path."""
    from app.earnings_exit import check_earnings_exit
    # Earnings in 5 Tagen, kein Trigger (max_days=1 Default)
    future_dt = datetime.now() + timedelta(days=5)
    with patch("app.events_calendar._fetch_earnings_date", return_value=future_dt):
        should_exit, reason = check_earnings_exit(
            "AAPL", 5000, 100000,
            config={"market_context": {"earnings_exit_enabled": True}},
        )
    assert should_exit is False  # > max_days -> kein Trigger
