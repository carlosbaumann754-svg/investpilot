"""Video-Generator — vom Thema zum fertig geschnittenen Kurzvideo.

Sechs Stufen:

1. ``script_generator``  Thema -> Hook + Szenen + Voiceover-Text (Claude)
2. ``prompt_generator``  Szene -> Video-Prompt
3. ``video_backends``    Prompt -> Clip (fal.ai: PixVerse, Fallback Kling)
4. ``tts``               Voiceover-Text -> Sprache + Wort-Timings (edge-tts)
5. ``timing`` + ``subtitles``  Ton und Bild aufeinander passen, Untertitel setzen
6. ``assembler``         Schnitt, Musik, Untertitel einbrennen (ffmpeg)

Gesteuert wird alles über ``pipeline.Pipeline`` bzw. die Kommandozeile in
``cli`` (``python -m video_generator "Thema"``).

Das Paket ist bewusst unabhängig vom Trading-Teil dieses Repositories: es
teilt sich weder Konfiguration noch Daten noch Abhängigkeiten. Installiert
wird es über ``video_generator/requirements.txt``, nicht über die des Bots.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
