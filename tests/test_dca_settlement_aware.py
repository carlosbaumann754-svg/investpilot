"""Tests fuer Trade-Settlement-aware DCA-Detection (v37cd, Audit-F2).

Stellt sicher dass nach F1-Fix (credit=TotalCashValue) der Cash-Increase
nach SELL/CLOSE NICHT als Einzahlung interpretiert wird.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.risk_manager import detect_cash_deposit


@pytest.fixture
def cfg():
    return {
        "deposit_handling": {
            "dca_on_new_cash": True,
            "min_new_cash_trigger_usd": 500,
            "dca_spread_cycles": 5,
        }
    }


def _make_state(prev_cash, last_seen_at=None):
    return {
        "last_seen_cash_usd": prev_cash,
        "last_seen_at": last_seen_at or datetime.now().isoformat(),
        "active_plan": None,
    }


def test_real_deposit_triggers_dca(cfg, tmp_path, monkeypatch):
    """Echte Einzahlung (Cash steigt OHNE Sell-Trade) -> DCA aktiv."""
    state_data = _make_state(440000.0)
    trade_hist: list = []

    saved_state = {}

    def fake_load(name):
        if name.endswith("trade_history.json"):
            return trade_hist
        return state_data

    def fake_save(name, data):
        saved_state[name] = data

    with patch("app.risk_manager.load_json", side_effect=fake_load), \
         patch("app.risk_manager.save_json", side_effect=fake_save):
        # +1800 USD Einzahlung, keine Sells
        result = detect_cash_deposit(441800.0, cfg)

    assert result["dca_active"] is True
    assert result["remaining_cycles"] == 5


def test_sell_proceeds_subtracted_no_dca(cfg, monkeypatch):
    """SELL erhoeht Cash um Erloes -> KEIN DCA-Trigger."""
    state_data = _make_state(440000.0,
                             last_seen_at=(datetime.now() - timedelta(hours=1)).isoformat())
    trade_hist = [
        {
            "timestamp": datetime.now().isoformat(),
            "action": "MANUAL_SELL",
            "symbol": "ROKU",
            "avg_fill_price": 100.0,
            "qty": 100,  # 10000 USD Erloes
        }
    ]

    def fake_load(name):
        if name.endswith("trade_history.json"):
            return trade_hist
        return state_data

    with patch("app.risk_manager.load_json", side_effect=fake_load), \
         patch("app.risk_manager.save_json"):
        # Cash steigt um 10000 (genau Sell-Erloes), keine echte Einzahlung
        result = detect_cash_deposit(450000.0, cfg)

    assert result["dca_active"] is False, \
        "DCA darf NICHT triggern wenn Cash-Increase durch Sell erklaerbar"


def test_partial_deposit_after_sell(cfg, monkeypatch):
    """Mix: Sell-Erloes + echte Einzahlung -> DCA nur fuer den Einzahl-Teil."""
    state_data = _make_state(440000.0,
                             last_seen_at=(datetime.now() - timedelta(hours=1)).isoformat())
    trade_hist = [
        {
            "timestamp": datetime.now().isoformat(),
            "action": "STOP_LOSS_CLOSE",
            "symbol": "BA",
            "avg_fill_price": 200.0,
            "qty": 50,  # 10000 USD Erloes
        }
    ]

    def fake_load(name):
        if name.endswith("trade_history.json"):
            return trade_hist
        return state_data

    with patch("app.risk_manager.load_json", side_effect=fake_load), \
         patch("app.risk_manager.save_json"):
        # Cash steigt um 12000: 10000 Sell + 2000 Einzahlung
        result = detect_cash_deposit(452000.0, cfg)

    # 12000 - 10000 = 2000 > 500 trigger -> DCA aktiv mit 2000 Budget
    assert result["dca_active"] is True


def test_buy_does_not_affect_settlement_adjustment(cfg, monkeypatch):
    """BUY in Trade-History wird IGNORIERT (reduziert Cash, ist kein Cash-In)."""
    state_data = _make_state(440000.0,
                             last_seen_at=(datetime.now() - timedelta(hours=1)).isoformat())
    trade_hist = [
        {
            "timestamp": datetime.now().isoformat(),
            "action": "BUY",
            "symbol": "NVDA",
            "avg_fill_price": 500.0,
            "qty": 20,  # 10000 USD raus
        }
    ]

    def fake_load(name):
        if name.endswith("trade_history.json"):
            return trade_hist
        return state_data

    with patch("app.risk_manager.load_json", side_effect=fake_load), \
         patch("app.risk_manager.save_json"):
        # Echte Einzahlung 1000 obwohl BUY 10000 raus ging -> Cash netto -9000
        result = detect_cash_deposit(431000.0, cfg)

    # Delta ist negativ -> kein DCA
    assert result["dca_active"] is False


def test_dca_disabled_no_trigger(monkeypatch):
    """Wenn dca_on_new_cash=False -> nie aktiv."""
    cfg_off = {"deposit_handling": {"dca_on_new_cash": False}}
    result = detect_cash_deposit(500000.0, cfg_off)
    assert result["dca_active"] is False
    assert result["remaining_budget_usd"] == 500000.0


def test_close_action_with_pnl_proceeds(cfg, monkeypatch):
    """CLOSE-Action ohne avg_fill_price: Fallback invested+pnl_usd."""
    state_data = _make_state(440000.0,
                             last_seen_at=(datetime.now() - timedelta(hours=1)).isoformat())
    trade_hist = [
        {
            "timestamp": datetime.now().isoformat(),
            "action": "EARNINGS_BLACKOUT_CLOSE",
            "symbol": "ROKU",
            "invested": 8000,
            "pnl_usd": 200,
            # avg_fill_price + qty fehlen -> Fallback
        }
    ]

    def fake_load(name):
        if name.endswith("trade_history.json"):
            return trade_hist
        return state_data

    with patch("app.risk_manager.load_json", side_effect=fake_load), \
         patch("app.risk_manager.save_json"):
        # Cash steigt um 8200 (genau Erloes laut Fallback)
        result = detect_cash_deposit(448200.0, cfg)

    assert result["dca_active"] is False
