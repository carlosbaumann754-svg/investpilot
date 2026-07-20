"""R-B12 (20.07.2026) — Tranchen-Simulation im signal_stack_backtester.

Der Live-Trader nimmt via leverage.tp_tranches Teilgewinne mit (30% bei +8,
30% bei +16, 40% bei +30). Der Backtester kannte diesen Mechanismus NICHT und
modellierte die Live-Exits dadurch systematisch zu MILD — der Exit-Sweep vom
20.07. lief ohne ihn. Live feuerten seit Soak-Start 10 von 10 Partials bei
+8% (keine einzige je bei +16%), d.h. die Tranche ist real der wirksamste
Gewinner-Deckel.

Diese Tests fixieren die nachgeruestete Logik — auf ihr beruht die Entscheidung
ueber die Exit-Rekalibrierung.
"""
import pytest

from app.signal_stack_backtester import _sim_position

T1 = [{"pct_of_position": 30, "profit_target_pct": 8}]
T3 = [{"pct_of_position": 30, "profit_target_pct": 8},
      {"pct_of_position": 30, "profit_target_pct": 16},
      {"pct_of_position": 40, "profit_target_pct": 30}]


# ============================================================
# Rueckwaertskompatibilitaet
# ============================================================

def test_without_tranches_identical_to_before():
    """tranches=None -> exakt das alte Verhalten (SL greift voll)."""
    r, reason, days = _sim_position(100.0, [95.0, 90.0], -8, None, None, None)
    assert reason == "SL" and r == -8.0 and days == 2


def test_without_tranches_rebalance_exit():
    r, reason, days = _sim_position(100.0, [102.0, 104.0], -8, None, None, None)
    assert reason == "REBAL"
    assert r == pytest.approx(4.0)


def test_empty_tranche_list_is_noop():
    r, reason, _ = _sim_position(100.0, [95.0, 90.0], -8, None, None, None, [])
    assert reason == "SL" and r == -8.0


# ============================================================
# Tranchen greifen
# ============================================================

def test_single_tranche_then_trailing_exit():
    """30% bei +8 realisiert, Rest wird ausgetrailt -> gewichteter Return.

    Tag1 105: nichts. Tag2 109: Tranche (+8 auf 30%) = 2.4; Trailing scharf
    (>=106). Tag3 104: 104 <= 109*0.96 -> Rest (70%) bei +4% raus = 2.8.
    Summe 5.2.
    """
    r, reason, days = _sim_position(100.0, [105.0, 109.0, 104.0],
                                    -8, None, 6.0, 4.0, T1)
    assert reason == "TRANCHE+TRAIL"
    assert r == pytest.approx(5.2)
    assert days == 3


def test_all_tranches_fire_closes_position():
    """Alle Ziele an einem Tag erreicht -> Position komplett zu, kein Rest."""
    r, reason, days = _sim_position(100.0, [135.0], -8, None, 6.0, 4.0, T3)
    # 0.3*8 + 0.3*16 + 0.4*30 = 2.4 + 4.8 + 12.0
    assert reason == "TRANCHE_ALL"
    assert r == pytest.approx(19.2)
    assert days == 1


def test_stop_loss_after_tranche_is_weighted():
    """DER KERN der Asymmetrie: Teilgewinn klein, Rest faellt voll in den SL.

    30% bei +8 = +2.4, danach 70% bei -8 = -5.6 -> netto -3.2 trotz
    zwischenzeitlichem Gewinn.
    """
    r, reason, _ = _sim_position(100.0, [109.0, 90.0], -8, None, None, None, T1)
    assert reason == "TRANCHE+SL"
    assert r == pytest.approx(-3.2)


def test_tranche_not_reached_behaves_normally():
    r, reason, _ = _sim_position(100.0, [104.0, 103.0], -8, None, None, None, T1)
    assert reason == "REBAL"
    assert r == pytest.approx(3.0)


def test_partial_tranches_then_rebalance():
    """Nur die erste Tranche erreicht, Rest laeuft bis Rebalance."""
    r, reason, _ = _sim_position(100.0, [110.0, 112.0], -8, None, None, None, T3)
    # 0.3*8 = 2.4 ; Rest 0.7 bei +12% = 8.4
    assert reason == "TRANCHE+REBAL"
    assert r == pytest.approx(10.8)


def test_tranches_sorted_by_target_regardless_of_config_order():
    unsorted = [{"pct_of_position": 40, "profit_target_pct": 30},
                {"pct_of_position": 30, "profit_target_pct": 8},
                {"pct_of_position": 30, "profit_target_pct": 16}]
    r, reason, _ = _sim_position(100.0, [135.0], -8, None, None, None, unsorted)
    assert reason == "TRANCHE_ALL"
    assert r == pytest.approx(19.2)


def test_tranche_fractions_capped_at_remaining():
    """Ueber 100% konfiguriert -> nie mehr als die Position verkaufen."""
    over = [{"pct_of_position": 80, "profit_target_pct": 8},
            {"pct_of_position": 80, "profit_target_pct": 10}]
    r, reason, _ = _sim_position(100.0, [115.0], -8, None, None, None, over)
    # 0.8*8 = 6.4, dann nur noch 0.2 uebrig: 0.2*10 = 2.0
    assert reason == "TRANCHE_ALL"
    assert r == pytest.approx(8.4)


# ============================================================
# Robustheit
# ============================================================

@pytest.mark.parametrize("bad", [
    [{"pct_of_position": "abc", "profit_target_pct": 8}],
    [{"pct_of_position": 30}],
    [{"profit_target_pct": 8}],
    [None],
    ["kaputt"],
])
def test_malformed_tranches_ignored_not_crash(bad):
    r, reason, _ = _sim_position(100.0, [95.0, 90.0], -8, None, None, None, bad)
    assert reason in ("SL", "TRANCHE+SL")
    assert isinstance(r, float)


def test_empty_forward_window_safe():
    r, reason, days = _sim_position(100.0, [], -8, None, 6.0, 4.0, T3)
    assert (r, reason, days) == (0.0, "flat", 0)


# ============================================================
# Der Mechanismus, um den es fachlich geht
# ============================================================

def test_tranches_cap_winners_versus_no_tranches():
    """Beweis im Modell: dieselbe Kursbewegung liefert MIT Tranchen weniger.

    Ein Laeufer auf +25%: ohne Tranchen voll mitgenommen, mit Tranchen werden
    30% schon bei +8 abgegeben -> der Gewinner ist gedeckelt.
    """
    path = [109.0, 118.0, 125.0]
    with_t, _, _ = _sim_position(100.0, path, -8, None, None, None, T3)
    without, _, _ = _sim_position(100.0, path, -8, None, None, None, None)
    assert without == pytest.approx(25.0)
    # 0.3*8 + 0.3*16 + 0.4*25 = 2.4 + 4.8 + 10.0 = 17.2
    assert with_t == pytest.approx(17.2)
    assert with_t < without
