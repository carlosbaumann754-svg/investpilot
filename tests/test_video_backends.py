"""Video-Backends: Payloads, Kosten und Fehlerverhalten.

Das ist die einzige Stufe der Pipeline, die Geld kostet. Entsprechend geht es
hier weniger um Glücksfälle als um die teuren Irrtümer: ein Retry auf einen
Fehler, der sich nie von selbst löst, oder ein stiller Abbruch nach dem
bereits bezahlten Call.
"""
import json

import pytest

from video_generator.config import lade_config
from video_generator.models import ScenePrompt
from video_generator.video_backends import (KlingBackend, PixverseBackend,
                                            VideoBackendFehler, get_backend)


class _Antwort:
    def __init__(self, status=200, daten=None, inhalt=b"", text=""):
        self.status_code = status
        self._daten = daten if daten is not None else {}
        self._inhalt = inhalt
        self.text = text or json.dumps(self._daten)

    def json(self):
        return self._daten

    def iter_content(self, chunk_size=1):
        yield self._inhalt

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    """Minimaler Ersatz für ``requests.Session`` — zählt alle Aufrufe mit."""

    def __init__(self, post_antworten, get_antworten):
        self.post_antworten = list(post_antworten)
        self.get_antworten = list(get_antworten)
        self.posts = 0
        self.gets = 0

    def post(self, url, **kw):
        self.posts += 1
        return self.post_antworten.pop(0)

    def get(self, url, **kw):
        self.gets += 1
        return self.get_antworten.pop(0)


@pytest.fixture
def cfg():
    return lade_config(video_retries=2, video_poll_intervall_s=0)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "test-key")


@pytest.fixture(autouse=True)
def _kein_schlaf(monkeypatch):
    """Backoff-Pausen im Test überspringen."""
    monkeypatch.setattr("video_generator.video_backends.time.sleep", lambda s: None)


def _erfolgsverlauf(inhalt=b"x" * 2048):
    return ([_Antwort(daten={"request_id": "abc123"})],
            [_Antwort(daten={"status": "COMPLETED"}),
             _Antwort(daten={"video": {"url": "https://example.test/v.mp4"}}),
             _Antwort(inhalt=inhalt)])


def test_pixverse_payload_und_laengen(cfg):
    b = PixverseBackend(cfg)
    assert b.payload(ScenePrompt(1, "a cat", "no text", 8))["duration"] == 8
    # PixVerse kennt nur 5s und 8s — dazwischen wird aufgerundet, damit das
    # Voiceover eher zu kurz als zu lang ist.
    assert b.snap_dauer(6.0) == 8.0
    assert b.snap_dauer(4.0) == 5.0


def test_kling_erwartet_die_dauer_als_string(cfg):
    """Kling nimmt ein String-Enum, PixVerse eine Zahl — eine echte Stolperkante."""
    body = KlingBackend(cfg).payload(ScenePrompt(1, "a cat", "", 8))
    assert body["duration"] == "10"
    assert isinstance(body["duration"], str)


def test_negativ_prompt_wird_nur_gesetzt_wenn_vorhanden(cfg):
    assert "negative_prompt" not in PixverseBackend(cfg).payload(
        ScenePrompt(1, "a cat", "", 8))


def test_kosten_skalieren_mit_aufloesung():
    guenstig = PixverseBackend(lade_config(aufloesung="540p")).kosten_pro_clip(8)
    teuer = PixverseBackend(lade_config(aufloesung="1080p")).kosten_pro_clip(8)
    assert 0 < guenstig < teuer


def test_erfolgreicher_lauf_laedt_das_video(cfg, tmp_path):
    posts, gets = _erfolgsverlauf()
    b = PixverseBackend(cfg, session=FakeSession(posts, gets))
    ziel = tmp_path / "scene_01.mp4"

    clip = b.generate(ScenePrompt(1, "a cat", "", 8), ziel)

    assert ziel.exists() and ziel.stat().st_size > 1024
    assert clip.request_id == "abc123"
    assert clip.backend == "pixverse"
    assert clip.kosten_usd > 0


def test_vier_hundert_wird_nicht_wiederholt(cfg, tmp_path):
    """Ein abgelehnter Prompt wird durch Wiederholung nicht besser — nur teurer."""
    session = FakeSession([_Antwort(status=422, text="prompt rejected")], [])
    b = PixverseBackend(cfg, session=session)

    with pytest.raises(VideoBackendFehler):
        b.generate(ScenePrompt(1, "a cat", "", 8), tmp_path / "x.mp4")

    assert session.posts == 1


def test_fuenf_hundert_wird_wiederholt(cfg, tmp_path):
    session = FakeSession([_Antwort(status=503, text="upstream"),
                           _Antwort(status=503, text="upstream"),
                           _Antwort(status=503, text="upstream")], [])
    b = PixverseBackend(cfg, session=session)

    with pytest.raises(VideoBackendFehler):
        b.generate(ScenePrompt(1, "a cat", "", 8), tmp_path / "x.mp4")

    assert session.posts == cfg.video_retries + 1


def test_queue_fehler_beendet_den_versuch(cfg, tmp_path):
    session = FakeSession(
        [_Antwort(daten={"request_id": "r1"})] * 3,
        [_Antwort(daten={"status": "ERROR", "error": "content policy"})] * 3)
    b = PixverseBackend(cfg, session=session)

    with pytest.raises(VideoBackendFehler, match="ERROR"):
        b.generate(ScenePrompt(1, "a cat", "", 8), tmp_path / "x.mp4")


def test_leerer_download_gilt_als_fehler(cfg, tmp_path):
    posts, gets = _erfolgsverlauf(inhalt=b"")
    session = FakeSession(posts * 3, gets * 3)
    b = PixverseBackend(cfg, session=session)

    with pytest.raises(VideoBackendFehler):
        b.generate(ScenePrompt(1, "a cat", "", 8), tmp_path / "x.mp4")
    assert not (tmp_path / "x.mp4").exists()


def test_fehlender_key_meldet_sich_klar(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.delenv("FAL_AI_KEY", raising=False)
    b = PixverseBackend(cfg, session=FakeSession([], []))

    with pytest.raises(VideoBackendFehler, match="FAL_KEY"):
        b.generate(ScenePrompt(1, "a cat", "", 8), tmp_path / "x.mp4")


def test_dry_run_kostet_nichts(cfg, tmp_path):
    b = get_backend("dry-run", cfg)
    clip = b.generate(ScenePrompt(1, "a cat", "", 8), tmp_path / "x.mp4")
    assert clip.kosten_usd == 0.0


def test_unbekanntes_backend_faellt_auf(cfg):
    with pytest.raises(ValueError, match="Unbekanntes Video-Backend"):
        get_backend("sora", cfg)


def test_modell_kennung_kommt_aus_der_config():
    """Ein Modellwechsel bei fal.ai darf keine Code-Änderung brauchen."""
    cfg = lade_config(pixverse_model_id="fal-ai/pixverse/v9/text-to-video")
    assert PixverseBackend(cfg).model_id.endswith("v9/text-to-video")
