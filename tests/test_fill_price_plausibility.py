"""R-B19 (21.07.2026) — Unplausible Fill-Preise erkennen und aussortieren.

DER VORFALL
-----------
Bei der Untersuchung der Luecke "Backtest PF 1.34 vs live 0.38" fielen sechs
Trades mit absurden Abweichungen zwischen avg_fill_price und intended_price auf:

    COPPER  intended 40.46  ->  avg_fill 446.09   (+1002 %)
    IWM     intended 279.03 ->  avg_fill  11.51   (  -96 %)
    OIL     intended 153.73 ->  avg_fill  60.26   (  -61 %)

Das ist keine Slippage. Die Fill-Daten gehoerten erkennbar zu einem anderen
Kontrakt (IWM angeblich 4204 Stueck zu 11.51, real ~279) — alles commodities
oder gehebelte ETFs, wo der Broker offenbar auf anderer Basis meldet.

WARUM ES ZAEHLT
---------------
Diese Felder speisen den Cost-Model-Kalibrator, der nach 20+ Fills die
Slippage-Annahmen in cost_model.py ueberschreibt — eine Groesse, die
Handelsentscheidungen beeinflusst. Der Median hat den Muell ueberlebt
(0.519 % blieb plausibel), aber Mittelwert (17.2 %) und Stdev (106 %) waren
unbrauchbar, und bei hoeherem Muell-Anteil kippt auch der Median.

Zwei Verteidigungslinien, beide hier getestet: Erkennung beim Schreiben
(trader) und Filter beim Auswerten (Kalibrator, wirkt auch auf Altdaten).
"""
from unittest.mock import patch

import pytest

from app.trader import _attach_fill_prices, _FILL_PLAUSIBILITY_MAX_DEV


def _result(avg, intended, ref=None):
    order = {"avgFillPrice": avg, "intendedPrice": intended}
    if ref is not None:
        order["refQuote"] = ref
    return {"orderForOpen": order}


# ============================================================
# Linie 1: Erkennung beim Schreiben
# ============================================================

@pytest.mark.parametrize("sym,avg,intended,abw", [
    ("COPPER", 446.09, 40.46, "+1002 %"),
    ("IWM", 11.51, 279.03, "-96 %"),
    ("OIL", 60.26, 153.73, "-61 %"),
    ("COPPER", 191.14, 40.49, "+372 %"),
])
def test_echte_vorfaelle_werden_markiert(sym, avg, intended, abw):
    """Genau die sechs Faelle aus der Historie muessen auffallen."""
    e = _attach_fill_prices({"action": "SCANNER_BUY", "symbol": sym},
                            _result(avg, intended))
    assert e.get("fill_price_implausible") is True, f"{sym} {abw} nicht erkannt"


@pytest.mark.parametrize("avg,intended", [
    (85.89, 91.79),    # LRN -6.4 % — schlechteste ECHTE Abweichung
    (100.0, 100.0),    # perfekt
    (110.0, 100.0),    # +10 % — grosse, aber moegliche Kursluecke
    (81.0, 100.0),     # -19 % — hart an der Grenze, noch plausibel
])
def test_echte_slippage_wird_nicht_markiert(avg, intended):
    """Kein Fehlalarm auf reale Ausfuehrungsabweichungen."""
    e = _attach_fill_prices({"action": "SCANNER_BUY", "symbol": "LRN"},
                            _result(avg, intended))
    assert "fill_price_implausible" not in e


def test_werte_werden_trotz_markierung_gespeichert():
    """Nicht loeschen — Rohdaten bleiben, nur die Bewertung kommt dazu."""
    e = _attach_fill_prices({"action": "SCANNER_BUY", "symbol": "COPPER"},
                            _result(446.09, 40.46, ref=39.86))
    assert e["avg_fill_price"] == 446.09
    assert e["intended_price"] == 40.46
    assert e["ref_quote"] == 39.86
    assert e["fill_price_implausible"] is True


def test_ohne_zielpreis_keine_bewertung():
    """Ohne intended_price laesst sich nichts beurteilen -> kein Flag."""
    e = _attach_fill_prices({"action": "SCANNER_BUY", "symbol": "AAA"},
                            {"orderForOpen": {"avgFillPrice": 123.45}})
    assert e["avg_fill_price"] == 123.45
    assert "fill_price_implausible" not in e


