"""R-B11 (07.06.2026) — Live-Trading-Behavior-Fixes aus dem LLM-Deep-Audit (Batch-2).

E1: Short-pnl_pct-Vorzeichen (Stop-Loss feuert auch bei Shorts korrekt).
E2: Voller/Risk-Close wird nicht von einem pending Partial-Close geblockt.
R1: Regime-Filter blockt bei stale Market-Context (kein blindes Handeln).
R5: Kelly darf den Single-Position-Cap nur SENKEN, nicht ueberschreiben.
C4: Drawdown-Tracking re-baselined bei Account-/Currency-Wechsel (Cutover).
"""
import pytest


# ── E1: Short-sicheres pnl_pct ──────────────────────────────────────────────
def test_e1_pnl_pct_sign_long_and_short():
    from app.ibkr_client import _position_pnl_pct
    # Long-Gewinn: qty>0, unreal>0 -> +
    assert _position_pnl_pct(100, 10.0, 50.0) > 0
    # Long-Verlust: unreal<0 -> -
    assert _position_pnl_pct(100, 10.0, -50.0) < 0
    # Short-VERLUST: qty<0, unreal<0 -> MUSS negativ sein (sonst SL feuert nie!)
    assert _position_pnl_pct(-100, 10.0, -50.0) < 0
    # Short-Gewinn: qty<0, unreal>0 -> positiv
    assert _position_pnl_pct(-100, 10.0, 50.0) > 0


def test_e1_magnitude_symmetric_long_short():
    from app.ibkr_client import _position_pnl_pct
    # Gleicher |qty|, gleicher unreal -> gleiche Magnitude unabh. vom Vorzeichen
    assert _position_pnl_pct(-100, 10.0, -50.0) == _position_pnl_pct(100, 10.0, -50.0)


# ── E2: SL nach Partial nicht geblockt ──────────────────────────────────────
@pytest.fixture
def trader_pending(monkeypatch):
    import app.trader as tr
    store = {}
    monkeypatch.setattr(tr, "load_json", lambda f: store.get(f))
    monkeypatch.setattr(tr, "save_json", lambda f, d: store.__setitem__(f, d))
    monkeypatch.setattr(tr, "_cleanup_pending_closes", lambda: None)
    return tr, store


def test_e2_full_close_after_partial_allowed(trader_pending):
    tr, _ = trader_pending
    client = object()  # kein _get_ib -> Live-Check uebersprungen
    tr._track_pending_close(999, {"ok": 1}, is_partial=True)
    # Voller/Risk-Close (is_partial=False) trotz pending Partial -> NICHT blocken
    skip, _reason = tr._check_close_idempotent(client, 999, is_partial=False)
    assert skip is False


def test_e2_partial_after_partial_still_blocked(trader_pending):
    tr, _ = trader_pending
    client = object()
    tr._track_pending_close(888, {"ok": 1}, is_partial=True)
    skip, _reason = tr._check_close_idempotent(client, 888, is_partial=True)
    assert skip is True


def test_e2_full_after_full_still_blocked(trader_pending):
    tr, _ = trader_pending
    client = object()
    tr._track_pending_close(777, {"ok": 1}, is_partial=False)
    skip, _reason = tr._check_close_idempotent(client, 777, is_partial=False)
    assert skip is True


# ── R1: Regime blockt bei stale Context ─────────────────────────────────────
def test_r1_stale_context_blocks_buys(monkeypatch):
    import app.market_context as mc
    monkeypatch.setattr(mc, "get_current_context", lambda: {"_stale": True})
    config = {"regime_filter": {}, "macro_signals": {"enabled": False}}
    buy_allowed, reason, data = mc.check_regime_filter(config)
    assert buy_allowed is False
    assert data.get("stale") is True


def test_r1_fresh_context_not_force_blocked(monkeypatch):
    import app.market_context as mc
    # Fresh, neutrale Indikatoren -> NICHT durch die Stale-Regel geblockt
    monkeypatch.setattr(mc, "get_current_context",
                        lambda: {"_stale": False, "vix_level": 15,
                                 "fear_greed_index": 55})
    config = {"regime_filter": {}, "macro_signals": {"enabled": False}}
    buy_allowed, reason, data = mc.check_regime_filter(config)
    assert buy_allowed is True


# ── R5: Kelly darf Cap nur senken ───────────────────────────────────────────
def test_r5_kelly_cannot_raise_single_position_cap(monkeypatch):
    import app.risk_manager as rm
    cfg = {"risk_management": {"max_single_position_pct": 10,
                               "risk_per_trade_pct": 2,
                               "max_single_trade_pct_of_portfolio": 90}}
    # Kelly will 15% -> darf den 10%-Cap NICHT anheben
    monkeypatch.setattr(rm, "_get_kelly_recommendation", lambda c: (0.04, 15.0))
    size = rm.calculate_position_size(100000, -3, cfg)
    assert size <= 100000 * 0.10 + 1, f"Kelly hat den 10%-Cap angehoben: {size}"


def test_r5_kelly_can_lower_cap(monkeypatch):
    import app.risk_manager as rm
    cfg = {"risk_management": {"max_single_position_pct": 10,
                               "risk_per_trade_pct": 2,
                               "max_single_trade_pct_of_portfolio": 90}}
    # Kelly will 5% -> senkt den Cap auf 5%
    monkeypatch.setattr(rm, "_get_kelly_recommendation", lambda c: (0.04, 5.0))
    size = rm.calculate_position_size(100000, -3, cfg)
    assert abs(size - 100000 * 0.05) < 50, f"5%-Kelly-Cap nicht angewandt: {size}"


# ── C4: Drawdown re-baseline bei Account/Currency-Wechsel ────────────────────
def test_c4_rebaseline_on_currency_switch(monkeypatch):
    import app.risk_manager as rm
    state = dict(rm._load_risk_state())  # frischer Default
    monkeypatch.setattr(rm, "_load_risk_state", lambda: state)
    monkeypatch.setattr(rm, "_save_risk_state", lambda s: state.update(s))

    # Tag 1, USD-Konto: Baseline 10000
    rm.update_portfolio_tracking(10000, account_key="USD")
    assert state["daily_start_value"] == 10000
    # Gleicher Tag, USD: -5%
    rm.update_portfolio_tracking(9500, account_key="USD")
    assert state["daily_pnl_pct"] == -5.0
    # Currency-Wechsel USD->CHF: RE-BASELINE statt -15%-Sprung
    rm.update_portfolio_tracking(8500, account_key="CHF")
    assert state["daily_start_value"] == 8500
    assert state["daily_pnl_pct"] == 0.0
    assert state["account_key"] == "CHF"
