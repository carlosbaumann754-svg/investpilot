"""R-B11 (07.06.2026) — Backtest-Metrik-Fixes aus dem LLM-Deep-Audit.

M1: Partial-Close-Trades werden zur Position gefaltet (nicht als volle, immer-
    profitable Trades gezaehlt) -> Win-Rate & Profit-Factor ehrlich.
M3: total_return/drawdown/sharpe aus EINER tagesbasierten Equity-Reihe statt
    sequentiellem Compounding ueberlappender Positionen.
M6: profit_factor auf _PROFIT_FACTOR_CAP gedeckelt (kein inf -> invalides JSON).
M2: Live-PF gewichtet Teil-Closes anteilig (konsistent zur WFO-Baseline).
"""
import json
import math
from datetime import datetime, timedelta

from app.backtester import (
    calculate_metrics, _aggregate_positions, _daily_return_map,
    _PROFIT_FACTOR_CAP,
)


def _trade(symbol, entry, exit_, pnl_net, exit_reason="STOP_LOSS",
           partial_pct=None, days=5):
    t = {
        "symbol": symbol, "entry_date": entry, "exit_date": exit_,
        "pnl_pct": pnl_net, "pnl_net_pct": pnl_net, "days_held": days,
        "exit_reason": exit_reason, "cost_pct": 0.1,
    }
    if partial_pct is not None:
        t["partial_close_pct"] = partial_pct
    return t


# ── M1: Positions-Aggregation ───────────────────────────────────────────────
def test_m1_partial_folded_into_one_position():
    """Partial (30% @ +10%) + Final (-5%) = EINE Position, gewichtet -0.5%."""
    trades = [
        _trade("AAA", "2026-01-01", "2026-01-05", 10.0,
               exit_reason="PARTIAL_CLOSE", partial_pct=30),
        _trade("AAA", "2026-01-01", "2026-01-10", -5.0, exit_reason="STOP_LOSS"),
    ]
    positions = _aggregate_positions(trades)
    assert len(positions) == 1
    # 0.3*0.10 + 0.7*(-0.05) = 0.03 - 0.035 = -0.005
    assert abs(positions[0]["net_return"] - (-0.005)) < 1e-9


def test_m1_partials_dont_inflate_winrate():
    """2 Tranchen-Gewinne + 1 finaler grosser Verlust derselben Position =
    1 Verlust-Position, NICHT 2 Wins + 1 Loss (alte ~66% Win-Rate)."""
    trades = [
        _trade("AAA", "2026-01-01", "2026-01-03", 8.0,
               exit_reason="PARTIAL_CLOSE", partial_pct=30),
        _trade("AAA", "2026-01-01", "2026-01-05", 8.0,
               exit_reason="PARTIAL_CLOSE", partial_pct=30),
        _trade("AAA", "2026-01-01", "2026-01-10", -40.0, exit_reason="STOP_LOSS"),
    ]
    m = calculate_metrics(trades)
    # 0.3*0.08 + 0.3*0.08 + 0.4*(-0.40) = 0.048 - 0.16 = -0.112 < 0
    assert m["total_positions"] == 1
    assert m["total_trades"] == 3  # Roh-Records bleiben sichtbar
    assert m["win_rate_pct"] == 0.0  # eine Verlust-Position, KEINE Wins


def test_m1_separate_positions_not_merged():
    """Verschiedene entry_dates -> getrennte Positionen."""
    trades = [
        _trade("AAA", "2026-01-01", "2026-01-05", 5.0),
        _trade("AAA", "2026-02-01", "2026-02-05", -3.0),
    ]
    assert len(_aggregate_positions(trades)) == 2


def test_m1_no_partials_behaves_normally():
    """Ohne Partials: jede Position = 1 Trade, Win-Rate wie erwartet."""
    trades = [
        _trade("AAA", "2026-01-01", "2026-01-05", 5.0, exit_reason="TAKE_PROFIT"),
        _trade("BBB", "2026-01-02", "2026-01-06", -3.0, exit_reason="STOP_LOSS"),
    ]
    m = calculate_metrics(trades)
    assert m["total_positions"] == 2
    assert m["win_rate_pct"] == 50.0


# ── M6: PF-Cap ──────────────────────────────────────────────────────────────
def test_m6_profit_factor_no_inf_and_json_safe():
    """Verlustfreies Fenster -> PF = Cap (nicht inf), JSON-serialisierbar."""
    trades = [
        _trade("AAA", "2026-01-01", "2026-01-05", 5.0, exit_reason="TAKE_PROFIT"),
        _trade("BBB", "2026-01-02", "2026-01-06", 7.0, exit_reason="TAKE_PROFIT"),
    ]
    m = calculate_metrics(trades)
    assert m["profit_factor"] == _PROFIT_FACTOR_CAP
    assert not math.isinf(m["profit_factor"])
    json.dumps(m)  # darf nicht werfen


def test_m6_profit_factor_capped_value():
    """Riesiger Gewinn / winziger Verlust -> auf Cap gedeckelt."""
    trades = [
        _trade("AAA", "2026-01-01", "2026-01-05", 100.0, exit_reason="TAKE_PROFIT"),
        _trade("BBB", "2026-01-02", "2026-01-06", -0.01, exit_reason="STOP_LOSS"),
    ]
    m = calculate_metrics(trades)
    assert m["profit_factor"] == _PROFIT_FACTOR_CAP


# ── M3: tagesbasiert, keine sequentielle Ueber-Compoundierung ───────────────
def test_m3_daily_sum_equals_position_return():
    """Tages-Verteilung einer Position summiert EXAKT zum Positions-Return
    (kein Doppelzaehlen)."""
    positions = [{"entry_date": "2026-01-05", "exit_date": "2026-01-09",
                  "net_return": 0.10}]
    daily = _daily_return_map(positions, kelly_frac=1.0)
    assert abs(sum(daily.values()) - 0.10) < 1e-9


def test_m3_daily_aggregation_below_sequential_compound():
    """30 zeitgleiche Voll-Positionen je +20%: tagesbasiert MUSS deutlich unter
    dem sequentiellen Ueber-Compound (1.2^30) liegen."""
    trades = [_trade(f"S{i}", "2026-01-05", "2026-01-09", 20.0, days=4)
              for i in range(30)]
    m = calculate_metrics(trades)
    naive = 1.0
    for _ in range(30):
        naive *= 1.20
    naive_pct = (naive - 1) * 100
    assert m["total_return_pct"] < naive_pct


# ── M2: Live-PF Partial-Gewichtung ──────────────────────────────────────────
def test_m2_live_pf_weights_partials():
    from app.wfo_drift_watchdog import _compute_live_pf
    ts = (datetime.now() - timedelta(days=1)).isoformat()
    trades = [
        {"action": "PARTIAL_CLOSE", "pnl_net_pct": 10.0,
         "partial_close_pct": 30, "timestamp": ts},
        {"action": "STOP_LOSS_CLOSE", "pnl_net_pct": -5.0, "timestamp": ts},
    ]
    pf, wr, n = _compute_live_pf(trades, lookback_days=30)
    # gross_win = 10*0.3 = 3.0; gross_loss = 5.0 -> PF = 0.6
    assert abs(pf - 0.6) < 0.01
    # Win-Rate-Basis = finale Closes (1 Loss) -> 0%
    assert wr == 0.0
