"""R-B75 (13.11.2026): Index-Vergleich als GO-Bedingung fuer echtes Geld.

Anlass: Carlos fragte, ob M2a "das richtige Modell ist, um Geld zu verdienen".
Die richtige Messlatte ist nicht "macht der Bot Gewinn", sondern "schlaegt er
einen simplen Small-Cap-Indexfonds (IWM) ueber denselben Zeitraum". Die
6-Monats-Leiter verglich bisher nur mit den eigenen Modell-Baendern.
Carlos-Freigabe 13.11.2026 ("ja passt so und nimm es auf").
"""
from pathlib import Path

import pytest

from app import m2a_motor


def _row(date, iwm):
    return {"date": date, "iwm_close": iwm}


ROWS = [
    _row("2026-08-28", 290.0),       # vor dem Schnitt: zaehlt nicht
    _row("2026-08-31", 293.93),
    _row("2026-09-15", 280.0),
    _row("2026-09-30", 277.89),
    _row("2026-10-30", 278.36),
    _row("2026-11-12", 278.44),
]


def test_index_monatsrenditen_gleiche_stichtage():
    r = m2a_motor.index_renditen(ROWS, "2026-08-31")
    assert sorted(r) == ["2026-09", "2026-10", "2026-11"]
    assert r["2026-09"] == pytest.approx((277.89 / 293.93 - 1) * 100, abs=0.005)
    assert r["2026-11"] == pytest.approx((278.44 / 278.36 - 1) * 100, abs=0.005)


def test_fehlender_indexwert_heisst_unbekannt():
    rows = [dict(r) for r in ROWS]
    rows[3]["iwm_close"] = None
    r = m2a_motor.index_renditen(rows, "2026-08-31")
    assert r["2026-09"] is None and r["2026-10"] is None


def test_vergleich_ueber_gemeinsame_monate():
    m2a = {"2026-09": {"ret_pct": -0.76}, "2026-10": {"ret_pct": 2.34},
           "2026-11": {"ret_pct": -1.29}}
    idx = m2a_motor.index_renditen(ROWS, "2026-08-31")
    v = m2a_motor.vergleich_mit_index(m2a, idx)
    erwartet_idx = (278.44 / 293.93 - 1) * 100
    assert v["index_pct"] == pytest.approx(erwartet_idx, abs=0.01)
    assert v["m2a_pct"] == m2a_motor.kumuliert_pct(m2a)
    assert v["vorsprung_pp"] == pytest.approx(v["m2a_pct"] - v["index_pct"], abs=0.01)
    assert v["schlaegt_index"] is True


def test_vergleich_bis_monat_begrenzt():
    m2a = {"2026-09": {"ret_pct": -0.76}, "2026-10": {"ret_pct": 2.34},
           "2026-11": {"ret_pct": -1.29}}
    idx = m2a_motor.index_renditen(ROWS, "2026-08-31")
    v = m2a_motor.vergleich_mit_index(m2a, idx, bis="2026-10")
    assert v["index_pct"] == pytest.approx((278.36 / 293.93 - 1) * 100, abs=0.01)


def test_vergleich_unbekannt_wenn_ein_monat_fehlt():
    v = m2a_motor.vergleich_mit_index({"2026-09": {"ret_pct": None}},
                                      {"2026-09": -5.4})
    assert v is None
    assert m2a_motor.vergleich_mit_index({}, {}) is None


def test_g1_text_nennt_index():
    from scripts import m2a_gate_check as g
    t = g.index_teil({"m2a_pct": 0.25, "index_pct": -5.27, "vorsprung_pp": 5.52,
                      "schlaegt_index": True}, {"2026-10": 0.17}, "2026-10")
    assert "IWM" in t and "+5.52" in t and "-5.27" in t


def test_regelwerk_und_karte_verdrahtet():
    root = Path(__file__).parent.parent
    regel = (root / "docs" / "M2A_MOTOR_REGELWERK.md").read_text(encoding="utf-8")
    assert "R-B75" in regel and "IWM" in regel
    web = (root / "web" / "app.py").read_text(encoding="utf-8")
    start = web.find("async def api_m2a_gates")
    block = web[start:web.find("\n@app.", start)]
    assert "vergleich_mit_index(" in block and '"index_kumuliert_pct"' in block
    js = (root / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "index_kumuliert_pct" in js
