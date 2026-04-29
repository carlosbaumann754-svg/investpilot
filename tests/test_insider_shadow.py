"""Tests fuer den Insider Shadow-Tracker (v37m, C1 Forward-A/B)."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("INVESTPILOT_DATA_DIR", str(tmp_path))
    import importlib
    from app import config_manager
    importlib.reload(config_manager)
    yield tmp_path


def test_log_and_read_roundtrip(temp_data_dir):
    from app.insider_shadow import log_shadow_decision, read_recent

    log_shadow_decision("AAPL", scanner_score=65.4, insider_score=2,
                        would_block=False, insider_min_score=-1)
    log_shadow_decision("ROKU", scanner_score=58.0, insider_score=-2,
                        would_block=True, insider_min_score=-1)

    entries = read_recent(limit=10)
    assert len(entries) == 2
    assert entries[0]["symbol"] == "AAPL"
    assert entries[0]["would_block"] is False
    assert entries[1]["symbol"] == "ROKU"
    assert entries[1]["would_block"] is True


def test_summary_stats_empty(temp_data_dir):
    from app.insider_shadow import summary_stats
    stats = summary_stats(days=14)
    assert stats["total_candidates_tracked"] == 0
    assert "Keine Shadow-Eintraege" in stats.get("note", "")


def test_summary_stats_with_data(temp_data_dir):
    from app.insider_shadow import log_shadow_decision, summary_stats

    # 3 passed, 2 would-block
    log_shadow_decision("AAPL", 65.4, 2, False, -1)
    log_shadow_decision("MSFT", 70.0, 0, False, -1)
    log_shadow_decision("NVDA", 80.0, 3, False, -1)
    log_shadow_decision("ROKU", 50.0, -2, True, -1)
    log_shadow_decision("TSLA", 55.0, -2, True, -1)

    stats = summary_stats(days=14)
    assert stats["total_candidates_tracked"] == 5
    assert stats["would_block_count"] == 2
    assert stats["would_block_pct"] == 40.0
    # AAPL+MSFT+NVDA passed -> avg = (65.4+70+80)/3 = 71.8
    assert stats["avg_scanner_score_passed"] == pytest.approx(71.8, abs=0.1)
    # ROKU+TSLA blocked -> avg = (50+55)/2 = 52.5
    assert stats["avg_scanner_score_blocked"] == pytest.approx(52.5, abs=0.1)
    assert stats["unique_symbols_tracked"] == 5


def test_summary_respects_age_window(temp_data_dir):
    """Eintraege aelter als days werden ignoriert."""
    from app.insider_shadow import log_shadow_decision, summary_stats
    import json

    # Direkt in die Datei einen alten Eintrag schreiben
    from app.config_manager import get_data_path
    path = get_data_path("insider_shadow_log.jsonl")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": old_ts, "symbol": "OLD", "scanner_score": 50,
            "insider_score": 0, "would_block": False, "insider_min_score": -1,
        }) + "\n")

    log_shadow_decision("NEW", 60, 1, False, -1)

    stats_14d = summary_stats(days=14)
    assert stats_14d["total_candidates_tracked"] == 1  # nur NEW

    stats_60d = summary_stats(days=60)
    assert stats_60d["total_candidates_tracked"] == 2  # OLD + NEW


def test_log_swallow_exception_no_raise(monkeypatch):
    """Shadow-Log darf Bot NIE durch Exception unterbrechen."""
    from app import insider_shadow

    def _broken(*args, **kwargs):
        raise IOError("disk full")

    monkeypatch.setattr("app.insider_shadow.get_data_path", _broken, raising=False)
    # Sollte SILENT durchgehen, keine Exception
    insider_shadow.log_shadow_decision("X", 50, 0, False, -1)


def test_histogram_by_insider_score(temp_data_dir):
    from app.insider_shadow import log_shadow_decision, summary_stats

    for score in [-2, -2, 0, 1, 2, 2, 3]:
        log_shadow_decision(f"S{score}", 60.0, score, score < -1, -1)

    stats = summary_stats(days=14)
    hist = stats["by_insider_score"]
    assert hist["-2"] == 2
    assert hist["0"] == 1
    assert hist["2"] == 2
    assert hist["3"] == 1
