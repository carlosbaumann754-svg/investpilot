"""v37h Tab-Audit-Day-3 (13.05.2026) — Tests fuer learn_rules Deduplication.

Carlos zeigte 13.05.2026 Brain-Card: 9 IDENTISCHE REGIME_ADJUSTMENT-
Regeln vom 05.05. zwischen 19:13-19:53. Bot's learn_rules() wurde alle
~5 Min aufgerufen waehrend Bear-Phase und appended jedes Mal denselben
Eintrag.

Fix: _is_duplicate_rule() Helper prueft (type, reason, instrument_id)
+ Age-Check (24h). Tests fixieren das Verhalten damit zukuenftige
Refactors die Dedup-Logic nicht silent kaputtmachen.
"""
from datetime import datetime, timedelta

import pytest


def _now_str():
    return datetime.now().isoformat()


def _ago_str(hours):
    return (datetime.now() - timedelta(hours=hours)).isoformat()


# ============================================================
# Duplicate Detection
# ============================================================

def test_dedup_detects_same_regime_rule():
    """Zwei REGIME_ADJUSTMENT mit gleichem reason innerhalb 24h = Duplikat."""
    from app.brain import _is_duplicate_rule
    existing = [{
        "type": "REGIME_ADJUSTMENT",
        "reason": "Baerischer Markt - defensiver positionieren",
        "created": _now_str(),
    }]
    new = {
        "type": "REGIME_ADJUSTMENT",
        "reason": "Baerischer Markt - defensiver positionieren",
        "created": _now_str(),
    }
    assert _is_duplicate_rule(existing, new) is True


def test_dedup_allows_different_reason():
    """Anderer reason (Bull statt Bear) -> kein Duplikat."""
    from app.brain import _is_duplicate_rule
    existing = [{
        "type": "REGIME_ADJUSTMENT",
        "reason": "Baerischer Markt - defensiver positionieren",
        "created": _now_str(),
    }]
    new = {
        "type": "REGIME_ADJUSTMENT",
        "reason": "Bullischer Markt - aggressiver positionieren",
        "created": _now_str(),
    }
    assert _is_duplicate_rule(existing, new) is False


def test_dedup_allows_different_type():
    """Anderer Typ (INCREASE_ALLOCATION) -> kein Duplikat."""
    from app.brain import _is_duplicate_rule
    existing = [{
        "type": "REGIME_ADJUSTMENT",
        "reason": "Bear",
        "created": _now_str(),
    }]
    new = {
        "type": "INCREASE_ALLOCATION",
        "reason": "Bear",  # gleicher reason
        "instrument_id": "12345",
        "created": _now_str(),
    }
    assert _is_duplicate_rule(existing, new) is False


def test_dedup_allows_different_instrument():
    """Symbol-spezifische Regeln mit verschiedenen instrument_ids = nicht-Duplikat."""
    from app.brain import _is_duplicate_rule
    existing = [{
        "type": "INCREASE_ALLOCATION",
        "reason": "Top Performer",
        "instrument_id": "AAPL-iid",
        "created": _now_str(),
    }]
    new = {
        "type": "INCREASE_ALLOCATION",
        "reason": "Top Performer",
        "instrument_id": "TSLA-iid",
        "created": _now_str(),
    }
    assert _is_duplicate_rule(existing, new) is False


# ============================================================
# Age-Window: 24h-Schwelle
# ============================================================

def test_dedup_allows_after_24h_window():
    """Gleiche Regel aelter als 24h -> erlaubt (sinnvolle Erinnerung)."""
    from app.brain import _is_duplicate_rule
    existing = [{
        "type": "REGIME_ADJUSTMENT",
        "reason": "Bear",
        "created": _ago_str(25),  # 25h alt -> Schwelle ueberschritten
    }]
    new = {
        "type": "REGIME_ADJUSTMENT",
        "reason": "Bear",
        "created": _now_str(),
    }
    assert _is_duplicate_rule(existing, new) is False


def test_dedup_blocks_within_24h_window():
    """Gleiche Regel innerhalb 24h -> Duplikat-Block."""
    from app.brain import _is_duplicate_rule
    existing = [{
        "type": "REGIME_ADJUSTMENT",
        "reason": "Bear",
        "created": _ago_str(12),  # 12h alt
    }]
    new = {
        "type": "REGIME_ADJUSTMENT",
        "reason": "Bear",
        "created": _now_str(),
    }
    assert _is_duplicate_rule(existing, new) is True


def test_dedup_custom_max_age():
    """max_age_hours parameter respektiert."""
    from app.brain import _is_duplicate_rule
    existing = [{
        "type": "REGIME_ADJUSTMENT",
        "reason": "Bear",
        "created": _ago_str(2),
    }]
    new = {
        "type": "REGIME_ADJUSTMENT",
        "reason": "Bear",
        "created": _now_str(),
    }
    # 1h Window: 2h alte Regel ist alt -> kein Duplikat
    assert _is_duplicate_rule(existing, new, max_age_hours=1) is False
    # 3h Window: 2h alte Regel ist frisch -> Duplikat
    assert _is_duplicate_rule(existing, new, max_age_hours=3) is True


# ============================================================
# Robust gegen Garbage-Input
# ============================================================

def test_dedup_handles_missing_created():
    """Regel ohne created-Field wird uebersprungen, nicht crash."""
    from app.brain import _is_duplicate_rule
    existing = [{"type": "REGIME_ADJUSTMENT", "reason": "Bear"}]  # kein 'created'
    new = {"type": "REGIME_ADJUSTMENT", "reason": "Bear", "created": _now_str()}
    # Kein Crash erwartet, kein Duplikat-Match
    assert _is_duplicate_rule(existing, new) is False


def test_dedup_handles_empty_existing():
    from app.brain import _is_duplicate_rule
    new = {"type": "REGIME_ADJUSTMENT", "reason": "Bear", "created": _now_str()}
    assert _is_duplicate_rule([], new) is False


def test_dedup_handles_non_dict_entries():
    """Defensive: existing kann unerwartete Eintraege haben."""
    from app.brain import _is_duplicate_rule
    existing = [None, "string-not-dict", 42,
                {"type": "REGIME_ADJUSTMENT", "reason": "Bear", "created": _now_str()}]
    new = {"type": "REGIME_ADJUSTMENT", "reason": "Bear", "created": _now_str()}
    # Nicht-dict ueberspringen, dict-Eintrag matchen
    assert _is_duplicate_rule(existing, new) is True
