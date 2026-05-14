"""v37h+1 Cash-Reserve (14.05.2026) — Tests fuer Hybrid Floor + Pct.

Carlos's Entscheidung 14.05.2026: ersetzt W7-Entnahme-Planer durch
dauerhaften Cash-Buffer. Floor 500 CHF + 10%% Equity, beide ueber
Dashboard editierbar. Bot pausiert Buys wenn Cash < Reserve — KEINE
Notverkaeufe. Auto-Refill durch normale TP/SL/Dividenden-Sells.

Test-Pattern:
  - Hybrid-Berechnung: max(floor, pct*equity) bei verschiedenen Equity-Werten
  - Compute-Deployable: cash - reserve, geclamped auf 0
  - Update-Settings: Validation, Clamping, Persistence
  - Defensive: None, negative, garbage-Inputs
"""
from unittest.mock import patch

import pytest


# ============================================================
# get_required_reserve_chf — Hybrid-Berechnung
# ============================================================

@pytest.fixture
def mock_config():
    """Mock config_manager.load_config fuer kontrollierte Reserve-Settings.

    load_config() liefert deepcopy damit save_config() echt aufgerufen wird
    (sonst waere state == config-Dict-Referenz und save = no-op).
    """
    import copy
    state = {"risk_management": {"min_cash_reserve_chf": 500.0,
                                  "min_cash_reserve_pct": 0.10}}

    def fake_load():
        return copy.deepcopy(state)

    def fake_save(cfg):
        state.clear()
        state.update(copy.deepcopy(cfg))

    with patch("app.config_manager.load_config", side_effect=fake_load), \
         patch("app.config_manager.save_config", side_effect=fake_save):
        yield state


def test_floor_greift_bei_kleinem_portfolio(mock_config):
    """Equity 2'000 + Pct 10% = 200 < Floor 500 -> 500 greift."""
    from app.cash_reserve import get_required_reserve_chf
    assert get_required_reserve_chf(2000.0) == 500.0


def test_pct_uebernimmt_bei_groesserem_portfolio(mock_config):
    """Equity 10'000 + Pct 10% = 1000 > Floor 500 -> 1000 greift."""
    from app.cash_reserve import get_required_reserve_chf
    assert get_required_reserve_chf(10_000.0) == 1000.0


def test_pct_skaliert_linear(mock_config):
    """Equity 50'000 -> 5000 CHF Reserve."""
    from app.cash_reserve import get_required_reserve_chf
    assert get_required_reserve_chf(50_000.0) == 5000.0


def test_break_even_floor_pct(mock_config):
    """Genau am Break-Even: Equity 5'000 + Pct 10% = 500 = Floor 500."""
    from app.cash_reserve import get_required_reserve_chf
    assert get_required_reserve_chf(5_000.0) == 500.0


def test_zero_equity_returns_floor(mock_config):
    """Equity 0 -> nur Floor."""
    from app.cash_reserve import get_required_reserve_chf
    assert get_required_reserve_chf(0.0) == 500.0


def test_negative_equity_safe(mock_config):
    """Defensiv: negativer Equity (sollte nicht passieren) -> 0."""
    from app.cash_reserve import get_required_reserve_chf
    assert get_required_reserve_chf(-100.0) == 0.0


def test_garbage_equity_safe(mock_config):
    """Defensiv: None / string -> 0."""
    from app.cash_reserve import get_required_reserve_chf
    assert get_required_reserve_chf(None) == 0.0
    assert get_required_reserve_chf("nope") == 0.0


def test_feature_off_when_both_zero(mock_config):
    """Floor=0 + Pct=0 -> Reserve 0 (Feature deaktiviert)."""
    from app.cash_reserve import get_required_reserve_chf
    mock_config["risk_management"]["min_cash_reserve_chf"] = 0
    mock_config["risk_management"]["min_cash_reserve_pct"] = 0
    assert get_required_reserve_chf(10_000.0) == 0.0


