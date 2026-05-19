"""Tests fuer R-A10 Symbol-Konzentrations-Hard-Cap (Sprint-Tag-9, 19.05.2026).

Anlass: 18.05.2026 5x SCANNER_BUY auf OIL/USO (~$267k Brutto-Exposure)
trotz existing_symbols-Filter in trader.py. Symbol-Konzentrations-Check
als Defense-in-Depth-Layer der unabhaengig greift.

Block-Strategie (Option D, Carlos's Wahl 19.05.):
  - Hart blocken + Pushover NUR bei pushover_threshold_per_day Blocks (Default 3)
  - Counter in risk_state.json mit Datum-Key (implicit daily reset)
"""

import pytest
from unittest.mock import patch, MagicMock


# ============================================================
# 1. Disabled-Toggle: durchlaufen ohne Check
# ============================================================

def test_concentration_disabled_passes_through():
    """Wenn enabled=False: jeder Buy erlaubt unabhaengig von Positions."""
    from app.risk_manager import check_symbol_concentration

    cfg = {"risk_management": {"symbol_concentration": {"enabled": False}}}
    positions = [{"symbol": "OIL", "invested": 50000}] * 10  # 10x OIL!
    allowed, reason = check_symbol_concentration(
        "OIL", 5000, positions, total_portfolio_value=100000, config=cfg)
    assert allowed is True
    assert "disabled" in reason.lower()


# ============================================================
# 2. Default-Case: erste Position erlaubt
# ============================================================

def test_first_position_allowed():
    """Kein bestehender Trade auf Symbol → Buy darf durch."""
    from app.risk_manager import check_symbol_concentration

    cfg = {"risk_management": {"symbol_concentration": {
        "enabled": True, "max_positions_per_symbol": 1,
        "max_exposure_per_symbol_pct": 15,
    }}}
    positions = []  # leer
    allowed, reason = check_symbol_concentration(
        "OIL", 5000, positions, total_portfolio_value=100000, config=cfg)
    assert allowed is True
    assert reason == "OK"


# ============================================================
# 3. Position-Count-Block (Kern-Schutz)
# ============================================================

def test_second_position_blocked_by_count():
    """max_positions_per_symbol=1 + existing=1 → Buy blockiert."""
    from app.risk_manager import check_symbol_concentration

    cfg = {"risk_management": {"symbol_concentration": {
        "enabled": True, "max_positions_per_symbol": 1,
        "max_exposure_per_symbol_pct": 100,  # off
    }}}
    positions = [{"symbol": "AAPL", "invested": 5000}]
    allowed, reason = check_symbol_concentration(
        "AAPL", 5000, positions, total_portfolio_value=100000, config=cfg)
    assert allowed is False
    assert "max_positions_per_symbol" in reason


# ============================================================
# 4. Bidirektional OIL/USO (Kern-Anlass-Case)
# ============================================================

def test_oil_uso_bidirectional_block():
    """Bot-Symbol 'OIL' candidate + IBKR-Position 'USO' → Block.

    Genau der Pattern vom 18.05.2026: BUY-Trades mit 'OIL' (Bot-intern),
    Position vom IBKR-Reconcile mit symbol='USO'. expand_symbol_for_match
    erkennt beide als gleich.
    """
    from app.risk_manager import check_symbol_concentration

    cfg = {"risk_management": {"symbol_concentration": {
        "enabled": True, "max_positions_per_symbol": 1,
        "max_exposure_per_symbol_pct": 100,
    }}}
    # IBKR-Position liefert symbol="USO" (ETF-Ticker)
    positions = [{"symbol": "USO", "invested": 53000}]
    # Scanner-Candidate hat symbol="OIL" (Bot-Universum-Key)
    allowed, reason = check_symbol_concentration(
        "OIL", 53000, positions, total_portfolio_value=400000, config=cfg)
    assert allowed is False
    assert "max_positions_per_symbol" in reason


def test_oil_uso_reverse_block():
    """Reverse: Candidate='USO', Position='OIL' soll auch blocken."""
    from app.risk_manager import check_symbol_concentration

    cfg = {"risk_management": {"symbol_concentration": {
        "enabled": True, "max_positions_per_symbol": 1,
        "max_exposure_per_symbol_pct": 100,
    }}}
    positions = [{"symbol": "OIL", "invested": 53000}]
    allowed, reason = check_symbol_concentration(
        "USO", 53000, positions, total_portfolio_value=400000, config=cfg)
    assert allowed is False


# ============================================================
# 5. Exposure-Pct-Block (Cap auf Portfolio-Anteil)
# ============================================================

def test_exposure_pct_block():
    """max_exposure_per_symbol_pct=15% + Buy wuerde 20% → Block."""
    from app.risk_manager import check_symbol_concentration

    cfg = {"risk_management": {"symbol_concentration": {
        "enabled": True, "max_positions_per_symbol": 99,  # count off
        "max_exposure_per_symbol_pct": 15,
    }}}
    positions = []
    # Buy von 20'000 bei Portfolio 100'000 = 20% > 15%
    allowed, reason = check_symbol_concentration(
        "AAPL", 20000, positions, total_portfolio_value=100000, config=cfg)
    assert allowed is False
    assert "max_exposure_per_symbol_pct" in reason


