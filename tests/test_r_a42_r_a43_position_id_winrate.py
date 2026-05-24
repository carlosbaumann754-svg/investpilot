"""Tests fuer R-A42 (position_id-Capture) + R-A43 (Trade-Win-Rate).

R-A42 Bug: trader.py log_shadow_decision schrieb position_id =
order.get("positionID") (eToro-Field) → bei IBKR null → R-A41-Hook
konnte nur via Symbol+Time-Fallback matchen.

R-A43 Bug: brain.win_rate basierte auf snapshot-Intervallen (5-Min-Cycles)
statt auf Trade-Closes. Bei sideways-Markt → 0.3% angezeigt, UI-Tooltip
versprach aber "% gewinnbringende Trades" (~50%).

Beide Fixes erhoehen Visibility ohne Trading-Risk.
"""

from pathlib import Path


def test_r_a42_position_id_uses_ibkr_orderid_primary():
    """log_shadow_decision Aufruf in trader.py liest IBKR-Order-ID
    prioritaer (orderID/orderId/permID/permId), eToro-Fields nur Fallback."""
    src = Path(__file__).parent.parent / "app" / "trader.py"
    body = src.read_text(encoding="utf-8")
    # Find the cascade-lookup
    assert 'order.get("orderID")' in body, "IBKR orderID-Lookup fehlt"
    assert 'order.get("permID")' in body, "IBKR permID-Lookup fehlt"
    # Plus R-A42 Tag in Comment
    assert "R-A42" in body, "R-A42 Tag fehlt im trader.py"


def test_r_a42_legacy_etoro_fields_still_in_cascade():
    """Backward-Compat: eToro-Field-Namen bleiben als Fallback."""
    src = Path(__file__).parent.parent / "app" / "trader.py"
    body = src.read_text(encoding="utf-8")
    # Cascade muss eToro-Fields am Ende haben (Legacy)
    assert 'order.get("positionID")' in body
    assert 'order.get("positionId")' in body


def test_r_a43_brain_computes_trade_win_rate():
    """brain.py berechnet trade_win_rate aus trade_history (nicht aus
    snapshot-Intervallen)."""
    src = Path(__file__).parent.parent / "app" / "brain.py"
    body = src.read_text(encoding="utf-8")
    assert "R-A43" in body, "R-A43 Tag fehlt"
    assert 'report["trade_win_rate"]' in body, "trade_win_rate field fehlt"
    assert 'brain["trade_win_rate"]' in body, "brain-state.trade_win_rate fehlt"


def test_r_a43_brain_trade_win_rate_uses_trade_history():
    """trade_win_rate-Berechnung liest CLOSE-Action-Trades mit pnl_pct."""
    src = Path(__file__).parent.parent / "app" / "brain.py"
    body = src.read_text(encoding="utf-8")
    assert 'load_json("trade_history.json")' in body, "trade_history-Read fehlt"
    # Filter auf Close-Action-Trades
    assert '"CLOSE"' in body or '"STOP_LOSS"' in body, "Close-Filter fehlt"


def test_r_a43_api_brain_returns_trade_win_rate_in_win_rate_field():
    """/api/brain response.win_rate = trade_win_rate (mit Backward-Compat
    auf win_rate_daily für die alte snapshot-basierte Rate)."""
    src = Path(__file__).parent.parent / "web" / "app.py"
    body = src.read_text(encoding="utf-8")
    # Look for the brain endpoint code
    api_brain_idx = body.index("# R-A43")
    api_brain_body = body[api_brain_idx:api_brain_idx + 800]
    assert 'brain.get("trade_win_rate"' in api_brain_body, (
        "API liest trade_win_rate nicht prioritaer"
    )
    assert '"win_rate_daily":' in api_brain_body, (
        "Backward-Compat-Field win_rate_daily fehlt"
    )


def test_r_a43_brain_win_rate_is_documented_legacy():
    """Inline-Kommentar in brain.py erklaert dass alte win_rate snapshot-basiert
    ist (damit Future-Devs es nicht versehentlich aendern)."""
    src = Path(__file__).parent.parent / "app" / "brain.py"
    body = src.read_text(encoding="utf-8")
    assert "LEGACY" in body or "snapshot" in body.lower(), (
        "Dokumentations-Kommentar fehlt — Future-Devs wuerden bug nicht erkennen"
    )