def test_only_pct_active(mock_config):
    """Floor=0 + Pct=0.05 -> nur prozentual."""
    from app.cash_reserve import get_required_reserve_chf
    mock_config["risk_management"]["min_cash_reserve_chf"] = 0
    mock_config["risk_management"]["min_cash_reserve_pct"] = 0.05
    assert get_required_reserve_chf(10_000.0) == 500.0


def test_only_floor_active(mock_config):
    """Floor=1000 + Pct=0 -> nur Floor."""
    from app.cash_reserve import get_required_reserve_chf
    mock_config["risk_management"]["min_cash_reserve_chf"] = 1000
    mock_config["risk_management"]["min_cash_reserve_pct"] = 0
    assert get_required_reserve_chf(50_000.0) == 1000.0


# ============================================================
# compute_deployable_cash_chf — Cash minus Reserve
# ============================================================

def test_deployable_normal_case(mock_config):
    """Cash 1500, Reserve 500 -> Deployable 1000."""
    from app.cash_reserve import compute_deployable_cash_chf
    assert compute_deployable_cash_chf(1500.0, 2000.0) == 1000.0


def test_deployable_unter_reserve_returns_zero(mock_config):
    """Cash 300, Reserve 500 -> 0 (Bot pausiert, kein negativ)."""
    from app.cash_reserve import compute_deployable_cash_chf
    assert compute_deployable_cash_chf(300.0, 2000.0) == 0.0


def test_deployable_exact_reserve_returns_zero(mock_config):
    """Cash = Reserve -> 0 (nichts disponibel, aber kein Block)."""
    from app.cash_reserve import compute_deployable_cash_chf
    assert compute_deployable_cash_chf(500.0, 2000.0) == 0.0


def test_deployable_negative_cash_safe(mock_config):
    """Defensiv: negatives Cash (Margin-Konto Loan) -> 0."""
    from app.cash_reserve import compute_deployable_cash_chf
    assert compute_deployable_cash_chf(-100.0, 2000.0) == 0.0


# ============================================================
# Sicherheits-Clamping (Tippfehler-Schutz)
# ============================================================

def test_clamping_floor_too_high(mock_config):
    """Floor 999'999 (Tippfehler) -> auf MAX_FLOOR_CHF geclamped."""
    from app.cash_reserve import get_required_reserve_chf, MAX_FLOOR_CHF
    mock_config["risk_management"]["min_cash_reserve_chf"] = 999_999
    # 100k Equity * 10%% = 10k, MAX_FLOOR = 100k -> Floor (geclamped) > Pct
    assert get_required_reserve_chf(100_000.0) == MAX_FLOOR_CHF


def test_clamping_pct_too_high(mock_config):
    """Pct 0.99 (= 99%, faktisch Bot abgeschaltet) -> auf MAX_PCT geclamped."""
    from app.cash_reserve import get_required_reserve_chf, MAX_PCT
    mock_config["risk_management"]["min_cash_reserve_pct"] = 0.99
    # 1000 Equity * MAX_PCT(0.5) = 500
    assert get_required_reserve_chf(1000.0) == max(500.0, MAX_PCT * 1000.0)


def test_clamping_negative_values(mock_config):
    """Negative Werte -> auf 0 geclamped."""
    from app.cash_reserve import get_required_reserve_chf
    mock_config["risk_management"]["min_cash_reserve_chf"] = -500
    mock_config["risk_management"]["min_cash_reserve_pct"] = -0.05
    assert get_required_reserve_chf(10_000.0) == 0.0


def test_garbage_config_uses_defaults(mock_config):
    """Garbage-Strings -> fallback auf Defaults (500 + 0.10)."""
    from app.cash_reserve import get_required_reserve_chf
    mock_config["risk_management"]["min_cash_reserve_chf"] = "nope"
    mock_config["risk_management"]["min_cash_reserve_pct"] = "garbage"
    # Defaults: floor 500, pct 0.10 -> bei equity 10k -> max(500, 1000) = 1000
    assert get_required_reserve_chf(10_000.0) == 1000.0


