"""v37h Tab-Audit-Day-4 (14.05.2026) — Tests fuer pending_closes Cleanup.

Carlos's Self-Test-FAIL 14.05.: 2 stale-Eintraege in pending_closes.json
blieben 28-30h drin weil:
  1. Cleanup nur bei neuer Close-Order lief (nicht periodisch)
  2. fromisoformat() crashte silent bei ISO-Z-Format -> Eintrag blieb
  3. Status-blind: Filled-Orders 24h warten obwohl sie sicher entfernbar sind

Fix: _cleanup_pending_closes() Helper mit 4 Kriterien (Age + Final-Status +
unparseable + non-dict). Aufruf in _check_close_idempotent() bei jeder
Close-Attempt.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


def _now_iso():
    return datetime.now().isoformat()


def _ago_iso(hours):
    return (datetime.now() - timedelta(hours=hours)).isoformat()


# ============================================================
# _parse_iso_safe — ISO-Z-Robustheit
# ============================================================

def test_parse_iso_handles_z_suffix():
    """ISO mit Z-Suffix wird tz-naive geparsed."""
    from app.trader import _parse_iso_safe
    dt = _parse_iso_safe("2026-05-13T17:58:20.139771Z")
    assert dt is not None
    assert dt.tzinfo is None


def test_parse_iso_handles_naive():
    from app.trader import _parse_iso_safe
    dt = _parse_iso_safe("2026-05-13T17:58:20.139771")
    assert dt is not None


def test_parse_iso_handles_offset():
    from app.trader import _parse_iso_safe
    dt = _parse_iso_safe("2026-05-13T17:58:20+02:00")
    assert dt is not None


def test_parse_iso_returns_none_on_garbage():
    from app.trader import _parse_iso_safe
    assert _parse_iso_safe("") is None
    assert _parse_iso_safe(None) is None
    assert _parse_iso_safe("not-a-date") is None


# ============================================================
# _cleanup_pending_closes — 4 Kriterien
# ============================================================

@pytest.fixture
def mock_state():
    """Mock load_json + save_json fuer in-memory state."""
    state = {}

    def fake_load(filename):
        return state.get(filename)

    def fake_save(filename, data):
        state[filename] = data

    with patch("app.trader.load_json", side_effect=fake_load), \
         patch("app.trader.save_json", side_effect=fake_save):
        yield state


def test_cleanup_removes_stale_age(mock_state):
    """Kriterium 1: Eintrag > 24h alt -> entfernen."""
    from app.trader import _cleanup_pending_closes
    mock_state["pending_closes.json"] = {
        "12345": {"submitted_at": _ago_iso(30), "result_summary": "Submitted"},
        "67890": {"submitted_at": _ago_iso(2), "result_summary": "Submitted"},
    }
    removed = _cleanup_pending_closes()
    assert removed == 1
    assert "12345" not in mock_state["pending_closes.json"]
    assert "67890" in mock_state["pending_closes.json"]


def test_cleanup_removes_filled_immediately(mock_state):
    """Kriterium 2: Final-Status 'Filled' -> sofort entfernen (auch wenn frisch)."""
    from app.trader import _cleanup_pending_closes
    mock_state["pending_closes.json"] = {
        "11111": {"submitted_at": _ago_iso(1),
                  "result_summary": "{'orderForOpen': {'statusID': 'Filled', ...}}"},
    }
    removed = _cleanup_pending_closes()
    assert removed == 1
    assert "11111" not in mock_state["pending_closes.json"]


def test_cleanup_removes_cancelled_immediately(mock_state):
    """Kriterium 3: Cancelled/Rejected/ApiCancelled -> sofort entfernen."""
    from app.trader import _cleanup_pending_closes
    mock_state["pending_closes.json"] = {
        "22222": {"submitted_at": _ago_iso(1),
                  "result_summary": "{'statusID': 'Cancelled'}"},
        "33333": {"submitted_at": _ago_iso(1),
                  "result_summary": "{'statusID': 'Rejected'}"},
        "44444": {"submitted_at": _ago_iso(1),
                  "result_summary": "{'statusID': 'ApiCancelled'}"},
    }
    removed = _cleanup_pending_closes()
    assert removed == 3


def test_cleanup_removes_unparseable(mock_state):
    """Kriterium 4: submitted_at unparseable -> entfernen."""
    from app.trader import _cleanup_pending_closes
    mock_state["pending_closes.json"] = {
        "55555": {"submitted_at": "GARBAGE", "result_summary": "Submitted"},
    }
    removed = _cleanup_pending_closes()
    assert removed == 1


def test_cleanup_removes_non_dict_entries(mock_state):
    """Defensive: non-dict-Eintraege -> entfernen."""
    from app.trader import _cleanup_pending_closes
    mock_state["pending_closes.json"] = {
        "good": {"submitted_at": _now_iso(), "result_summary": "Submitted"},
        "bad1": "string-not-dict",
        "bad2": None,
        "bad3": 42,
    }
    removed = _cleanup_pending_closes()
    assert removed == 3
    assert "good" in mock_state["pending_closes.json"]


def test_cleanup_keeps_fresh_submitted(mock_state):
    """Fresh + Status='Submitted' -> bleibt (laeuft noch)."""
    from app.trader import _cleanup_pending_closes
    mock_state["pending_closes.json"] = {
        "12345": {"submitted_at": _ago_iso(0.5),  # 30min
                  "result_summary": "{'statusID': 'Submitted', 'filledQty': 0}"},
    }
    removed = _cleanup_pending_closes()
    assert removed == 0
    assert "12345" in mock_state["pending_closes.json"]


def test_cleanup_keeps_presubmitted(mock_state):
    """PreSubmitted = noch laufende Order, fresh -> bleibt."""
    from app.trader import _cleanup_pending_closes
    mock_state["pending_closes.json"] = {
        "77777": {"submitted_at": _ago_iso(2),
                  "result_summary": "{'statusID': 'PreSubmitted'}"},
    }
    removed = _cleanup_pending_closes()
    assert removed == 0


def test_cleanup_empty_state(mock_state):
    """Leer / nicht existent -> 0 removed, kein Crash."""
    from app.trader import _cleanup_pending_closes
    mock_state["pending_closes.json"] = None
    assert _cleanup_pending_closes() == 0
    mock_state["pending_closes.json"] = {}
    assert _cleanup_pending_closes() == 0


def test_cleanup_iso_z_suffix_robust(mock_state):
    """Regression: ISO-Z-Format crashte vorher den Cleanup-Filter silent."""
    from app.trader import _cleanup_pending_closes
    # 30h alter Eintrag mit Z-Suffix (war der Original-Bug)
    mock_state["pending_closes.json"] = {
        "76792991": {
            "submitted_at": (datetime.now() - timedelta(hours=30)).isoformat() + "Z",
            "result_summary": "Submitted",
        },
    }
    removed = _cleanup_pending_closes()
    # Mit Z-Robustheit: 30h alt -> wird entfernt (war vorher: blieb drin)
    assert removed == 1


def test_cleanup_custom_max_age(mock_state):
    """max_age_hours parameter respected."""
    from app.trader import _cleanup_pending_closes
    mock_state["pending_closes.json"] = {
        "12345": {"submitted_at": _ago_iso(10), "result_summary": "Submitted"},
    }
    # 5h max-age: 10h-alter Eintrag -> entfernt
    assert _cleanup_pending_closes(max_age_hours=5) == 1
    # Wieder setzen, 20h max-age: 10h-alter Eintrag -> bleibt
    mock_state["pending_closes.json"] = {
        "12345": {"submitted_at": _ago_iso(10), "result_summary": "Submitted"},
    }
    assert _cleanup_pending_closes(max_age_hours=20) == 0


# ============================================================
# v37h+2 (Q3-7) — Kriterium 5: stuck PreSubmitted mit filledQuantity=0
# ============================================================

def test_cleanup_removes_stale_presubmitted_zero_fills(mock_state):
    """Kriterium 5 (Q3-7): PreSubmitted + filledQuantity=0 + age > 4h -> weg.

    Carlos's Self-Test-FAIL 15.05.: Order #97462781 hing 23h als
    PreSubmitted mit 0 Fills weil 'PreSubmitted' nicht in Final-Status-
    Liste war und 24h-Age-Gate noch nicht griff.
    """
    from app.trader import _cleanup_pending_closes
    mock_state["pending_closes.json"] = {
        "97462781": {
            "submitted_at": _ago_iso(5),  # 5h alt = > 4h Stuck-Limit
            "result_summary": "{'orderForOpen': {'statusID': 'PreSubmitted', "
                              "'filledQuantity': 0}}",
        },
    }
    removed = _cleanup_pending_closes()
    assert removed == 1
    assert "97462781" not in mock_state["pending_closes.json"]


def test_cleanup_keeps_presubmitted_with_partial_fill(mock_state):
    """Negativ-Test: PreSubmitted MIT partial-fill bleibt drin (laeuft noch).

    Wenn filledQuantity > 0 ist, ist die Order aktiv und darf nicht
    versehentlich aus dem Tracking entfernt werden.
    """
    from app.trader import _cleanup_pending_closes
    mock_state["pending_closes.json"] = {
        "12345": {
            "submitted_at": _ago_iso(5),
            "result_summary": "{'orderForOpen': {'statusID': 'PreSubmitted', "
                              "'filledQuantity': 50}}",  # partial-fill!
        },
    }
    removed = _cleanup_pending_closes()
    assert removed == 0
    assert "12345" in mock_state["pending_closes.json"]


def test_cleanup_keeps_fresh_presubmitted_zero_fills(mock_state):
    """Negativ-Test: PreSubmitted + 0 fills aber FRISCH (<4h) bleibt drin.

    Order braucht Zeit um durch IBKR-Routing zu fliessen. Erst nach 4h
    ist sie sicher tot.
    """
    from app.trader import _cleanup_pending_closes
    mock_state["pending_closes.json"] = {
        "12345": {
            "submitted_at": _ago_iso(2),  # 2h alt = < 4h Stuck-Limit
            "result_summary": "{'statusID': 'PreSubmitted', 'filledQuantity': 0}",
        },
    }
    removed = _cleanup_pending_closes()
    assert removed == 0
    assert "12345" in mock_state["pending_closes.json"]


def test_cleanup_presubmitted_custom_stuck_hours(mock_state):
    """presubmitted_stuck_hours-Parameter respektiert."""
    from app.trader import _cleanup_pending_closes
    mock_state["pending_closes.json"] = {
        "12345": {
            "submitted_at": _ago_iso(3),  # 3h alt
            "result_summary": "{'statusID': 'PreSubmitted', 'filledQuantity': 0}",
        },
    }
    # 2h Stuck-Limit: 3h-alter PreSubmitted -> entfernt
    assert _cleanup_pending_closes(presubmitted_stuck_hours=2) == 1
    # Wieder setzen, 6h Stuck-Limit: 3h-alter -> bleibt
    mock_state["pending_closes.json"] = {
        "12345": {
            "submitted_at": _ago_iso(3),
            "result_summary": "{'statusID': 'PreSubmitted', 'filledQuantity': 0}",
        },
    }
    assert _cleanup_pending_closes(presubmitted_stuck_hours=6) == 0


# ============================================================
# _is_zero_filled — Robustness gegen Quoting-Varianten
# ============================================================

def test_is_zero_filled_single_quotes():
    from app.trader import _is_zero_filled
    assert _is_zero_filled("{'filledQuantity': 0}") is True


def test_is_zero_filled_double_quotes():
    from app.trader import _is_zero_filled
    assert _is_zero_filled('{"filledQuantity": 0}') is True


def test_is_zero_filled_float_zero():
    from app.trader import _is_zero_filled
    assert _is_zero_filled("{'filledQuantity': 0.0}") is True


def test_is_zero_filled_with_whitespace():
    from app.trader import _is_zero_filled
    assert _is_zero_filled("{'filledQuantity' :  0}") is True


def test_is_zero_filled_nonzero_returns_false():
    """Bei >0 -> False (sicher: Order laeuft noch)."""
    from app.trader import _is_zero_filled
    assert _is_zero_filled("{'filledQuantity': 50}") is False
    assert _is_zero_filled("{'filledQuantity': 0.5}") is False


def test_is_zero_filled_missing_returns_false():
    """Kein filledQuantity-Key -> False (sicher: nicht löschen)."""
    from app.trader import _is_zero_filled
    assert _is_zero_filled("{'statusID': 'PreSubmitted'}") is False
    assert _is_zero_filled("") is False
    assert _is_zero_filled(None) is False


def test_is_zero_filled_garbage_returns_false():
    """Defensiv: garbage input -> False, kein Crash."""
    from app.trader import _is_zero_filled
    assert _is_zero_filled("'filledQuantity': abc") is False
