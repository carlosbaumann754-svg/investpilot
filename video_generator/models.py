"""Datenmodelle der Video-Pipeline.

Bewusst reine Dataclasses ohne Verhalten: sie wandern durch alle sechs Stufen
(Skript -> Prompts -> Clips -> Voiceover -> Untertitel -> Schnitt) und werden
zwischen den Stufen als JSON auf die Platte geschrieben. Dadurch ist jeder
Lauf wiederaufnehmbar (``--resume``) — was bei kostenpflichtigen Video-Calls
kein Komfort, sondern Budgetschutz ist.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


def _clean(d: dict) -> dict:
    """Entfernt None-Werte, damit die Job-JSON lesbar bleibt."""
    return {k: v for k, v in d.items() if v is not None}


@dataclass
class Scene:
    """Eine Szene des Skripts (Stufe 1)."""
    index: int
    titel: str
    visual: str           # was zu sehen ist (Bildbeschreibung)
    kamera: str           # Kamerabewegung, z.B. "langsamer Push-In"
    voiceover: str        # gesprochener Text dieser Szene
    dauer_s: float = 8.0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Scene":
        return Scene(
            index=int(d["index"]),
            titel=str(d.get("titel", "")),
            visual=str(d["visual"]),
            kamera=str(d.get("kamera", "")),
            voiceover=str(d["voiceover"]),
            dauer_s=float(d.get("dauer_s", 8.0)),
        )


@dataclass
class Script:
    """Gesamtes Skript (Stufe 1)."""
    thema: str
    hook: str
    scenes: list[Scene]
    cta: str = ""
    sprache: str = "de"
    titel: str = ""
    hashtags: list[str] = field(default_factory=list)
    modell: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["scenes"] = [s.to_dict() for s in self.scenes]
        return d

    @staticmethod
    def from_dict(d: dict) -> "Script":
        return Script(
            thema=d["thema"],
            hook=d.get("hook", ""),
            scenes=[Scene.from_dict(s) for s in d.get("scenes", [])],
            cta=d.get("cta", ""),
            sprache=d.get("sprache", "de"),
            titel=d.get("titel", ""),
            hashtags=list(d.get("hashtags", [])),
            modell=d.get("modell", ""),
        )

    @property
    def gesamt_dauer_s(self) -> float:
        return sum(s.dauer_s for s in self.scenes)


@dataclass
class ScenePrompt:
    """Video-Prompt einer Szene (Stufe 2)."""
    index: int
    prompt: str
    negative_prompt: str = ""
    dauer_s: float = 8.0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ScenePrompt":
        return ScenePrompt(
            index=int(d["index"]),
            prompt=str(d["prompt"]),
            negative_prompt=str(d.get("negative_prompt", "")),
            dauer_s=float(d.get("dauer_s", 8.0)),
        )


@dataclass
class ClipResult:
    """Ergebnis eines Video-Generierungs-Calls (Stufe 3)."""
    index: int
    pfad: str
    backend: str
    modell: str
    dauer_s: float
    kosten_usd: float = 0.0
    request_id: str | None = None
    url: str | None = None
    fallback: bool = False

    def to_dict(self) -> dict:
        return _clean(asdict(self))

    @staticmethod
    def from_dict(d: dict) -> "ClipResult":
        return ClipResult(
            index=int(d["index"]), pfad=d["pfad"], backend=d["backend"],
            modell=d["modell"], dauer_s=float(d["dauer_s"]),
            kosten_usd=float(d.get("kosten_usd", 0.0)),
            request_id=d.get("request_id"), url=d.get("url"),
            fallback=bool(d.get("fallback", False)),
        )


@dataclass
class WordTiming:
    """Ein Wort mit Zeitstempel relativ zum Beginn der Voiceover-Datei."""
    text: str
    start_s: float
    end_s: float

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "WordTiming":
        return WordTiming(text=d["text"], start_s=float(d["start_s"]),
                          end_s=float(d["end_s"]))


@dataclass
class VoiceResult:
    """Ergebnis der Sprachsynthese einer Szene (Stufe 4)."""
    index: int
    pfad: str
    dauer_s: float
    stimme: str
    woerter: list[WordTiming] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["woerter"] = [w.to_dict() for w in self.woerter]
        return d

    @staticmethod
    def from_dict(d: dict) -> "VoiceResult":
        return VoiceResult(
            index=int(d["index"]), pfad=d["pfad"],
            dauer_s=float(d["dauer_s"]), stimme=d.get("stimme", ""),
            woerter=[WordTiming.from_dict(w) for w in d.get("woerter", [])],
        )


@dataclass
class SubtitleCue:
    """Ein Untertitel-Block mit absoluter Zeit im Endvideo."""
    start_s: float
    end_s: float
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SceneFit:
    """Wie eine Szene im Schnitt auf die Voiceover-Länge gepasst wird (Stufe 5).

    Alle Werte sind Sekunden, ``audio_tempo`` ist der ``atempo``-Faktor.
    Diese Struktur ist das Ergebnis von :func:`video_generator.timing.fit_scene`
    und damit ohne ffmpeg testbar.
    """
    index: int
    scene_dauer_s: float
    audio_lead_s: float
    audio_tail_s: float
    audio_tempo: float
    video_hold_s: float
    video_trim_s: float
    voice_dauer_s: float
    warnungen: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class JobPaths:
    """Verzeichnis-Layout eines Laufs."""
    root: str
    clips: str
    voice: str
    work: str
    output: str
