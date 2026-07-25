"""v37h+2 (17.05.2026) — Tests fuer Health-Audit-Modul.

Smoke + Logic-Tests fuer die 5 Check-Gruppen + Diff-Tracking.
"""
import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


# ============================================================
# API-Keys
# ============================================================

def test_api_keys_all_present(monkeypatch):
    """Alle 5 Keys gesetzt -> alle passed."""
    from app.health_audit import check_api_keys
    monkeypatch.setenv("FINNHUB_API_KEY", "x" * 40)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y" * 100)
    monkeypatch.setenv("POLYGON_API_KEY", "z" * 32)
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "a" * 16)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "b" * 36)
    results = check_api_keys()
    assert len(results) == 5
    assert all(r.passed for r in results)


def test_api_keys_finnhub_missing(monkeypatch):
    """FINNHUB fehlt -> critical."""
    from app.health_audit import check_api_keys
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    results = check_api_keys()
    finnhub_check = next(r for r in results if "finnhub" in r.name)
    assert not finnhub_check.passed
    assert finnhub_check.severity == "critical"


# ============================================================
# State-Files
# ============================================================

@pytest.fixture
def mock_state(tmp_path, monkeypatch):
    """In-Memory-State fuer load_json/save_json + isoliertes DATA_DIR.

    Warum zusaetzlich DATA_DIR->tmp_path (Fix 25.07.2026): run_full_audit
    schreibt den Markdown-Report via get_data_path (data/audits/...) AN den
    load_json/save_json-Mocks VORBEI — ohne Umleitung landete er bei jedem
    Suite-Lauf im echten data/ des Repos (Symbole 'y'/'z' als Beweis).
    """
    from app import config_manager
    monkeypatch.setattr(config_manager, "DATA_DIR", tmp_path)

    state = {}

    def fake_load(filename):
        return state.get(filename)

    def fake_save(filename, data):
        state[filename] = data

    with patch("app.config_manager.load_json", side_effect=fake_load), \
         patch("app.config_manager.save_json", side_effect=fake_save):
        yield state


def test_state_files_brain_stale(mock_state):
    """brain_state mit altem Snapshot -> warning."""
    from app.health_audit import check_state_files
    old_ts = (datetime.now() - timedelta(hours=30)).isoformat()
    mock_state["brain_state.json"] = {
        "performance_snapshots": [{"timestamp": old_ts}],
    }
    mock_state["wfo_status.json"] = {"windows": [{"oos_sharpe": 5}]}
    results = check_state_files()
    brain_check = next(r for r in results if "brain_state" in r.name)
    assert not brain_check.passed


def test_state_files_market_context_stale(mock_state):
    """market_context >6h -> critical."""
    from app.health_audit import check_state_files
    mock_state["market_context.json"] = {
        "vix_level": 18,
        "last_update": (datetime.now() - timedelta(hours=8)).isoformat(),
    }
    results = check_state_files()
    mc_check = next(r for r in results if "market_context" in r.name)
    assert not mc_check.passed
    assert mc_check.severity == "critical"


# ============================================================
# Feature-Toggles
# ============================================================

def test_toggle_ml_scoring_false_default(mock_state):
    """use_ml_scoring=False -> info (OK)."""
    from app.health_audit import check_feature_toggles
    with patch("app.config_manager.load_config",
               return_value={"demo_trading": {"use_ml_scoring": False}}):
        results = check_feature_toggles()
    ml = next(r for r in results if r.name == "toggle_ml_scoring")
    assert ml.passed
    assert "default-off" in ml.message


def test_toggle_ml_scoring_true_no_model(mock_state):
    """use_ml_scoring=True ohne Modell -> warning."""
    from app.health_audit import check_feature_toggles
    with patch("app.config_manager.load_config",
               return_value={"demo_trading": {"use_ml_scoring": True}}):
        results = check_feature_toggles()
    ml = next(r for r in results if r.name == "toggle_ml_scoring")
    assert not ml.passed
    assert "silent dead" in ml.message


def test_toggle_insider_signals_finnhub_missing(monkeypatch, mock_state):
    """insider enabled aber FINNHUB fehlt -> warning."""
    from app.health_audit import check_feature_toggles
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    with patch("app.config_manager.load_config",
               return_value={"scanner": {"insider_signal_enabled": True}}):
        results = check_feature_toggles()
    insider = next(r for r in results if r.name == "toggle_insider_signals")
    assert not insider.passed


def test_toggle_cash_reserve_active(mock_state):
    """Cash-Reserve aktiv -> info."""
    from app.health_audit import check_feature_toggles
    with patch("app.config_manager.load_config",
               return_value={"risk_management": {
                   "min_cash_reserve_chf": 500, "min_cash_reserve_pct": 0.1}}):
        results = check_feature_toggles()
    cr = next(r for r in results if r.name == "toggle_cash_reserve")
    assert cr.passed


