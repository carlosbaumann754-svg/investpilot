"""Szenen-Timing: wie Clip-Länge und Voiceover-Länge zusammenfinden.

DAS PROBLEM
===========
Die beiden Quellen der Pipeline haben unabhängige Längen:

* Der Video-Clip kommt mit fester Dauer aus dem Modell (PixVerse: 5s oder 8s).
* Das Voiceover ist so lang, wie die TTS-Stimme für den Satz braucht — das
  weiss man erst *nach* der Synthese, nicht beim Schreiben des Skripts.

Wer beides naiv aneinanderhängt, bekommt ab Szene 2 eine Tonspur, die gegen
das Bild läuft. Bei sieben Szenen summiert sich das auf mehrere Sekunden
Versatz — das Video ist unbrauchbar, obwohl jeder einzelne Baustein stimmt.

DIE LÖSUNG
==========
Jede Szene wird einzeln gepasst, bevor irgendetwas geschnitten wird. Die
Reihenfolge der Mittel folgt ihrer Nebenwirkung — das schonendste zuerst:

1. **Ton kürzer als Bild** → Stille anhängen. Kostenlos, unhörbar. Überschüssiges
   Bild jenseits von ``max_tail_stille_s`` wird weggeschnitten, damit keine
   toten Sekunden entstehen.
2. **Ton wenig länger** → Stimme bis ``max_audio_tempo`` beschleunigen
   (Standard 1.15x — hörbar straffer, aber nicht comichaft).
3. **Ton deutlich länger** → letztes Videobild einfrieren und stehen lassen.
   Hässlich, aber besser als ein mitten im Wort abgeschnittener Satz.

Der Voiceover-Text wird **nie** gekürzt: Stufe 1 hat ihn mit Wortbudget
erzeugt, ein Abschneiden im Schnitt wäre ein stiller Inhaltsverlust.

Diese Datei enthält bewusst keine ffmpeg-Aufrufe. Sie ist reine Arithmetik und
damit vollständig unit-testbar — die teure Stufe (ffmpeg) bekommt nur noch
fertige Zahlen serviert.
"""
from __future__ import annotations

from .config import VideoConfig
from .models import SceneFit


def fit_scene(index: int, clip_dauer_s: float, voice_dauer_s: float,
              cfg: VideoConfig) -> SceneFit:
    """Berechnet, wie Szene ``index`` im Schnitt zusammengesetzt wird.

    Args:
        index: Szenen-Index (nur zur Rückverfolgung im Ergebnis).
        clip_dauer_s: tatsächliche Länge des generierten Clips (aus ffprobe).
        voice_dauer_s: tatsächliche Länge der Voiceover-Datei (aus ffprobe).
        cfg: Konfiguration mit den Grenzwerten.

    Returns:
        Ein :class:`SceneFit` mit allen Werten, die der Assembler an ffmpeg
        weiterreicht. ``warnungen`` ist nicht leer, wenn die Szene nur mit
        sichtbarem Kompromiss gepasst werden konnte.
    """
    warnungen: list[str] = []
    clip = max(0.0, float(clip_dauer_s))
    voice = max(0.0, float(voice_dauer_s))
    lead = max(0.0, cfg.lead_in_s)
    tail_min = max(0.0, cfg.tail_out_s)

    if clip <= 0:
        raise ValueError(f"Szene {index}: Clip-Dauer {clip_dauer_s} ist nicht nutzbar")

    # Fall 0: kein Voiceover (z.B. reine Stimmungsszene) -> Clip bleibt wie er ist.
    if voice <= 0:
        return SceneFit(index=index, scene_dauer_s=clip, audio_lead_s=0.0,
                        audio_tail_s=clip, audio_tempo=1.0, video_hold_s=0.0,
                        video_trim_s=0.0, voice_dauer_s=0.0,
                        warnungen=["Kein Voiceover — Szene läuft stumm"])

    benoetigt = lead + voice + tail_min

    # ---- Fall 1: Ton passt in den Clip ------------------------------------
    if benoetigt <= clip:
        ueberschuss = clip - lead - voice          # was hinten frei bleibt
        tail = min(ueberschuss, max(tail_min, cfg.max_tail_stille_s))
        scene = max(cfg.min_szene_s, lead + voice + tail)
        scene = min(scene, clip)                   # nie länger als das Material
        trim = clip - scene
        tail = scene - lead - voice
        return SceneFit(index=index, scene_dauer_s=scene, audio_lead_s=lead,
                        audio_tail_s=tail, audio_tempo=1.0, video_hold_s=0.0,
                        video_trim_s=trim, voice_dauer_s=voice,
                        warnungen=warnungen)

    # ---- Fall 2: Stimme leicht beschleunigen ------------------------------
    # Beschleunigt wird nur die Stimme — Vor- und Nachlauf bleiben stehen.
    # Der nötige Faktor bezieht sich deshalb auf die Zeit, die nach Abzug der
    # beiden Pausen übrig bleibt, nicht auf die volle Cliplänge. (Mit
    # ``benoetigt / clip`` bleibt sonst genau die Differenz der Pausen übrig
    # und erzwingt ein unnötiges Standbild von ein paar Hundertstelsekunden.)
    nutzbar = max(0.05, clip - lead - tail_min)
    tempo = min(voice / nutzbar, max(1.0, cfg.max_audio_tempo))
    voice_schnell = voice / tempo
    benoetigt2 = lead + voice_schnell + tail_min
    if tempo > 1.001:
        warnungen.append(f"Stimme auf {tempo:.2f}x beschleunigt")

    if benoetigt2 <= clip + 1e-6:
        tail = clip - lead - voice_schnell
        return SceneFit(index=index, scene_dauer_s=clip, audio_lead_s=lead,
                        audio_tail_s=max(0.0, tail), audio_tempo=tempo,
                        video_hold_s=0.0, video_trim_s=0.0,
                        voice_dauer_s=voice_schnell, warnungen=warnungen)

    # ---- Fall 3: letztes Bild einfrieren ----------------------------------
    hold = benoetigt2 - clip
    if hold > cfg.max_video_hold_s + 1e-6:
        warnungen.append(
            f"Standbild {hold:.1f}s (Limit {cfg.max_video_hold_s:.1f}s) — "
            f"Voiceover der Szene {index} ist zu lang für {clip:.1f}s Clip. "
            "Skript kürzen oder szenen_dauer_s erhöhen.")
    else:
        warnungen.append(f"Letztes Bild {hold:.1f}s eingefroren")

    scene = clip + hold
    return SceneFit(index=index, scene_dauer_s=scene, audio_lead_s=lead,
                    audio_tail_s=max(0.0, scene - lead - voice_schnell),
                    audio_tempo=tempo, video_hold_s=hold, video_trim_s=0.0,
                    voice_dauer_s=voice_schnell, warnungen=warnungen)


def fit_all(clips: dict[int, float], voices: dict[int, float],
            cfg: VideoConfig) -> list[SceneFit]:
    """Passt alle Szenen und liefert sie nach Index sortiert."""
    return [fit_scene(i, clips[i], voices.get(i, 0.0), cfg)
            for i in sorted(clips)]


def szenen_startzeiten(fits: list[SceneFit]) -> list[float]:
    """Absolute Startzeit jeder Szene im fertigen Video (kumulativ)."""
    start = 0.0
    out = []
    for f in fits:
        out.append(start)
        start += f.scene_dauer_s
    return out


def gesamt_dauer(fits: list[SceneFit]) -> float:
    return sum(f.scene_dauer_s for f in fits)
