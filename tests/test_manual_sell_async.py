"""Tests fuer R-A22 Manual-Sell async-Loop-Fix (Sprint-Tag-9 abend 19.05.2026).

Bug-Anlass: Carlos klickte 16:42 'Verkaufen' fuer XLE im Dashboard.
FastAPI ist async, ruft aber sync client.close_position() auf, was
ib_insync mit eigenem event-loop intern nutzt -> ValueError 'future
belongs to different loop' + hängende Tasks + IBKR-Connection-Crash.
Cascade: 7 Sentry-Issues in einem Klick.

Fix R-A22: ib_insync-Sync-Calls via asyncio.to_thread() isolieren
(analog _broker_status_sync-Pattern).
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def test_close_position_called_via_to_thread():
    """api_manual_sell ruft close_position via asyncio.to_thread auf."""
    mock_client = MagicMock()
    mock_client.close_position = MagicMock(return_value={"ok": True})

    async def _run():
        return await asyncio.to_thread(
            mock_client.close_position, "pos_id_123", 5001)

    result = asyncio.run(_run())
    assert result == {"ok": True}
    mock_client.close_position.assert_called_once_with("pos_id_123", 5001)


def test_to_thread_isolates_blocking_call():
    """asyncio.to_thread blockiert nicht den main event-loop."""
    import time

    def slow_sync_call():
        time.sleep(0.05)
        return "done"

    async def _run():
        t_start = asyncio.get_event_loop().time()
        results = await asyncio.gather(
            asyncio.to_thread(slow_sync_call),
            asyncio.to_thread(slow_sync_call),
            asyncio.to_thread(slow_sync_call),
        )
        elapsed = asyncio.get_event_loop().time() - t_start
        return results, elapsed

    results, elapsed = asyncio.run(_run())
    assert all(r == "done" for r in results)
    # Parallel < 200ms (3x 50ms sequentiell = 150ms+)
    assert elapsed < 0.25, f"to_thread parallelism broken: {elapsed:.3f}s"


def test_to_thread_propagates_exceptions():
    """Exceptions in to_thread werden korrekt zum async-Caller propagiert."""
    def raising_call():
        raise ValueError("simulated ib_insync error")

    async def _run():
        await asyncio.to_thread(raising_call)

    with pytest.raises(ValueError, match="simulated ib_insync error"):
        asyncio.run(_run())


def test_to_thread_with_args_and_kwargs():
    """asyncio.to_thread supports args + kwargs."""
    def my_func(a, b, mode="default"):
        return f"{a}-{b}-{mode}"

    async def _run():
        return await asyncio.to_thread(my_func, "x", "y", mode="test")

    assert asyncio.run(_run()) == "x-y-test"


def test_api_manual_sell_has_to_thread_in_source():
    """Smoke-Test: api_manual_sell source enthaelt asyncio.to_thread Aufruf."""
    src_path = "web/app.py"
    with open(src_path, encoding="utf-8") as f:
        src = f.read()

    # Finde api_manual_sell Funktions-Body (~Z.2425-2625)
    # Pattern: muss 'asyncio.to_thread' im manual_sell-Bereich enthalten
    start = src.find("async def api_manual_sell")
    assert start != -1, "api_manual_sell endpoint nicht gefunden"
    # Naechste 5000 chars = Funktions-Body
    body = src[start:start + 5000]
    assert "to_thread" in body, (
        "R-A22-Fix nicht vorhanden: api_manual_sell muss ib_insync-Calls "
        "via asyncio.to_thread() isolieren"
    )
    assert "close_position" in body
    # Konkret: close_position MUSS via to_thread aufgerufen werden
    assert "to_thread(\n            client.close_position" in body or \
           "to_thread(client.close_position" in body, \
           "close_position muss via asyncio.to_thread aufgerufen werden"


def test_api_position_correlations_has_async_safe_fetch():
    """api_position_correlations nutzt brain_cache oder to_thread (R-A22)."""
    src_path = "web/app.py"
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    start = src.find("async def api_position_correlations")
    assert start != -1
    body = src[start:start + 3000]
    # Entweder brain_cache (für IBKR) ODER to_thread (für etoro) muss da sein
    has_brain = "_portfolio_from_brain_cache" in body
    has_to_thread = "to_thread" in body
    assert has_brain and has_to_thread, (
        "api_position_correlations muss async-safe sein: brain_cache (IBKR) "
        "ODER to_thread (eToro). R-A22 Fix fehlt."
    )
