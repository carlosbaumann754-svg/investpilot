"""Untertitel: Blockbildung, Zeitbasis und Lesbarkeit.

Die zwei Fehlerklassen, die hier abgesichert werden, sind im ersten Testlauf
der Pipeline tatsächlich aufgetreten:

* Zeilen liefen seitlich aus dem Bild, weil Zeichenlimit und Schriftgrösse
  unabhängig voneinander konfiguriert waren.
* Am Satzende blieb ein Ein-Wort-Block mit 0.4 Sekunden Standzeit übrig —
  technisch korrekt, aber nicht lesbar.
"""
import pytest

from video_generator.config import lade_config
from video_generator.models import SceneFit, VoiceResult, WordTiming
from video_generator.subtitles import (_wrap_zeilen, cues_fuer_szene,
                                       max_zeichen_pro_zeile, schreibe_ass,
                                       schreibe_srt)


def _voice(text: str, dauer: float = 6.0, index: int = 1) -> VoiceResult:
    """Baut ein Voiceover-Ergebnis mit gleichmässig verteilten Wortzeiten."""
    tokens = text.split()
    pro_wort = dauer / len(tokens)
    return VoiceResult(
        index=index, pfad="egal.mp3", dauer_s=dauer, stimme="test",
        woerter=[WordTiming(t, i * pro_wort, (i + 1) * pro_wort)
                 for i, t in enumerate(tokens)])


def _fit(index=1, scene=8.0, lead=0.25, tempo=1.0, voice=6.0) -> SceneFit:
    return SceneFit(index=index, scene_dauer_s=scene, audio_lead_s=lead,
                    audio_tail_s=scene - lead - voice, audio_tempo=tempo,
                    video_hold_s=0.0, video_trim_s=0.0, voice_dauer_s=voice)


@pytest.fixture
def cfg():
    return lade_config()


def test_zeichenlimit_folgt_der_schriftgroesse(cfg):
    """Hochformat braucht kürzere Zeilen als Querformat — automatisch."""
    hoch = max_zeichen_pro_zeile(lade_config(aspect_ratio="9:16"))
    quer = max_zeichen_pro_zeile(lade_config(aspect_ratio="16:9"))

    assert hoch < quer
    # Und über die Auflösungen hinweg bleibt es stabil, weil beides in Prozent
    # der Bildhöhe gerechnet wird.
    for res in ("540p", "720p", "1080p"):
        assert abs(max_zeichen_pro_zeile(lade_config(aufloesung=res)) - hoch) <= 2


def test_explizites_limit_schlaegt_die_berechnung():
    assert max_zeichen_pro_zeile(lade_config(untertitel_max_zeichen=25)) == 25


def test_keine_zeile_laeuft_aus_dem_bild(cfg):
    voice = _voice("Die meisten Menschen rechnen linear. Geld waechst aber "
                   "nicht linear sondern exponentiell ueber viele Jahre.")
    cues = cues_fuer_szene(voice, _fit(), start_s=0.0, cfg=cfg)

    grenze = max_zeichen_pro_zeile(cfg)
    for c in cues:
        zeilen = c.text.split("\n")
        assert len(zeilen) <= cfg.untertitel_max_zeilen, c.text
        assert all(len(z) <= grenze for z in zeilen), c.text


def test_keine_unlesbaren_restbloecke(cfg):
    """Ein Satz wird gleichmässig verteilt, nicht gierig plus Rest."""
    voice = _voice("Auf dem ersten Feld ein Reiskorn.", dauer=4.0)
    cues = cues_fuer_szene(voice, _fit(voice=4.0), start_s=0.0, cfg=cfg)

    assert len(cues) >= 2
    for c in cues:
        assert c.end_s - c.start_s >= 0.7, f"zu kurz: {c.text!r}"


