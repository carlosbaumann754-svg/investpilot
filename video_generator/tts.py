"""Stufe 4 — Voiceover per edge-tts.

edge-tts nutzt die Sprachsynthese von Microsoft Edge: kostenlos, kein Key,
sehr ordentliche deutsche Stimmen. Entscheidend für diese Pipeline ist aber
etwas anderes: der Stream liefert neben den Audiodaten **WordBoundary-Events**
mit exakten Zeitstempeln pro Wort.

Damit werden die Untertitel in Stufe 5 nicht geschätzt, sondern auf das
tatsächlich Gesprochene gesetzt. Der Unterschied ist im fertigen Video sofort
sichtbar — geschätzte Timings driften spätestens nach zwei Sätzen.

Stimmen (Auswahl, ``edge-tts --list-voices`` zeigt alle):
    de-DE-KatjaNeural     weiblich, neutral      (Standard)
    de-DE-ConradNeural    männlich, ruhig
    de-DE-AmalaNeural     weiblich, wärmer
    de-CH-LeniNeural      weiblich, Schweizer Färbung
    de-CH-JanNeural       männlich, Schweizer Färbung
    en-US-AriaNeural      englisch, weiblich
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from .config import VideoConfig
from .models import VoiceResult, WordTiming

log = logging.getLogger(__name__)

# edge-tts liefert Offsets in 100-Nanosekunden-Ticks (wie .NET TimeSpan).
_TICKS_PRO_SEKUNDE = 10_000_000


class TTSFehler(RuntimeError):
    """Sprachsynthese fehlgeschlagen."""


def _asyncio_run(coro):
    """``asyncio.run`` — auch wenn bereits ein Loop läuft (Notebook, Server)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _synthese(text: str, cfg: VideoConfig) -> tuple[bytes, list[WordTiming]]:
    try:
        import edge_tts
    except ImportError as e:                       # pragma: no cover
        raise TTSFehler(
            "Paket 'edge-tts' fehlt — pip install -r video_generator/requirements.txt"
        ) from e

    comm = edge_tts.Communicate(text, cfg.tts_stimme, rate=cfg.tts_rate,
                                volume=cfg.tts_volume, pitch=cfg.tts_pitch)
    audio = bytearray()
    woerter: list[WordTiming] = []
    async for chunk in comm.stream():
        typ = chunk.get("type")
        if typ == "audio" and chunk.get("data"):
            audio.extend(chunk["data"])
        elif typ == "WordBoundary":
            start = chunk["offset"] / _TICKS_PRO_SEKUNDE
            dauer = chunk.get("duration", 0) / _TICKS_PRO_SEKUNDE
            woerter.append(WordTiming(text=chunk.get("text", ""),
                                      start_s=start, end_s=start + dauer))
    if not audio:
        raise TTSFehler(f"Keine Audiodaten für Stimme {cfg.tts_stimme!r} erhalten")
    return bytes(audio), woerter


def _dauer(pfad: Path, woerter: list[WordTiming]) -> float:
    """Exakte Dauer via ffprobe, sonst aus dem letzten Wort geschätzt."""
    from . import ffmpeg_utils as ff
    if ff.ffmpeg_vorhanden():
        try:
            return ff.probe_dauer(pfad)
        except Exception as e:
            log.warning("ffprobe für %s fehlgeschlagen (%s) — schätze aus Wort-Timings",
                        pfad.name, e)
    if woerter:
        return woerter[-1].end_s + 0.20
    raise TTSFehler(f"Dauer von {pfad} nicht bestimmbar (kein ffprobe, keine Timings)")


def synthesize(index: int, text: str, cfg: VideoConfig, zielpfad: Path) -> VoiceResult:
    """Spricht ``text`` und legt die MP3 unter ``zielpfad`` ab."""
    text = (text or "").strip()
    if not text:
        raise TTSFehler(f"Szene {index}: leerer Voiceover-Text")

    zielpfad.parent.mkdir(parents=True, exist_ok=True)
    audio, woerter = _asyncio_run(_synthese(text, cfg))
    zielpfad.write_bytes(audio)
    dauer = _dauer(zielpfad, woerter)

    log.info("Szene %d: Voiceover %.2fs, %d Wort-Timings (%s)",
             index, dauer, len(woerter), cfg.tts_stimme)
    return VoiceResult(index=index, pfad=str(zielpfad), dauer_s=dauer,
                       stimme=cfg.tts_stimme, woerter=woerter)


def synthesize_dry(index: int, text: str, cfg: VideoConfig,
                   zielpfad: Path) -> VoiceResult:
    """Trockenlauf-Variante: Stille in geschätzter Länge, synthetische Timings.

    Die Wortzeiten werden gleichmässig über die geschätzte Sprechdauer verteilt.
    Das ist nicht exakt, reicht aber, um Schnitt und Untertitel-Layout ohne
    Netzzugriff durchzuspielen.
    """
    from . import ffmpeg_utils as ff

    tokens = [w for w in re.split(r"\s+", (text or "").strip()) if w]
    dauer = max(0.8, len(tokens) / max(0.5, cfg.woerter_pro_sekunde))
    zielpfad.parent.mkdir(parents=True, exist_ok=True)

    if ff.ffmpeg_vorhanden():
        ff.run(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-t", f"{dauer:.3f}", "-c:a", "libmp3lame", "-q:a", "9",
                str(zielpfad)])
    else:
        zielpfad.write_bytes(b"")

    pro_wort = dauer / max(1, len(tokens))
    woerter = [WordTiming(text=t, start_s=i * pro_wort, end_s=(i + 1) * pro_wort)
               for i, t in enumerate(tokens)]
    return VoiceResult(index=index, pfad=str(zielpfad), dauer_s=dauer,
                       stimme=f"dry-run:{cfg.tts_stimme}", woerter=woerter)


def stimmen_auflisten(praefix: str = "de-") -> list[str]:
    """Listet verfügbare edge-tts-Stimmen (für ``--list-voices``)."""
    try:
        import edge_tts
    except ImportError as e:                       # pragma: no cover
        raise TTSFehler("Paket 'edge-tts' fehlt") from e

    async def _hole():
        return await edge_tts.list_voices()

    alle = _asyncio_run(_hole())
    return sorted(v["ShortName"] for v in alle
                  if not praefix or v["ShortName"].startswith(praefix))
