"""v37h+2 Q3-8/Q3-9 (15.05.2026) — State-Cleanup-Resilience.

Carlos's Pattern-Audit nach Q3-7: weitere zwei Cleanup-Mechanismen
zeigten Anti-Patterns analog zu pending_closes vom 14.05.

Q3-8 (HIGH): _cleanup_partial_close_state hat partial_close_state.json
GECLEARED wenn portfolio=None aus IBKR-Fetch-Fail. Worst-Case: Bot
vergisst getriggerte Tranchen -> falsche Order-Groessen.

Q3-9 (MID): buy_cooldown.json nutzte datetime.fromisoformat() ohne
ISO-Z-Robustness (=gleicher Bug der pending_closes am 14.05. silent
crashte).

Beide gefixt am 15.05.2026 ~12:00 CEST.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


def _now_iso():
    return datetime.now().isoformat()


def _ago_iso(hours):
    return (datetime.now() - timedelta(hours=hours)).isoformat()


# ============================================================
# Q3-8: _cleanup_partial_close_state Defensive-Guards
# ============================================================

@pytest.fixture
def mock_state():
    state = {}

    def fake_load(filename):
        return state.get(filename)

    def fake_save(filename, data):
        state[filename] = data

    with patch("app.trader.load_json", side_effect=fake_load), \
         patch("app.trader.save_json", side_effect=fake_save):
        yield state


def test_partial_close_cleanup_skipped_when_portfolio_none(mock_state):
    """Q3-8 HIGH-RISK: portfolio=None -> Cleanup MUSS skippen, nicht clearen."""
    from app.trader import _cleanup_partial_close_state
    mock_state["partial_close_state.json"] = {
        "AAPL_123": {"triggered": [0, 1]},  # Tranche 1 schon geschlossen!
        "MSFT_456": {"triggered": [0]},
    }
    _cleanup_partial_close_state(None)
    # State bleibt UNVERAENDERT — kein Datenverlust
    assert len(mock_state["partial_close_state.json"]) == 2
    assert mock_state["partial_close_state.json"]["AAPL_123"]["triggered"] == [0, 1]


def test_partial_close_cleanup_skipped_when_portfolio_empty_dict(mock_state):
    """Edge-Case: portfolio = {} statt None — auch skippen."""
    from app.trader import _cleanup_partial_close_state
    mock_state["partial_close_state.json"] = {"AAPL_123": {"triggered": [0]}}
    _cleanup_partial_close_state({})
    assert "AAPL_123" in mock_state["partial_close_state.json"]


def test_partial_close_cleanup_skipped_when_portfolio_not_dict(mock_state):
    """Defensive: portfolio = 'string' / list / int -> skippen."""
    from app.trader import _cleanup_partial_close_state
    mock_state["partial_close_state.json"] = {"AAPL_123": {"triggered": [0]}}
    _cleanup_partial_close_state("not-a-dict")
    _cleanup_partial_close_state([1, 2, 3])
    _cleanup_partial_close_state(42)
    # Alle 3 Calls haben nichts veraendert
    assert "AAPL_123" in mock_state["partial_close_state.json"]


def test_partial_close_cleanup_skipped_when_positions_none(mock_state):
    """Edge-Case: portfolio = {'positions': None} — auch skippen."""
    from app.trader import _cleanup_partial_close_state
    mock_state["partial_close_state.json"] = {"AAPL_123": {"triggered": [0]}}
    _cleanup_partial_close_state({"positions": None, "credit": 1000})
    assert "AAPL_123" in mock_state["partial_close_state.json"]


def test_partial_close_cleanup_normal_case(mock_state):
    """Positive-Test: portfolio valide, geschlossene Positionen werden entfernt."""
    from app.trader import _cleanup_partial_close_state
    mock_state["partial_close_state.json"] = {
        "AAPL_123": {"triggered": [0]},      # noch offen
        "MSFT_456": {"triggered": [0, 1]},   # nicht mehr im Portfolio = geschlossen
    }
    # Simuliere portfolio mit nur AAPL offen
    with patch("app.trader.EtoroClient.parse_position",
               side_effect=lambda pos: {"position_id": pos.get("positionID")}):
        _cleanup_partial_close_state({
            "positions": [{"positionID": "AAPL_123"}],
        })
    assert "AAPL_123" in mock_state["partial_close_state.json"]
    assert "MSFT_456" not in mock_state["partial_close_state.json"]


def test_partial_close_cleanup_skipped_when_all_positions_parse_fail(mock_state):
    """Schema-Drift-Detection: alle parse_position-Calls failen -> nicht clearen."""
    from app.trader import _cleanup_partial_close_state
    mock_state["partial_close_state.json"] = {"AAPL_123": {"triggered": [0]}}
    with patch("app.trader.EtoroClient.parse_position",
               side_effect=Exception("schema drift")):
        _cleanup_partial_close_state({
            "positions": [{"weird_shape": True}, {"also_weird": True}],
        })
    # State bleibt unveraendert — wir wissen nicht ob die Positions wirklich
    # geschlossen sind oder nur parse-broken.
    assert "AAPL_123" in mock_state["partial_close_state.json"]


def test_partial_close_cleanup_no_op_when_empty_state(mock_state):
    """Defensiv: leerer state -> no-op, kein Crash auch bei portfolio=None."""
    from app.trader import _cleanup_partial_close_state
    mock_state["partial_close_state.json"] = {}
    _cleanup_partial_close_state(None)  # darf nicht crashen
    mock_state["partial_close_state.json"] = None
    _cleanup_partial_close_state(None)


def test_partial_close_legitimately_empty_portfolio(mock_state):
    """Edge-Case: portfolio = {'positions': []} (Bot hat tatsaechlich keine Positionen).

    Hier ist es korrekt zu clearen — kein 'unsicherer Fetch'-Fall.
    """
    from app.trader import _cleanup_partial_close_state
    mock_state["partial_close_state.json"] = {"AAPL_123": {"triggered": [0]}}
    _cleanup_partial_close_state({"positions": []})
    # Bei explizit leerer Liste: legitime "alle geschlossen"-Situation
    assert mock_state["partial_close_state.json"] == {}


# ============================================================
# Q3-9: buy_cooldown.json ISO-Z-Robustness
# ============================================================
# Diese Tests verifizieren dass die ISO-Z-Robustness im buy_cooldown
# Cleanup greift. Wir testen direkt _parse_iso_safe weil der Cleanup
# inline-lambda im _run_buy_logic ist und schwer isoliert testbar.

def test_parse_iso_safe_handles_buy_cooldown_z_format():
    """Q3-9: ISO-Z-Format im buy_cooldown.last_attempt -> parsbar."""
    from app.trader import _parse_iso_safe
    dt = _parse_iso_safe("2026-05-13T17:58:20.139771Z")
    assert dt is not None
    assert dt.tzinfo is None


def test_parse_iso_safe_handles_garbage_in_cooldown():
    """Defensiv: garbage last_attempt -> None (= Eintrag wird entfernt)."""
    from app.trader import _parse_iso_safe
    assert _parse_iso_safe("nope") is None
    assert _parse_iso_safe("") is None
    assert _parse_iso_safe(None) is None
