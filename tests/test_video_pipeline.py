"""Pipeline und Konfiguration: Budget-Bremse, Wiederaufnahme, Formate.

Der teuerste denkbare Fehler dieser Pipeline ist nicht ein Absturz, sondern
ein Lauf, der still das Vielfache des Geplanten ausgibt — oder einer, der nach
sieben bezahlten Clips in Stufe 6 abbricht und alles wegwirft. Beides wird
hier abgesichert.
"""
import json
import shutil

import pytest

from video_generator.config import lade_config
from video_generator.models import Scene, Script
from video_generator.pipeline import (BudgetUeberschritten, JobState, Pipeline,
                                      slugify)

FFMPEG_DA = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.fixture
def skript_datei(tmp_path):
    script = Script(
        thema="Zinseszins", hook="H", titel="T", cta="C",
        scenes=[Scene(i, f"S{i}", "a coin on a dark table, side light",
                      "slow push-in", "Ein kurzer gesprochener Satz hier.")
                for i in range(1, 5)])
    pfad = tmp_path / "script.json"
    pfad.write_text(json.dumps(script.to_dict(), ensure_ascii=False), encoding="utf-8")
    return str(pfad)


# -- Konfiguration ----------------------------------------------------------

def test_jedes_format_hat_gerade_pixelmasse():
    """Ungerade Kantenlängen lässt libx264 mit yuv420p nicht durch."""
    for ar in ("9:16", "16:9", "1:1"):
        for res in ("540p", "720p", "1080p"):
            if ar == "1:1" and res == "540p":
                continue
            breite, hoehe = lade_config(aspect_ratio=ar, aufloesung=res).breite_hoehe
            assert breite % 2 == 0 and hoehe % 2 == 0, (ar, res)


def test_unbekanntes_format_meldet_sich_mit_alternativen():
    cfg = lade_config(aspect_ratio="21:9")
    probleme = cfg.validate()
    assert probleme and "21:9" in probleme[0]


def test_wortbudget_folgt_der_szenenlaenge():
    kurz = lade_config(szenen_dauer_s=5).woerter_budget_pro_szene
    lang = lade_config(szenen_dauer_s=10).woerter_budget_pro_szene
    assert kurz < lang


def test_unsinniges_tempo_wird_bemaengelt():
    assert any("max_audio_tempo" in p
               for p in lade_config(max_audio_tempo=3.0).validate())


def test_fehlende_musikdatei_wird_vor_dem_lauf_bemaengelt():
    assert any("musik_pfad" in p
               for p in lade_config(musik_pfad="/gibt/es/nicht.mp3").validate())


def test_unbekannte_config_keys_brechen_nicht_ab(tmp_path, caplog):
    """Eine Config aus einer neueren Version soll nicht blockieren."""
    pfad = tmp_path / "c.json"
    pfad.write_text(json.dumps({"aspect_ratio": "16:9", "quantenmodus": True}),
                    encoding="utf-8")

    cfg = lade_config(pfad)

    assert cfg.aspect_ratio == "16:9"
    assert any("quantenmodus" in r.getMessage() for r in caplog.records), \
        "Tippfehler in der Config müssen sichtbar bleiben"


# -- Verzeichnisnamen -------------------------------------------------------

@pytest.mark.parametrize("roh,erwartet", [
    ("Warum Zinseszins unterschätzt wird", "warum-zinseszins-unterschatzt-wird"),
    ("  ETF? Ja/Nein!  ", "etf-ja-nein"),
    ("", "video"),
    ("---", "video"),
])
def test_slugify(roh, erwartet):
    assert slugify(roh) == erwartet


# -- Budget-Bremse ----------------------------------------------------------

def test_budget_bremse_greift_vor_dem_ersten_bezahlten_call(tmp_path, skript_datei):
    cfg = lade_config(szenen_anzahl=4, max_kosten_usd=0.01, output_dir=str(tmp_path))
    with pytest.raises(BudgetUeberschritten, match="max_kosten_usd"):
        Pipeline(cfg).run("Zinseszins", script_datei=skript_datei)


def test_ja_flag_hebt_die_budget_bremse_auf(tmp_path, skript_datei, monkeypatch):
    """Mit --yes darf es teuer werden — aber nur dann."""
    cfg = lade_config(szenen_anzahl=4, max_kosten_usd=0.01, output_dir=str(tmp_path))
    pipeline = Pipeline(cfg, kosten_bestaetigt=True)

    # Nicht wirklich generieren: der Test prüft nur, dass die Bremse durchlässt.
    gerufen = []
    monkeypatch.setattr(Pipeline, "_stufe_clips",
                        lambda self, state, prompts: gerufen.append(True) or {})
    pipeline.run("Zinseszins", script_datei=skript_datei, bis="clips")

    assert gerufen


