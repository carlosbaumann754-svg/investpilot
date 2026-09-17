"""Sprachsynthese: Wort-Timings und Fehlerverhalten.

Die eigentliche Leistung von edge-tts für diese Pipeline sind nicht die
Audiodaten, sondern die WordBoundary-Events: sie machen die Untertitel exakt
statt geschätzt. Deren Umrechnung ist der Teil, der hier abgesichert wird —
mit einem gefälschten ``edge_tts``-Modul, damit der Test ohne Netzzugriff
läuft und nicht an Microsofts Verfügbarkeit hängt.
"""
import sys
import types

import pytest

from video_generator.config import lade_config
from video_generator.tts import TTSFehler, synthesize, synthesize_dry

TICKS = 10_000_000          # edge-tts rechnet in 100-Nanosekunden-Schritten


def _fake_edge_tts(monkeypatch, chunks, erwartet: dict | None = None):
    """Hängt ein Ersatz-``edge_tts`` in sys.modules, das ``chunks`` streamt."""
    gesehen = {}

    class Communicate:
        def __init__(self, text, voice, **kw):
            gesehen.update({"text": text, "voice": voice, **kw})

        async def stream(self):
            for c in chunks:
                yield c

    modul = types.ModuleType("edge_tts")
    modul.Communicate = Communicate
    monkeypatch.setitem(sys.modules, "edge_tts", modul)
    return gesehen


@pytest.fixture
def cfg():
    return lade_config()


def test_wort_offsets_werden_in_sekunden_umgerechnet(monkeypatch, cfg, tmp_path):
    """100-Nanosekunden-Ticks in Sekunden — hier entstehen sonst Faktor-10-Fehler."""
    _fake_edge_tts(monkeypatch, [
        {"type": "audio", "data": b"\x00" * 4096},
        {"type": "WordBoundary", "offset": 0, "duration": TICKS // 2, "text": "Die"},
        {"type": "WordBoundary", "offset": TICKS, "duration": TICKS, "text": "Zeit"},
    ])

    r = synthesize(1, "Die Zeit", cfg, tmp_path / "v.mp3")

    assert [(w.text, w.start_s, w.end_s) for w in r.woerter] == [
        ("Die", 0.0, 0.5), ("Zeit", 1.0, 2.0)]


def test_audiodaten_landen_vollstaendig_auf_der_platte(monkeypatch, cfg, tmp_path):
    _fake_edge_tts(monkeypatch, [
        {"type": "audio", "data": b"AB"},
        {"type": "audio", "data": b"CD"},
        {"type": "WordBoundary", "offset": 0, "duration": TICKS, "text": "x"},
    ])
    pfad = tmp_path / "v.mp3"

    synthesize(1, "x", cfg, pfad)

    assert pfad.read_bytes() == b"ABCD"


def test_dauer_faellt_auf_die_wort_timings_zurueck(monkeypatch, cfg, tmp_path):
    """Kein lesbares Audio für ffprobe: dann zählt das letzte Wort.

    Besser eine leicht geschätzte Dauer als ein abgebrochener Lauf — die
    Szene wird im Schnitt ohnehin auf diese Länge gepasst.
    """
    _fake_edge_tts(monkeypatch, [
        {"type": "audio", "data": b"kein echtes mp3"},
        {"type": "WordBoundary", "offset": 2 * TICKS, "duration": TICKS, "text": "Ende"},
    ])

    r = synthesize(1, "Ende", cfg, tmp_path / "v.mp3")

    assert r.dauer_s == pytest.approx(3.2, abs=0.01)


def test_konfigurierte_stimme_wird_durchgereicht(monkeypatch, tmp_path):
    cfg = lade_config(tts_stimme="de-CH-LeniNeural", tts_rate="+10%")
    gesehen = _fake_edge_tts(monkeypatch, [
        {"type": "audio", "data": b"x" * 100},
        {"type": "WordBoundary", "offset": 0, "duration": TICKS, "text": "x"},
    ])

    synthesize(1, "x", cfg, tmp_path / "v.mp3")

    assert gesehen["voice"] == "de-CH-LeniNeural"
    assert gesehen["rate"] == "+10%"


def test_stumme_antwort_ist_ein_fehler(monkeypatch, cfg, tmp_path):
    """Ohne Audiodaten darf keine leere Datei als Erfolg durchgehen."""
    _fake_edge_tts(monkeypatch, [
        {"type": "WordBoundary", "offset": 0, "duration": TICKS, "text": "x"}])

    with pytest.raises(TTSFehler, match="Keine Audiodaten"):
        synthesize(1, "x", cfg, tmp_path / "v.mp3")


def test_leerer_text_wird_gar_nicht_erst_gesendet(cfg, tmp_path):
    with pytest.raises(TTSFehler, match="leerer Voiceover-Text"):
        synthesize(1, "   ", cfg, tmp_path / "v.mp3")


def test_trockenlauf_verteilt_die_woerter_gleichmaessig(cfg, tmp_path):
    r = synthesize_dry(1, "eins zwei drei vier", cfg, tmp_path / "v.mp3")

    assert len(r.woerter) == 4
    assert r.woerter[0].start_s == 0.0
    assert r.woerter[-1].end_s == pytest.approx(r.dauer_s)
    assert r.dauer_s == pytest.approx(4 / cfg.woerter_pro_sekunde, abs=0.01)
    # Lückenlos: jedes Wort schliesst an das vorige an.
    for a, b in zip(r.woerter, r.woerter[1:]):
        assert a.end_s == pytest.approx(b.start_s)