def test_bloecke_ueberlappen_sich_nicht(cfg):
    voice = _voice("Eins zwei drei vier fuenf sechs sieben acht neun zehn elf zwoelf.")
    cues = cues_fuer_szene(voice, _fit(), start_s=0.0, cfg=cfg)

    for a, b in zip(cues, cues[1:]):
        assert a.end_s <= b.start_s + 1e-6
        assert a.start_s < a.end_s


def test_zeiten_beruecksichtigen_szenenstart_und_vorlauf(cfg):
    voice = _voice("Ein kurzer Satz hier.", dauer=2.0)
    cues = cues_fuer_szene(voice, _fit(lead=0.25, voice=2.0), start_s=16.0, cfg=cfg)

    assert cues[0].start_s == pytest.approx(16.25, abs=0.01)


def test_tempo_korrigiert_die_zeitbasis(cfg):
    """Wird die Stimme gestaucht, rücken die Untertitel mit.

    Ohne diese Division wandern genau die Szenen aus dem Takt, in denen der
    Ton ohnehin schon eng ist — der Fehler fällt am Videoende maximal auf.
    Kurzer Text, damit in beiden Fällen genau ein Block entsteht und wirklich
    die Zeitbasis verglichen wird und nicht die Blockaufteilung.
    """
    voice = _voice("Ein kurzer Satz.", dauer=2.0)
    normal = cues_fuer_szene(voice, _fit(tempo=1.0, voice=2.0), 0.0, cfg)
    schnell = cues_fuer_szene(voice, _fit(tempo=1.25, voice=1.6), 0.0, cfg)

    assert len(normal) == len(schnell) == 1
    assert normal[0].end_s == pytest.approx(0.25 + 2.0, abs=0.02)
    assert schnell[0].end_s == pytest.approx(0.25 + 2.0 / 1.25, abs=0.02)


def test_ohne_wort_timings_gibt_es_trotzdem_untertitel(cfg):
    """Fallback, falls die Stimme keine WordBoundary-Events liefert."""
    voice = VoiceResult(index=1, pfad="x.mp3", dauer_s=3.0, stimme="test", woerter=[])
    cues = cues_fuer_szene(voice, _fit(voice=3.0), 0.0, cfg,
                           text_fallback="Ein Satz ohne Timings.")

    assert len(cues) == 1
    assert "Satz" in cues[0].text


def test_wrap_trennt_keine_woerter():
    assert _wrap_zeilen(["Donaudampfschifffahrtsgesellschaft"], 10) == \
        ["Donaudampfschifffahrtsgesellschaft"]


def test_srt_ist_wohlgeformt(cfg, tmp_path):
    voice = _voice("Ein Satz. Noch ein Satz hier.", dauer=4.0)
    cues = cues_fuer_szene(voice, _fit(voice=4.0), 0.0, cfg)
    pfad = schreibe_srt(cues, tmp_path / "t.srt")

    inhalt = pfad.read_text(encoding="utf-8")
    assert inhalt.startswith("1\n")
    assert " --> " in inhalt
    assert inhalt.count("-->") == len(cues)


def test_ass_traegt_die_echten_videomasse(cfg, tmp_path):
    voice = _voice("Ein Satz hier.", dauer=2.0)
    cues = cues_fuer_szene(voice, _fit(voice=2.0), 0.0, cfg)
    inhalt = schreibe_ass(cues, tmp_path / "t.ass", cfg).read_text(encoding="utf-8")

    breite, hoehe = cfg.breite_hoehe
    assert f"PlayResX: {breite}" in inhalt
    assert f"PlayResY: {hoehe}" in inhalt
    assert inhalt.count("Dialogue:") == len(cues)
    # Mehrzeilige Blöcke werden als \N geschrieben, nicht als echter Umbruch —
    # ein echter Zeilenumbruch würde die Dialogue-Zeile zerreissen.
    for zeile in inhalt.splitlines():
        if zeile.startswith("Dialogue:"):
            assert "\n" not in zeile
