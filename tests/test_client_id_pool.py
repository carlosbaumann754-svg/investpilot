"""R-B29 (21.07.2026) — clientId-Vergabe ohne Kollision mit festen Diensten.

Kontext: Beim Not-Aus-Vorfall am 21.07. fiel als Nebenbefund auf, dass 197
(Session-Watchdog) und 199 (SelfTest) INNERHALB des Zufallsbereichs 100-999
liegen, aus dem readonly-Verbindungen ihre clientId ziehen. Jede solche
Verbindung hatte damit eine Chance von 2/900, einem laufenden Dienst die
IBKR-Verbindung wegzuschiessen ("Error 326: client id already in use").

Die Folgen sind unangenehm asymmetrisch: der Watchdog ist genau das System, das
Verbindungsprobleme melden soll. Faellt er durch eine Kollision aus, faellt die
Ueberwachung still mit aus.
"""
import random

from app.ibkr_client import (
    RESERVED_CLIENT_IDS,
    _RANDOM_CLIENT_ID_MAX,
    _RANDOM_CLIENT_ID_MIN,
    _RANDOM_CLIENT_ID_POOL,
    random_client_id,
)


def test_pool_enthaelt_keine_reservierten_ids():
    """Der Kern: strukturell ausgeschlossen, nicht nur unwahrscheinlich."""
    assert not (set(_RANDOM_CLIENT_ID_POOL) & set(RESERVED_CLIENT_IDS))


def test_pool_deckt_den_bereich_ab():
    """Nur die reservierten IDs fehlen — sonst bleibt der Bereich vollstaendig."""
    erwartet = set(range(_RANDOM_CLIENT_ID_MIN, _RANDOM_CLIENT_ID_MAX + 1)) \
        - set(RESERVED_CLIENT_IDS)
    assert set(_RANDOM_CLIENT_ID_POOL) == erwartet
    # 900 moegliche Werte, davon liegen 197 und 199 im Bereich -> 898
    assert len(_RANDOM_CLIENT_ID_POOL) == 898


def test_197_und_199_sind_ausgeschlossen():
    """Die zwei konkreten Kollisionskandidaten aus dem Vorfall."""
    assert 197 not in _RANDOM_CLIENT_ID_POOL
    assert 199 not in _RANDOM_CLIENT_ID_POOL


def test_ids_ausserhalb_des_bereichs_stoeren_nicht():
    """1, 88, 89, 97, 99 liegen unter 100 und waren nie im Zufallsbereich —
    sie gehoeren trotzdem in die Liste, damit eine spaetere Bereichs-Aenderung
    sie nicht versehentlich freigibt."""
    for feste_id in (1, 88, 89, 97, 99):
        assert feste_id in RESERVED_CLIENT_IDS
        assert feste_id not in _RANDOM_CLIENT_ID_POOL


def test_random_client_id_liefert_nur_gueltige_werte():
    random.seed(1234)
    for _ in range(5000):
        cid = random_client_id()
        assert _RANDOM_CLIENT_ID_MIN <= cid <= _RANDOM_CLIENT_ID_MAX
        assert cid not in RESERVED_CLIENT_IDS


def test_random_client_id_streut_ueber_den_pool():
    """Gegenprobe zur Trivialloesung 'immer denselben Wert zurueckgeben'."""
    random.seed(99)
    gezogen = {random_client_id() for _ in range(2000)}
    assert len(gezogen) > 500


def test_not_aus_id_bleibt_ausserhalb_des_pools():
    """R-B22: der Not-Aus braucht eine ID, die ihm niemand wegnehmen kann —
    sonst ist der Fehler von damals wieder da, nur seltener."""
    from app.ibkr_client import IBG_EMERGENCY_CLIENT_ID
    assert IBG_EMERGENCY_CLIENT_ID in RESERVED_CLIENT_IDS
    assert IBG_EMERGENCY_CLIENT_ID not in _RANDOM_CLIENT_ID_POOL
