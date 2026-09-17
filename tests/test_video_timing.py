"""Szenen-Timing: passt das Voiceover zum Clip?

Diese Tests decken die Fehlerklasse ab, die ein fertiges Video unbrauchbar
macht, ohne dass ein einziger Baustein "kaputt" ist: Ton und Bild laufen
auseinander. Die Arithmetik dafür steckt in ``video_generator.timing`` und ist
bewusst ffmpeg-frei — deshalb ist sie hier vollständig prüfbar.
"""
import pytest

from video_generator.config import lade_config
from video_generator.timing import (fit_all, fit_scene, gesamt_dauer,
                                    szenen_startzeiten)


@pytest.fixture
def cfg():
    return lade_config()


def test_kurzes_voiceover_schneidet_totes_bild_weg(cfg):
    """3s Ton in 8s Clip: der Clip wird gekürzt statt still weiterzulaufen."""
    fit = fit_scene(1, clip_dauer_s=8.0, voice_dauer_s=3.0, cfg=cfg)

    assert fit.audio_tempo == 1.0
    assert fit.video_hold_s == 0.0
    assert fit.video_trim_s > 0
    assert fit.audio_tail_s <= cfg.max_tail_stille_s + 1e-6
    assert fit.scene_dauer_s == pytest.approx(
        cfg.lead_in_s + 3.0 + cfg.max_tail_stille_s)


def test_passendes_voiceover_laesst_clip_unangetastet(cfg):
    fit = fit_scene(1, clip_dauer_s=8.0, voice_dauer_s=7.2, cfg=cfg)

    assert fit.scene_dauer_s == pytest.approx(8.0)
    assert fit.audio_tempo == 1.0
    assert fit.video_hold_s == 0.0
    assert fit.video_trim_s == pytest.approx(0.0)


def test_leichter_ueberhang_wird_ueber_tempo_geloest(cfg):
    """8.3s Ton in 8s Clip: Stimme leicht straffen, Bild bleibt ungestört."""
    fit = fit_scene(1, clip_dauer_s=8.0, voice_dauer_s=8.3, cfg=cfg)

    assert 1.0 < fit.audio_tempo <= cfg.max_audio_tempo
    assert fit.video_hold_s == pytest.approx(0.0, abs=0.02)
    assert fit.scene_dauer_s == pytest.approx(8.0, abs=0.02)
    assert any("beschleunigt" in w for w in fit.warnungen)


def test_tempo_wird_nie_ueber_das_limit_gedreht(cfg):
    """Auch bei 12s Ton bleibt die Stimme hörbar — der Rest wird Standbild."""
    fit = fit_scene(1, clip_dauer_s=8.0, voice_dauer_s=12.0, cfg=cfg)

    assert fit.audio_tempo == pytest.approx(cfg.max_audio_tempo)
    assert fit.video_hold_s > 0
    assert fit.scene_dauer_s > 8.0


def test_zu_langes_voiceover_warnt_statt_zu_kuerzen(cfg):
    """Der gesprochene Text wird nie abgeschnitten — es gibt eine Warnung."""
    fit = fit_scene(3, clip_dauer_s=8.0, voice_dauer_s=20.0, cfg=cfg)

    assert fit.video_hold_s > cfg.max_video_hold_s
    assert any("Limit" in w for w in fit.warnungen)
    # Die Szene ist lang genug, dass das komplette Voiceover hineinpasst.
    assert fit.scene_dauer_s >= cfg.lead_in_s + fit.voice_dauer_s


def test_szene_ohne_voiceover_laeuft_stumm_durch(cfg):
    fit = fit_scene(1, clip_dauer_s=8.0, voice_dauer_s=0.0, cfg=cfg)

    assert fit.scene_dauer_s == pytest.approx(8.0)
    assert fit.voice_dauer_s == 0.0
    assert fit.audio_tempo == 1.0


def test_clip_ohne_dauer_ist_ein_fehler(cfg):
    with pytest.raises(ValueError):
        fit_scene(1, clip_dauer_s=0.0, voice_dauer_s=3.0, cfg=cfg)


def test_voiceover_passt_immer_vollstaendig_in_die_szene(cfg):
    """Invariante über den gesamten Wertebereich: kein Wort fällt hinten raus."""
    for voice in (0.5, 2.0, 5.0, 7.9, 8.1, 9.5, 11.0, 15.0, 25.0):
        fit = fit_scene(1, clip_dauer_s=8.0, voice_dauer_s=voice, cfg=cfg)
        assert fit.audio_lead_s + fit.voice_dauer_s <= fit.scene_dauer_s + 1e-6, voice


def test_startzeiten_sind_kumulativ(cfg):
    fits = fit_all({1: 8.0, 2: 8.0, 3: 8.0}, {1: 4.0, 2: 6.0, 3: 5.0}, cfg)
    starts = szenen_startzeiten(fits)

    assert starts[0] == 0.0
    assert starts[1] == pytest.approx(fits[0].scene_dauer_s)
    assert starts[2] == pytest.approx(fits[0].scene_dauer_s + fits[1].scene_dauer_s)
    assert gesamt_dauer(fits) == pytest.approx(starts[-1] + fits[-1].scene_dauer_s)


def test_fit_all_sortiert_nach_index(cfg):
    fits = fit_all({3: 8.0, 1: 8.0, 2: 8.0}, {1: 4.0, 2: 4.0, 3: 4.0}, cfg)
    assert [f.index for f in fits] == [1, 2, 3]
