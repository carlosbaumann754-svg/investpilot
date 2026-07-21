"""R-B16 (21.07.2026) — Hold-Modus: der Backtester bildet endlich den echten Bot ab.

HINTERGRUND
-----------
run_backtest verkauft jede Position am Monatsende zwangsweise ("REBAL") und
besetzt das Depot neu. Der LIVE-Bot rotiert NICHT aus dem Ranking heraus — er
haelt, bis ein Exit feuert. Bei der Exit-Analyse am 20.07.2026 kamen dadurch bis
zu 100 % des Backtest-Gewinns aus einem Mechanismus, den es live nicht gibt; die
Empfehlung musste am selben Abend zurueckgedreht werden.

run_backtest_hold schliesst die Luecke. Diese Tests sichern, dass er (a) sich in
der Grenzsituation exakt wie der bewaehrte Modus verhaelt (Aequivalenz-Probe) und
(b) sonst das tut, was den Unterschied ausmacht: Slots ueber Monate belegen.
"""
from datetime import date, timedelta

import pytest

from app import signal_stack_backtester as bt


def _series(start: date, days: int, fn):
    """Taegliche (Datum, Kurs)-Reihe; fn(i) liefert den Kurs."""
    return [(start + timedelta(days=i), float(fn(i))) for i in range(days)]


@pytest.fixture
def flat_world():
    """Zwei Symbole, konstanter Kurs 100 — ohne Exits passiert nichts."""
    s = date(2019, 10, 1)
    ph = {"AAA": _series(s, 400, lambda i: 100.0),
          "BBB": _series(s, 400, lambda i: 100.0)}
    picks = {d: ["AAA", "BBB"] for d in bt._month_starts("2020-01-01", "2020-06-01")}
    return ph, picks


@pytest.fixture
def crash_world():
    """Kurs faellt sofort hart -> jeder SL feuert am ersten Folgetag."""
    s = date(2019, 10, 1)
    ph = {"AAA": _series(s, 400, lambda i: 100.0 if i < 95 else 50.0),
          "BBB": _series(s, 400, lambda i: 100.0 if i < 95 else 50.0)}
    picks = {d: ["AAA", "BBB"] for d in bt._month_starts("2020-01-01", "2020-06-01")}
    return ph, picks


# ============================================================
# Aequivalenz-Probe — der wichtigste Test
# ============================================================

def test_hold_equals_monthly_when_every_exit_fires_immediately():
    """DIE AEQUIVALENZ-PROBE: feuert jeder Exit sofort, muessen beide Modi
    identisch rechnen.

    Dann wird jeder Slot noch im selben Monat frei -> der Hold-Modus kauft
    genauso jeden Monat neu wie der Rotations-Modus. Weichen die Ergebnisse ab,
    steckt ein Fehler im neuen Pfad.

    Dafuer braucht es STETIG fallende Kurse (-3 %/Tag): egal wann eingestiegen
    wird, der SL -8 greift binnen weniger Tage. (Ein einmaliger Crash reicht
    nicht — danach liegen die Kurse flach und im Hold-Modus feuert nie wieder
    etwas.)
    """
    s = date(2019, 10, 1)
    ph = {"AAA": _series(s, 400, lambda i: 100.0 * (0.97 ** i)),
          "BBB": _series(s, 400, lambda i: 100.0 * (0.97 ** i))}
    picks = {d: ["AAA", "BBB"] for d in bt._month_starts("2020-01-01", "2020-06-01")}
    kw = dict(top_n=2, deployment=1.0, sl_pct=-8, tp_pct=None,
              trail_act_pct=None, trail_pct=None, picks_by_month=picks)
    a = bt.run_backtest(ph, {}, ["AAA", "BBB"], "2020-01-01", "2020-06-01", **kw)
    b = bt.run_backtest_hold(ph, {}, ["AAA", "BBB"], "2020-01-01", "2020-06-01", **kw)

    # Der letzte Rebalance-Termin hat kein Folgefenster mehr: der alte Modus
    # bucht dafuer einen Leer-Trade ("flat"), der Hold-Modus kauft gar nicht
    # erst. Randfall, fuer den Vergleich rausnehmen.
    ta = [t for t in a["trades"] if t["reason"] != "flat"]
    tb = [t for t in b["trades"] if t["reason"] != "flat"]

    assert len(ta) == len(tb), "unterschiedlich viele Trades"
    assert [t["ret_net"] for t in ta] == [t["ret_net"] for t in tb]
    assert {t["reason"] for t in tb} == {"SL"}


# ============================================================
# Der eigentliche Unterschied: Slots bleiben belegt
# ============================================================

