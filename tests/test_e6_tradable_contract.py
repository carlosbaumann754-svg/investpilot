"""R-B46 (28.07.2026) — E6-Stops muessen auf handelbare Boersen routen.

Live-Fall: ib.positions() lieferte den AVNS-Kontrakt mit exchange='VALUE'
(IBKRs reiner Bewertungs-Platz). Phase 2 des E6-Abgleichs reichte ihn
unveraendert an placeOrder — Error 201 'No trading for non-tradable
valuation-only contracts', drei Tage lang taeglich. Ausgerechnet die
Absturz-Versicherung fehlte damit fuer die groesste Position (99.5k),
waehrend der Freitag-Vorfall gerade gezeigt hatte, wofuer E6 existiert.

Ohne ib_insync-Abhaengigkeit getestet (die Suite fakt das Modul) — der Helper
arbeitet per copy und muss mit Fakes wie mit echten Kontrakten funktionieren.
"""
from app.ibkr_client import _orderfaehiger_kontrakt


class _FakeContract:
    def __init__(self, conId, symbol, exchange, currency="USD"):
        self.conId = conId
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency


def test_value_wird_auf_smart_umgeroutet():
    """DER AVNS-Fall: VALUE ist nicht handelbar -> SMART, Rest bleibt."""
    c = _FakeContract(321325546, "AVNS", "VALUE")
    o = _orderfaehiger_kontrakt(c)
    assert o.exchange == "SMART"
    assert o.conId == 321325546
    assert o.symbol == "AVNS"
    assert c.exchange == "VALUE"          # Original unangetastet (Kopie!)


def test_leere_exchange_wird_auf_smart_umgeroutet():
    assert _orderfaehiger_kontrakt(_FakeContract(1, "X", "")).exchange == "SMART"


def test_handelbare_boerse_bleibt_dasselbe_objekt():
    """NYSE/NASDAQ/SMART: unveraendert durchreichen — kein Klon, keine Kopie."""
    c = _FakeContract(6929, "ESE", "NYSE")
    assert _orderfaehiger_kontrakt(c) is c


def test_funktioniert_ohne_ib_insync():
    """Der Grund fuer die copy-Loesung: im Test-Umfeld existiert ib_insync nur
    als Fake ohne Stock — ein Import-Versuch haette den E6-Abgleich still
    gekillt (non-fatal-except) und der Fix waere wirkungslos gewesen."""
    import sys
    assert "ib_insync" not in sys.modules or not hasattr(
        sys.modules.get("ib_insync"), "__version__")
    o = _orderfaehiger_kontrakt(_FakeContract(2, "Y", "VALUE"))
    assert o.exchange == "SMART"
