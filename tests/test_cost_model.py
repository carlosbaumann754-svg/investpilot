"""Tests fuer das E2-Cost-Model (Corwin-Schultz + Almgren-Chriss)."""

from __future__ import annotations

import math
import pytest

from app import cost_model


# ---------- Corwin-Schultz Spread ----------

def test_corwin_schultz_returns_positive_bounded_spread():
    today = cost_model.OHLCDay(high=102.0, low=99.0, close=100.5)
    yest = cost_model.OHLCDay(high=101.0, low=98.0, close=99.5)
    spread = cost_model.estimate_corwin_schultz_spread(today, yest)
    assert spread >= cost_model.MIN_SPREAD_PCT
    assert spread <= cost_model.MAX_SPREAD_PCT
    assert math.isfinite(spread)


def test_corwin_schultz_clamps_extreme_inputs():
    # Pathological: low > high should still not blow up (clamped)
    today = cost_model.OHLCDay(high=100.0, low=100.0, close=100.0)
    yest = cost_model.OHLCDay(high=100.0, low=100.0, close=100.0)
    spread = cost_model.estimate_corwin_schultz_spread(today, yest)
    assert spread == cost_model.MIN_SPREAD_PCT


# ---------- Almgren-Chriss Volume Impact ----------

def test_almgren_chriss_increases_with_size():
    small = cost_model.almgren_chriss_impact(1_000, 10_000_000, 1.0)
    big = cost_model.almgren_chriss_impact(100_000, 10_000_000, 1.0)
    assert big > small


def test_almgren_chriss_capped_at_5pct():
    huge = cost_model.almgren_chriss_impact(10_000_000_000, 1_000, 50.0)
    assert huge <= 0.05


def test_almgren_chriss_zero_volume_safe():
    assert cost_model.almgren_chriss_impact(1000, 0, 1.0) == 0.0


# ---------- total_cost_pct ----------

@pytest.mark.parametrize("asset_class,expected_min,expected_max", [
    ("stocks", 0.0010, 0.0050),
    ("etf", 0.0005, 0.0030),
    ("crypto", 0.0020, 0.0100),
    ("forex", 0.0002, 0.0030),
])
def test_total_cost_per_class_in_realistic_range(asset_class, expected_min, expected_max):
    breakdown = cost_model.total_cost_pct(
        asset_class=asset_class,
        amount_usd=5000,
        days_held=5,
    )
    assert expected_min <= breakdown.total_pct <= expected_max, \
        f"{asset_class}: total={breakdown.total_pct} outside [{expected_min}, {expected_max}]"


def test_total_cost_components_sum_to_total():
    b = cost_model.total_cost_pct("stocks", 5000, 3)
    s = b.spread_pct + b.volume_impact_pct + b.slippage_buffer_pct + b.overnight_fee_pct
    assert abs(s - b.total_pct) < 1e-9


def test_total_cost_overnight_scales_with_days():
    b1 = cost_model.total_cost_pct("stocks", 5000, 1)
    b10 = cost_model.total_cost_pct("stocks", 5000, 10)
    # Overnight muss linear skalieren, andere Komponenten gleich bleiben
    assert b10.overnight_fee_pct > b1.overnight_fee_pct
    assert b10.spread_pct == b1.spread_pct
    assert b10.slippage_buffer_pct == b1.slippage_buffer_pct


# ---------- Backtester-Integration ----------

def test_backtester_calc_costs_legacy_fallback():
    """Ohne Symbol greift der Legacy-Pfad."""
    from app.backtester import _calc_costs, SPREAD_PCT, OVERNIGHT_FEE_PCT, SLIPPAGE_PCT
    cost = _calc_costs(150.0, 5)
    expected = SPREAD_PCT * 2 + OVERNIGHT_FEE_PCT * 5 + SLIPPAGE_PCT * 2
    assert abs(cost - expected) < 1e-9


def test_backtester_calc_costs_with_known_symbol():
    """Bekanntes Symbol -> cost_model-Pfad, deutlich detaillierter als Legacy."""
    from app.backtester import _calc_costs
    cost = _calc_costs(150.0, 5, symbol="AAPL", amount_usd=5000)
    # Plausibilitaet: zwischen 1bp und 100bps Round-Trip
    assert 0.0001 < cost < 0.01


def test_backtester_calc_costs_unknown_symbol_uses_stocks_default():
    """Unbekanntes Symbol -> cost_model mit Asset-Klasse 'stocks' als Default."""
    from app.backtester import _calc_costs
    cost = _calc_costs(150.0, 5, symbol="UNKNOWN_XYZ_999", amount_usd=5000)
    # Stocks-Default: ~0.21% Round-Trip
    assert 0.0010 < cost < 0.0050


# ---------- Calibrator ----------

def test_calibrator_handles_empty_history(tmp_path, monkeypatch):
    """Calibrator faellt graceful zurueck bei leerer Historie."""
    from app import cost_model_calibrator as cmc
    # Patch: load_json liefert leere Liste
    monkeypatch.setattr(
        "app.config_manager.load_json",
        lambda name: [] if name == "trade_history.json" else {},
    )
    report = cmc.calibrate(persist=False)
    assert report.total_fills_analyzed == 0
    assert report.slippage_buffer_pct_overrides == {}
    assert any("Keine Fills" in n for n in report.notes)


def test_calibrator_classcalibration_from_fills():
    from app.cost_model_calibrator import ClassCalibration, TradeFill
    fills = [
        TradeFill("AAPL", "stocks", intended_price=100.0, fill_price=100.05,
                  side="BUY", timestamp="2026-04-01T12:00:00+00:00"),
        TradeFill("AAPL", "stocks", intended_price=100.0, fill_price=100.10,
                  side="BUY", timestamp="2026-04-02T12:00:00+00:00"),
        TradeFill("AAPL", "stocks", intended_price=100.0, fill_price=100.03,
                  side="BUY", timestamp="2026-04-03T12:00:00+00:00"),
    ]
    cal = ClassCalibration.from_fills("stocks", fills)
    assert cal.sample_count == 3
    assert cal.median_slippage_pct == 0.05
    assert cal.is_reliable is False  # < 20 samples
