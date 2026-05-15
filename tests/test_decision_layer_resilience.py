"""v37h+2 R-A4/R-A5 (15.05.2026) — Decision-Layer-Resilience.

Audit-Phase-2 (Carlos's Wunsch 14:30 CEST) deckte 9 RISK-Items in
Decision-Layer-Modulen auf. Zwei davon Cutover-kritisch:

R-A4: Hedging-Trigger DEAD in ersten ~10 Cycles nach Cutover.
  brain_regime kommt aus detect_market_regime() mit >=3 Snapshots.
  Bei Cutover wird brain_state.json reset -> regime='unknown'.
  Wenn nur brain_regime triggert, gibt es kein Bear-Schutz beim
  ersten Down-Day. Fix: VIX-Backup + combined_score-Trigger.

R-A5: Stale VIX/F&G-Cache wenn fetch-APIs down.
  _load_context() liefert gecachte alte Werte ohne Age-Check.
  Bei yfinance-Outage am Cutover-Morgen trade Bot mit veralteten
  Regime-Daten. Fix: 6h-Age-Limit, bei stale -> None + Warning.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


# ============================================================
# R-A4: Hedging VIX-Backup-Trigger
# ============================================================

def test_hedge_fires_on_brain_bear():
    """Original-Trigger: brain_regime='bear' -> Hedge aktiv."""
    from app.hedging import check_hedge_needed
    result = check_hedge_needed(
        regime_data={"brain_regime": "bear", "combined_score": 0, "vix_level": 20},
        positions=[{"invested": 10000, "leverage": 1}],
        config={"hedging": {"enabled": True, "bear_position_multiplier": 0.5}},
    )
    assert result["hedge_needed"] is True
    assert "brain=bear" in result["hedge_trigger"]


def test_hedge_fires_on_high_vix_even_if_brain_unknown():
    """R-A4 Kern: brain=unknown (Cutover) + VIX=40 -> Hedge MUSS feuern."""
    from app.hedging import check_hedge_needed
    result = check_hedge_needed(
        regime_data={"brain_regime": "unknown", "combined_score": 0, "vix_level": 40},
        positions=[{"invested": 10000, "leverage": 1}],
        config={
            "hedging": {"enabled": True, "bear_position_multiplier": 0.5},
            "regime_filter": {"vix_crisis_threshold": 35},
        },
    )
    assert result["hedge_needed"] is True
    assert "VIX=40" in result["hedge_trigger"]


def test_hedge_fires_on_extreme_negative_combined_score():
    """R-A4: brain=unknown aber combined_score sehr negativ -> Hedge."""
    from app.hedging import check_hedge_needed
    result = check_hedge_needed(
        regime_data={"brain_regime": "unknown", "combined_score": -5, "vix_level": 20},
        positions=[{"invested": 10000, "leverage": 1}],
        config={"hedging": {"enabled": True, "bear_position_multiplier": 0.5}},
    )
    assert result["hedge_needed"] is True
    assert "score=-5" in result["hedge_trigger"]


def test_hedge_not_fires_when_all_indicators_normal():
    """Negativ-Test: Kein Bear, kein hoher VIX, score normal -> KEIN Hedge."""
    from app.hedging import check_hedge_needed
    result = check_hedge_needed(
        regime_data={"brain_regime": "neutral", "combined_score": 0, "vix_level": 18},
        positions=[{"invested": 10000, "leverage": 1}],
        config={"hedging": {"enabled": True}},
    )
    assert result["hedge_needed"] is False
    assert "Kein Bear-Signal" in result["reason"]


def test_hedge_disabled_via_config():
    """Feature-Toggle: enabled=False -> kein Hedge auch bei VIX=50."""
    from app.hedging import check_hedge_needed
    result = check_hedge_needed(
        regime_data={"brain_regime": "bear", "combined_score": -10, "vix_level": 50},
        positions=[{"invested": 10000, "leverage": 1}],
        config={"hedging": {"enabled": False}},
    )
    assert result["hedge_needed"] is False
    assert "deaktiviert" in result["reason"]


def test_hedge_multiple_triggers_dokumentiert():
    """Mehrere Trigger gleichzeitig: alle in hedge_trigger aufgelistet."""
    from app.hedging import check_hedge_needed
    result = check_hedge_needed(
        regime_data={"brain_regime": "bear", "combined_score": -5, "vix_level": 40},
        positions=[{"invested": 10000}],
        config={
            "hedging": {"enabled": True},
            "regime_filter": {"vix_crisis_threshold": 35},
        },
    )
    assert "brain=bear" in result["hedge_trigger"]
    assert "VIX=40" in result["hedge_trigger"]
    assert "score=-5" in result["hedge_trigger"]


def test_hedge_vix_threshold_configurable():
    """vix_crisis_threshold aus regime_filter respektiert."""
    from app.hedging import check_hedge_needed
    # VIX=32, Threshold=30 -> triggert
    result = check_hedge_needed(
        regime_data={"brain_regime": "unknown", "combined_score": 0, "vix_level": 32},
        positions=[{"invested": 10000}],
        config={
            "hedging": {"enabled": True},
            "regime_filter": {"vix_crisis_threshold": 30},
        },
    )
    assert result["hedge_needed"] is True
    # VIX=32, Threshold=40 -> triggert NICHT
    result2 = check_hedge_needed(
        regime_data={"brain_regime": "unknown", "combined_score": 0, "vix_level": 32},
        positions=[{"invested": 10000}],
        config={
            "hedging": {"enabled": True},
            "regime_filter": {"vix_crisis_threshold": 40},
        },
    )
    assert result2["hedge_needed"] is False


# ============================================================
# R-A5: Stale-Cache-Detection in market_context
# ============================================================

def _ctx_iso(hours_ago):
    return (datetime.now() - timedelta(hours=hours_ago)).isoformat()


@pytest.fixture
def mock_ctx_state():
    """Mock market_context.json fuer kontrollierte _load_context-Tests."""
    state = {}

    def fake_load(filename):
        return state.get(filename)

    def fake_save(filename, data):
        state[filename] = data

    with patch("app.market_context.load_json", side_effect=fake_load), \
         patch("app.market_context.save_json", side_effect=fake_save):
        yield state


def test_is_context_stale_fresh(mock_ctx_state):
    """Cache 1h alt -> nicht stale."""
    from app.market_context import _is_context_stale
    ctx = {"vix_level": 18.5, "last_update": _ctx_iso(1)}
    assert _is_context_stale(ctx) is False


def test_is_context_stale_old(mock_ctx_state):
    """Cache 8h alt -> stale."""
    from app.market_context import _is_context_stale
    ctx = {"vix_level": 18.5, "last_update": _ctx_iso(8)}
    assert _is_context_stale(ctx) is True


def test_is_context_stale_no_update_field(mock_ctx_state):
    """Cache ohne last_update -> stale (defensiv)."""
    from app.market_context import _is_context_stale
    assert _is_context_stale({"vix_level": 18.5}) is True
    assert _is_context_stale({}) is True
    assert _is_context_stale(None) is True


def test_is_context_stale_unparseable_update(mock_ctx_state):
    """last_update unparseable -> stale (defensiv)."""
    from app.market_context import _is_context_stale
    assert _is_context_stale({"last_update": "GARBAGE"}) is True


def test_load_context_fresh_returns_values(mock_ctx_state):
    """Fresh-Cache: vix_level wird returned."""
    from app.market_context import _load_context
    mock_ctx_state["market_context.json"] = {
        "vix_level": 22.5,
        "fear_greed_index": 45,
        "market_regime": "neutral",
        "last_update": _ctx_iso(2),
    }
    ctx = _load_context()
    assert ctx["vix_level"] == 22.5
    assert ctx["fear_greed_index"] == 45
    assert ctx.get("_stale") is not True


def test_load_context_stale_invalidates_vix(mock_ctx_state):
    """R-A5 Kern: Cache 8h alt -> vix_level wird None, _stale=True."""
    from app.market_context import _load_context
    mock_ctx_state["market_context.json"] = {
        "vix_level": 22.5,
        "fear_greed_index": 45,
        "market_regime": "neutral",
        "last_update": _ctx_iso(8),
    }
    ctx = _load_context()
    assert ctx["vix_level"] is None
    assert ctx["fear_greed_index"] is None
    assert ctx["market_regime"] == "unknown"
    assert ctx["_stale"] is True


def test_load_context_stale_does_not_modify_file(mock_ctx_state):
    """Original-File bleibt unveraendert wenn stale gefiltert wird."""
    from app.market_context import _load_context
    original = {
        "vix_level": 22.5,
        "fear_greed_index": 45,
        "last_update": _ctx_iso(8),
    }
    mock_ctx_state["market_context.json"] = original
    _load_context()  # returnt copy
    # Original-State im Mock bleibt mit alten Werten
    assert mock_ctx_state["market_context.json"]["vix_level"] == 22.5


# ============================================================
# R-A5: get_position_size_multiplier reagiert konservativ auf stale-Cache
# ============================================================

def test_position_multiplier_normal_when_vix_known(mock_ctx_state):
    """Bei bekanntem niedrigem VIX -> Multiplier 1.0."""
    from app.market_context import get_position_size_multiplier
    mock_ctx_state["market_context.json"] = {
        "macro_events_today": [],
        "last_update": _ctx_iso(1),
    }
    assert get_position_size_multiplier(vix_level=18) == 1.0


def test_position_multiplier_reduced_at_high_vix(mock_ctx_state):
    """VIX=35 -> Multiplier 0.5."""
    from app.market_context import get_position_size_multiplier
    mock_ctx_state["market_context.json"] = {
        "macro_events_today": [],
        "last_update": _ctx_iso(1),
    }
    assert get_position_size_multiplier(vix_level=35) == 0.5


def test_position_multiplier_conservative_when_cache_stale(mock_ctx_state):
    """R-A5 Kern: vix_level=None UND Cache stale -> Multiplier 0.5."""
    from app.market_context import get_position_size_multiplier
    mock_ctx_state["market_context.json"] = {
        "macro_events_today": [],
        "last_update": _ctx_iso(8),  # stale
    }
    # Caller liefert None weil _load_context den vix_level invalidiert hat
    assert get_position_size_multiplier(vix_level=None, events=[]) == 0.5


def test_position_multiplier_normal_when_cache_fresh_but_vix_none(mock_ctx_state):
    """Edge-Case: Cache fresh, aber kein VIX gefetcht. Multiplier bleibt 1.0
    (Cache nicht stale, also vermutlich nur noch nie gefetcht — kein Crash-Signal).
    """
    from app.market_context import get_position_size_multiplier
    mock_ctx_state["market_context.json"] = {
        "macro_events_today": [],
        "last_update": _ctx_iso(1),  # fresh
        # kein vix_level
    }
    assert get_position_size_multiplier(vix_level=None, events=[]) == 1.0
