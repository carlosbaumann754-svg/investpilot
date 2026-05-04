"""Tests fuer v37cu Anti-Loop-Idempotency-Schutz im Trader.

Verhindert dass close_position-Calls mehrmals fuer dieselbe Position
in kurzer Zeit (innerhalb Cooldown) ausgegeben werden.

Nach Episode 04.05.2026 wo Bot 19x SELL fuer BA + 55x SELL fuer CPER
ausgab → IBKR fillte alle → BA/CPER landeten massiv SHORT.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("INVESTPILOT_DATA_DIR", str(tmp_path))
    import importlib
    from app import config_manager
    importlib.reload(config_manager)
    yield tmp_path


def test_check_close_idempotent_no_pending(temp_data_dir):
    """Wenn nichts pending: kein Skip."""
    from app.trader import _check_close_idempotent
    client = MagicMock(spec=[])  # kein _get_ib
    skip, reason = _check_close_idempotent(client, "12345")
    assert skip is False
    assert reason == ""


def test_check_close_idempotent_recent_submitted_blocks(temp_data_dir):
    """Wenn vor 60s eine close-Order submitted wurde: Skip."""
    from app.config_manager import save_json
    from app.trader import _check_close_idempotent
    save_json("pending_closes.json", {
        "12345": {
            "submitted_at": (datetime.now() - timedelta(seconds=60)).isoformat(),
            "result_summary": "test",
        }
    })
    client = MagicMock(spec=[])
    skip, reason = _check_close_idempotent(client, "12345")
    assert skip is True
    assert "Anti-Loop" in reason
    assert "60s submitted" in reason or "60s" in reason


def test_check_close_idempotent_old_submit_passes(temp_data_dir):
    """Wenn der letzte Submit > 5min her ist: nicht mehr Skip."""
    from app.config_manager import save_json
    from app.trader import _check_close_idempotent
    save_json("pending_closes.json", {
        "12345": {
            "submitted_at": (datetime.now() - timedelta(minutes=10)).isoformat(),
            "result_summary": "test",
        }
    })
    client = MagicMock(spec=[])
    skip, reason = _check_close_idempotent(client, "12345")
    assert skip is False


def test_check_close_idempotent_open_trade_at_ibkr_blocks(temp_data_dir):
    """Wenn ib.openTrades() bereits einen Submitted-Trade fuer conId hat: Skip."""
    from app.trader import _check_close_idempotent

    contract = MagicMock()
    contract.conId = 12345
    order_status = MagicMock()
    order_status.status = "Submitted"
    trade = MagicMock()
    trade.contract = contract
    trade.order = MagicMock()
    trade.orderStatus = order_status

    ib = MagicMock()
    ib.openTrades.return_value = [trade]
    client = MagicMock()
    client._get_ib.return_value = ib

    skip, reason = _check_close_idempotent(client, "12345")
    assert skip is True
    assert "Submitted" in reason


def test_check_close_idempotent_filled_trade_passes(temp_data_dir):
    """Wenn open-trade-Status 'Filled' ist (also nicht mehr aktiv): nicht skippen."""
    from app.trader import _check_close_idempotent

    contract = MagicMock()
    contract.conId = 12345
    order_status = MagicMock()
    order_status.status = "Filled"  # Bereits gefillt -> nicht mehr aktiv pending
    trade = MagicMock()
    trade.contract = contract
    trade.order = MagicMock()
    trade.orderStatus = order_status

    ib = MagicMock()
    ib.openTrades.return_value = [trade]
    client = MagicMock()
    client._get_ib.return_value = ib

    skip, reason = _check_close_idempotent(client, "12345")
    assert skip is False


def test_track_pending_close_writes_state(temp_data_dir):
    """_track_pending_close persistiert Submit-Timestamp."""
    from app.config_manager import load_json
    from app.trader import _track_pending_close

    result = {"orderForOpen": {"orderID": "1", "filledQuantity": 100}}
    _track_pending_close("12345", result)

    pending = load_json("pending_closes.json") or {}
    assert "12345" in pending
    assert "submitted_at" in pending["12345"]


def test_track_pending_close_skips_already_closed(temp_data_dir):
    """Bei _already_closed Sentinel: kein Tracking (war eh kein neuer Submit)."""
    from app.config_manager import load_json
    from app.trader import _track_pending_close

    result = {"_already_closed": True, "_conId": 12345}
    _track_pending_close("12345", result)

    pending = load_json("pending_closes.json") or {}
    assert "12345" not in pending


def test_track_pending_close_cleanup_old_entries(temp_data_dir):
    """Eintraege > 24h werden automatisch beim naechsten Track gepruned."""
    from app.config_manager import save_json, load_json
    from app.trader import _track_pending_close

    # Vorher: ein alter und ein frischer Eintrag
    save_json("pending_closes.json", {
        "OLD": {
            "submitted_at": (datetime.now() - timedelta(hours=30)).isoformat(),
        },
        "FRESH": {
            "submitted_at": (datetime.now() - timedelta(hours=1)).isoformat(),
        },
    })

    # Neuen Track ausloesen
    _track_pending_close("NEW", {"orderForOpen": {"orderID": "1"}})

    pending = load_json("pending_closes.json") or {}
    assert "OLD" not in pending  # Cleanup
    assert "FRESH" in pending
    assert "NEW" in pending


def test_close_position_safe_skip_returns_sentinel(temp_data_dir):
    """Wenn Pre-Check skip sagt: Wrapper returnt _skipped_idempotent-Sentinel,
    ohne client.close_position() ueberhaupt aufzurufen."""
    from app.config_manager import save_json
    from app.trader import _close_position_safe, _is_skipped_idempotent

    # Pending-State setzen das Skip ausloest
    save_json("pending_closes.json", {
        "12345": {
            "submitted_at": (datetime.now() - timedelta(seconds=10)).isoformat(),
        }
    })

    client = MagicMock(spec=[])
    result = _close_position_safe(client, "pos1", "12345", "TEST")

    assert _is_skipped_idempotent(result) is True
    # Wichtig: client.close_position darf NICHT aufgerufen worden sein
    client.close_position.assert_not_called() if hasattr(client, "close_position") else None


def test_close_position_safe_normal_flow(temp_data_dir):
    """Wenn nichts pending: Wrapper ruft client.close_position auf + tracked."""
    from app.config_manager import load_json
    from app.trader import _close_position_safe, _is_skipped_idempotent

    client = MagicMock(spec=["close_position"])
    client.close_position.return_value = {"orderForOpen": {"orderID": "abc"}}

    result = _close_position_safe(client, "pos1", "99999", "TEST")

    assert not _is_skipped_idempotent(result)
    client.close_position.assert_called_once_with("pos1", "99999")
    # Tracking persistiert
    pending = load_json("pending_closes.json") or {}
    assert "99999" in pending
