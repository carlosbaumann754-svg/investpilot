"""v37f Tests: Push-Format pro Drift-Type + Brain-Snapshot-Schema.

Background: Am 08.05.2026 morgens 11h kamen ~12 Pushover-Alerts mit Body
"CASH_DRIFT:" und LEEREM Wert dahinter. Carlos's Cry-Wolf-Risk konkret.

Wurzel:
  scripts/ibkr_reconcile.py:maybe_alert formatierte
    f"• {d['type']}: {d.get('symbol', '')} {d.get('comment', '')[:80]}"
  Bei CASH_DRIFT-Drift gibt es weder 'symbol' noch 'comment' →
  Push-Body war "• CASH_DRIFT:  ".

Fix: _format_drift_for_alert() gibt pro Type die relevanten Felder mit.
"""
from scripts.ibkr_reconcile import _format_drift_for_alert


# ============================================================
# Format-Tests pro Drift-Type
# ============================================================

def test_format_cash_drift_includes_amounts():
    """CASH_DRIFT muss Bot-Cash, IBKR-Cash und Diff zeigen."""
    drift = {
        "type": "CASH_DRIFT",
        "bot_cash": 793154.18,
        "ibkr_cash": 794960.87,
        "diff_usd": 1806.69,
        "diff_pct": 0.227,
        "threshold_usd": 3974.80,
        "tolerance_pct_setting": 0.5,
    }
    msg = _format_drift_for_alert(drift)
    assert "CASH_DRIFT" in msg
    assert "793,154" in msg
    assert "794,960" in msg or "794,961" in msg  # rounding tolerance
    assert "1,807" in msg or "1,806" in msg
    assert "0.23%" in msg or "0.22%" in msg


def test_format_cash_drift_not_empty():
    """Regression: Push-Body darf nicht "CASH_DRIFT: " mit leerem Wert sein."""
    drift = {"type": "CASH_DRIFT", "bot_cash": 100, "ibkr_cash": 200,
             "diff_usd": 100, "diff_pct": 50.0}
    msg = _format_drift_for_alert(drift)
    # Body muss mehr als nur den Header enthalten
    assert len(msg) > len("• CASH_DRIFT: "), \
        f"Push-Body fuer CASH_DRIFT zu kurz/leer: {msg!r}"


def test_format_phantom_position_includes_symbol_and_qty():
    drift = {
        "type": "PHANTOM_POSITION",
        "symbol": "SLV",
        "qty": 597.0,
        "avg_cost": 70.17,
        "comment": "IBKR hat Position, Bot-trade-history kennt sie nicht im Lookback-Fenster.",
    }
    msg = _format_drift_for_alert(drift)
    assert "PHANTOM" in msg
    assert "SLV" in msg
    assert "597" in msg


def test_format_missed_fill_includes_symbol_and_comment():
    drift = {
        "type": "MISSED_FILL",
        "symbol": "COPPER",
        "comment": "Bot loggte SCANNER_BUY, IBKR-Executions zeigen kein matching BOT-Trade.",
    }
    msg = _format_drift_for_alert(drift)
    assert "MISSED_FILL" in msg
    assert "COPPER" in msg
    assert "SCANNER_BUY" in msg


def test_format_position_mismatch_includes_qtys():
    drift = {
        "type": "POSITION_MISMATCH",
        "symbol": "AAPL",
        "bot_qty": 100,
        "ibkr_qty": 95,
    }
    msg = _format_drift_for_alert(drift)
    assert "POSITION_MISMATCH" in msg
    assert "AAPL" in msg
    assert "100" in msg
    assert "95" in msg


def test_format_unknown_type_fallback_not_empty():
    """Unbekannter Type: Body soll trotzdem Info enthalten, nicht leer sein."""
    drift = {"type": "WEIRD_FUTURE_DRIFT", "weird_field": "weird_value", "amount": 42}
    msg = _format_drift_for_alert(drift)
    assert "WEIRD_FUTURE_DRIFT" in msg
    # Mindestens eines der non-empty fields sollte mit drin sein
    assert ("weird_value" in msg) or ("42" in msg), \
        f"Fallback-Format hat keine Drift-Felder mitgenommen: {msg!r}"


def test_format_truncates_long_messages():
    """Sehr lange comments sollen den Push-Body nicht aufblaehen."""
    drift = {
        "type": "MISSED_FILL",
        "symbol": "X",
        "comment": "A" * 500,
    }
    msg = _format_drift_for_alert(drift)
    assert len(msg) < 200, \
        f"Push-Body zu lang ({len(msg)} chars), Pushover-Limit ist 1024"


# ============================================================
# Brain-Snapshot-Schema-Test
# ============================================================

def test_brain_snapshot_has_reconcile_compatible_keys(monkeypatch, tmp_path):
    """Brain-Snapshot muss 'cash', 'timestamp', 'equity', 'positions_count' haben.

    Reconcile sucht diese Keys. Vorher hat Brain nur 'credit', 'total_value',
    'num_positions' geschrieben → Reconcile-or-Chain fiel auf 'credit' zurück.
    Funktionierte zufaellig. Fragil bei Schema-Drift.
    """
    from app import brain as brain_mod

    # Isoliere brain_state.json
    storage = {}
    monkeypatch.setattr(brain_mod, "load_brain", lambda: {
        "created": "2026-01-01",
        "total_runs": 0,
        "performance_snapshots": [],
    })
    monkeypatch.setattr(brain_mod, "save_brain", lambda b: storage.update(brain=b))

    # Fake-Portfolio (eToro-style) als input
    portfolio = {
        "credit": 100_000.0,
        "positions": [],
        "unrealizedPnL": 0.0,
        "_equity": 105_000.0,  # IBKR-style
    }

    brain_mod.record_snapshot(portfolio)

    snap = storage["brain"]["performance_snapshots"][-1]

    # v37f: Reconcile-kompatible Keys
    assert "cash" in snap, f"Snapshot fehlt 'cash'. Keys: {list(snap.keys())}"
    assert "timestamp" in snap, f"Snapshot fehlt 'timestamp'. Keys: {list(snap.keys())}"
    assert "equity" in snap, f"Snapshot fehlt 'equity'. Keys: {list(snap.keys())}"
    assert "positions_count" in snap, f"Snapshot fehlt 'positions_count'. Keys: {list(snap.keys())}"

    # Werte konsistent mit Legacy-Keys
    assert snap["cash"] == snap["credit"]
    assert snap["equity"] == snap["total_value"]
    assert snap["positions_count"] == snap["num_positions"]

    # Timestamp ist ISO-8601 mit Timezone
    from datetime import datetime
    parsed = datetime.fromisoformat(snap["timestamp"])
    assert parsed.tzinfo is not None
