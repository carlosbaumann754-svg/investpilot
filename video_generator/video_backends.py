"""Stufe 3 — Video-Generierung über fal.ai.

Ein API-Call pro Szene. Das ist die einzige Stufe, die echtes Geld kostet,
deshalb ist sie defensiv gebaut:

* **Queue statt Sync.** fal.ai stellt lange Läufe in eine Warteschlange
  (``POST https://queue.fal.run/{model_id}`` -> ``request_id``, danach
  ``GET .../requests/{id}/status`` bis ``COMPLETED``, dann
  ``GET .../requests/{id}``). Ein Sync-Call würde bei 8s-Clips regelmässig in
  Gateway-Timeouts laufen.
* **Retry nur bei sinnvollen Fehlern.** 5xx, Netzwerkabbruch und
  Queue-``ERROR`` werden mit exponentiellem Backoff wiederholt. Ein 4xx
  (kaputter Prompt, falscher Key, Guardrail) wird *nicht* wiederholt — das
  würde nur dreimal dasselbe kosten.
* **Fallback-Backend.** Scheitert PixVerse endgültig, übernimmt Kling für
  genau diese Szene. Der Lauf bricht nicht wegen einer Szene ab.
* **Kostenschätzung vorab.** ``kosten_pro_clip()`` speist die Budget-Bremse
  in ``pipeline.py``.

Die Modell-Kennungen stehen in der Config (``pixverse_model_id``,
``kling_model_id``), nicht im Code — fal.ai versioniert Modelle im Pfad.
"""
from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path

from .config import VideoConfig, fal_key
from .models import ClipResult, ScenePrompt

log = logging.getLogger(__name__)


class VideoBackendFehler(RuntimeError):
    """Generierung ist endgültig fehlgeschlagen (nach allen Retries)."""


class _NichtWiederholbar(VideoBackendFehler):
    """Fehler, bei dem ein Retry sinnlos ist (4xx, ungültige Eingabe)."""


# Richtwerte in USD pro Clip, Stand Anfang 2026. Sie dienen der Budget-Warnung
# vor dem Lauf, nicht der Abrechnung — die macht fal.ai. Bei Preisänderungen
# hier nachziehen; falsch geschätzte Kosten blockieren sonst unnötig Läufe.
_PREISE_USD = {
    "pixverse": {"540p": 0.030, "720p": 0.040, "1080p": 0.090},   # pro Sekunde
    "kling": {"540p": 0.070, "720p": 0.070, "1080p": 0.070},
}


class VideoBackend(ABC):
    """Gemeinsames Interface aller Video-Modelle."""

    name = "abstract"

    def __init__(self, cfg: VideoConfig):
        self.cfg = cfg

    @property
    @abstractmethod
    def model_id(self) -> str: ...

    @abstractmethod
    def payload(self, prompt: ScenePrompt) -> dict:
        """Baut den modellspezifischen Request-Body."""

    def snap_dauer(self, dauer_s: float) -> float:
        """Rundet auf eine vom Modell unterstützte Clip-Länge."""
        return float(dauer_s)

    def kosten_pro_clip(self, dauer_s: float) -> float:
        tabelle = _PREISE_USD.get(self.name, {})
        pro_sekunde = tabelle.get(self.cfg.aufloesung, max(tabelle.values(), default=0.05))
        return round(self.snap_dauer(dauer_s) * pro_sekunde, 4)

    @abstractmethod
    def generate(self, prompt: ScenePrompt, zielpfad: Path) -> ClipResult: ...


# ---------------------------------------------------------------------------
# fal.ai
# ---------------------------------------------------------------------------

