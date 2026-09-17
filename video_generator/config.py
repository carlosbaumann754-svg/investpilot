"""Konfiguration der Video-Pipeline.

Drei Ebenen, spätere schlägt frühere:

1. ``config.default.json`` neben diesem Modul (im Repo eingecheckt)
2. eine per ``--config`` übergebene JSON-Datei
3. CLI-Flags bzw. explizite Keyword-Argumente

Secrets kommen ausschliesslich aus Umgebungsvariablen (``FAL_KEY``,
``ANTHROPIC_API_KEY``) und werden nie in die Job-JSON geschrieben.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, fields
from pathlib import Path

_DEFAULT_PFAD = Path(__file__).with_name("config.default.json")

# Zielauflösungen je Seitenverhältnis. PixVerse/Kling liefern das Rohmaterial,
# ffmpeg normalisiert am Ende hart auf diese Werte — sonst scheitert der
# concat-Demuxer an unterschiedlichen Stream-Parametern.
_AUFLOESUNGEN = {
    ("9:16", "720p"): (720, 1280),
    ("9:16", "1080p"): (1080, 1920),
    ("9:16", "540p"): (540, 960),
    ("16:9", "720p"): (1280, 720),
    ("16:9", "1080p"): (1920, 1080),
    ("16:9", "540p"): (960, 540),
    ("1:1", "720p"): (720, 720),
    ("1:1", "1080p"): (1080, 1080),
}


@dataclass
class VideoConfig:
    """Alle Stellschrauben eines Laufs. Feldnamen = JSON-Keys = CLI-Flags."""

    sprache: str = "de"
    zielplattform: str = "reels"
    aspect_ratio: str = "9:16"
    aufloesung: str = "720p"
    fps: int = 30

    szenen_anzahl: int = 7
    szenen_dauer_s: float = 8.0
    woerter_pro_sekunde: float = 2.4

    script_modell: str = "claude-sonnet-5"
    script_max_tokens: int = 4000
    prompt_modus: str = "rule_based"          # rule_based | llm
    prompt_modell: str = "claude-sonnet-5"
    stil: str = ""
    negative_prompt: str = ""

    # Modell-Kennungen und Queue-Host sind bewusst konfigurierbar: fal.ai
    # versioniert Modelle im Pfad (v3.5 -> v4.5 -> ...). Ein Wechsel ist damit
    # eine Config-Zeile statt eines Code-Deploys.
    fal_queue_url: str = "https://queue.fal.run"
    pixverse_model_id: str = "fal-ai/pixverse/v3.5/text-to-video"
    kling_model_id: str = "fal-ai/kling-video/v2.5-turbo/pro/text-to-video"

    video_backend: str = "pixverse"
    video_fallback_backend: str | None = "kling"
    video_parallel: int = 3
    video_timeout_s: int = 900
    video_poll_intervall_s: float = 5.0
    video_retries: int = 2
    max_kosten_usd: float = 5.0

    tts_stimme: str = "de-DE-KatjaNeural"
    tts_rate: str = "+0%"
    tts_pitch: str = "+0Hz"
    tts_volume: str = "+0%"

    untertitel_aktiv: bool = True
    # None = aus Schriftgrösse und Bildbreite berechnen (siehe
    # subtitles.max_zeichen_pro_zeile). Ein fester Wert hier widerspricht
    # schnell der Schriftgrösse — genau so lief der erste Testlauf seitlich
    # aus dem Bild.
    untertitel_max_zeichen: int | None = None
    untertitel_max_zeilen: int = 2
    untertitel_max_dauer_s: float = 3.5
    untertitel_min_dauer_s: float = 0.9
    # Beide Werte in Prozent der Videohöhe, damit dieselbe Config bei 540p,
    # 720p und 1080p und in beiden Seitenverhältnissen gleich aussieht.
    untertitel_schriftgroesse_pct: float = 4.5
    untertitel_rand_unten_pct: float = 12.0

    lead_in_s: float = 0.25
    tail_out_s: float = 0.35
    max_audio_tempo: float = 1.15
    max_video_hold_s: float = 2.5
    max_tail_stille_s: float = 1.5
    min_szene_s: float = 2.0

    musik_pfad: str | None = None
    musik_lautstaerke_db: float = -22.0
    musik_fade_s: float = 1.5

    video_bitrate: str = "6M"
    audio_bitrate: str = "192k"
    crf: int = 20
    preset: str = "medium"

    output_dir: str = "video_output"

    # ---- abgeleitete Werte -------------------------------------------------

    @property
    def breite_hoehe(self) -> tuple[int, int]:
        key = (self.aspect_ratio, self.aufloesung)
        if key not in _AUFLOESUNGEN:
            raise ValueError(
                f"Unbekannte Kombination aspect_ratio={self.aspect_ratio!r} / "
                f"aufloesung={self.aufloesung!r}. Bekannt: "
                + ", ".join(f"{a}@{r}" for a, r in sorted(_AUFLOESUNGEN))
            )
        return _AUFLOESUNGEN[key]

    @property
    def woerter_budget_pro_szene(self) -> int:
        """Wieviele Wörter pro Szene gesprochen werden dürfen.

        Das ist der wichtigste Wert der ganzen Pipeline: ein zu langes
        Voiceover sprengt die Clip-Länge und erzwingt entweder Zeitlupe im Ton
        oder eingefrorene Standbilder im Video. Lieber im Skript kürzen als im
        Schnitt reparieren.
        """
        nutzbar = max(1.0, self.szenen_dauer_s - self.lead_in_s - self.tail_out_s)
        return max(4, int(nutzbar * self.woerter_pro_sekunde))

    # ---- Laden / Speichern -------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> list[str]:
        """Gibt eine Liste von Problemen zurück (leer = alles gut)."""
        probleme: list[str] = []
        try:
            self.breite_hoehe
        except ValueError as e:
            probleme.append(str(e))
        if not 3 <= self.szenen_anzahl <= 20:
            probleme.append(f"szenen_anzahl={self.szenen_anzahl} ausserhalb 3..20")
        if not 3.0 <= self.szenen_dauer_s <= 30.0:
            probleme.append(f"szenen_dauer_s={self.szenen_dauer_s} ausserhalb 3..30")
        if self.max_audio_tempo < 1.0 or self.max_audio_tempo > 1.5:
            probleme.append(
                f"max_audio_tempo={self.max_audio_tempo} — sinnvoll ist 1.0..1.5 "
                "(darüber klingt die Stimme gehetzt)")
        if self.prompt_modus not in ("rule_based", "llm"):
            probleme.append(f"prompt_modus={self.prompt_modus!r} — erlaubt: rule_based, llm")
        if self.video_parallel < 1:
            probleme.append("video_parallel muss >= 1 sein")
        if self.musik_pfad and not Path(self.musik_pfad).exists():
            probleme.append(f"musik_pfad existiert nicht: {self.musik_pfad}")
        return probleme


def _bekannte_felder() -> set[str]:
    return {f.name for f in fields(VideoConfig)}


def lade_config(pfad: str | os.PathLike | None = None, **overrides) -> VideoConfig:
    """Baut die Konfiguration aus Defaults + optionaler Datei + Overrides.

    Unbekannte Keys werden ignoriert statt zu crashen — eine Config-Datei aus
    einer neueren Version soll eine ältere Pipeline nicht blockieren. Sie
    tauchen aber in ``unbekannt`` auf, damit Tippfehler sichtbar bleiben.
    """
    daten: dict = {}
    if _DEFAULT_PFAD.exists():
        daten.update(json.loads(_DEFAULT_PFAD.read_text(encoding="utf-8")))
    if pfad:
        daten.update(json.loads(Path(pfad).read_text(encoding="utf-8")))
    daten.update({k: v for k, v in overrides.items() if v is not None})

    erlaubt = _bekannte_felder()
    unbekannt = sorted(set(daten) - erlaubt)
    if unbekannt:
        # Kein Abbruch, aber sichtbar (Stufe-1-Audit: keine Silent-Fails).
        import logging
        logging.getLogger(__name__).warning(
            "Unbekannte Config-Keys ignoriert: %s", ", ".join(unbekannt))

    return VideoConfig(**{k: v for k, v in daten.items() if k in erlaubt})


def anthropic_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or None


def fal_key() -> str | None:
    return os.environ.get("FAL_KEY") or os.environ.get("FAL_AI_KEY") or None
