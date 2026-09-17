"""Stufe 1 — Skript-Generator.

Aus einem Thema (Freitext) entsteht ein sendefähiges Kurzvideo-Skript:
Hook, 6–8 Szenen mit Bildbeschreibung, Kamerabewegung und Voiceover-Text,
plus Call-to-Action und Hashtags.

Der kritische Teil ist nicht die Kreativität, sondern das **Wortbudget**:
jede Szene darf nur so viel Text enthalten, wie die TTS-Stimme in der
Clip-Länge sprechen kann. Wird das verletzt, muss der Schnitt das später mit
beschleunigter Stimme oder Standbildern kaschieren (siehe ``timing.py``).
Deshalb prüft dieses Modul nach der Generierung nach und lässt zu lange
Szenen einmal gezielt nachbessern, statt den Fehler durchzureichen.
"""
from __future__ import annotations

import json
import logging
import re

from .config import VideoConfig, anthropic_key
from .models import Scene, Script

log = logging.getLogger(__name__)

_SYSTEM = """Du bist Drehbuchautor für virale Kurzvideos (Reels/Shorts/TikTok).
Du schreibst in {sprache}. Du lieferst ausschliesslich gültiges JSON, keinen Fliesstext,
keine Markdown-Codefences, keine Erklärungen.

Handwerkliche Regeln:
- Der Hook entscheidet über alles. Er sitzt in Szene 1 und muss in den ersten
  2 Sekunden eine Spannung, eine Zahl oder einen Widerspruch setzen.
- Jede Szene ist ein eigener Gedanke mit einer eigenen Bildidee. Keine
  Wiederholungen, kein Füllmaterial.
- Das Voiceover ist gesprochene Sprache: kurze Hauptsätze, keine
  Schachtelsätze, keine Aufzählungszeichen, keine Klammern, keine Emojis,
  keine Abkürzungen die man nicht ausspricht.
- Das Voiceover jeder Szene hat HÖCHSTENS {budget} Wörter. Das ist eine harte
  technische Grenze, keine Empfehlung: längerer Text passt nicht in den Clip.
- "visual" und "kamera" schreibst du auf ENGLISCH — sie gehen direkt an ein
  Text-to-Video-Modell, und die verstehen englische Prompts deutlich besser.
  Alles andere ("voiceover", "titel", "hook", "cta") bleibt in {sprache}.
- "visual" beschreibt ein einzelnes, konkretes Bild — Motiv, Handlung,
  Umgebung, Licht. Kein abstraktes Konzept, keine Texteinblendung, keine
  Schrift im Bild, keine real existierenden Personen oder Marken.
- "kamera" ist genau eine Bewegung, z.B. "slow push-in", "lateral dolly right",
  "static wide shot", "slow crane shot from above".
- Die letzte Szene enthält den Call-to-Action im Voiceover."""

_USER = """Thema: {thema}

Erzeuge ein Skript mit genau {n} Szenen à {dauer} Sekunden.
Zielplattform: {plattform} ({ratio}).
{zusatz}
Antworte mit diesem JSON-Schema:

{{
  "titel": "kurzer Titel für die Plattform",
  "hook": "der Aufhänger in einem Satz",
  "scenes": [
    {{"index": 1, "titel": "Kurzlabel", "visual": "...", "kamera": "...", "voiceover": "..."}}
  ],
  "cta": "Call-to-Action in einem Satz",
  "hashtags": ["...", "..."]
}}"""

_REPAIR = """Diese Szenen überschreiten das Wortbudget von {budget} Wörtern:

{liste}

Schreibe NUR diese Szenen neu — gleiche Aussage, gleiche Bildidee, aber das
Voiceover auf höchstens {budget} Wörter gekürzt. Antworte mit JSON:

{{"scenes": [{{"index": <n>, "voiceover": "..."}}]}}"""


