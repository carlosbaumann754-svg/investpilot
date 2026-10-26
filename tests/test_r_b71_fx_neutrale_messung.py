"""R-B71 (26.10.2026): G1 + 6-Monats-Leiter FX-neutral und nur M2a messen.

Anlass: Das Paper-Konto haelt ~1.01 Mio GBP Bargeld (Basis CHF), die US-Aktien
sind ueber ein USD-Minus finanziert. G1 rechnete portfolio_total_value (CHF) /
USDCHF — und mass damit vor allem GBP/USD statt der Strategie:
September -3.38% (G1) gegen -0.76% M2a-Strategie, Oktober bis 23.10. +3.14%
gegen +2.14%. Ausserdem zaehlten die Verkaeufe geerbter Positionen mit,
obwohl das Regelwerk sie ausklammert. Carlos-Freigabe 26.10.2026.

Neue Messung: Strategie-P/L in USD = Delta unrealisiert (nur M2a) + realisierte
M2a-Closes; Nenner = USD-Equity am Vormonatsende.
"""
import json
from pathlib import Path

import pytest

from app import m2a_motor

FREEZE = "2026-08-31T07:48:30.305237"


@pytest.fixture
def geerbt(tmp_path, monkeypatch):
    gf = tmp_path / "m2a_geerbt.json"
    gf.write_text(json.dumps({"eingefroren_am": FREEZE,
                              "position_ids": ["28840439", "455946269", "8428"]}))
    monkeypatch.setattr(m2a_motor, "_GEERBT_PATH", gf)
    m2a_motor._geerbt_cache = {"mtime": None, "ids": frozenset()}
    return gf


def _row(date, ts, tv, fx, u, g):
    r = {"date": date, "ts": ts, "portfolio_total_value": tv,
         "usdchf_close": fx, "unrealized_pnl": u}
    if g is not ...:
        r["geerbt_unrealized_pnl"] = g
    return r


# Echte Werte (equity_history + IBKR updatePortfolio 30.09. 22:32: IIIN -482.0)
ROWS = [
    _row("2026-08-28", "2026-08-28T22:31:00", 1090000.0, 0.80, 100.0, ...),
    _row("2026-08-31", "2026-08-31T22:33:33", 1092730.36, 0.8084, 389.91, 389.91),
    _row("2026-09-15", "2026-09-15T22:31:00", 1090000.0, 0.82, -5000.0, ...),
    _row("2026-09-30", "2026-09-30T22:32:09", 1091536.36, 0.8358, -10713.78, -482.0),
    _row("2026-10-23", "2026-10-23T22:31:40", 1111667.57, 0.8253, 16678.3, 0.0),
]

TRADES = [
    # geerbte Closes im September (zaehlen NICHT)
    {"timestamp": "2026-09-18T15:43:00", "action": "STOP_LOSS_CLOSE",
     "position_id": "28840439", "status": "executed", "pnl_usd": -4000.0},
    {"timestamp": "2026-09-21T15:46:31", "action": "STOP_LOSS_CLOSE",
     "position_id": "455946269", "status": "executed", "pnl_usd": -900.0},
    # M2a-Neukaeufe 01.10. (gleiche conIds)
    {"timestamp": "2026-10-01T16:10:10", "action": "SCANNER_BUY",
     "position_id": "28840439", "status": "partial"},
    {"timestamp": "2026-10-01T16:11:17", "action": "SCANNER_BUY",
     "position_id": "455946269", "status": "partial"},
    # IIIN: echte geerbte Position, Close 13.10. (zaehlt NICHT)
    {"timestamp": "2026-10-13T17:11:16", "action": "TIME_STOP_CLOSE",
     "position_id": "8428", "status": "executed", "pnl_usd": -58.38},
    # LRN: M2a-Position (nach Neukauf), Close 14.10. (zaehlt)
    {"timestamp": "2026-10-14T20:39:15", "action": "TRAILING_SL_CLOSE",
     "position_id": "455946269", "status": "executed", "pnl_usd": 999.81},
    # stornierter Close zaehlt nie
    {"timestamp": "2026-10-15T16:00:00", "action": "TIME_STOP_CLOSE",
     "position_id": "999", "status": "cancelled", "pnl_usd": 5000.0},
]


def test_september_und_oktober_echte_werte(geerbt):
    r = m2a_motor.monatsrenditen(ROWS, TRADES, "2026-08-31")
    assert sorted(r) == ["2026-09", "2026-10"]          # nichts vor dem Schnitt
    assert r["2026-09"]["ret_pct"] == pytest.approx(-0.76, abs=0.005)
    assert r["2026-10"]["ret_pct"] == pytest.approx(2.14, abs=0.005)
    # Oktober-P/L: 16678.30 - (-10231.78) + 999.81 (nur LRN, nicht IIIN)
    assert r["2026-10"]["pnl_usd"] == pytest.approx(27909.89, abs=0.01)
    assert r["2026-10"]["bis"] == "2026-10-23"