# ============================================================
# Diff-Tracking
# ============================================================

def test_diff_tracking_new_findings(mock_state):
    """Erster Run mit Findings -> alle als new_findings markiert."""
    from app.health_audit import run_full_audit
    # Patch alle Check-Groups damit deterministische Resultate
    with patch("app.health_audit.check_api_keys") as m_api, \
         patch("app.health_audit.check_state_files", return_value=[]), \
         patch("app.health_audit.check_feature_toggles", return_value=[]), \
         patch("app.health_audit.check_cron_freshness", return_value=[]), \
         patch("app.health_audit.check_code_antipatterns", return_value=[]), \
         patch("app.health_audit.check_module_coverage", return_value=[]), \
         patch("app.health_audit._send_pushover_alert") as m_pushover:
        from app.health_audit import HealthCheckResult
        m_api.return_value = [
            HealthCheckResult(name="x", category="api", passed=False,
                               severity="critical", message="fail"),
        ]
        summary = run_full_audit(dry_run=False)
    assert summary["failed"] == 1
    assert summary["new_findings_count"] == 1
    m_pushover.assert_called_once()


def test_diff_tracking_no_new_findings(mock_state):
    """Zweiter Run mit gleichen Findings -> kein Pushover."""
    from app.health_audit import run_full_audit, HealthCheckResult
    # Prev state mit X bereits failed
    mock_state["health_audit_state.json"] = {
        "failed_names": ["x"],
        "results": [],
    }
    with patch("app.health_audit.check_api_keys") as m_api, \
         patch("app.health_audit.check_state_files", return_value=[]), \
         patch("app.health_audit.check_feature_toggles", return_value=[]), \
         patch("app.health_audit.check_cron_freshness", return_value=[]), \
         patch("app.health_audit.check_code_antipatterns", return_value=[]), \
         patch("app.health_audit.check_module_coverage", return_value=[]), \
         patch("app.health_audit._send_pushover_alert") as m_pushover:
        m_api.return_value = [
            HealthCheckResult(name="x", category="api", passed=False,
                               severity="critical", message="fail"),
        ]
        summary = run_full_audit(dry_run=False)
    assert summary["failed"] == 1
    assert summary["new_findings_count"] == 0
    m_pushover.assert_not_called()


def test_diff_tracking_resolved(mock_state):
    """Vorheriger Finding ist jetzt gefixt -> resolved-Liste."""
    from app.health_audit import run_full_audit, HealthCheckResult
    mock_state["health_audit_state.json"] = {
        "failed_names": ["y", "z"],
        "results": [],
    }
    with patch("app.health_audit.check_api_keys") as m_api, \
         patch("app.health_audit.check_state_files", return_value=[]), \
         patch("app.health_audit.check_feature_toggles", return_value=[]), \
         patch("app.health_audit.check_cron_freshness", return_value=[]), \
         patch("app.health_audit.check_code_antipatterns", return_value=[]), \
         patch("app.health_audit.check_module_coverage", return_value=[]), \
         patch("app.health_audit._send_pushover_alert") as m_pushover:
        # Jetzt nur "y" failed, "z" resolved
        m_api.return_value = [
            HealthCheckResult(name="y", category="api", passed=False,
                               severity="warning", message="still"),
        ]
        summary = run_full_audit(dry_run=False)
    assert "z" in summary["resolved"]
    assert summary["new_findings_count"] == 0  # y war schon failed
    m_pushover.assert_not_called()


# ============================================================
# Dry-Run
# ============================================================

def test_dry_run_no_persist(mock_state):
    """dry_run=True -> kein State-Save."""
    from app.health_audit import run_full_audit, HealthCheckResult
    with patch("app.health_audit.check_api_keys") as m_api, \
         patch("app.health_audit.check_state_files", return_value=[]), \
         patch("app.health_audit.check_feature_toggles", return_value=[]), \
         patch("app.health_audit.check_cron_freshness", return_value=[]), \
         patch("app.health_audit.check_code_antipatterns", return_value=[]), \
         patch("app.health_audit.check_module_coverage", return_value=[]), \
         patch("app.health_audit._send_pushover_alert") as m_pushover:
        m_api.return_value = [
            HealthCheckResult(name="x", category="api", passed=False,
                               severity="warning", message="fail"),
        ]
        run_full_audit(dry_run=True)
    # Kein State-Update + kein Pushover
    assert "health_audit_state.json" not in mock_state
    m_pushover.assert_not_called()