def _wortzahl(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


def _json_aus_antwort(text: str) -> dict:
    """Holt das JSON-Objekt aus der Modellantwort — auch wenn es eingerahmt ist."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        if start == -1:
            raise ValueError(f"Keine JSON-Antwort erhalten: {text[:200]!r}")
        text = text[start:]
    # Bis zur passenden schliessenden Klammer lesen (robust gegen Nachgeplapper).
    tiefe, ende, in_string, escape = 0, None, False, False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            tiefe += 1
        elif ch == "}":
            tiefe -= 1
            if tiefe == 0:
                ende = i + 1
                break
    if ende is None:
        raise ValueError("JSON-Antwort ist unvollständig (Klammern gehen nicht auf)")
    return json.loads(text[:ende])


def _client():
    try:
        import anthropic
    except ImportError as e:                       # pragma: no cover
        raise RuntimeError(
            "Paket 'anthropic' fehlt — pip install -r video_generator/requirements.txt"
        ) from e
    if not anthropic_key():
        raise RuntimeError(
            "ANTHROPIC_API_KEY ist nicht gesetzt. Ohne Key kann Stufe 1 (Skript) "
            "nicht laufen. Alternativ ein fertiges Skript per --script-file übergeben."
        )
    return anthropic.Anthropic(api_key=anthropic_key())


def _call(client, modell: str, system: str, user: str, max_tokens: int) -> str:
    antwort = client.messages.create(
        model=modell,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user},
                  # Prefill zwingt das Modell direkt in die JSON-Struktur.
                  {"role": "assistant", "content": "{"}],
    )
    teile = [b.text for b in antwort.content if getattr(b, "type", "") == "text"]
    return "{" + "".join(teile)


def generate_script(thema: str, cfg: VideoConfig, zusatz: str = "",
                    client=None) -> Script:
    """Erzeugt das Skript zu ``thema``.

    Args:
        thema: Freitext-Thema des Videos.
        cfg: Konfiguration (Szenenzahl, Dauer, Sprache, Wortbudget).
        zusatz: optionale zusätzliche Anweisung (Tonalität, Zielgruppe, Fakten).
        client: optionaler Anthropic-Client (für Tests injizierbar).

    Raises:
        RuntimeError: wenn kein API-Key gesetzt ist.
        ValueError: wenn die Antwort kein verwertbares Skript enthält.
    """
    client = client or _client()
    budget = cfg.woerter_budget_pro_szene
    sprache = {"de": "Deutsch", "en": "Englisch", "fr": "Französisch",
               "it": "Italienisch"}.get(cfg.sprache, cfg.sprache)

    roh = _call(
        client, cfg.script_modell,
        _SYSTEM.format(sprache=sprache, budget=budget),
        _USER.format(thema=thema, n=cfg.szenen_anzahl, dauer=int(cfg.szenen_dauer_s),
                     plattform=cfg.zielplattform, ratio=cfg.aspect_ratio,
                     zusatz=(zusatz.strip() + "\n") if zusatz else ""),
        cfg.script_max_tokens)
    daten = _json_aus_antwort(roh)

    scenes = _scenes_aus_daten(daten, cfg)
    scenes = _budget_nachbessern(client, cfg, scenes, budget)

    script = Script(
        thema=thema,
        hook=str(daten.get("hook", "")).strip(),
        scenes=scenes,
        cta=str(daten.get("cta", "")).strip(),
        sprache=cfg.sprache,
        titel=str(daten.get("titel", thema)).strip(),
        hashtags=[str(h).lstrip("#") for h in daten.get("hashtags", [])][:12],
        modell=cfg.script_modell,
    )
    log.info("Skript erzeugt: %d Szenen, %d Wörter gesamt, ~%.0fs",
             len(script.scenes),
             sum(_wortzahl(s.voiceover) for s in script.scenes),
             script.gesamt_dauer_s)
    return script


def _scenes_aus_daten(daten: dict, cfg: VideoConfig) -> list[Scene]:
    roh_scenes = daten.get("scenes") or daten.get("szenen") or []
    if not roh_scenes:
        raise ValueError("Antwort enthält keine Szenen")
    scenes: list[Scene] = []
    for i, s in enumerate(roh_scenes, start=1):
        visual = str(s.get("visual") or s.get("bild") or "").strip()
        voice = str(s.get("voiceover") or s.get("text") or "").strip()
        if not visual or not voice:
            raise ValueError(f"Szene {i}: 'visual' oder 'voiceover' fehlt")
        scenes.append(Scene(
            index=i,
            titel=str(s.get("titel") or s.get("title") or f"Szene {i}").strip(),
            visual=visual,
            kamera=str(s.get("kamera") or s.get("camera") or "statische Totale").strip(),
            voiceover=voice,
            dauer_s=float(cfg.szenen_dauer_s),
        ))
    if len(scenes) < 3:
        raise ValueError(f"Nur {len(scenes)} Szenen erhalten — zu wenig für ein Video")
    return scenes


def _budget_nachbessern(client, cfg: VideoConfig, scenes: list[Scene],
                        budget: int) -> list[Scene]:
    """Eine Reparaturrunde für Szenen über Wortbudget.

    Toleranz: bis zu 15% über Budget lassen wir durch — das fängt der Schnitt
    per Tempo-Anpassung sauber ab. Alles darüber geht zurück ans Modell.
    """
    toleranz = int(budget * 1.15)
    zu_lang = [s for s in scenes if _wortzahl(s.voiceover) > toleranz]
    if not zu_lang:
        return scenes

    log.info("%d Szene(n) über Wortbudget (%d) — eine Reparaturrunde",
             len(zu_lang), budget)
    liste = "\n".join(
        f'- Szene {s.index} ({_wortzahl(s.voiceover)} Wörter): "{s.voiceover}"'
        for s in zu_lang)
    try:
        roh = _call(client, cfg.script_modell,
                    "Du kürzt Voiceover-Texte. Nur JSON, keine Erklärung.",
                    _REPAIR.format(budget=budget, liste=liste), 1500)
        neu = {int(s["index"]): str(s["voiceover"]).strip()
               for s in _json_aus_antwort(roh).get("scenes", [])}
    except Exception as e:
        # Sichtbar, aber kein Abbruch: der Schnitt kann das notfalls auffangen.
        log.error("Reparaturrunde fehlgeschlagen (%s) — Szenen bleiben zu lang, "
                  "der Schnitt wird Stimme beschleunigen oder Standbilder halten", e)
        return scenes

    for s in scenes:
        if s.index in neu and neu[s.index]:
            vorher = _wortzahl(s.voiceover)
            s.voiceover = neu[s.index]
            log.info("Szene %d gekürzt: %d -> %d Wörter", s.index, vorher,
                     _wortzahl(s.voiceover))
    return scenes


def lade_script(pfad: str) -> Script:
    """Lädt ein bereits vorhandenes Skript (JSON) — überspringt Stufe 1."""
    with open(pfad, encoding="utf-8") as f:
        return Script.from_dict(json.load(f))
