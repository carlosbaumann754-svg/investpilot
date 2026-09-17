"""Orchestrierung der sechs Stufen.

Thema -> Skript -> Prompts -> Clips -> Voiceover -> Schnitt -> MP4.

Zwei Eigenschaften machen den Unterschied zwischen "Skript, das einmal lief"
und "Werkzeug, das man benutzt":

**Wiederaufnahme.** Jede Stufe schreibt ihr Ergebnis in ``job.json`` im
Lauf-Verzeichnis. Bricht der Lauf in Stufe 5 ab, weil ffmpeg fehlt oder die
Musik nicht gefunden wurde, sind die bereits bezahlten Clips nicht verloren:
``--resume <verzeichnis>`` setzt genau dort wieder an.

**Budget-Bremse.** Vor dem ersten kostenpflichtigen Call wird geschätzt, was
der Lauf kostet. Über ``max_kosten_usd`` bricht die Pipeline ab, statt still
das Dreifache auszugeben — der klassische Weg, wie aus einem Tippfehler in
``szenen_anzahl`` eine Rechnung wird.
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import ffmpeg_utils as ff
from .assembler import assemble
from .config import VideoConfig
from .models import ClipResult, SceneFit, Script, ScenePrompt, VoiceResult
from .prompt_generator import build_prompts
from .script_generator import generate_script, lade_script
from .subtitles import alle_cues
from .timing import fit_all, szenen_startzeiten
from .tts import synthesize, synthesize_dry
from .video_backends import VideoBackendFehler, get_backend

log = logging.getLogger(__name__)

SCHRITTE = ("script", "prompts", "clips", "voice", "timing", "assemble")


class BudgetUeberschritten(RuntimeError):
    """Der geschätzte Preis des Laufs liegt über ``max_kosten_usd``."""


def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text or "video")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:max_len].rstrip("-") or "video")


@dataclass
class JobState:
    """Persistenter Zustand eines Laufs (``job.json``)."""
    thema: str
    verzeichnis: Path
    config: dict = field(default_factory=dict)
    erstellt: str = ""
    dry_run: bool = False
    script: dict | None = None
    prompts: list[dict] = field(default_factory=list)
    clips: dict[str, dict] = field(default_factory=dict)
    voices: dict[str, dict] = field(default_factory=dict)
    fits: list[dict] = field(default_factory=list)
    kosten_usd: float = 0.0
    warnungen: list[str] = field(default_factory=list)
    ergebnis: str | None = None

    @property
    def pfad(self) -> Path:
        return self.verzeichnis / "job.json"

    def speichern(self) -> None:
        self.verzeichnis.mkdir(parents=True, exist_ok=True)
        daten = {
            "thema": self.thema, "erstellt": self.erstellt, "dry_run": self.dry_run,
            "config": self.config, "script": self.script, "prompts": self.prompts,
            "clips": self.clips, "voices": self.voices, "fits": self.fits,
            "kosten_usd": round(self.kosten_usd, 4), "warnungen": self.warnungen,
            "ergebnis": self.ergebnis,
        }
        tmp = self.pfad.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(daten, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.pfad)     # atomar, damit ein Abbruch keine halbe Datei hinterlässt

    @staticmethod
    def laden(verzeichnis: Path) -> "JobState":
        pfad = Path(verzeichnis) / "job.json"
        if not pfad.exists():
            raise FileNotFoundError(f"Kein Lauf in {verzeichnis} gefunden ({pfad} fehlt)")
        d = json.loads(pfad.read_text(encoding="utf-8"))
        return JobState(
            thema=d.get("thema", ""), verzeichnis=Path(verzeichnis),
            config=d.get("config", {}), erstellt=d.get("erstellt", ""),
            dry_run=bool(d.get("dry_run", False)), script=d.get("script"),
            prompts=d.get("prompts", []), clips=d.get("clips", {}),
            voices=d.get("voices", {}), fits=d.get("fits", []),
            kosten_usd=float(d.get("kosten_usd", 0.0)),
            warnungen=d.get("warnungen", []), ergebnis=d.get("ergebnis"))


class Pipeline:
    """Führt einen Lauf aus — neu oder fortgesetzt."""

    def __init__(self, cfg: VideoConfig, dry_run: bool = False,
                 kosten_bestaetigt: bool = False, script_client=None):
        self.cfg = cfg
        self.dry_run = dry_run
        self.kosten_bestaetigt = kosten_bestaetigt
        self.script_client = script_client

    # -- Verzeichnisse ------------------------------------------------------

    def _neues_verzeichnis(self, thema: str) -> Path:
        stempel = datetime.now().strftime("%Y%m%d-%H%M%S")
        return Path(self.cfg.output_dir) / f"{slugify(thema)}-{stempel}"

    # -- Einstieg -----------------------------------------------------------

    def run(self, thema: str, script_datei: str | None = None,
            zusatz: str = "", verzeichnis: Path | None = None,
            bis: str = "assemble") -> JobState:
        """Startet einen neuen Lauf."""
        verzeichnis = Path(verzeichnis) if verzeichnis else self._neues_verzeichnis(thema)
        state = JobState(thema=thema, verzeichnis=verzeichnis,
                         config=self.cfg.to_dict(),
                         erstellt=datetime.now().isoformat(timespec="seconds"),
                         dry_run=self.dry_run)
        state.speichern()
        return self._ausfuehren(state, script_datei=script_datei, zusatz=zusatz, bis=bis)

    def resume(self, verzeichnis: str | Path, bis: str = "assemble") -> JobState:
        """Setzt einen unterbrochenen Lauf fort."""
        state = JobState.laden(Path(verzeichnis))
        log.info("Setze Lauf fort: %s (Thema: %s)", state.verzeichnis, state.thema)
        return self._ausfuehren(state, bis=bis)

    # -- Ablauf -------------------------------------------------------------

    def _ausfuehren(self, state: JobState, script_datei: str | None = None,
                    zusatz: str = "", bis: str = "assemble") -> JobState:
        if bis not in SCHRITTE:
            raise ValueError(f"Unbekannter Schritt {bis!r}. Erlaubt: {', '.join(SCHRITTE)}")
        grenze = SCHRITTE.index(bis)
        start = time.monotonic()

        script = self._stufe_script(state, script_datei, zusatz)
        if grenze < SCHRITTE.index("prompts"):
            return self._abschluss(state, start)

        prompts = self._stufe_prompts(state, script)
        if grenze < SCHRITTE.index("clips"):
            return self._abschluss(state, start)

        clips = self._stufe_clips(state, prompts)
        if grenze < SCHRITTE.index("voice"):
            return self._abschluss(state, start)

        voices = self._stufe_voice(state, script)
        if grenze < SCHRITTE.index("timing"):
            return self._abschluss(state, start)

        fits = self._stufe_timing(state, clips, voices)
        if grenze < SCHRITTE.index("assemble"):
            return self._abschluss(state, start)

        self._stufe_assemble(state, script, clips, voices, fits)
        return self._abschluss(state, start)

    def _abschluss(self, state: JobState, start: float) -> JobState:
        state.speichern()
        log.info("Fertig in %.1fs — Verzeichnis: %s", time.monotonic() - start,
                 state.verzeichnis)
        if state.warnungen:
            log.warning("%d Hinweis(e) im Lauf — siehe job.json", len(state.warnungen))
        return state

    # -- Stufe 1 ------------------------------------------------------------

    def _stufe_script(self, state: JobState, script_datei: str | None,
                      zusatz: str) -> Script:
        if state.script:
            log.info("Stufe 1/6 Skript: übernommen aus job.json")
            return Script.from_dict(state.script)

        if script_datei:
            log.info("Stufe 1/6 Skript: geladen aus %s", script_datei)
            script = lade_script(script_datei)
            for s in script.scenes:
                s.dauer_s = float(self.cfg.szenen_dauer_s)
        else:
            log.info("Stufe 1/6 Skript: %s erzeugt %d Szenen à %.0fs",
                     self.cfg.script_modell, self.cfg.szenen_anzahl,
                     self.cfg.szenen_dauer_s)
            script = generate_script(state.thema, self.cfg, zusatz=zusatz,
                                     client=self.script_client)

        state.script = script.to_dict()
        (state.verzeichnis / "script.json").write_text(
            json.dumps(state.script, indent=2, ensure_ascii=False), encoding="utf-8")
        state.speichern()
        return script

    # -- Stufe 2 ------------------------------------------------------------

    def _stufe_prompts(self, state: JobState, script: Script) -> list[ScenePrompt]:
        if state.prompts:
            log.info("Stufe 2/6 Prompts: übernommen aus job.json")
            return [ScenePrompt.from_dict(p) for p in state.prompts]

        log.info("Stufe 2/6 Prompts: Modus %s", self.cfg.prompt_modus)
        prompts = build_prompts(script, self.cfg, client=self.script_client)
        state.prompts = [p.to_dict() for p in prompts]
        state.speichern()
        return prompts

    # -- Stufe 3 ------------------------------------------------------------

    def _budget_pruefen(self, prompts: list[ScenePrompt], offen: list[int]) -> float:
        backend = get_backend(self._backend_name(), self.cfg)
        schaetzung = sum(backend.kosten_pro_clip(p.dauer_s)
                         for p in prompts if p.index in offen)
        if self.dry_run or schaetzung <= self.cfg.max_kosten_usd or self.kosten_bestaetigt:
            log.info("Stufe 3/6 Video: %d Clip(s), geschätzt %.2f USD (Limit %.2f)",
                     len(offen), schaetzung, self.cfg.max_kosten_usd)
            return schaetzung
        raise BudgetUeberschritten(
            f"Geschätzte Kosten {schaetzung:.2f} USD für {len(offen)} Clips liegen "
            f"über max_kosten_usd={self.cfg.max_kosten_usd:.2f}. "
            "Entweder --max-kosten erhöhen, --yes setzen, weniger/kürzere Szenen "
            "wählen — oder erst mit --dry-run die Kette testen.")

    def _backend_name(self) -> str:
        return "dry-run" if self.dry_run else self.cfg.video_backend

    def _stufe_clips(self, state: JobState,
                     prompts: list[ScenePrompt]) -> dict[int, ClipResult]:
        clips = {int(k): ClipResult.from_dict(v) for k, v in state.clips.items()
                 if Path(v.get("pfad", "")).exists()}
        offen = [p.index for p in prompts if p.index not in clips]
        if clips:
            log.info("Stufe 3/6 Video: %d Clip(s) bereits vorhanden", len(clips))
        if not offen:
            return clips

        self._budget_pruefen(prompts, offen)

        backend = get_backend(self._backend_name(), self.cfg)
        fallback = None
        if not self.dry_run and self.cfg.video_fallback_backend:
            fallback = get_backend(self.cfg.video_fallback_backend, self.cfg)

        zielordner = state.verzeichnis / "clips"
        nach_index = {p.index: p for p in prompts}

        def erzeuge(idx: int) -> ClipResult:
            prompt = nach_index[idx]
            ziel = zielordner / f"scene_{idx:02d}.mp4"
            try:
                return backend.generate(prompt, ziel)
            except VideoBackendFehler as e:
                if fallback is None:
                    raise
                log.error("Szene %d: %s endgültig gescheitert (%s) — Fallback %s",
                          idx, backend.name, e, fallback.name)
                ergebnis = fallback.generate(prompt, ziel)
                ergebnis.fallback = True
                return ergebnis

        fehler: list[str] = []
        with ThreadPoolExecutor(max_workers=max(1, self.cfg.video_parallel)) as pool:
            futures = {pool.submit(erzeuge, i): i for i in offen}
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    clip = fut.result()
                except Exception as e:
                    meldung = f"Szene {idx}: Video-Generierung fehlgeschlagen — {e}"
                    log.error(meldung)
                    fehler.append(meldung)
                    continue
                clips[idx] = clip
                state.clips[str(idx)] = clip.to_dict()
                state.kosten_usd += clip.kosten_usd
                state.speichern()      # nach jedem bezahlten Clip sichern
                log.info("Szene %d: Clip fertig (%s, %.2f USD)",
                         idx, clip.backend, clip.kosten_usd)

        state.warnungen.extend(fehler)
        if not clips:
            raise RuntimeError("Keine einzige Szene konnte generiert werden — "
                               "siehe Fehler oben. Kosten bisher: "
                               f"{state.kosten_usd:.2f} USD")
        if fehler:
            log.warning("%d von %d Szenen fehlen — das Video wird kürzer",
                        len(fehler), len(prompts))
        state.speichern()
        return clips

    # -- Stufe 4 ------------------------------------------------------------

    def _stufe_voice(self, state: JobState, script: Script) -> dict[int, VoiceResult]:
        voices = {int(k): VoiceResult.from_dict(v) for k, v in state.voices.items()
                  if Path(v.get("pfad", "")).exists()}
        offen = [s for s in script.scenes if s.index not in voices]
        log.info("Stufe 4/6 Voiceover: %d neu, %d vorhanden (%s)",
                 len(offen), len(voices), self.cfg.tts_stimme)
        if not offen:
            return voices

        sprecher = synthesize_dry if self.dry_run else synthesize
        zielordner = state.verzeichnis / "voice"

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(sprecher, s.index, s.voiceover, self.cfg,
                                   zielordner / f"scene_{s.index:02d}.mp3"): s.index
                       for s in offen}
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    voices[idx] = fut.result()
                except Exception as e:
                    meldung = f"Szene {idx}: Voiceover fehlgeschlagen — {e}"
                    log.error(meldung)
                    state.warnungen.append(meldung)

        state.voices = {str(i): v.to_dict() for i, v in voices.items()}
        state.speichern()
        return voices

    # -- Stufe 5a: Timing ---------------------------------------------------

    def _stufe_timing(self, state: JobState, clips: dict[int, ClipResult],
                      voices: dict[int, VoiceResult]) -> list[SceneFit]:
        clip_dauern, voice_dauern = {}, {}
        probe = ff.ffmpeg_vorhanden()

        for idx, clip in clips.items():
            dauer = clip.dauer_s
            if probe:
                try:
                    dauer = ff.probe_dauer(clip.pfad)
                except Exception as e:
                    log.warning("Szene %d: ffprobe am Clip fehlgeschlagen (%s) — "
                                "nutze gemeldete %.2fs", idx, e, dauer)
            clip_dauern[idx] = dauer

        for idx, voice in voices.items():
            voice_dauern[idx] = voice.dauer_s

        fits = fit_all(clip_dauern, voice_dauern, self.cfg)
        for f in fits:
            for w in f.warnungen:
                log.info("Szene %d: %s", f.index, w)
                if "Limit" in w:
                    state.warnungen.append(f"Szene {f.index}: {w}")

        state.fits = [f.to_dict() for f in fits]
        state.speichern()
        gesamt = sum(f.scene_dauer_s for f in fits)
        log.info("Stufe 5/6 Timing: %d Szenen, Gesamtlänge %.1fs", len(fits), gesamt)
        return fits

    # -- Stufe 6 ------------------------------------------------------------

    def _stufe_assemble(self, state: JobState, script: Script,
                        clips: dict[int, ClipResult], voices: dict[int, VoiceResult],
                        fits: list[SceneFit]) -> Path:
        starts = szenen_startzeiten(fits)
        texte = {s.index: s.voiceover for s in script.scenes}
        cues = alle_cues(voices, fits, starts, self.cfg, texte)

        ziel = state.verzeichnis / "video.mp4"
        log.info("Stufe 6/6 Schnitt: %d Segmente, %d Untertitel-Blöcke",
                 len(fits), len(cues))
        ergebnis = assemble(fits, clips, voices, cues, self.cfg,
                            state.verzeichnis / "work", ziel)
        state.ergebnis = str(ergebnis)
        state.speichern()
        log.info("Video fertig: %s", ergebnis)
        return ergebnis
