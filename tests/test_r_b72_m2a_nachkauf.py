"""R-B72 (30.10.2026): M2a-Kaeufe voll ausfuehren — Nachkauf im Kauf-Fenster
und Preisobergrenze 2% statt 0.5%.

Befund: Von 15 M2a-Kauforders wurden nur ~39% des Zielbetrags gefuellt.
(1) Die Limit-Order wartet 30 s und storniert dann den Rest; bei Small Caps
werden in 30 s nur einige hundert Aktien gehandelt (CARG 120/1424, AGO 100/636).
(2) Paper-Kurse sind 15 Min verzoegert: Limit = alter Kurs +0.5% lag bei GIII,
EPRT, ARI unter dem Markt -> null Fuellung. Eine Teilfuellung zaehlte als einer
der 5 Monatskaeufe und wurde nie aufgestockt.
Carlos-Freigabe 30.10.2026 ("ja setze 1 und 2 so um").
"""
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from app import m2a_motor

HEUTE = date(2026, 11, 2)
SCHNITT = "2026-08-31"


def _buy(pid, symbol, ts, status, ziel, iid=None, action="SCANNER_BUY"):
    return {"timestamp": ts, "action": action, "symbol": symbol,
            "position_id": str(pid), "instrument_id": iid or pid,
            "status": status, "amount_usd": ziel}


HIST = [
    _buy(292179068, "CARG", "2026-10-01T16:09:28", "partial", 43423.01, 900111),
    _buy(1, "ALT", "2026-11-02T16:00:10", "partial", 43000.0, 901),
    _buy(2, "VOLL", "2026-11-02T16:01:10", "executed", 43000.0, 902),
    _buy(3, "NULL", "2026-11-02T16:02:10", "cancelled", 43000.0, 903),
    _buy(4, "NACH", "2026-11-02T16:03:10", "partial", 43000.0, 904),
]


class TestNachkaufZiele:
    def test_nur_gezaehlte_kaeufe_des_laufenden_monats(self):
        z = m2a_motor.nachkauf_ziele(HIST, heute=HEUTE, schnitt_datum=SCHNITT)
        assert set(z) == {"1", "2", "4"}       # Oktober-CARG + Storno draussen
        assert z["1"]["ziel_usd"] == 43000.0
        assert z["1"]["instrument_id"] == 901  # Scanner-ID fuer client.buy
        assert z["1"]["symbol"] == "ALT"

    def test_vor_dem_schnitt_zaehlt_nicht(self):
        z = m2a_motor.nachkauf_ziele(
            [_buy(9, "X", "2026-08-31T07:00:00", "partial", 40000.0)],
            heute=date(2026, 8, 31), schnitt_datum="2026-08-31T07:48:30")
        assert z == {}

    def test_nachkauf_eintraege_sind_keine_ziele(self):
        hist = HIST + [_buy(1, "ALT", "2026-11-02T16:10:00", "partial", 30000.0,
                            901, action="M2A_NACHKAUF")]
        z = m2a_motor.nachkauf_ziele(hist, heute=HEUTE, schnitt_datum=SCHNITT)
        assert z["1"]["ziel_usd"] == 43000.0


class TestNachkaufRest:
    def test_carg_fall(self):
        assert m2a_motor.nachkauf_rest(43423.01, 3665.0) == pytest.approx(39758.01)

    def test_fast_voll_kein_nachkauf(self):
        """ENOV 99%: Rest unter 10% des Ziels -> kein Mini-Nachkauf."""
        assert m2a_motor.nachkauf_rest(43705.86, 42470.0) == 0.0

    def test_mindestbetrag(self):
        assert m2a_motor.nachkauf_rest(5000.0, 4100.0) == 0.0   # 900 < 1000 USD

    def test_ueberfuellt_nie_negativ(self):
        assert m2a_motor.nachkauf_rest(43000.0, 44000.0) == 0.0


def test_kauf_limit_puffer():
    assert m2a_motor.kauf_limit_puffer_pct({}) == 2.0
    assert m2a_motor.kauf_limit_puffer_pct({"m2a": {"kauf_limit_puffer_pct": 1.5}}) == 1.5


def test_nachkauf_zaehlt_nicht_ins_anlauf_limit():
    hist = HIST + [_buy(1, "ALT", "2026-11-02T16:10:00", "executed", 30000.0,
                        901, action="M2A_NACHKAUF")]
    assert m2a_motor.neukaeufe_diesen_monat(hist, heute=HEUTE, schnitt_datum=SCHNITT) == 3


def test_nachkauf_verschiebt_die_126_tage_uhr_nicht(monkeypatch):
    from app import trader
    hist = [_buy(1, "ALT", "2026-11-02T16:00:10", "partial", 43000.0, 901),
            _buy(1, "ALT", "2026-11-03T16:00:10", "executed", 30000.0, 901,
                 action="M2A_NACHKAUF")]
    monkeypatch.setattr(trader, "load_json", lambda name: hist)
    dt, _ = trader._find_position_open_time("1")
    assert dt.astimezone().date() == date(2026, 11, 2)


