"""R-B70 (22.10.2026): Geerbt-Schutz endet mit dem Neukauf derselben Aktie.

Anlass: m2a_geerbt.json speichert IBKR-conIds — das ist die Kennung der AKTIE,
nicht der einzelnen Position. LRN (geerbt) wurde am 21.09. regulaer verkauft,
am 01.10. von M2a neu gekauft (gleiche conId 455946269) und galt damit wieder
als "geerbt": am 14.10. verkaufte der Alt-Trailing-Stop die M2a-Position nach
9 Handelstagen (+3.18%) — Verstoss gegen das bindende Regelwerk (H126, kein
Trailing). AGO + CENT (ebenfalls 01.10. neu gekauft) standen unter demselben
Fehler. Gleiche Fehlerklasse wie R-A23 (conId-Wiederverwendung bei Open-Time).
"""
import json
import os
import time
from pathlib import Path

import pytest

from app import m2a_motor

FREEZE = "2026-08-31T07:48:30.305237"


@pytest.fixture
def daten(tmp_path, monkeypatch):
    gf = tmp_path / "m2a_geerbt.json"
    gf.write_text(json.dumps({"eingefroren_am": FREEZE,
                              "position_ids": ["455946269", "8428", "28840439"]}))
    monkeypatch.setattr(m2a_motor, "_GEERBT_PATH", gf)
    m2a_motor._geerbt_cache = {"mtime": None, "ids": frozenset()}
    th = tmp_path / "trade_history.json"

    def schreibe(eintraege):
        th.write_text(json.dumps(eintraege))
        # mtime sicher veraendern (Cache-Schluessel), auch bei grober FS-Aufloesung
        t = time.time() + len(eintraege)
        os.utime(th, (t, t))
    return schreibe


def _buy(pid, ts, status="partial", action="SCANNER_BUY"):
    return {"timestamp": ts, "action": action, "position_id": pid, "status": status}


def test_neukauf_nach_schnitt_beendet_geerbt(daten):
    """Der LRN-Fall: Kauf 26.08. (geerbt), Verkauf 21.09., Neukauf 01.10."""
    daten([_buy(455946269, "2026-08-26T16:16:18"),
           {"timestamp": "2026-09-21T15:46:31", "action": "STOP_LOSS_CLOSE",
            "position_id": 455946269, "status": "executed"},
           _buy(455946269, "2026-10-01T16:11:17")])
    assert m2a_motor.ist_geerbt("455946269") is False


def test_ohne_neukauf_bleibt_geerbt(daten):
    """IIIN-Fall: seit dem Schnitt durchgehend gehalten -> Alt-Exits gelten."""
    daten([_buy(8428, "2026-08-25T18:16:03")])
    assert m2a_motor.ist_geerbt("8428") is True
    assert m2a_motor.ist_geerbt(8428) is True


def test_stornierter_neukauf_zaehlt_nicht(daten):
    daten([_buy(28840439, "2026-10-01T16:10:10", status="cancelled")])
    assert m2a_motor.ist_geerbt("28840439") is True


def test_nicht_gelistete_id_nie_geerbt(daten):
    daten([])
    assert m2a_motor.ist_geerbt("999") is False


def test_fehlende_trade_history_heisst_alte_liste(daten, tmp_path):
    assert not (tmp_path / "trade_history.json").exists()
    assert m2a_motor.ist_geerbt("455946269") is True


def test_ohne_einfrierzeitpunkt_kein_filter(tmp_path, monkeypatch):
    """Ohne Zeitanker wuerde JEDER alte Kauf zaehlen -> alles faelschlich M2a.
    Dann lieber die unveraenderte Liste (bisheriges Verhalten)."""
    gf = tmp_path / "m2a_geerbt.json"
    gf.write_text(json.dumps({"position_ids": ["455946269"]}))
    monkeypatch.setattr(m2a_motor, "_GEERBT_PATH", gf)
    m2a_motor._geerbt_cache = {"mtime": None, "ids": frozenset()}
    (tmp_path / "trade_history.json").write_text(
        json.dumps([_buy(455946269, "2026-10-01T16:11:17")]))
    assert m2a_motor.ist_geerbt("455946269") is True


def test_cache_bemerkt_neuen_kauf(daten):
    daten([])
    assert m2a_motor.ist_geerbt("28840439") is True
    daten([_buy(28840439, "2026-10-01T16:10:10")])
    assert m2a_motor.ist_geerbt("28840439") is False


def test_dashboard_zaehlt_ueber_dieselbe_funktion():
    """Die M2a-Karte darf die Rohliste nicht selbst auswerten (sonst zeigt sie
    re-gekaufte Positionen weiter als 'geerbt offen')."""
    src = (Path(__file__).parent.parent / "web" / "app.py").read_text(encoding="utf-8")
    start = src.find("geerbt_offen = None")
    block = src[start - 600:start + 600]
    assert "geerbte_ids()" in block
    assert 'geerbt.get("position_ids")' not in block
