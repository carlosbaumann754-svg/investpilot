"""Tests fuer R-A23 Position-Age-Lookup bei IBKR conId-Wiederverwendung.

Bug-Anlass 19.05.2026 abend: Bot kaufte OIL/USO um 17:08, schloss um
17:08 via TIME_STOP_CLOSE mit age_days=20.01. Dann 18:12 wieder BUY,
18:12 wieder Close. Loop alle 1h.

Wurzel: position_id bei IBKR == conId (USO=418893644), das UEBER alle
Buy-Sell-Zyklen WIEDERVERWENDET wird. _find_position_open_time nahm
ersten BUY-Match (= aelteste Buy = vor 20 Tagen) statt aktueller (Sekunden).

Fix R-A23: State-Machine durch History — letzte BUY NACH letztem CLOSE
mit dieser position_id/symbol.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import pytest


def _trade(action, ts_iso, position_id="418893644", symbol="USO", status="executed"):
    return {
        "action": action,
        "position_id": position_id,
        "symbol": symbol,
        "timestamp": ts_iso,
        "status": status,
    }


@pytest.fixture
def mock_history():
    """Patcht load_json('trade_history.json') auf custom history."""
    saved_history = {"history": []}

    def fake_load(filename):
        if filename == "trade_history.json":
            return saved_history["history"]
        return None

    with patch("app.trader.load_json", side_effect=fake_load):
        yield saved_history


# ============================================================
# 1. Bug-Reproduktion (vor R-A23 Fix)
# ============================================================

def test_conid_reuse_returns_latest_buy_after_close(mock_history):
    """OIL/USO conId 418893644: BUY 20d ago, CLOSE 19d ago, BUY heute.
    Result MUSS heute sein, NICHT vor 20d.
    """
    from app.trader import _find_position_open_time

    now = datetime.now(timezone.utc)
    days_ago = lambda d: (now - timedelta(days=d)).isoformat()
    seconds_ago = lambda s: (now - timedelta(seconds=s)).isoformat()

    mock_history["history"] = [
        _trade("SCANNER_BUY", days_ago(20)),       # alte Buy
        _trade("TIME_STOP_CLOSE", days_ago(19)),   # alte Close
        _trade("SCANNER_BUY", seconds_ago(30)),    # aktuelle Buy (30s alt)
    ]

    _, age = _find_position_open_time("418893644", api_open_time=None, symbol="USO")
    # Age in Tagen — 30s = ~0.000347d
    assert age is not None
    assert age < 0.01, f"Erwarte <0.01d (Sekunden alt), got {age:.4f}d"


def test_no_close_between_buys_takes_latest_buy(mock_history):
    """2 BUYs ohne Close dazwischen: latest_buy gewinnt (gleiche Position
    via DCA = add zu existierender Position)."""
    from app.trader import _find_position_open_time

    now = datetime.now(timezone.utc)
    days_ago = lambda d: (now - timedelta(days=d)).isoformat()

    mock_history["history"] = [
        _trade("SCANNER_BUY", days_ago(5)),
        _trade("SCANNER_BUY", days_ago(3)),  # DCA-Add
    ]

    _, age = _find_position_open_time("418893644", api_open_time=None)
    # Latest buy = vor 3 Tagen
    assert 2.5 < age < 3.5


def test_close_resets_then_buy_takes_new(mock_history):
    """BUY -> CLOSE -> BUY: age = neue Buy, nicht alte."""
    from app.trader import _find_position_open_time

    now = datetime.now(timezone.utc)
    days_ago = lambda d: (now - timedelta(days=d)).isoformat()

    mock_history["history"] = [
        _trade("SCANNER_BUY", days_ago(15)),
        _trade("STOP_LOSS_CLOSE", days_ago(10)),
        _trade("SCANNER_BUY", days_ago(2)),  # neue Pos
    ]

    _, age = _find_position_open_time("418893644", api_open_time=None)
    assert 1.5 < age < 2.5


# ============================================================
# 2. Close-Pattern-Detection
# ============================================================

@pytest.mark.parametrize("close_action", [
    "TRAILING_SL_CLOSE",
    "STOP_LOSS_CLOSE",
    "TIME_STOP_CLOSE",
    "SCANNER_SELL",
    "MANUAL_SELL",
    "MANUAL_COVER",
])
def test_all_close_actions_trigger_reset(mock_history, close_action):
    """Alle Close-Action-Varianten resetten den BUY-State."""
    from app.trader import _find_position_open_time

    now = datetime.now(timezone.utc)
    days_ago = lambda d: (now - timedelta(days=d)).isoformat()
    sec_ago = lambda s: (now - timedelta(seconds=s)).isoformat()

    mock_history["history"] = [
        _trade("SCANNER_BUY", days_ago(20)),
        _trade(close_action, days_ago(19)),
        _trade("SCANNER_BUY", sec_ago(60)),
    ]

    _, age = _find_position_open_time("418893644")
    assert age < 0.01, f"Close={close_action}: erwarte age <0.01d, got {age}"


def test_failed_close_does_not_reset(mock_history):
    """STOP_LOSS_CLOSE_FAILED ist kein echtes Close -> kein Reset."""
    from app.trader import _find_position_open_time

    now = datetime.now(timezone.utc)
    days_ago = lambda d: (now - timedelta(days=d)).isoformat()
    sec_ago = lambda s: (now - timedelta(seconds=s)).isoformat()

    mock_history["history"] = [
        _trade("SCANNER_BUY", days_ago(20)),
        _trade("STOP_LOSS_CLOSE_FAILED", days_ago(15)),  # try-fail
        # KEIN echtes Close -> alte BUY-Pos bleibt offen
    ]

    _, age = _find_position_open_time("418893644")
    # Alte BUY ist immer noch die aktive Position
    assert age is not None
    assert 19 < age < 21


def test_status_close_failed_does_not_reset(mock_history):
    """status='close_failed' (im trade_entry status-Feld) -> kein Reset."""
    from app.trader import _find_position_open_time

    now = datetime.now(timezone.utc)
    days_ago = lambda d: (now - timedelta(days=d)).isoformat()

    history_entries = [
        _trade("SCANNER_BUY", days_ago(20)),
        {"action": "TRAILING_SL_CLOSE", "position_id": "418893644",
         "symbol": "USO", "timestamp": days_ago(10),
         "status": "close_failed"},  # try fehlgeschlagen
    ]
    mock_history["history"] = history_entries

    _, age = _find_position_open_time("418893644")
    assert age is not None
    assert 19 < age < 21, f"FAILED close darf nicht resetten, got age={age}"


# ============================================================
# 3. Symbol-Fallback (wenn position_id None)
# ============================================================

def test_symbol_fallback_also_uses_state_machine(mock_history):
    """Symbol-Fallback-Path (3) muss auch latest_buy_after_last_close haben."""
    from app.trader import _find_position_open_time

    now = datetime.now(timezone.utc)
    days_ago = lambda d: (now - timedelta(days=d)).isoformat()
    sec_ago = lambda s: (now - timedelta(seconds=s)).isoformat()

    mock_history["history"] = [
        _trade("SCANNER_BUY", days_ago(20)),
        _trade("TIME_STOP_CLOSE", days_ago(19)),
        _trade("SCANNER_BUY", sec_ago(45)),
    ]

    # KEIN position_id -> nutzt symbol-fallback
    _, age = _find_position_open_time(None, api_open_time=None, symbol="USO")
    assert age is not None
    assert age < 0.01


# ============================================================
# 4. Edge-Cases
# ============================================================

def test_empty_history_returns_none(mock_history):
    from app.trader import _find_position_open_time
    mock_history["history"] = []
    dt, age = _find_position_open_time("418893644")
    assert dt is None and age is None


def test_only_closes_no_buys_returns_none(mock_history):
    """Nur CLOSE-Events ohne BUY -> keine offene Position -> None."""
    from app.trader import _find_position_open_time

    now = datetime.now(timezone.utc)
    days_ago = lambda d: (now - timedelta(days=d)).isoformat()

    mock_history["history"] = [
        _trade("TRAILING_SL_CLOSE", days_ago(5)),
    ]

    dt, age = _find_position_open_time("418893644")
    assert dt is None and age is None


def test_api_open_time_takes_priority(mock_history):
    """Wenn api_open_time gegeben (eToro): primary path, history ignored."""
    from app.trader import _find_position_open_time

    now = datetime.now(timezone.utc)
    api_time = (now - timedelta(hours=2)).isoformat()  # 2h ago via API

    mock_history["history"] = [
        # History sagt 20d alt — würde ohne api_open_time picken
        _trade("SCANNER_BUY", (now - timedelta(days=20)).isoformat()),
    ]

    _, age = _find_position_open_time("418893644", api_open_time=api_time)
    # API-Zeit gewinnt: 2h = 0.083d
    assert 0.05 < age < 0.12