def test_trockenlauf_kennt_kein_budgetproblem(tmp_path, skript_datei):
    cfg = lade_config(szenen_anzahl=4, max_kosten_usd=0.0, output_dir=str(tmp_path))
    state = Pipeline(cfg, dry_run=True).run("Zinseszins", script_datei=skript_datei,
                                            bis="clips")
    assert state.kosten_usd == 0.0
    assert len(state.clips) == 4


# -- Zustand und Wiederaufnahme ---------------------------------------------

def test_lauf_haelt_am_gewuenschten_schritt_an(tmp_path, skript_datei):
    cfg = lade_config(szenen_anzahl=4, output_dir=str(tmp_path))
    state = Pipeline(cfg, dry_run=True).run("Thema", script_datei=skript_datei,
                                            bis="prompts")

    assert state.script and state.prompts
    assert not state.clips, "Stufe 3 hätte nicht laufen dürfen"


def test_unbekannter_schritt_meldet_sich(tmp_path, skript_datei):
    cfg = lade_config(output_dir=str(tmp_path))
    with pytest.raises(ValueError, match="Unbekannter Schritt"):
        Pipeline(cfg, dry_run=True).run("Thema", script_datei=skript_datei, bis="rendern")


def test_job_json_ueberlebt_den_neustart(tmp_path, skript_datei):
    cfg = lade_config(szenen_anzahl=4, output_dir=str(tmp_path))
    state = Pipeline(cfg, dry_run=True).run("Thema", script_datei=skript_datei,
                                            bis="prompts")

    geladen = JobState.laden(state.verzeichnis)

    assert geladen.thema == "Thema"
    assert geladen.script == state.script
    assert geladen.prompts == state.prompts


def test_wiederaufnahme_erzeugt_das_skript_nicht_neu(tmp_path, skript_datei):
    """Nach einem Abbruch darf Stufe 1 nicht erneut Tokens verbrennen."""
    cfg = lade_config(szenen_anzahl=4, output_dir=str(tmp_path))
    state = Pipeline(cfg, dry_run=True).run("Thema", script_datei=skript_datei,
                                            bis="script")

    # Ohne Skript-Datei und ohne API-Key: ginge Stufe 1 erneut los, käme ein
    # RuntimeError. Dass der Lauf durchgeht, ist der eigentliche Nachweis.
    fortgesetzt = Pipeline(cfg, dry_run=True).resume(state.verzeichnis, bis="prompts")

    assert fortgesetzt.script == state.script
    assert fortgesetzt.prompts


def test_wiederaufnahme_ohne_lauf_meldet_sich(tmp_path):
    with pytest.raises(FileNotFoundError, match="Kein Lauf"):
        Pipeline(lade_config()).resume(tmp_path / "gibtsnicht")


def test_bereits_bezahlte_clips_werden_nicht_neu_erzeugt(tmp_path, skript_datei):
    cfg = lade_config(szenen_anzahl=4, output_dir=str(tmp_path))
    state = Pipeline(cfg, dry_run=True).run("Thema", script_datei=skript_datei,
                                            bis="clips")
    vorher = dict(state.clips)

    fortgesetzt = Pipeline(cfg, dry_run=True).resume(state.verzeichnis, bis="clips")

    assert fortgesetzt.clips == vorher


# -- Gesamtkette ------------------------------------------------------------

@pytest.mark.skipif(not FFMPEG_DA, reason="ffmpeg/ffprobe nicht installiert")
def test_trockenlauf_erzeugt_ein_abspielbares_video(tmp_path, skript_datei):
    from video_generator import ffmpeg_utils as ff

    cfg = lade_config(szenen_anzahl=4, output_dir=str(tmp_path),
                      aspect_ratio="9:16", aufloesung="540p")
    state = Pipeline(cfg, dry_run=True).run("Thema", script_datei=skript_datei)

    video = state.verzeichnis / "video.mp4"
    assert video.exists()
    assert (state.verzeichnis / "video.srt").exists()

    erwartet = sum(f["scene_dauer_s"] for f in state.fits)
    assert ff.probe_dauer(video) == pytest.approx(erwartet, abs=0.5)
