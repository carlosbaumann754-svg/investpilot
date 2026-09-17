"""Dünne Hülle um ffmpeg/ffprobe.

Absichtlich klein gehalten: alle Entscheidungen (wie lang, wie schnell, wo
geschnitten wird) fallen in ``timing.py`` und werden hier nur noch ausgeführt.
Damit bleibt die Logik testbar, ohne ffmpeg installieren zu müssen.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class FFmpegFehlt(RuntimeError):
    """ffmpeg/ffprobe ist nicht im PATH."""


class FFmpegFehler(RuntimeError):
    """Ein ffmpeg-Aufruf ist fehlgeschlagen — enthält stderr im Text."""


def ffmpeg_pfad() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe_pfad() -> str | None:
    return shutil.which("ffprobe")


def ffmpeg_vorhanden() -> bool:
    return bool(ffmpeg_pfad() and ffprobe_pfad())


def _fordere_ffmpeg() -> tuple[str, str]:
    ff, fp = ffmpeg_pfad(), ffprobe_pfad()
    if not ff or not fp:
        raise FFmpegFehlt(
            "ffmpeg und ffprobe werden für den Schnitt gebraucht, sind aber nicht "
            "im PATH.\n"
            "  Debian/Ubuntu: sudo apt-get install -y ffmpeg\n"
            "  macOS:         brew install ffmpeg\n"
            "  Windows:       winget install Gyan.FFmpeg\n"
            "Ohne ffmpeg laufen Stufe 1–4 trotzdem (Skript, Prompts, Clips, "
            "Voiceover) — nur der Zusammenschnitt fehlt.")
    return ff, fp


def run(args: list[str], timeout: int = 1800) -> str:
    """Führt ffmpeg aus und wirft bei Fehler mit lesbarem stderr."""
    ff, _ = _fordere_ffmpeg()
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args]
    log.debug("ffmpeg %s", " ".join(args))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()[-12:]
        raise FFmpegFehler("ffmpeg fehlgeschlagen:\n  " + "\n  ".join(tail))
    return p.stderr or ""


def probe_dauer(pfad: str | Path, timeout: int = 60) -> float:
    """Liest die Dauer einer Medien-Datei in Sekunden.

    Fragt zuerst das Format, dann — falls dort keine Dauer steht (kommt bei
    manchen MP3s vor) — den ersten Stream.
    """
    _, fp = _fordere_ffmpeg()
    p = subprocess.run(
        [fp, "-v", "error", "-show_entries", "format=duration:stream=duration",
         "-of", "json", str(pfad)],
        capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise FFmpegFehler(f"ffprobe fehlgeschlagen für {pfad}: {p.stderr.strip()}")
    daten = json.loads(p.stdout or "{}")
    kandidaten = [daten.get("format", {}).get("duration")]
    kandidaten += [s.get("duration") for s in daten.get("streams", [])]
    for k in kandidaten:
        try:
            wert = float(k)
        except (TypeError, ValueError):
            continue
        if wert > 0:
            return wert
    raise FFmpegFehler(f"Keine Dauer in {pfad} gefunden")


def hat_audiospur(pfad: str | Path, timeout: int = 60) -> bool:
    _, fp = _fordere_ffmpeg()
    p = subprocess.run(
        [fp, "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", str(pfad)],
        capture_output=True, text=True, timeout=timeout)
    return bool((p.stdout or "").strip())


def escape_filter_pfad(pfad: str | Path) -> str:
    """Maskiert einen Pfad für die Verwendung in einem ffmpeg-Filterausdruck.

    Der ``subtitles=``-Filter parst seinen Parameter selbst — Backslashes,
    Doppelpunkte (Windows-Laufwerke!) und einfache Anführungszeichen müssen
    escaped werden, sonst bricht der Filtergraph.
    """
    s = str(pfad).replace("\\", "/")
    s = s.replace("'", r"\'").replace(":", r"\:")
    return s
