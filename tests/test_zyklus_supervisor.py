"""R-B49 — Zyklus-Supervisor: jede Entscheidung einzeln fixiert.

Der Supervisor ist die letzte Verteidigungslinie gegen den Vorfall vom
24.07. (Handelsschleife hing 3h35min, Watchdog meldete, nichts heilte).
Gerade WEIL er `docker restart` ausfuehren darf, muss seine Logik wasserdicht
sein: falsche Neustarts kosten Handelszeit, endloses Durchstarten wuerde ein
echtes Problem verschleiern.
"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sup = importlib.import_module("zyklus_supervisor")

NOW = 1_000_000.0


def test_frischer_herzschlag_ok():
    a, _ = sup.decide(300, {}, NOW)
    assert a == "ok"


def test_genau_an_der_schwelle_noch_ok():
    a, _ = sup.decide(sup.STALE_SEC, {}, NOW)
    assert a == "ok"


def test_stale_ohne_vorgeschichte_restartet():
    a, grund = sup.decide(sup.STALE_SEC + 60, {}, NOW)
    assert a == "restart"
    assert "min alt" in grund


def test_unlesbarer_herzschlag_greift_nie_ein():
    """Kein Eingriff auf Verdacht: Datei kaputt/fehlend darf NIE einen
    Neustart ausloesen — sonst wird ein Backup-/IO-Problem zum Trading-Ausfall."""
    a, _ = sup.decide(None, {}, NOW)
    assert a == "ok"


def test_cooldown_nach_frischem_neustart():
    """Nach einem Eingriff erst wirken lassen (Container braucht ~1 Min,
    Herzschlag ~5 Min) — sonst Restart-Schleife im 5-Min-Takt."""
    state = {"restarts": [NOW - 600]}          # vor 10 Min neu gestartet
    a, _ = sup.decide(sup.STALE_SEC + 60, state, NOW)
    assert a == "cooldown"


def test_nach_cooldown_wieder_restart():
    state = {"restarts": [NOW - sup.COOLDOWN_SEC - 1]}
    a, _ = sup.decide(sup.STALE_SEC + 60, state, NOW)
    assert a == "restart"


def test_gibt_nach_drei_neustarts_auf():
    """Heilt dreimaliges Durchstarten nicht, braucht es Haende — Emergency
    statt Endlosschleife (die das echte Problem nur verschleiern wuerde)."""
    state = {"restarts": [NOW - 5000, NOW - 3600, NOW - sup.COOLDOWN_SEC - 1]}
    a, _ = sup.decide(sup.STALE_SEC + 60, state, NOW)
    assert a == "give_up"


def test_eskaliert_nur_einmal():
    state = {"restarts": [NOW - 5000, NOW - 3600, NOW - 2000],
             "gave_up_at": NOW - 300}
    a, _ = sup.decide(sup.STALE_SEC + 60, state, NOW)
    assert a == "silent"


def test_alte_neustarts_fallen_aus_dem_fenster():
    """Neustarts aelter als 6h zaehlen nicht mehr — ein Vorfall pro Woche
    darf nicht in die Aufgabe-Logik von heute hineinwirken."""
    state = {"restarts": [NOW - sup.ROLLING_WINDOW_SEC - 10] * 3}
    a, _ = sup.decide(sup.STALE_SEC + 60, state, NOW)
    assert a == "restart"


def test_naive_herzschlag_zeit_wird_als_zurich_gelesen(tmp_path, monkeypatch):
    """Die Zeitzonen-Falle der Woche: Herzschlag ist naive Container-Zeit
    (Europe/Zurich), der Host laeuft UTC. Ein frischer Herzschlag darf nicht
    wegen +2h-Fehlinterpretation als 2h-stale gelten."""
    import json
    from datetime import datetime
    hb = tmp_path / "alert_state.json"
    jetzt_zurich = datetime(2026, 7, 29, 19, 0, 0)
    hb.write_text(json.dumps({"last_heartbeat": jetzt_zurich.isoformat()}))
    monkeypatch.setattr(sup, "HEARTBEAT_FILE", str(hb))
    now = jetzt_zurich.replace(minute=3, tzinfo=sup.TZ)   # 3 Min spaeter
    alter = sup.heartbeat_age_sec(now)
    assert 170 <= alter <= 190                             # ~180s, NICHT ~7380s
