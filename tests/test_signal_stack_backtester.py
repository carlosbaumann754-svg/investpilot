"""Tests fuer app/signal_stack_backtester.py — die deterministischen Kern-Funktionen
(Exit-Simulation, Rebalance-Termine, Point-in-Time-Preise, Metriken). Ohne Netz/EDGAR.
"""
from datetime import date
from app import signal_stack_backtester as bt


def test_month_starts():
    assert bt._month_starts("2022-01-01", "2022-03-31") == [
        date(2022, 1, 1), date(2022, 2, 1), date(2022, 3, 1)]
    assert bt._month_starts("2021-11-01", "2022-02-15") == [
        date(2021, 11, 1), date(2021, 12, 1), date(2022, 1, 1), date(2022, 2, 1)]


def test_sim_position_sl_triggers_at_first_breach():
    r, reason, days = bt._sim_position(100, [98, 94, 110], sl_pct=-5, tp_pct=None,
                                       trail_act_pct=None, trail_pct=None)
    assert reason == "SL" and r == -5 and days == 2


def test_sim_position_tp_triggers():
    r, reason, days = bt._sim_position(100, [105, 116], sl_pct=-5, tp_pct=15,
                                       trail_act_pct=None, trail_pct=None)
    assert reason == "TP" and r == 15


def test_sim_position_rebalance_exit_at_last_close():
    r, reason, days = bt._sim_position(100, [102, 104, 103], sl_pct=-10, tp_pct=20,
                                       trail_act_pct=None, trail_pct=None)
    assert reason == "REBAL" and abs(r - 3.0) < 1e-9 and days == 3


def test_sim_position_trailing():
    # steigt auf 110 (>+6% Aktivierung), faellt auf 105 (<= high*0.96=105.6) -> Trailing-Exit
    r, reason, days = bt._sim_position(100, [110, 105], sl_pct=-20, tp_pct=None,
                                       trail_act_pct=6, trail_pct=4)
    assert reason == "TRAIL" and abs(r - 5.0) < 1e-9


def test_sim_position_sl_priority_over_tp():
    # -10%-Tag: SL wird vor TP geprueft
    r, reason, days = bt._sim_position(100, [90], sl_pct=-5, tp_pct=15,
                                       trail_act_pct=None, trail_pct=None)
    assert reason == "SL"


def test_sim_position_empty_path():
    assert bt._sim_position(100, [], sl_pct=-5, tp_pct=None,
                            trail_act_pct=None, trail_pct=None) == (0.0, "flat", 0)


def test_prices_asof():
    ph = {"AAA": [(date(2022, 1, d), 100 + d) for d in range(1, 30)]}  # 101..129
    p = bt._prices_asof(ph, date(2022, 1, 29), ref_offset=21)
    assert "AAA" in p
    now, ref = p["AAA"]
    assert now == 129 and ref == 108  # letzter, 21 Handelstage davor


def test_prices_asof_too_short_skipped():
    ph = {"BBB": [(date(2022, 1, d), 50 + d) for d in range(1, 10)]}  # nur 9 < 22
    assert "BBB" not in bt._prices_asof(ph, date(2022, 1, 9))


def test_metrics_basic():
    m = bt._metrics([1.0, -0.5, 2.0], 1.025,
                    [{"ret_net": 5, "reason": "TP"}, {"ret_net": -3, "reason": "SL"}])
    assert m["months"] == 3 and m["trades"] == 2 and m["win_rate_pct"] == 50.0
    assert abs(m["total_return_pct"] - 2.5) < 1e-9
    assert m["exit_reasons"] == {"TP": 1, "SL": 1}