def test_hold_keeps_position_across_months(flat_world):
    """Ohne Exit bleibt die Position liegen — genau das kann der alte Modus nicht."""
    ph, picks = flat_world
    b = bt.run_backtest_hold(ph, {}, ["AAA", "BBB"], "2020-01-01", "2020-06-01",
                             top_n=2, deployment=1.0, sl_pct=None, tp_pct=None,
                             trail_act_pct=None, trail_pct=None, picks_by_month=picks)
    # 2 Slots, nie frei -> genau 2 Trades statt 2 pro Monat
    assert len(b["trades"]) == 2
    assert {t["sym"] for t in b["trades"]} == {"AAA", "BBB"}
    assert {t["reason"] for t in b["trades"]} == {"OPEN_AT_END"}


def test_monthly_mode_rebuys_every_month(flat_world):
    """Gegenprobe: der Rotations-Modus kauft dieselben Titel jeden Monat neu."""
    ph, picks = flat_world
    a = bt.run_backtest(ph, {}, ["AAA", "BBB"], "2020-01-01", "2020-06-01",
                        top_n=2, deployment=1.0, sl_pct=None, tp_pct=None,
                        trail_act_pct=None, trail_pct=None, picks_by_month=picks)
    assert len(a["trades"]) > 2, "Rotations-Modus muesste mehrfach kaufen"
    # "flat" = letzter Rebalance-Termin ohne Folgefenster (Randfall)
    assert {t["reason"] for t in a["trades"]} <= {"REBAL", "flat"}
    assert "REBAL" in {t["reason"] for t in a["trades"]}


def test_occupied_slot_blocks_new_buy(flat_world):
    """Nur ein Slot, dauerhaft belegt -> genau ein Kauf im ganzen Zeitraum."""
    ph, picks = flat_world
    b = bt.run_backtest_hold(ph, {}, ["AAA", "BBB"], "2020-01-01", "2020-06-01",
                             top_n=1, deployment=1.0, sl_pct=None, tp_pct=None,
                             trail_act_pct=None, trail_pct=None, picks_by_month=picks)
    assert len(b["trades"]) == 1


def test_freed_slot_is_refilled():
    """Position steigt aus -> der Slot wird beim naechsten Rebalance neu besetzt."""
    s = date(2019, 10, 1)
    # AAA bricht Mitte Februar ein (SL feuert), BBB laeuft flach weiter
    ph = {"AAA": _series(s, 400, lambda i: 100.0 if i < 140 else 50.0),
          "BBB": _series(s, 400, lambda i: 100.0)}
    picks = {d: ["AAA", "BBB"] for d in bt._month_starts("2020-01-01", "2020-06-01")}
    b = bt.run_backtest_hold(ph, {}, ["AAA", "BBB"], "2020-01-01", "2020-06-01",
                             top_n=1, deployment=1.0, sl_pct=-8, tp_pct=None,
                             trail_act_pct=None, trail_pct=None, picks_by_month=picks)
    assert len(b["trades"]) >= 2, "nach dem SL haette nachgekauft werden muessen"
    # Nachgekauft wird der BESTPLATZIERTE freie Titel — hier wieder AAA.
    # (Der Live-Bot hat zusaetzlich einen buy_cooldown, den der Backtester
    #  NICHT modelliert — als Limitation im Modul-Docstring vermerkt.)
    erster, zweiter = b["trades"][0], b["trades"][1]
    assert zweiter["entry"] >= erster["exit"], "Nachkauf muss NACH dem Ausstieg liegen"


def test_no_rotation_exit_reason_in_hold_mode(flat_world):
    """'REBAL' waere hier irrefuehrend — es gibt keine Rotation im Hold-Modus."""
    ph, picks = flat_world
    b = bt.run_backtest_hold(ph, {}, ["AAA", "BBB"], "2020-01-01", "2020-06-01",
                             top_n=2, deployment=1.0, sl_pct=None, tp_pct=None,
                             trail_act_pct=None, trail_pct=None, picks_by_month=picks)
    assert "REBAL" not in {t["reason"] for t in b["trades"]}


def test_exit_date_recorded_and_after_entry(crash_world):
    ph, picks = crash_world
    b = bt.run_backtest_hold(ph, {}, ["AAA", "BBB"], "2020-01-01", "2020-06-01",
                             top_n=2, deployment=1.0, sl_pct=-8, picks_by_month=picks)
    for t in b["trades"]:
        assert t["exit"] > t["entry"], f"Ausstieg vor Einstieg: {t}"


def test_metrics_consumable(crash_world):
    """Ergebnis muss durch dieselbe Metrik-Funktion laufen wie der alte Modus."""
    ph, picks = crash_world
    b = bt.run_backtest_hold(ph, {}, ["AAA", "BBB"], "2020-01-01", "2020-06-01",
                             top_n=2, deployment=1.0, sl_pct=-8, picks_by_month=picks)
    m = bt._metrics(b["monthly_pct"], b["equity_final"], b["trades"])
    assert m["trades"] == len(b["trades"])
    assert isinstance(m["total_return_pct"], float)


def test_empty_universe_safe():
    b = bt.run_backtest_hold({}, {}, [], "2020-01-01", "2020-03-01",
                             top_n=5, picks_by_month={})
    assert b["trades"] == []
    assert b["equity_final"] == 1.0
