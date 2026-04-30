"""Tests fuer Earnings-Exit-Filter (v37v) — Variante-E-Logik."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.earnings_exit import check_earnings_exit


# ============================================================
# Trigger-Logik (Variante E)
# ============================================================

def test_no_trigger_when_no_earnings_date():
    """Symbol ohne Earnings-Termin -> kein Trigger."""
    with patch("app.events_calendar._fetch_earnings_date", return_value=None):
        should_exit, reason = check_earnings_exit("ROKU", 100_000, 1_000_000, {})
    assert should_exit is False
    assert reason is None


def test_no_trigger_when_earnings_too_far():
    """Earnings in 3 Tagen, default max_days=1 -> kein Trigger."""
    future = datetime.now() + timedelta(days=3)
    with patch("app.events_calendar._fetch_earnings_date", return_value=future), \
         patch("app.earnings_exit._fetch_volatility_proxy", return_value=15.0):
        should_exit, reason = check_earnings_exit("ROKU", 200_000, 1_000_000, {})
    assert should_exit is False  # > max_days_before=1


def test_trigger_when_position_too_large():
    """Earnings morgen + Position 20% Portfolio + niedrige Vola -> Trigger via pos."""
    tomorrow = datetime.now() + timedelta(days=1)
    with patch("app.events_calendar._fetch_earnings_date", return_value=tomorrow), \
         patch("app.earnings_exit._fetch_volatility_proxy", return_value=2.0):  # niedrig
        should_exit, reason = check_earnings_exit("ROKU", 200_000, 1_000_000, {})
    assert should_exit is True
    assert "Position 20" in reason or "20.0%" in reason


def test_trigger_when_vola_too_high():
    """Earnings morgen + kleine Position (5%) + hohe Vola (12%) -> Trigger via vola."""
    tomorrow = datetime.now() + timedelta(days=1)
    with patch("app.events_calendar._fetch_earnings_date", return_value=tomorrow), \
         patch("app.earnings_exit._fetch_volatility_proxy", return_value=12.0):
        should_exit, reason = check_earnings_exit("XYZ", 50_000, 1_000_000, {})
    assert should_exit is True
    assert "Vola" in reason


def test_no_trigger_small_position_low_vola():
    """Earnings morgen + kleine Position + niedrige Vola -> kein Trigger (gewollt)."""
    tomorrow = datetime.now() + timedelta(days=1)
    with patch("app.events_calendar._fetch_earnings_date", return_value=tomorrow), \
         patch("app.earnings_exit._fetch_volatility_proxy", return_value=3.0):
        should_exit, _ = check_earnings_exit("AAPL", 50_000, 1_000_000, {})
    assert should_exit is False  # 5% Position + 3% Vola unter Schwellen


def test_trigger_both_criteria():
    """Earnings + grosse Position + hohe Vola -> Trigger mit beidem in Reason."""
    tomorrow = datetime.now() + timedelta(days=1)
    with patch("app.events_calendar._fetch_earnings_date", return_value=tomorrow), \
         patch("app.earnings_exit._fetch_volatility_proxy", return_value=15.0):
        should_exit, reason = check_earnings_exit("ROKU", 200_000, 1_000_000, {})
    assert should_exit is True
    assert "Position" in reason
    assert "Vola" in reason


def test_no_trigger_after_earnings():
    """Earnings 2 Tage in der Vergangenheit -> kein Trigger."""
    past = datetime.now() - timedelta(days=2)
    with patch("app.events_calendar._fetch_earnings_date", return_value=past):
        should_exit, _ = check_earnings_exit("ROKU", 200_000, 1_000_000, {})
    assert should_exit is False  # days_until < 0


# ============================================================
# Master-Switch (config.market_context.earnings_exit_enabled=False)
# ============================================================

def test_disabled_via_config():
    tomorrow = datetime.now() + timedelta(days=1)
    cfg = {"market_context": {"earnings_exit_enabled": False}}
    with patch("app.events_calendar._fetch_earnings_date", return_value=tomorrow), \
         patch("app.earnings_exit._fetch_volatility_proxy", return_value=15.0):
        should_exit, _ = check_earnings_exit("ROKU", 200_000, 1_000_000, cfg)
    assert should_exit is False


# ============================================================
# Konfigurierbare Schwellen
# ============================================================

def test_custom_thresholds():
    """User kann Schwellen erhoehen damit Filter weniger schnell triggert."""
    tomorrow = datetime.now() + timedelta(days=1)
    cfg = {"market_context": {
        "earnings_exit_min_position_pct": 25.0,  # erhoeht
        "earnings_exit_min_vola_pct": 20.0,      # erhoeht
    }}
    # Position 15% (unter 25%), Vola 10% (unter 20%) -> kein Trigger
    with patch("app.events_calendar._fetch_earnings_date", return_value=tomorrow), \
         patch("app.earnings_exit._fetch_volatility_proxy", return_value=10.0):
        should_exit, _ = check_earnings_exit("ROKU", 150_000, 1_000_000, cfg)
    assert should_exit is False


def test_custom_max_days_before():
    """max_days_before=3: Earnings in 2 Tagen triggert."""
    in_2d = datetime.now() + timedelta(days=2)
    cfg = {"market_context": {"earnings_exit_max_days_before": 3}}
    with patch("app.events_calendar._fetch_earnings_date", return_value=in_2d), \
         patch("app.earnings_exit._fetch_volatility_proxy", return_value=15.0):
        should_exit, _ = check_earnings_exit("ROKU", 200_000, 1_000_000, cfg)
    assert should_exit is True


# ============================================================
# Edge-Cases
# ============================================================

def test_zero_portfolio_value_no_crash():
    """Portfolio = 0 darf nicht crashen."""
    tomorrow = datetime.now() + timedelta(days=1)
    with patch("app.events_calendar._fetch_earnings_date", return_value=tomorrow), \
         patch("app.earnings_exit._fetch_volatility_proxy", return_value=15.0):
        should_exit, _ = check_earnings_exit("ROKU", 0, 0, {})
    # Vola triggert weil 15% > 8% default
    assert should_exit is True


def test_vola_unavailable_falls_back_to_position_only():
    """Wenn yfinance nicht klappt -> Vola=None -> nur Position-Check."""
    tomorrow = datetime.now() + timedelta(days=1)
    with patch("app.events_calendar._fetch_earnings_date", return_value=tomorrow), \
         patch("app.earnings_exit._fetch_volatility_proxy", return_value=None):
        # Position 5% (unter 10%) -> kein Trigger
        should_exit, _ = check_earnings_exit("XYZ", 50_000, 1_000_000, {})
    assert should_exit is False
    # Position 20% -> Trigger
    with patch("app.events_calendar._fetch_earnings_date", return_value=tomorrow), \
         patch("app.earnings_exit._fetch_volatility_proxy", return_value=None):
        should_exit, _ = check_earnings_exit("ROKU", 200_000, 1_000_000, {})
    assert should_exit is True


# ============================================================
# Watchlist (Dashboard-Helper)
# ============================================================

# ============================================================
# v37x: Exemption-Liste
# ============================================================

@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("INVESTPILOT_DATA_DIR", str(tmp_path))
    import importlib
    from app import config_manager
    importlib.reload(config_manager)
    yield tmp_path


def test_exemption_persists(temp_data_dir):
    from app.earnings_exit import add_exemption, load_exemptions
    add_exemption("ROKU", reason="user-wants-to-hold")
    assert load_exemptions() == {"ROKU"}


def test_exemption_idempotent(temp_data_dir):
    from app.earnings_exit import add_exemption, load_exemptions
    add_exemption("ROKU")
    add_exemption("ROKU")
    add_exemption("roku")  # auch lower-case wird zu ROKU
    assert load_exemptions() == {"ROKU"}


def test_exemption_remove(temp_data_dir):
    from app.earnings_exit import add_exemption, remove_exemption, load_exemptions
    add_exemption("ROKU")
    add_exemption("AAPL")
    remove_exemption("ROKU")
    assert load_exemptions() == {"AAPL"}


def test_exempt_symbol_skips_filter(temp_data_dir):
    """Wenn Symbol exempt -> Filter triggert NICHT auch wenn alle Kriterien zutreffen."""
    from app.earnings_exit import add_exemption, check_earnings_exit
    add_exemption("ROKU", reason="test")
    tomorrow = datetime.now() + timedelta(days=1)
    with patch("app.events_calendar._fetch_earnings_date", return_value=tomorrow), \
         patch("app.earnings_exit._fetch_volatility_proxy", return_value=15.0):
        # Position 20%, Vola 15% — wuerde sonst klar triggern
        should_exit, _ = check_earnings_exit("ROKU", 200_000, 1_000_000, {})
    assert should_exit is False  # weil exempt


def test_exempt_audit_trail(temp_data_dir):
    from app.earnings_exit import add_exemption, remove_exemption
    from app.config_manager import load_json
    add_exemption("ROKU", reason="hold-thru-earnings")
    add_exemption("AAPL", reason="hold-thru-earnings")
    remove_exemption("ROKU", reason="changed-mind")
    data = load_json("earnings_exit_exemptions.json") or {}
    audit = data.get("audit", [])
    assert len(audit) == 3
    assert audit[0]["action"] == "ADD"
    assert audit[2]["action"] == "REMOVE"
    assert audit[2]["reason"] == "changed-mind"


# ============================================================
# v37y: One-shot Auto-Cleanup
# ============================================================

def test_auto_cleanup_removes_expired_exemption(temp_data_dir):
    """Earnings vorbei (gestern) -> Symbol wird auto-entfernt."""
    from app.earnings_exit import add_exemption, cleanup_expired_exemptions, load_exemptions
    from app.config_manager import load_json
    yesterday = datetime.now() - timedelta(days=1)
    add_exemption("ROKU", reason="hold-thru-earnings",
                  auto_cleanup_after_earnings=True, earnings_date=yesterday)
    # Pre-cleanup state direkt aus File lesen (load_exemptions triggert cleanup)
    pre_state = load_json("earnings_exit_exemptions.json") or {}
    assert "ROKU" in pre_state.get("exempt_symbols", [])
    # Direct cleanup-call
    removed = cleanup_expired_exemptions()
    assert "ROKU" in removed
    assert "ROKU" not in load_exemptions()


def test_auto_cleanup_keeps_future_exemption(temp_data_dir):
    """Earnings noch in der Zukunft -> Symbol bleibt exempt."""
    from app.earnings_exit import add_exemption, cleanup_expired_exemptions, load_exemptions
    tomorrow = datetime.now() + timedelta(days=1)
    add_exemption("ROKU", auto_cleanup_after_earnings=True, earnings_date=tomorrow)
    removed = cleanup_expired_exemptions()
    assert removed == []
    assert "ROKU" in load_exemptions()


def test_auto_cleanup_keeps_today_exemption(temp_data_dir):
    """Earnings heute -> Symbol bleibt exempt (erst NACH Earnings clean-up)."""
    from app.earnings_exit import add_exemption, cleanup_expired_exemptions, load_exemptions
    today = datetime.now()
    add_exemption("ROKU", auto_cleanup_after_earnings=True, earnings_date=today)
    removed = cleanup_expired_exemptions()
    assert removed == []
    assert "ROKU" in load_exemptions()


def test_persistent_exemption_no_auto_cleanup(temp_data_dir):
    """auto_cleanup_after_earnings=False -> persistent (legacy)."""
    from app.earnings_exit import add_exemption, cleanup_expired_exemptions, load_exemptions
    yesterday = datetime.now() - timedelta(days=1)
    add_exemption("ROKU", auto_cleanup_after_earnings=False, earnings_date=yesterday)
    removed = cleanup_expired_exemptions()
    # Symbol bleibt weil nicht in auto_cleanup-Map
    assert "ROKU" in load_exemptions()
    assert removed == []


def test_audit_trail_includes_auto_remove(temp_data_dir):
    from app.earnings_exit import add_exemption, cleanup_expired_exemptions
    from app.config_manager import load_json
    yesterday = datetime.now() - timedelta(days=1)
    add_exemption("ROKU", auto_cleanup_after_earnings=True, earnings_date=yesterday)
    cleanup_expired_exemptions()
    data = load_json("earnings_exit_exemptions.json") or {}
    actions = [a["action"] for a in data.get("audit", [])]
    assert "ADD" in actions
    assert "AUTO_REMOVE" in actions


def test_load_exemptions_triggers_auto_cleanup(temp_data_dir):
    """load_exemptions() ruft cleanup_expired_exemptions() implicit auf."""
    from app.earnings_exit import add_exemption, load_exemptions
    yesterday = datetime.now() - timedelta(days=1)
    add_exemption("ROKU", auto_cleanup_after_earnings=True, earnings_date=yesterday)
    # Direkter load_exemptions() Aufruf (ohne expliziten cleanup_call)
    result = load_exemptions()
    # Cleanup wurde von load_exemptions() ausgeloest
    assert "ROKU" not in result


def test_pending_earnings_watchlist():
    from app.earnings_exit import get_pending_earnings_for_positions
    tomorrow = datetime.now() + timedelta(days=1)
    far_future = datetime.now() + timedelta(days=30)

    positions = [
        {"symbol": "ROKU", "amount": 150_000},
        {"symbol": "AAPL", "amount": 50_000},  # earnings far away
        {"symbol": "NOSYM", "amount": 10_000},  # no earnings date
    ]

    def _fake_fetch(sym):
        return {"ROKU": tomorrow, "AAPL": far_future}.get(sym)

    with patch("app.events_calendar._fetch_earnings_date", side_effect=_fake_fetch), \
         patch("app.earnings_exit._fetch_volatility_proxy", return_value=12.0):
        result = get_pending_earnings_for_positions(positions, 1_000_000, {})

    # Nur ROKU (in 1d) im 7-Tage-Window, AAPL (30d) zu weit, NOSYM keine Daten
    syms = [r["symbol"] for r in result]
    assert "ROKU" in syms
    assert "AAPL" not in syms
    assert "NOSYM" not in syms
    roku = next(r for r in result if r["symbol"] == "ROKU")
    assert roku["would_exit"] is True
