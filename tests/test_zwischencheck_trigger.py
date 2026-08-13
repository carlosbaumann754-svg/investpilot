"""Tests fuer den Zwischencheck-25-Wecker (R-B53) — jeder decide()-Zweig."""
from scripts.zwischencheck_trigger import TARGET, decide


def test_unter_der_marke_wartet():
    aktion, _ = decide(19, already_fired=False)
    assert aktion == "waiting"


def test_exakt_auf_der_marke_feuert():
    aktion, grund = decide(TARGET, already_fired=False)
    assert aktion == "fire"
    assert str(TARGET) in grund


def test_ueber_der_marke_feuert():
    aktion, _ = decide(TARGET + 7, already_fired=False)
    assert aktion == "fire"


def test_bereits_gemeldet_bleibt_still():
    aktion, _ = decide(TARGET + 1, already_fired=True)
    assert aktion == "silent"


def test_unlesbarer_zaehler_wartet_statt_zu_raten():
    aktion, _ = decide(None, already_fired=False)
    assert aktion == "waiting"


def test_marke_ist_25():
    # Das Protokoll (docs/ZWISCHENCHECK_25_PROTOKOLL.md) ist auf 25
    # vorregistriert — eine stille Aenderung der Marke soll auffallen.
    assert TARGET == 25