def test_waehrung_bewegt_die_rendite_nicht(geerbt):
    """Der Kern: derselbe Strategie-P/L, anderer USD/CHF am Monatsende ->
    gleiche Rendite (Nenner kommt vom Vormonatsende)."""
    rows = [dict(r) for r in ROWS]
    rows[3]["usdchf_close"] = 0.95
    rows[3]["portfolio_total_value"] = 900000.0
    r = m2a_motor.monatsrenditen(rows, TRADES, "2026-08-31")
    assert r["2026-09"]["ret_pct"] == pytest.approx(-0.76, abs=0.005)


def test_fehlender_geerbt_anteil_heisst_unbekannt(geerbt):
    rows = [dict(r) for r in ROWS]
    del rows[3]["geerbt_unrealized_pnl"]
    r = m2a_motor.monatsrenditen(rows, TRADES, "2026-08-31")
    assert r["2026-09"]["ret_pct"] is None
    assert r["2026-10"]["ret_pct"] is None              # braucht 30.09. als Basis
    assert m2a_motor.kumuliert_pct(r) is None


def test_kumuliert_verzinst(geerbt):
    r = m2a_motor.monatsrenditen(ROWS, TRADES, "2026-08-31")
    erwartet = ((1 + r["2026-09"]["ret_pct"] / 100)
                * (1 + r["2026-10"]["ret_pct"] / 100) - 1) * 100
    assert m2a_motor.kumuliert_pct(r) == pytest.approx(erwartet, abs=0.01)
    assert m2a_motor.kumuliert_pct({}) is None


def test_geerbt_unrealized_summiert_nur_geerbte(geerbt, tmp_path):
    (tmp_path / "trade_history.json").write_text(json.dumps(TRADES[:4]))
    m2a_motor._geerbt_cache = {"mtime": None, "ids": frozenset()}
    pos = [{"position_id": "8428", "pnl": -482.0},        # geerbt
           {"position_id": "455946269", "pnl": 300.0},    # neu gekauft -> M2a
           {"position_id": "29308796", "pnl": -445.28}]   # M2a
    assert m2a_motor.geerbt_unrealized(pos) == pytest.approx(-482.0)
    assert m2a_motor.geerbt_unrealized([]) == 0.0
    assert m2a_motor.geerbt_unrealized(None) is None


def test_snapshot_schreibt_geerbt_anteil(geerbt, monkeypatch):
    from app import equity_snapshot
    brain = {"performance_snapshots": [{
        "total_value": 1110000.0, "unrealized_pnl": 100.0,
        "positions": [{"position_id": "8428", "pnl": -50.0},
                      {"position_id": "1", "pnl": 150.0}]}]}
    monkeypatch.setattr(equity_snapshot, "load_json",
                        lambda name: brain if name == "brain_state.json" else None)
    comp = equity_snapshot._fetch_portfolio_components()
    assert comp["geerbt_unrealized_pnl"] == pytest.approx(-50.0)
    assert "geerbt_unrealized_pnl" in equity_snapshot._COMPONENT_KEYS


def test_g1_text_fx_neutral_und_unvollstaendig():
    from scripts import m2a_gate_check as g
    baender = {"p01": -8.3, "p05": -5.4, "p50": 1.2}
    text, prio = g.g1_teil({"2026-10": {"ret_pct": 2.14}}, "2026-10", baender)
    assert "+2.14%" in text and "FX-neutral" in text and prio == 0
    text, prio = g.g1_teil({"2026-10": {"ret_pct": None}}, "2026-10", baender)
    assert "unvollstaendig" in text.lower() and prio == 1
    text, prio = g.g1_teil({"2026-10": {"ret_pct": -9.0}}, "2026-10", baender)
    assert prio == 1


def test_keine_alte_kontowert_umrechnung_mehr():
    """Die CHF/USDCHF-Umrechnung darf weder im Wecker noch in der Karte
    zurueckkommen (sie mass GBP/USD statt der Strategie)."""
    root = Path(__file__).parent.parent
    gate = (root / "scripts" / "m2a_gate_check.py").read_text(encoding="utf-8")
    web = (root / "web" / "app.py").read_text(encoding="utf-8")
    assert "usd_equity_je_monat" not in gate
    start = web.find("async def api_m2a_gates")
    block = web[start:web.find("\n@app.", start)]
    assert 'r.get("usdchf_close")' not in block
    assert "monatsrenditen(" in block
