"""
Tests fuer PairsBot.calculate_signals() — W3 Signal-Generation.

yfinance wird gemockt, damit Tests ohne Network-Access laufen.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


def _make_pair(sym_a="KO", sym_b="PEP", beta=1.0, mean=0.0, std=1.0, hl=10.0):
    from app.pairs_trading import Pair
    return Pair(
        symbol_a=sym_a, symbol_b=sym_b, beta=beta,
        half_life_days=hl, mean_spread=mean, std_spread=std,
        last_updated="2026-04-26T00:00:00",
    )


def _make_bot():
    from app.pairs_trading import PairsBot
    broker = MagicMock(); broker.broker_name = "ibkr"
    return PairsBot(broker, {})


def _patch_yf_close(close_dict):
    """Mockt yfinance.download return so dass df.iloc[-1].to_dict() == close_dict."""
    import pandas as pd
    fake_yf = MagicMock()
    df = pd.DataFrame([close_dict, close_dict])  # 2 rows so iloc[-1] works
    # Closes-Sub-Slot
    fake_dl = MagicMock()
    fake_dl.__getitem__.return_value = df
    fake_yf.download.return_value = fake_dl
    return patch.dict(sys.modules, {"yfinance": fake_yf})


def test_no_signal_when_z_is_within_thresholds():
    """z=0.5, entry=2.0, exit=0.5 -> kein Signal."""
    bot = _make_bot()
    pair = _make_pair(beta=1.0, mean=0.0, std=10.0)
    # spread_now = 105 - 1.0*105 = 0 -> z=0
    with _patch_yf_close({"KO": 105, "PEP": 105}):
        signals = bot.calculate_signals([pair], portfolio_value_usd=100000)
    assert signals == []


def test_short_a_long_b_when_z_above_entry():
    """Spread zu hoch (z=+3) -> SHORT_A_LONG_B."""
    bot = _make_bot()
    pair = _make_pair(beta=1.0, mean=0.0, std=1.0)
    # spread_now = 103 - 1*100 = 3, z = (3-0)/1 = 3 > 2.0
    with _patch_yf_close({"KO": 103, "PEP": 100}):
        signals = bot.calculate_signals([pair], portfolio_value_usd=100000)
    assert len(signals) == 1
    s = signals[0]
    assert s.direction == "SHORT_A_LONG_B"
    assert s.z_score > 2.0
    assert "Spread zu hoch" in s.reason


def test_long_a_short_b_when_z_below_negative_entry():
    """Spread zu tief (z=-3) -> LONG_A_SHORT_B."""
    bot = _make_bot()
    pair = _make_pair(beta=1.0, mean=0.0, std=1.0)
    # spread_now = 97 - 1*100 = -3, z = -3
    with _patch_yf_close({"KO": 97, "PEP": 100}):
        signals = bot.calculate_signals([pair], portfolio_value_usd=100000)
    assert len(signals) == 1
    s = signals[0]
    assert s.direction == "LONG_A_SHORT_B"
    assert s.z_score < -2.0


def test_close_signal_when_open_position_and_z_neutral():
    """Open Position, z=0.3 (< exit=0.5) -> CLOSE."""
    bot = _make_bot()
    pair = _make_pair(beta=1.0, mean=0.0, std=10.0)  # std hoch -> kleiner z
    # spread = 100.3 - 100 = 0.3, z = 0.3/10 = 0.03 < 0.5
    with _patch_yf_close({"KO": 100.3, "PEP": 100}):
        signals = bot.calculate_signals(
            [pair], portfolio_value_usd=100000,
            open_pair_positions=[pair.name],
        )
    assert len(signals) == 1
    assert signals[0].direction == "CLOSE"
    assert "mean-reverted" in signals[0].reason


def test_no_close_signal_for_open_when_z_still_above_entry():
    """Open Position, z=+3 -> KEIN Signal (nicht CLOSE, nicht erneut Entry)."""
    bot = _make_bot()
    pair = _make_pair(beta=1.0, mean=0.0, std=1.0)
    with _patch_yf_close({"KO": 103, "PEP": 100}):
        signals = bot.calculate_signals(
            [pair], portfolio_value_usd=100000,
            open_pair_positions=[pair.name],
        )
    # Open + abs(z)=3 > exit=0.5 + nicht CLOSE-Bedingung -> kein Signal
    assert signals == []


def test_position_sizing_uses_config_ratios():
    """portfolio=$100k, max_pct=20%, max_pairs=3 -> per_pair=$6667, per_leg=$3333."""
    bot = _make_bot()
    pair = _make_pair(beta=1.0, mean=0.0, std=1.0)
    with _patch_yf_close({"KO": 103, "PEP": 100}):
        signals = bot.calculate_signals([pair], portfolio_value_usd=100000)
    assert len(signals) == 1
    # Default config: max_portfolio_pct=20, max_concurrent_pairs=3
    # per_pair = 100000 * 0.2 / 3 = 6666.67, per_leg = 3333.33
    assert 3000 < signals[0].suggested_amount_usd < 4000


def test_skip_pair_with_zero_std_spread():
    """std_spread = 0 -> Division-by-zero vermeiden, Pair skippen."""
    bot = _make_bot()
    pair = _make_pair(std=0.0)  # invalid
    with _patch_yf_close({"KO": 100, "PEP": 100}):
        signals = bot.calculate_signals([pair], portfolio_value_usd=100000)
    assert signals == []


def test_empty_pairs_list_returns_empty_signals():
    """Wenn keine Paare uebergeben, kommt leere Liste zurueck (kein Crash)."""
    bot = _make_bot()
    signals = bot.calculate_signals([], portfolio_value_usd=100000)
    assert signals == []
