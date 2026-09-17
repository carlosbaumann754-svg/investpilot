"""Stufe 2 — Prompt-Generator.

Übersetzt jede Szene in einen Prompt, den ein Text-to-Video-Modell versteht.

Zwei Modi:

``rule_based`` (Standard, kostenlos)
    Baut den Prompt deterministisch aus den Feldern zusammen, die Stufe 1
    ohnehin schon liefert (``visual`` + ``kamera``), und ergänzt Stil-Suffix,
    Seitenverhältnis-Hinweis und Negativ-Prompt. Deterministisch heisst: der
    gleiche Input ergibt denselben Prompt — reproduzierbar und diffbar.

``llm``
    Lässt Claude die Bildbeschreibung zu einem dichteren Video-Prompt
    ausformulieren. Sinnvoll bei abstrakten Themen, bei denen Stufe 1 nur
    vage Bilder liefert. Kostet einen zusätzlichen API-Call.

Wichtig in beiden Modi: der Negativ-Prompt schliesst **Text im Bild** aus.
Untertitel brennt ffmpeg in Stufe 5 ein — vom Modell generierte Pseudo-Schrift
wäre doppelt und sähe zuverlässig kaputt aus.
"""
from __future__ import annotations

import logging
import re

from .config import VideoConfig
from .models import Scene, ScenePrompt, Script

log = logging.getLogger(__name__)

# Modelle reagieren auf Formatbegriffe im Prompt, auch wenn das
# Seitenverhältnis separat als Parameter geht — beides setzen hilft.
_FORMAT_HINWEIS = {
    "9:16": "vertical composition, subject centered, headroom for on-screen text",
    "16:9": "wide cinematic composition",
    "1:1": "square composition, subject centered",
}

_SYSTEM_LLM = """Du schreibst Prompts für Text-to-Video-Modelle (PixVerse, Kling).
Regeln:
- Ein Prompt = ein Satz bis maximal 60 Wörter, englisch.
- Reihenfolge: Motiv, Handlung, Umgebung, Licht, Kamerabewegung, Look.
- Konkret statt abstrakt. Keine Metaphern, keine Marken, keine realen Personen.
- Niemals Text, Schrift, Logos oder Untertitel im Bild verlangen.
Antworte ausschliesslich mit JSON: {"prompts": [{"index": 1, "prompt": "..."}]}"""


def _saeubern(text: str) -> str:
    """Entfernt Zeilenumbrüche und Doppel-Whitespace aus Prompt-Fragmenten."""
    return re.sub(r"\s+", " ", (text or "").strip()).rstrip(".,;")


def _regel_prompt(scene: Scene, cfg: VideoConfig) -> str:
    teile = [
        _saeubern(scene.visual),
        _saeubern(scene.kamera),
        _FORMAT_HINWEIS.get(cfg.aspect_ratio, ""),
        _saeubern(cfg.stil),
    ]
    return ", ".join(t for t in teile if t)


def build_prompts(script: Script, cfg: VideoConfig, client=None) -> list[ScenePrompt]:
    """Erzeugt die Video-Prompts zu allen Szenen des Skripts."""
    if cfg.prompt_modus == "llm":
        try:
            return _llm_prompts(script, cfg, client)
        except Exception as e:
            # Sichtbar loggen, dann auf den deterministischen Pfad zurückfallen —
            # ein fehlgeschlagener Komfort-Call darf den Lauf nicht killen.
            log.error("LLM-Prompt-Modus fehlgeschlagen (%s) — nutze rule_based", e)

    return [ScenePrompt(index=s.index, prompt=_regel_prompt(s, cfg),
                        negative_prompt=cfg.negative_prompt, dauer_s=s.dauer_s)
            for s in script.scenes]


def _llm_prompts(script: Script, cfg: VideoConfig, client=None) -> list[ScenePrompt]:
    from .script_generator import _call, _client, _json_aus_antwort

    client = client or _client()
    szenen_text = "\n".join(
        f'{s.index}. Bild: {s.visual} | Kamera: {s.kamera} | Gesagt wird: "{s.voiceover}"'
        for s in script.scenes)
    user = (f"Video-Thema: {script.thema}\n"
            f"Format: {cfg.aspect_ratio}, {int(cfg.szenen_dauer_s)} Sekunden pro Szene.\n"
            f"Gewünschter Look: {cfg.stil}\n\nSzenen:\n{szenen_text}")

    daten = _json_aus_antwort(_call(client, cfg.prompt_modell, _SYSTEM_LLM, user, 3000))
    nach_index = {int(p["index"]): _saeubern(p["prompt"])
                  for p in daten.get("prompts", []) if p.get("prompt")}

    out = []
    for s in script.scenes:
        prompt = nach_index.get(s.index) or _regel_prompt(s, cfg)
        if s.index not in nach_index:
            log.warning("Szene %d: kein LLM-Prompt erhalten — rule_based genutzt", s.index)
        # Format- und Stil-Anker auch im LLM-Modus anhängen, damit alle Clips
        # denselben Look haben.
        anker = _FORMAT_HINWEIS.get(cfg.aspect_ratio, "")
        if anker and anker.split(",")[0] not in prompt:
            prompt = f"{prompt}, {anker}"
        out.append(ScenePrompt(index=s.index, prompt=prompt,
                               negative_prompt=cfg.negative_prompt, dauer_s=s.dauer_s))
    return out
