"""R-B22 (21.07.2026) — Der Not-Aus braucht eine EIGENE clientId.

DER DEFEKT
----------
Dashboard (uvicorn) und Handelsschleife (python -m app.scheduler) sind GETRENNTE
Prozesse. Der Scheduler haelt clientId=1 dauerhaft. Der Kill-Switch braucht eine
SCHREIBFAEHIGE Verbindung und holte sie sich mit readonly=False — womit er
ebenfalls clientId=1 aus der config bekam:

    Error 326, reqId -1: Unable to connect as the client id is already in use
    [3/3] Alle 3 Fetch-Versuche lieferten keine Positionen
    Emergency-Close-Resultat: 0 geschlossen

Am 21.07.2026 real passiert. Das Fatale ist der Umkehrschluss: **Der Not-Aus
konnte genau dann nicht schliessen, wenn der Bot laeuft — also immer, wenn man
ihn braucht.** Trading-Flag und Risk-Pause (Phase 1+2) griffen; nur das
Schliessen bestehender Positionen war tot.

Vorgeschichte: davor war der Pfad readonly=True und lieferte leere Portfolios
(Vorfall 29.04.2026). Es wurde ein Problem gegen das andere getauscht — beide
Varianten waren kaputt, nur unterschiedlich.

Diese Tests halten fest, dass der Not-Aus schreibfaehig UND konfliktfrei ist.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.ibkr_client import IBG_EMERGENCY_CLIENT_ID, IbkrBroker


# ============================================================
# Die ID selbst
# ============================================================

def test_notaus_id_ist_nicht_die_bot_id():
    from app.ibkr_client import IBG_CLIENT_ID
    assert IBG_EMERGENCY_CLIENT_ID != IBG_CLIENT_ID


def test_notaus_id_liegt_ausserhalb_des_zufallsbereichs():
    """readonly-Verbindungen ziehen zufaellig aus 100-999 — dort darf sie nicht liegen."""
    assert not (100 <= IBG_EMERGENCY_CLIENT_ID <= 999)


def test_notaus_id_kollidiert_nicht_mit_reservierten():
    """1=Bot, 88=Preis, 89=Sizing, 99=Reconcile, 197=Watchdog, 199=SelfTest."""
    assert IBG_EMERGENCY_CLIENT_ID not in (1, 88, 89, 99, 197, 199)


# ============================================================
# Broker-Verhalten
# ============================================================

def test_schreibfaehig_mit_expliziter_id_nutzt_diese():
    b = IbkrBroker({"ibkr": {"client_id": IBG_EMERGENCY_CLIENT_ID}}, readonly=False)
    assert b.client_id == IBG_EMERGENCY_CLIENT_ID
    assert b.readonly is False


def test_readonly_ignoriert_explizite_id():
    """Dokumentiert das bestehende Verhalten — DESHALB reicht readonly nicht.

    Ein readonly-Broker wuerde die 97 verwerfen und zufaellig ziehen; er darf
    ausserdem keine Orders senden. Fuer den Not-Aus also doppelt untauglich.
    """
    b = IbkrBroker({"ibkr": {"client_id": IBG_EMERGENCY_CLIENT_ID}}, readonly=True)
    assert b.client_id != IBG_EMERGENCY_CLIENT_ID
    assert 100 <= b.client_id <= 999


def test_bot_pfad_unveraendert():
    """Der Trader selbst muss weiterhin clientId 1 bekommen."""
    b = IbkrBroker({"ibkr": {"client_id": 1}}, readonly=False)
    assert b.client_id == 1


# ============================================================
# Die Config-Funktion, die der Not-Aus benutzt
# ============================================================

def test_notaus_config_setzt_eigene_id():
    """DER KERN: der Not-Aus darf NICHT die Bot-ID anfordern."""
    from app.ibkr_client import emergency_broker_config
    cfg = emergency_broker_config({"ibkr": {"client_id": 1, "port": 4004}})
    assert cfg["ibkr"]["client_id"] == IBG_EMERGENCY_CLIENT_ID,         "Not-Aus fordert immer noch die Bot-ID an — Error 326 kaeme zurueck"


def test_notaus_config_ist_schreibfaehig():
    from app.ibkr_client import emergency_broker_config
    cfg = emergency_broker_config({"ibkr": {"client_id": 1, "readonly": True}})
    assert cfg["ibkr"]["readonly"] is False, "Not-Aus muss Orders senden koennen"


def test_notaus_config_erhaelt_uebrige_werte():
    """Host/Port/Timeout duerfen beim Ueberschreiben nicht verlorengehen."""
    from app.ibkr_client import emergency_broker_config
    cfg = emergency_broker_config({"ibkr": {"client_id": 1, "port": 4004,
                                            "host": "ib-gateway", "timeout": 15},
                                   "demo_trading": {"enabled": True}})
    ib = cfg["ibkr"]
    assert (ib["port"], ib["host"], ib["timeout"]) == (4004, "ib-gateway", 15)
    assert cfg["demo_trading"] == {"enabled": True}


def test_notaus_config_veraendert_das_original_nicht():
    """Kopie, keine Mutation — sonst traegt der Bot-Prozess die 97 weiter."""
    from app.ibkr_client import emergency_broker_config
    original = {"ibkr": {"client_id": 1}}
    emergency_broker_config(original)
    assert original["ibkr"]["client_id"] == 1


def test_notaus_config_ohne_ibkr_section():
    from app.ibkr_client import emergency_broker_config
    for leer in (None, {}, {"ibkr": None}):
        cfg = emergency_broker_config(leer)
        assert cfg["ibkr"]["client_id"] == IBG_EMERGENCY_CLIENT_ID


def test_broker_aus_notaus_config_nutzt_die_id():
    """Ende-zu-Ende der reinen Logik: Config -> Broker -> tatsaechliche ID."""
    from app.ibkr_client import emergency_broker_config
    b = IbkrBroker(emergency_broker_config({"ibkr": {"client_id": 1}}),
                   readonly=False)
    assert b.client_id == IBG_EMERGENCY_CLIENT_ID
    assert b.readonly is False
