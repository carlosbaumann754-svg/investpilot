"""
Tests fuer InvestPilot Risk Manager.
Deckt Position Sizing, Concentration Score und Drawdown-Logik ab.
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# POSITION SIZING
# ============================================================

class TestPositionSizing:
    """Tests fuer calculate_position_size()."""

    def _make_config(self, risk_pct=2.0, max_trade=5000, max_pos_pct=10):
        return {
            "risk_management": {
                "risk_per_trade_pct": risk_pct,
                "max_single_position_pct": max_pos_pct,
            },
            "demo_trading": {
                "max_single_trade_usd": max_trade,
            },
        }

    def test_basic_sizing(self):
        from app.risk_manager import calculate_position_size
        # max_pos_pct=100 deaktiviert Portfolio-Cap fuer diesen Test
        config = self._make_config(risk_pct=2.0, max_trade=500000, max_pos_pct=100)
        # $100k * 2% / 3% = $2000 risk / 0.03 = $66,666.67
        result = calculate_position_size(100000, -3, config)
        assert abs(result - 66666.67) < 1, f"Expected ~66666.67, got {result}"

    def test_max_single_trade_cap(self):
        from app.risk_manager import calculate_position_size
        config = self._make_config(risk_pct=2.0, max_trade=1000)
        # Without cap would be ~2222, but max_single_trade=1000
        result = calculate_position_size(100000, -3, config)
        assert result <= 1000, f"Should be capped at 1000, got {result}"

    def test_max_portfolio_pct_cap(self):
        from app.risk_manager import calculate_position_size
        config = self._make_config(risk_pct=5.0, max_trade=50000, max_pos_pct=10)
        # 5% risk / 1% SL = 500% -> huge, but capped at 10% of portfolio
        result = calculate_position_size(10000, -1, config)
        assert result <= 1000, f"Should be capped at 10% of 10k=1000, got {result}"

    def test_zero_stop_loss_fallback(self):
        from app.risk_manager import calculate_position_size
        config = self._make_config()
        # SL=0 should use -3% fallback, not divide by zero
        result = calculate_position_size(100000, 0, config)
        assert result > 0, "Should not crash on SL=0"

    def test_zero_portfolio_value(self):
        from app.risk_manager import calculate_position_size
        config = self._make_config()
        result = calculate_position_size(0, -3, config)
        assert result == 0, f"Zero portfolio should give zero position, got {result}"


# ============================================================
# DYNAMIC POSITION SIZING
# ============================================================

class TestDynamicPositionSizing:
    """Tests fuer calculate_dynamic_position_size()."""

    def _make_config(self):
        return {
            "risk_management": {
                "risk_per_trade_pct": 2.0,
                "max_single_position_pct": 10,
                "dynamic_sizing_reference_score": 30,
            },
            "demo_trading": {"max_single_trade_usd": 50000},
        }

    def test_high_score_scales_up(self):
        from app.risk_manager import calculate_dynamic_position_size, calculate_position_size
        config = self._make_config()
        base = calculate_position_size(100000, -3, config)
        dynamic = calculate_dynamic_position_size(100000, -3, 45, config)
        # Score 45 / ref 30 = 1.5x
        assert dynamic > base, f"High score should increase position: {dynamic} vs {base}"
        assert dynamic == round(base * 1.5, 2), f"Expected 1.5x base"

    def test_low_score_scales_down(self):
        from app.risk_manager import calculate_dynamic_position_size, calculate_position_size
        config = self._make_config()
        base = calculate_position_size(100000, -3, config)
        dynamic = calculate_dynamic_position_size(100000, -3, 15, config)
        # Score 15 / ref 30 = 0.5x
        assert dynamic < base, f"Low score should decrease position: {dynamic} vs {base}"
        assert dynamic == round(base * 0.5, 2), f"Expected 0.5x base"

    def test_scale_capped_at_150pct(self):
        from app.risk_manager import calculate_dynamic_position_size, calculate_position_size
        config = self._make_config()
        base = calculate_position_size(100000, -3, config)
        dynamic = calculate_dynamic_position_size(100000, -3, 100, config)
        # Score 100 / ref 30 = 3.33 -> capped at 1.5
        assert dynamic == round(base * 1.5, 2), f"Should cap at 1.5x, got {dynamic}"


# ============================================================
# CONCENTRATION SCORE (HERFINDAHL-INDEX)
# ============================================================

class TestConcentrationScore:
    """Tests fuer get_portfolio_concentration_score()."""

    def test_empty_portfolio(self):
        from app.risk_manager import get_portfolio_concentration_score
        assert get_portfolio_concentration_score([], {}) == 0

    def test_single_sector_is_100(self):
        from app.risk_manager import get_portfolio_concentration_score
        positions = [
            {"sector": "tech", "invested": 1000},
            {"sector": "tech", "invested": 2000},
        ]
        score = get_portfolio_concentration_score(positions, {})
        assert score == 100, f"Single sector should be 100, got {score}"

    def test_perfectly_diversified(self):
        from app.risk_manager import get_portfolio_concentration_score
        positions = [
            {"sector": "tech", "invested": 1000},
            {"sector": "health", "invested": 1000},
            {"sector": "finance", "invested": 1000},
            {"sector": "energy", "invested": 1000},
        ]
        score = get_portfolio_concentration_score(positions, {})
        assert score == 0, f"Equal distribution should be 0, got {score}"

    def test_moderate_concentration(self):
        from app.risk_manager import get_portfolio_concentration_score
        positions = [
            {"sector": "tech", "invested": 7000},
            {"sector": "health", "invested": 1000},
            {"sector": "finance", "invested": 1000},
            {"sector": "energy", "invested": 1000},
        ]
        score = get_portfolio_concentration_score(positions, {})
        assert 30 < score < 80, f"Expected moderate concentration, got {score}"


# ============================================================
# CONFIG MANAGER THREAD SAFETY
# ============================================================

class TestConfigManagerThreadSafety:
    """Tests fuer thread-safe JSON read/write."""

    def test_file_lock_creation(self):
        from app.config_manager import _get_file_lock
        lock1 = _get_file_lock("test_file.json")
        lock2 = _get_file_lock("test_file.json")
        assert lock1 is lock2, "Same filename should return same lock object"

    def test_different_files_different_locks(self):
        from app.config_manager import _get_file_lock
        lock1 = _get_file_lock("file_a.json")
        lock2 = _get_file_lock("file_b.json")
        assert lock1 is not lock2, "Different filenames should have different locks"


# ============================================================
# ML SCORE FORMULA
# ============================================================

class TestMLScoreFormula:
    """Tests fuer die additive ML Score-Logik."""

    def test_neutral_probability_no_change(self):
        """ml_prob=0.5 sollte Score nicht veraendern."""
        original_score = 40
        ml_prob = 0.5
        ml_bonus = round((ml_prob - 0.5) * 50, 1)
        adjusted = round(original_score + ml_bonus, 1)
        assert adjusted == original_score, f"Neutral prob should not change score: {adjusted}"

    def test_high_probability_increases_score(self):
        """ml_prob=0.8 sollte Score erhoehen."""
        original_score = 40
        ml_prob = 0.8
        ml_bonus = round((ml_prob - 0.5) * 50, 1)
        adjusted = round(original_score + ml_bonus, 1)
        assert adjusted == 55.0, f"Expected 40 + 15 = 55, got {adjusted}"

    def test_low_probability_decreases_score(self):
        """ml_prob=0.2 sollte Score reduzieren."""
        original_score = 40
        ml_prob = 0.2
        ml_bonus = round((ml_prob - 0.5) * 50, 1)
        adjusted = round(original_score + ml_bonus, 1)
        assert adjusted == 25.0, f"Expected 40 - 15 = 25, got {adjusted}"

    def test_old_formula_was_broken(self):
        """Zeige dass die alte multiplikative Formel systematisch reduziert."""
        original_score = 40
        ml_prob = 0.6  # Leicht bullisch
        old_result = round(original_score * ml_prob, 1)  # Alte Formel
        new_bonus = round((ml_prob - 0.5) * 50, 1)
        new_result = round(original_score + new_bonus, 1)  # Neue Formel
        # Alte Formel: 40 * 0.6 = 24 (Score HALBIERT trotz bullish Signal!)
        # Neue Formel: 40 + 5 = 45 (sinnvoller Boost)
        assert old_result == 24.0, f"Old formula: {old_result}"
        assert new_result == 45.0, f"New formula: {new_result}"
        assert new_result > old_result, "New formula should be better for bullish signals"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