# ============================================================
# update_reserve_settings — Validation + Persistence
# ============================================================

def test_update_floor_persists(mock_config):
    from app.cash_reserve import update_reserve_settings
    update_reserve_settings(floor_chf=750.0)
    assert mock_config["risk_management"]["min_cash_reserve_chf"] == 750.0


def test_update_pct_persists(mock_config):
    from app.cash_reserve import update_reserve_settings
    update_reserve_settings(pct=0.15)
    assert mock_config["risk_management"]["min_cash_reserve_pct"] == 0.15


def test_update_pct_auto_detects_percent_input(mock_config):
    """User gibt 15 (= 15%) statt 0.15 -> auto-convert."""
    from app.cash_reserve import update_reserve_settings
    update_reserve_settings(pct=15)
    assert mock_config["risk_management"]["min_cash_reserve_pct"] == 0.15


def test_update_rejects_pct_over_max(mock_config):
    from app.cash_reserve import update_reserve_settings
    with pytest.raises(ValueError, match="ausserhalb"):
        update_reserve_settings(pct=0.99)


def test_update_rejects_floor_over_max(mock_config):
    from app.cash_reserve import update_reserve_settings
    with pytest.raises(ValueError, match="ausserhalb"):
        update_reserve_settings(floor_chf=10_000_000)


def test_update_rejects_garbage(mock_config):
    from app.cash_reserve import update_reserve_settings
    with pytest.raises(ValueError, match="keine Zahl"):
        update_reserve_settings(floor_chf="nope")


def test_update_noop_when_both_none(mock_config):
    """Beide None -> kein Persistence-Call."""
    from app.cash_reserve import update_reserve_settings
    before = dict(mock_config["risk_management"])
    update_reserve_settings()
    assert mock_config["risk_management"] == before


# ============================================================
# get_status — Dashboard-Snapshot
# ============================================================

def test_status_without_live_portfolio(mock_config):
    """Ohne cash/equity -> nur Config-Soll."""
    from app.cash_reserve import get_status
    s = get_status()
    assert s["min_cash_reserve_chf"] == 500.0
    assert s["min_cash_reserve_pct"] == 0.10
    assert "required_reserve_chf" not in s


def test_status_with_live_portfolio_filled(mock_config):
    """Cash > Reserve -> reserve_filled True."""
    from app.cash_reserve import get_status
    s = get_status(available_cash_chf=2000, equity_chf=10000)
    assert s["required_reserve_chf"] == 1000.0
    assert s["available_cash_chf"] == 2000.0
    assert s["deployable_cash_chf"] == 1000.0
    assert s["reserve_filled"] is True
    assert s["binding"] == "pct"  # 0.10*10000=1000 > Floor 500
    assert s["fill_pct"] == 1.0   # capped


def test_status_with_live_portfolio_unter_soll(mock_config):
    """Cash < Reserve -> reserve_filled False + Deployable 0."""
    from app.cash_reserve import get_status
    s = get_status(available_cash_chf=300, equity_chf=10000)
    assert s["reserve_filled"] is False
    assert s["deployable_cash_chf"] == 0.0
    assert 0 < s["fill_pct"] < 1.0


def test_status_binding_floor_at_small_portfolio(mock_config):
    """Small portfolio -> Floor greift, binding=floor."""
    from app.cash_reserve import get_status
    s = get_status(available_cash_chf=1000, equity_chf=2000)
    assert s["binding"] == "floor"
    assert s["required_reserve_chf"] == 500.0


# ============================================================
# Regression: keine WFO-Lock-Kollision
# ============================================================

def test_min_cash_reserve_not_in_wfo_locks():
    """Carlos darf Reserve frei anpassen — darf nicht in WFO_LOCKED_KEYS sein."""
    from app.wfo_lock import LOCKED_KEYS
    locked_paths = {path for _, path, _ in LOCKED_KEYS}
    assert "risk_management.min_cash_reserve_chf" not in locked_paths
    assert "risk_management.min_cash_reserve_pct" not in locked_paths