class FalBackend(VideoBackend):
    """Gemeinsame Queue-Mechanik aller fal.ai-Modelle."""

    def __init__(self, cfg: VideoConfig, session=None):
        super().__init__(cfg)
        self._session = session

    @property
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def _headers(self) -> dict:
        key = fal_key()
        if not key:
            raise _NichtWiederholbar(
                "FAL_KEY ist nicht gesetzt. Ohne Key kann Stufe 3 (Video) nicht "
                "laufen — für einen Trockenlauf ohne Kosten: --dry-run")
        return {"Authorization": f"Key {key}", "Content-Type": "application/json"}

    def _basis(self) -> str:
        return f"{self.cfg.fal_queue_url.rstrip('/')}/{self.model_id}"

    # -- HTTP ---------------------------------------------------------------

    def _pruefe(self, resp, kontext: str) -> dict:
        if 400 <= resp.status_code < 500:
            raise _NichtWiederholbar(
                f"{kontext}: HTTP {resp.status_code} — {resp.text[:400]}")
        if resp.status_code >= 500:
            raise VideoBackendFehler(
                f"{kontext}: HTTP {resp.status_code} — {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as e:
            raise VideoBackendFehler(f"{kontext}: keine JSON-Antwort ({e})") from e

    def _submit(self, prompt: ScenePrompt) -> str:
        body = self.payload(prompt)
        log.debug("submit %s: %s", self.model_id, json.dumps(body)[:300])
        r = self.session.post(self._basis(), headers=self._headers(),
                              json=body, timeout=60)
        daten = self._pruefe(r, "Submit")
        rid = daten.get("request_id") or daten.get("requestId")
        if not rid:
            raise VideoBackendFehler(f"Submit ohne request_id: {str(daten)[:300]}")
        return rid

    def _warte(self, request_id: str) -> dict:
        """Pollt bis COMPLETED und holt das Ergebnis."""
        status_url = f"{self._basis()}/requests/{request_id}/status"
        ende = time.monotonic() + self.cfg.video_timeout_s
        letzter = ""
        while time.monotonic() < ende:
            r = self.session.get(status_url, headers=self._headers(), timeout=60)
            daten = self._pruefe(r, "Status")
            status = str(daten.get("status", "")).upper()
            if status != letzter:
                log.info("  %s %s: %s", self.name, request_id[:8], status or "?")
                letzter = status
            if status == "COMPLETED":
                break
            if status in ("ERROR", "FAILED"):
                raise VideoBackendFehler(
                    f"Queue meldet {status}: {str(daten.get('error') or daten)[:300]}")
            time.sleep(self.cfg.video_poll_intervall_s)
        else:
            raise VideoBackendFehler(
                f"Timeout nach {self.cfg.video_timeout_s}s (Status zuletzt: {letzter or '?'})")

        r = self.session.get(f"{self._basis()}/requests/{request_id}",
                             headers=self._headers(), timeout=120)
        return self._pruefe(r, "Result")

    @staticmethod
    def _video_url(ergebnis: dict) -> str:
        video = ergebnis.get("video") or {}
        url = video.get("url") if isinstance(video, dict) else None
        if not url:
            # Manche Modelle liefern eine Liste statt eines Objekts.
            videos = ergebnis.get("videos") or []
            if videos and isinstance(videos[0], dict):
                url = videos[0].get("url")
        if not url:
            raise VideoBackendFehler(
                f"Ergebnis enthält keine Video-URL: {str(ergebnis)[:300]}")
        return url

    def _download(self, url: str, ziel: Path) -> None:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        tmp = ziel.with_suffix(ziel.suffix + ".part")
        with self.session.get(url, stream=True, timeout=300) as r:
            if r.status_code != 200:
                raise VideoBackendFehler(f"Download HTTP {r.status_code} für {url}")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
        if tmp.stat().st_size < 1024:
            tmp.unlink(missing_ok=True)
            raise VideoBackendFehler(f"Heruntergeladene Datei ist leer: {url}")
        tmp.replace(ziel)

    # -- öffentlich ---------------------------------------------------------

    def generate(self, prompt: ScenePrompt, zielpfad: Path) -> ClipResult:
        """Generiert einen Clip inklusive Retries."""
        versuche = max(1, self.cfg.video_retries + 1)
        letzter_fehler: Exception | None = None

        for versuch in range(1, versuche + 1):
            try:
                rid = self._submit(prompt)
                ergebnis = self._warte(rid)
                url = self._video_url(ergebnis)
                self._download(url, zielpfad)
                return ClipResult(
                    index=prompt.index, pfad=str(zielpfad), backend=self.name,
                    modell=self.model_id, dauer_s=self.snap_dauer(prompt.dauer_s),
                    kosten_usd=self.kosten_pro_clip(prompt.dauer_s),
                    request_id=rid, url=url)
            except _NichtWiederholbar:
                raise
            except Exception as e:
                letzter_fehler = e
                if versuch >= versuche:
                    break
                wartezeit = min(60, 2 ** versuch * 5)
                log.error("Szene %d: %s-Versuch %d/%d fehlgeschlagen (%s) — "
                          "neuer Versuch in %ds", prompt.index, self.name,
                          versuch, versuche, e, wartezeit)
                time.sleep(wartezeit)

        raise VideoBackendFehler(
            f"Szene {prompt.index}: {self.name} nach {versuche} Versuchen "
            f"fehlgeschlagen — {letzter_fehler}")


class PixverseBackend(FalBackend):
    """PixVerse v3.5 — Standard. Günstig, schnell, solide für Broll."""

    name = "pixverse"

    @property
    def model_id(self) -> str:
        return self.cfg.pixverse_model_id

    def snap_dauer(self, dauer_s: float) -> float:
        # PixVerse akzeptiert 5s oder 8s. Alles dazwischen wird aufgerundet,
        # damit das Voiceover eher zu kurz als zu lang ist.
        return 5.0 if float(dauer_s) <= 5.0 else 8.0

    def payload(self, prompt: ScenePrompt) -> dict:
        body = {
            "prompt": prompt.prompt,
            "aspect_ratio": self.cfg.aspect_ratio,
            "resolution": self.cfg.aufloesung,
            "duration": int(self.snap_dauer(prompt.dauer_s)),
        }
        if prompt.negative_prompt:
            body["negative_prompt"] = prompt.negative_prompt
        return body


class KlingBackend(FalBackend):
    """Kling 2.5 Turbo Pro — Fallback. Bessere Bewegung, höherer Preis."""

    name = "kling"

    @property
    def model_id(self) -> str:
        return self.cfg.kling_model_id

    def snap_dauer(self, dauer_s: float) -> float:
        # Kling kennt 5s und 10s.
        return 5.0 if float(dauer_s) <= 7.5 else 10.0

    def payload(self, prompt: ScenePrompt) -> dict:
        body = {
            "prompt": prompt.prompt,
            "aspect_ratio": self.cfg.aspect_ratio,
            # Kling erwartet die Dauer als String-Enum, nicht als Zahl.
            "duration": str(int(self.snap_dauer(prompt.dauer_s))),
        }
        if prompt.negative_prompt:
            body["negative_prompt"] = prompt.negative_prompt
        return body


# ---------------------------------------------------------------------------
# Trockenlauf
# ---------------------------------------------------------------------------

class DryRunBackend(VideoBackend):
    """Erzeugt Platzhalter-Clips ohne API-Call und ohne Kosten.

    Mit ffmpeg entsteht ein echter Farbverlauf-Clip mit eingeblendetem
    Szenen-Index — damit lässt sich die komplette Kette inklusive Schnitt,
    Voiceover und Untertitel testen, bevor auch nur ein Cent fliesst.
    Ohne ffmpeg wird nur die Absicht als JSON abgelegt.
    """

    name = "dry-run"

    @property
    def model_id(self) -> str:
        return f"dry-run:{self.cfg.video_backend}"

    def payload(self, prompt: ScenePrompt) -> dict:
        return {"prompt": prompt.prompt, "dauer_s": self.snap_dauer(prompt.dauer_s)}

    def kosten_pro_clip(self, dauer_s: float) -> float:
        return 0.0

    def generate(self, prompt: ScenePrompt, zielpfad: Path) -> ClipResult:
        from . import ffmpeg_utils as ff

        dauer = float(prompt.dauer_s)
        zielpfad.parent.mkdir(parents=True, exist_ok=True)

        if ff.ffmpeg_vorhanden():
            b, h = self.cfg.breite_hoehe
            farbe = ["#1d3557", "#457b9d", "#2a9d8f", "#e76f51", "#6a4c93",
                     "#264653", "#c1121f"][(prompt.index - 1) % 7]
            ff.run(["-f", "lavfi", "-i",
                    f"color=c={farbe}:s={b}x{h}:r={self.cfg.fps}:d={dauer}",
                    "-vf", f"drawtext=text='Szene {prompt.index}':fontcolor=white:"
                           f"fontsize={max(24, h // 14)}:x=(w-text_w)/2:y=(h-text_h)/2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", f"{dauer}",
                    str(zielpfad)])
        else:
            zielpfad.with_suffix(".json").write_text(
                json.dumps(self.payload(prompt), indent=2, ensure_ascii=False),
                encoding="utf-8")
            log.warning("Szene %d: ohne ffmpeg nur Prompt-Stub geschrieben (%s)",
                        prompt.index, zielpfad.with_suffix(".json").name)

        return ClipResult(index=prompt.index, pfad=str(zielpfad), backend=self.name,
                          modell=self.model_id, dauer_s=dauer, kosten_usd=0.0)


_BACKENDS: dict[str, type[VideoBackend]] = {
    "pixverse": PixverseBackend,
    "kling": KlingBackend,
    "dry-run": DryRunBackend,
}


def get_backend(name: str, cfg: VideoConfig, **kwargs) -> VideoBackend:
    """Liefert das Backend zu ``name`` (case-insensitive)."""
    schluessel = (name or "").strip().lower()
    if schluessel not in _BACKENDS:
        raise ValueError(f"Unbekanntes Video-Backend {name!r}. Bekannt: "
                         + ", ".join(sorted(_BACKENDS)))
    klasse = _BACKENDS[schluessel]
    if issubclass(klasse, FalBackend):
        return klasse(cfg, **kwargs)
    return klasse(cfg)


def backend_namen() -> list[str]:
    return sorted(_BACKENDS)