def test_grenzwert_ist_symmetrisch():
    """Knapp drueber faellt auf, knapp drunter nicht — in beide Richtungen."""
    d = _FILL_PLAUSIBILITY_MAX_DEV
    drunter = _attach_fill_prices({}, _result(100 * (1 + d * 0.9), 100))
    drueber = _attach_fill_prices({}, _result(100 * (1 + d * 1.1), 100))
    runter = _attach_fill_prices({}, _result(100 * (1 - d * 1.1), 100))
    assert "fill_price_implausible" not in drunter
    assert drueber["fill_price_implausible"] is True
    assert runter["fill_price_implausible"] is True


def test_kaputter_broker_result_kippt_nicht():
    for murks in (None, {}, {"orderForOpen": None}, {"orderForOpen": "kaputt"},
                  {"orderForOpen": {"avgFillPrice": "abc", "intendedPrice": 1}}):
        e = _attach_fill_prices({"action": "SCANNER_BUY"}, murks)
        assert isinstance(e, dict)


# ============================================================
# Linie 2: Filter beim Auswerten (wirkt auf Altdaten)
# ============================================================

def _hist(*eintraege):
    basis = {"status": "executed", "action": "SCANNER_BUY",
             "timestamp": "2026-07-15T10:00:00"}
    return [dict(basis, **e) for e in eintraege]


def _fills(history):
    # load_json wird IN der Funktion importiert -> an der Quelle patchen.
    from app import cost_model_calibrator as c
    with patch("app.config_manager.load_json", return_value=history), \
         patch.object(c, "_build_symbol_to_class_map", return_value={}):
        return c._load_trade_fills(max_age_days=3650)


def test_kalibrator_verwirft_unplausible_altdaten():
    """Ohne Flag, nur ueber die Schwelle — greift rueckwirkend."""
    h = _hist({"symbol": "COPPER", "avg_fill_price": 446.09, "intended_price": 40.46},
              {"symbol": "LRN", "avg_fill_price": 85.89, "intended_price": 91.79})
    fills = _fills(h)
    assert [f.symbol for f in fills] == ["LRN"]


def test_kalibrator_respektiert_das_flag():
    """Markierte Eintraege fliegen raus, auch wenn die Zahlen harmlos aussehen."""
    h = _hist({"symbol": "AAA", "avg_fill_price": 101.0, "intended_price": 100.0,
               "fill_price_implausible": True},
              {"symbol": "BBB", "avg_fill_price": 101.0, "intended_price": 100.0})
    fills = _fills(h)
    assert [f.symbol for f in fills] == ["BBB"]


def test_kalibrator_behaelt_gute_fills():
    h = _hist({"symbol": "AAA", "avg_fill_price": 100.5, "intended_price": 100.0},
              {"symbol": "BBB", "avg_fill_price": 99.2, "intended_price": 100.0},
              {"symbol": "CCC", "avg_fill_price": 103.0, "intended_price": 100.0})
    assert len(_fills(h)) == 3


def test_verworfene_werden_geloggt(caplog):
    """Still filtern waere derselbe Fehler wie bei der Tages-Zusammenfassung."""
    import logging
    h = _hist({"symbol": "COPPER", "avg_fill_price": 446.09, "intended_price": 40.46},
              {"symbol": "IWM", "avg_fill_price": 11.51, "intended_price": 279.03})
    with caplog.at_level(logging.WARNING):
        _fills(h)
    text = caplog.text
    assert "unplausible" in text.lower()
    assert "2" in text and "COPPER" in text


def test_alle_sechs_vorfaelle_fliegen_raus():
    """Regression auf den kompletten Datensatz vom 21.07.2026."""
    h = _hist(
        {"symbol": "COPPER", "avg_fill_price": 446.09, "intended_price": 40.46},
        {"symbol": "COPPER", "avg_fill_price": 284.45, "intended_price": 37.93},
        {"symbol": "COPPER", "avg_fill_price": 191.14, "intended_price": 40.49},
        {"symbol": "COPPER", "avg_fill_price": 74.25, "intended_price": 40.45},
        {"symbol": "IWM", "avg_fill_price": 11.51, "intended_price": 279.03},
        {"symbol": "OIL", "avg_fill_price": 60.26, "intended_price": 153.73},
        {"symbol": "LRN", "avg_fill_price": 85.89, "intended_price": 91.79},
    )
    fills = _fills(h)
    assert [f.symbol for f in fills] == ["LRN"], "nur der echte Slippage-Fall bleibt"
