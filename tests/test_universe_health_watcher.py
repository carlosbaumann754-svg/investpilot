"""Tests fuer Universe-Health-Watcher (Auto-Disable + Re-Enable Suggestions)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


def _setup_data_dir(monkeypatch, tmp_path):
    """Patch DATA_DIR to a tmpdir so tests don't touch real data/."""
    monkeypatch.setenv("INVESTPILOT_DATA_DIR", str(tmp_path))
    # Force config_manager to re-resolve DATA_DIR
    import importlib
    from app import config_manager
    importlib.reload(config_manager)
    return tmp_path


def test_disable_suggestion_after_3_consecutive_not_ok(monkeypatch, tmp_path):
    data_dir = _setup_data_dir(monkeypatch, tmp_path)
    from app import universe_health_watcher as uhw

    # Simulate 3 universe-health checks all 'insufficient_data' for SQ
    health = {"report": {
        "AAPL": {"status": "ok", "days": 1000},
        "SQ":   {"status": "insufficient_data", "days": 5},
    }}
    for _ in range(3):
        result = uhw.update_counters(universe_health=health, disabled_symbols=[])

    # SQ sollte in to_disable sein, AAPL nicht
    suggestions = result["suggestions"]
    disable_symbols = [s["symbol"] for s in suggestions["to_disable"]]
    assert "SQ" in disable_symbols
    assert "AAPL" not in disable_symbols


def test_no_disable_suggestion_below_threshold(monkeypatch, tmp_path):
    _setup_data_dir(monkeypatch, tmp_path)
    from app import universe_health_watcher as uhw

    health = {"report": {"SQ": {"status": "insufficient_data"}}}
    # Nur 2 Checks (threshold ist 3)
    for _ in range(2):
        result = uhw.update_counters(universe_health=health, disabled_symbols=[])

    assert not result["suggestions"]["to_disable"]


def test_consecutive_ok_resets_disable_counter(monkeypatch, tmp_path):
    _setup_data_dir(monkeypatch, tmp_path)
    from app import universe_health_watcher as uhw

    # 2x not-ok, dann 1x ok -> Counter resetted, kein Disable-Vorschlag
    health_bad = {"report": {"SQ": {"status": "insufficient_data"}}}
    health_ok = {"report": {"SQ": {"status": "ok"}}}
    uhw.update_counters(universe_health=health_bad, disabled_symbols=[])
    uhw.update_counters(universe_health=health_bad, disabled_symbols=[])
    result = uhw.update_counters(universe_health=health_ok, disabled_symbols=[])

    assert not result["suggestions"]["to_disable"]


def test_re_enable_suggestion_for_disabled_symbol(monkeypatch, tmp_path):
    _setup_data_dir(monkeypatch, tmp_path)
    from app import universe_health_watcher as uhw

    # Symbol ist disabled, aber liefert 3x in Folge ok
    health = {"report": {"SQ": {"status": "ok"}}}
    for _ in range(3):
        result = uhw.update_counters(universe_health=health, disabled_symbols=["SQ"])

    enable_symbols = [s["symbol"] for s in result["suggestions"]["to_enable"]]
    assert "SQ" in enable_symbols


def test_no_re_enable_for_active_symbol(monkeypatch, tmp_path):
    _setup_data_dir(monkeypatch, tmp_path)
    from app import universe_health_watcher as uhw

    # AAPL ist aktiv (nicht in disabled), 3x ok -> kein Re-Enable-Vorschlag (sinnlos)
    health = {"report": {"AAPL": {"status": "ok"}}}
    for _ in range(3):
        result = uhw.update_counters(universe_health=health, disabled_symbols=[])

    assert not result["suggestions"]["to_enable"]


def test_history_capped_at_max_history(monkeypatch, tmp_path):
    _setup_data_dir(monkeypatch, tmp_path)
    from app import universe_health_watcher as uhw

    health = {"report": {"AAPL": {"status": "ok"}}}
    for _ in range(15):
        uhw.update_counters(universe_health=health, disabled_symbols=[])

    counters = result_counters = uhw._load(uhw.COUNTERS_FILE)
    assert len(counters["AAPL"]["history"]) <= uhw.MAX_HISTORY


def test_get_suggestions_returns_default_when_empty(monkeypatch, tmp_path):
    _setup_data_dir(monkeypatch, tmp_path)
    from app import universe_health_watcher as uhw

    s = uhw.get_suggestions()
    assert s["to_disable"] == []
    assert s["to_enable"] == []
    assert "thresholds" in s