class _Client:
    broker_name = "ibkr"

    def __init__(self, ergebnis_status="Filled"):
        self.calls = []
        self.status = ergebnis_status

    def buy(self, instrument_id, amount_usd, leverage=1, limit_slippage_pct=None):
        self.calls.append((instrument_id, round(amount_usd, 2), limit_slippage_pct))
        return {"orderForOpen": {"orderID": "77", "statusID": self.status,
                                 "filledQuantity": 10, "avgFillPrice": 30.0,
                                 "intendedPrice": 30.0, "refQuote": 30.0}}


def _pos(pid, symbol, investiert):
    return {"position_id": str(pid), "symbol": symbol, "invested": investiert}


class TestTraderNachkauf:
    def _lauf(self, monkeypatch, positionen, cash=1_000_000.0, client=None):
        from app import trader
        gespeichert = []
        monkeypatch.setattr(trader, "save_trade", lambda e: gespeichert.append(e))
        client = client or _Client()
        out = trader._m2a_nachkaeufe(client, {"m2a": {"aktiv": True}}, positionen,
                                     cash, HIST, heute=HEUTE, schnitt_datum=SCHNITT)
        return client, out, gespeichert

    def test_teilgefuellte_position_wird_aufgestockt(self, monkeypatch):
        client, out, gespeichert = self._lauf(monkeypatch, [
            _pos(1, "ALT", 8000.0),      # 35k fehlen
            _pos(2, "VOLL", 42900.0),    # voll
            _pos(292179068, "CARG", 3665.0),  # Oktober-Kauf: NICHT aufstocken
        ])
        assert client.calls == [(901, 35000.0, 2.0)]
        assert len(out) == 1 and out[0]["action"] == "M2A_NACHKAUF"
        assert out[0]["symbol"] == "ALT" and out[0]["position_id"] == "1"
        assert out[0]["ziel_usd"] == 43000.0
        assert gespeichert == out

    def test_nicht_mehr_gehaltene_position_wird_nicht_gekauft(self, monkeypatch):
        """Teilgefuellt, aber inzwischen weg (z.B. E6-Stop) -> kein Nachkauf."""
        client, out, _ = self._lauf(monkeypatch, [_pos(2, "VOLL", 42900.0)])
        assert client.calls == [] and out == []

    def test_cash_begrenzt_den_nachkauf(self, monkeypatch):
        client, out, _ = self._lauf(monkeypatch, [_pos(1, "ALT", 8000.0)], cash=12000.0)
        assert client.calls == [(901, 12000.0, 2.0)]

    def test_zu_wenig_cash_kein_auftrag(self, monkeypatch):
        client, out, _ = self._lauf(monkeypatch, [_pos(1, "ALT", 8000.0)], cash=400.0)
        assert client.calls == []

    def test_mehrere_nachkaeufe_teilen_sich_das_cash(self, monkeypatch):
        client, out, _ = self._lauf(monkeypatch, [_pos(1, "ALT", 8000.0),
                                                  _pos(4, "NACH", 3000.0)],
                                    cash=50000.0)
        assert client.calls == [(901, 35000.0, 2.0), (904, 15000.0, 2.0)]


def test_ibkr_buy_reicht_puffer_durch():
    from app.ibkr_client import IbkrBroker
    b = IbkrBroker.__new__(IbkrBroker)
    with mock.patch.object(IbkrBroker, "_place_market_order", return_value={}) as pm:
        b.buy(5, 1000.0, limit_slippage_pct=2.0)
        assert pm.call_args.kwargs.get("limit_slippage_pct") == 2.0
        b.buy(5, 1000.0)
        assert pm.call_args.kwargs.get("limit_slippage_pct") is None


def test_trader_verdrahtung():
    """Quellen-Check: Limit erschoepft -> kein frueher Abbruch mehr (Nachkauf
    muss trotzdem laufen); Nachkauf vor den Neukaeufen; Neukaeufe mit Puffer."""
    src = (Path(__file__).parent.parent / "app" / "trader.py").read_text(encoding="utf-8")
    start = src.find("def execute_scanner_trades")
    body = src[start:src.find("\ndef ", start + 1)]
    m2a_block = body[body.find("R-B66: M2A-Kauf-Fenster"):body.find("credit = portfolio.get")]
    assert "return []" in m2a_block.split("rest_neukaeufe")[0]   # Fenster-Check bleibt
    assert "return []" not in m2a_block.split("rest_neukaeufe")[1]
    assert body.find("_m2a_nachkaeufe(") < body.find("available_slots = max_positions")
    assert "limit_slippage_pct" in body[body.find("SCANNER BUY: {symbol}"):]


def test_m2a_positionen_nie_auf_scanner_signal_verkauft():
    """Regelwerk: M2a-Ausstieg nur Horizont/E6. Der SCANNER_SELL-Pfad muss
    M2a-Positionen ueberspringen (er laeuft seit R-B72 an Kauftagen oefter)."""
    src = (Path(__file__).parent.parent / "app" / "trader.py").read_text(encoding="utf-8")
    start = src.find("# --- VERKAUFEN: Positionen mit SELL-Signal ---")
    block = src[start:src.find('result = _close_position_safe(client, p["position_id"], '
                               'p["instrument_id"], "SCANNER_SELL")', start)]
    assert "_m2a_rest is not None and not _m2a.ist_geerbt(" in block
    assert "continue" in block.split("ist_geerbt(")[1]
