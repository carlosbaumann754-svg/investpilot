"""Stufe 5 — Zusammenschnitt mit ffmpeg.

Drei Durchläufe statt eines Riesen-Filtergraphen:

1. **Pro Szene ein Segment.** Clip auf Zielmass zuschneiden, Voiceover mit
   Vorlauf, Tempo und Stille exakt nach :class:`SceneFit` dagegenlegen, auf
   einheitliche Codec-Parameter bringen.
2. **Segmente aneinanderhängen** — mit dem concat-Demuxer und ``-c copy``.
   Weil Schritt 1 alle Segmente identisch encodiert hat, ist das ein reiner
   Dateikopiervorgang: schnell und ohne Qualitätsverlust.
3. **Finaler Durchlauf** für Untertitel (eingebrannt) und Hintergrundmusik.

Warum nicht alles in einem Aufruf? Ein Filtergraph über sieben Clips, sieben
Tonspuren, Musik und Untertitel ist zwar möglich, aber praktisch nicht
debuggbar: fällt er um, sagt ffmpeg nur "Invalid argument". Mit Segmenten
lässt sich jede Szene einzeln anschauen und die kaputte finden. Der Preis ist
ein zweiter Video-Encode — bei CRF 18 im Zwischenschritt sichtbar unkritisch.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import ffmpeg_utils as ff
from .config import VideoConfig
from .models import ClipResult, SceneFit, SubtitleCue, VoiceResult
from .subtitles import schreibe_ass, schreibe_srt

log = logging.getLogger(__name__)


def _ms(sekunden: float) -> int:
    return max(0, int(round(sekunden * 1000)))


def baue_segment(fit: SceneFit, clip: ClipResult, voice: VoiceResult | None,
                 cfg: VideoConfig, ziel: Path) -> Path:
    """Rendert genau eine Szene als fertiges Segment."""
    breite, hoehe = cfg.breite_hoehe
    video_quelle_s = max(0.05, fit.scene_dauer_s - fit.video_hold_s)

    v_filter = [
        f"trim=start=0:end={video_quelle_s:.3f}",
        "setpts=PTS-STARTPTS",
        # Formatfüllend skalieren und beschneiden statt schwarze Balken zu
        # setzen — auf Reels sind Balken der sichtbarste Amateur-Marker.
        f"scale={breite}:{hoehe}:force_original_aspect_ratio=increase",
        f"crop={breite}:{hoehe}",
        f"fps={cfg.fps}",
        "setsar=1",
    ]
    if fit.video_hold_s > 0.01:
        # Letztes Bild stehen lassen, statt den Satz abzuschneiden.
        v_filter.append(f"tpad=stop_mode=clone:stop_duration={fit.video_hold_s:.3f}")

    args = ["-i", clip.pfad]
    if voice is not None and fit.voice_dauer_s > 0:
        args += ["-i", voice.pfad]
        a_filter = []
        if abs(fit.audio_tempo - 1.0) > 0.001:
            a_filter.append(f"atempo={fit.audio_tempo:.4f}")
        a_filter += [
            "aresample=48000",
            f"adelay={_ms(fit.audio_lead_s)}:all=1",
            "apad",
            f"atrim=start=0:end={fit.scene_dauer_s:.3f}",
            "asetpts=PTS-STARTPTS",
        ]
        audio_kette = f"[1:a]{','.join(a_filter)}[a]"
    else:
        args += ["-f", "lavfi", "-t", f"{fit.scene_dauer_s:.3f}",
                 "-i", "anullsrc=r=48000:cl=stereo"]
        audio_kette = "[1:a]aresample=48000,asetpts=PTS-STARTPTS[a]"

    filter_complex = f"[0:v]{','.join(v_filter)}[v];{audio_kette}"

    ff.run(args + [
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-t", f"{fit.scene_dauer_s:.3f}",
        "-c:v", "libx264", "-preset", cfg.preset,
        # Zwischenschritt bewusst hochwertiger als das Endergebnis, damit der
        # zweite Encode in Stufe 3 nicht auf bereits verlorene Details trifft.
        "-crf", str(max(14, cfg.crf - 2)),
        "-pix_fmt", "yuv420p", "-r", str(cfg.fps),
        "-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        str(ziel),
    ])
    return ziel


def concat(segmente: list[Path], ziel: Path, arbeitsverzeichnis: Path) -> Path:
    """Hängt die Segmente verlustfrei aneinander."""
    liste = arbeitsverzeichnis / "concat.txt"
    liste.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in segmente),
        encoding="utf-8")
    ff.run(["-f", "concat", "-safe", "0", "-i", str(liste),
            "-c", "copy", "-movflags", "+faststart", str(ziel)])
    return ziel


def finalisieren(quelle: Path, ziel: Path, cfg: VideoConfig,
                 untertitel: Path | None, gesamt_s: float) -> Path:
    """Brennt Untertitel ein und mischt Musik — in einem einzigen Durchlauf."""
    musik = cfg.musik_pfad and Path(cfg.musik_pfad).exists()

    if not untertitel and not musik:
        # Nichts zu tun: Datei nur umbenennen statt sinnlos neu zu encodieren.
        quelle.replace(ziel)
        return ziel

    args = ["-i", str(quelle)]
    filter_teile = []
    maps = []

    if musik:
        # -stream_loop muss VOR dem Input stehen, sonst wird es ignoriert.
        args = ["-i", str(quelle), "-stream_loop", "-1", "-i", str(cfg.musik_pfad)]
        fade = max(0.1, cfg.musik_fade_s)
        aus_start = max(0.0, gesamt_s - fade)
        filter_teile.append(
            f"[1:a]volume={cfg.musik_lautstaerke_db}dB,"
            f"afade=t=in:st=0:d={fade:.2f},"
            f"afade=t=out:st={aus_start:.2f}:d={fade:.2f},"
            f"aresample=48000[musik];"
            # duration=first: die Musik endet mit dem Voiceover, nicht umgekehrt.
            # normalize=0: sonst senkt amix beide Spuren pauschal ab und das
            # Voiceover wird leise.
            f"[0:a][musik]amix=inputs=2:duration=first:dropout_transition=0:"
            f"normalize=0[a]"
        )
        maps += ["-map", "[a]"]
    else:
        maps += ["-map", "0:a"]

    if untertitel:
        filter_teile.append(
            f"[0:v]subtitles='{ff.escape_filter_pfad(untertitel)}'[v]")
        maps = ["-map", "[v]"] + maps
        video_codec = ["-c:v", "libx264", "-preset", cfg.preset,
                       "-crf", str(cfg.crf), "-pix_fmt", "yuv420p",
                       "-r", str(cfg.fps)]
    else:
        maps = ["-map", "0:v"] + maps
        video_codec = ["-c:v", "copy"]

    ff.run(args + ["-filter_complex", ";".join(filter_teile)] + maps
           + video_codec
           + ["-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", "48000", "-ac", "2",
              "-movflags", "+faststart", str(ziel)])
    return ziel


def assemble(fits: list[SceneFit], clips: dict[int, ClipResult],
             voices: dict[int, VoiceResult], cues: list[SubtitleCue],
             cfg: VideoConfig, arbeit: Path, ziel: Path) -> Path:
    """Führt alle drei Durchläufe aus und liefert den Pfad des fertigen MP4."""
    arbeit.mkdir(parents=True, exist_ok=True)
    ziel.parent.mkdir(parents=True, exist_ok=True)

    segmente = []
    for fit in fits:
        clip = clips.get(fit.index)
        if clip is None:
            log.warning("Szene %d ohne Clip — wird übersprungen", fit.index)
            continue
        seg = arbeit / f"seg_{fit.index:02d}.mp4"
        log.info("Szene %d: Segment (%.2fs, Tempo %.2f, Standbild %.2fs)",
                 fit.index, fit.scene_dauer_s, fit.audio_tempo, fit.video_hold_s)
        segmente.append(baue_segment(fit, clip, voices.get(fit.index), cfg, seg))

    if not segmente:
        raise RuntimeError("Kein einziges Segment gebaut — es gibt nichts zu schneiden")

    roh = concat(segmente, arbeit / "concat.mp4", arbeit)

    untertitel_datei = None
    if cfg.untertitel_aktiv and cues:
        schreibe_srt(cues, ziel.with_suffix(".srt"))       # zum Nachbearbeiten
        untertitel_datei = schreibe_ass(cues, arbeit / "untertitel.ass", cfg)

    gesamt = sum(f.scene_dauer_s for f in fits)
    return finalisieren(roh, ziel, cfg, untertitel_datei, gesamt)