def test_exposure_pct_just_under_limit():
    """14.9% Exposure < 15% limit → erlaubt."""
    from app.risk_manager import check_symbol_concentration

    cfg = {"risk_management": {"symbol_concentration": {
        "enabled": True, "max_positions_per_symbol": 99,
        "max_exposure_per_symbol_pct": 15,
    }}}
    positions = []
    allowed, _ = check_symbol_concentration(
        "AAPL", 14900, positions, total_portfolio_value=100000, config=cfg)
    assert allowed is True


# ============================================================
# 6. Unknown Symbol (Stock ohne Override)
# ============================================================

def test_unknown_symbol_passthrough():
    """Stock ohne ibkr_override (z.B. AAPL): nur direkter Symbol-Match.

    AAPL hat keinen Override, MSFT-Position blockt also nicht AAPL-Buy.
    """
    from app.risk_manager import check_symbol_concentration

    cfg = {"risk_management": {"symbol_concentration": {
        "enabled": True, "max_positions_per_symbol": 1,
        "max_exposure_per_symbol_pct": 100,
    }}}
    positions = [{"symbol": "MSFT", "invested": 5000}]
    allowed, _ = check_symbol_concentration(
        "AAPL", 5000, positions, total_portfolio_value=100000, config=cfg)
    assert allowed is True


def test_empty_symbol_passes():
    """Defensive: None/empty symbol → kein Block (defensive degrade)."""
    from app.risk_manager import check_symbol_concentration

    cfg = {"risk_management": {"symbol_concentration": {
        "enabled": True, "max_positions_per_symbol": 1,
    }}}
    allowed, reason = check_symbol_concentration(
        None, 5000, [], total_portfolio_value=100000, config=cfg)
    assert allowed is True
    assert "no symbol" in reason.lower()


# ============================================================
# 7. Counter-State im risk_state.json
# ============================================================

def test_record_block_counter_increments(tmp_path, monkeypatch):
    """record_concentration_block schreibt Counter in risk_state.json."""
    from app import risk_manager

    state_store = {}

    def fake_load(filename):
        return state_store.get(filename)

    def fake_save(filename, data):
        state_store[filename] = data

    monkeypatch.setattr(risk_manager, "load_json", fake_load)
    monkeypatch.setattr(risk_manager, "save_json", fake_save)

    cfg = {"risk_management": {"symbol_concentration": {
        "pushover_threshold_per_day": 3,
    }}}

    # 3 Blocks fuer OIL hintereinander
    with patch("app.alerts.send_pushover"):  # Pushover-Mock
        c1, t1 = risk_manager.record_concentration_block("OIL", "reason1", cfg)
        c2, t2 = risk_manager.record_concentration_block("OIL", "reason2", cfg)
        c3, t3 = risk_manager.record_concentration_block("OIL", "reason3", cfg)

    assert (c1, c2, c3) == (1, 2, 3)
    # Pushover NUR beim 3. Block (exact-threshold-match)
    assert (t1, t2, t3) == (False, False, True)


def test_pushover_only_at_threshold_exact(tmp_path, monkeypatch):
    """Pushover feuert EXACT bei threshold, nicht bei spaeteren Blocks."""
    from app import risk_manager

    state_store = {}
    monkeypatch.setattr(risk_manager, "load_json",
                        lambda fn: state_store.get(fn))
    monkeypatch.setattr(risk_manager, "save_json",
                        lambda fn, d: state_store.update({fn: d}))

    cfg = {"risk_management": {"symbol_concentration": {
        "pushover_threshold_per_day": 2,
    }}}

    pushover_calls = []
    with patch("app.alerts.send_pushover",
               side_effect=lambda *a, **kw: pushover_calls.append((a, kw))):
        for i in range(5):
            risk_manager.record_concentration_block("OIL", f"reason{i}", cfg)

    # Genau 1 Pushover-Call (beim 2. Block)
    assert len(pushover_calls) == 1


def test_separate_symbols_separate_counters(monkeypatch):
    """Counter sind per-Symbol getrennt."""
    from app import risk_manager

    state_store = {}
    monkeypatch.setattr(risk_manager, "load_json",
                        lambda fn: state_store.get(fn))
    monkeypatch.setattr(risk_manager, "save_json",
                        lambda fn, d: state_store.update({fn: d}))

    cfg = {"risk_management": {"symbol_concentration": {
        "pushover_threshold_per_day": 3,
    }}}

    with patch("app.alerts.send_pushover"):
        c_oil, _ = risk_manager.record_concentration_block("OIL", "r", cfg)
        c_msft, _ = risk_manager.record_concentration_block("MSFT", "r", cfg)
        c_oil2, _ = risk_manager.record_concentration_block("OIL", "r", cfg)

    assert c_oil == 1
    assert c_msft == 1  # separater Counter
    assert c_oil2 == 2  # OIL-Counter inkrementiert
