"""Untertitel: aus Wort-Timings werden lesbare Blöcke mit absoluter Zeit.

Zwei Dinge unterscheiden diese Umsetzung von "Skripttext mit geschätztem
Timing":

1. **Echte Zeitstempel.** Die Wortzeiten kommen aus den WordBoundary-Events
   der Sprachsynthese (siehe ``tts.py``), nicht aus einer Wörter-pro-Sekunde-
   Annahme. Untertitel und Stimme laufen dadurch auch am Videoende synchron.
2. **Zeitbasis-Korrektur.** Wird das Voiceover im Schnitt beschleunigt
   (``SceneFit.audio_tempo``), verschieben sich alle Wortzeiten mit. Die Cues
   werden deshalb durch denselben Faktor geteilt und um den Szenenstart plus
   Vorlauf verschoben. Ohne diese Korrektur wandern die Untertitel genau in
   den Szenen aus dem Takt, in denen der Ton ohnehin schon eng ist.

Ausgabe wahlweise als SRT (universell) oder ASS (gestaltbar — Schriftgrösse,
Outline, Position; das ist das Format, das ffmpeg einbrennt).
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import VideoConfig
from .models import SceneFit, SubtitleCue, VoiceResult, WordTiming

_SATZENDE = re.compile(r"[.!?…]$")

# Seitlicher Rand der Untertitel, als Anteil der Bildbreite je Seite.
_RAND_ANTEIL = 0.07
# Mittlere Glyphenbreite von DejaVu Sans Bold, als Anteil der Schriftgrösse.
# Empirisch konservativ gewählt: lieber eine Zeile zu früh umbrechen als eine
# Zeile seitlich aus dem Bild laufen lassen.
_GLYPHENBREITE = 0.60


def schriftgroesse_px(cfg: VideoConfig) -> int:
    _, hoehe = cfg.breite_hoehe
    return max(12, int(round(hoehe * cfg.untertitel_schriftgroesse_pct / 100)))


def max_zeichen_pro_zeile(cfg: VideoConfig) -> int:
    """Wieviele Zeichen in eine Untertitelzeile passen.

    Wird ``untertitel_max_zeichen`` nicht explizit gesetzt, ergibt sich der
    Wert aus Bildbreite, Randabstand und Schriftgrösse. Das ist der einzige
    Weg, bei dem dieselbe Config in 9:16 und 16:9 funktioniert — ein fester
    Wert passt immer nur zu genau einem Format.
    """
    if cfg.untertitel_max_zeichen:
        return int(cfg.untertitel_max_zeichen)
    breite, _ = cfg.breite_hoehe
    nutzbar = breite * (1 - 2 * _RAND_ANTEIL)
    return max(12, int(nutzbar / (schriftgroesse_px(cfg) * _GLYPHENBREITE)))


def _wrap_zeilen(woerter: list[str], max_zeichen: int) -> list[str]:
    """Bricht Wörter zeilenweise um — ohne Begrenzung der Zeilenzahl.

    Ein einzelnes Wort, das länger ist als ``max_zeichen``, bekommt eine eigene
    (zu lange) Zeile. Das ist Absicht: Wörter werden nicht getrennt, und der
    ASS-Renderer bricht so etwas notfalls selbst um.
    """
    zeilen: list[str] = []
    aktuell = ""
    for w in woerter:
        kandidat = f"{aktuell} {w}".strip()
        if aktuell and len(kandidat) > max_zeichen:
            zeilen.append(aktuell)
            aktuell = w
        else:
            aktuell = kandidat
    if aktuell:
        zeilen.append(aktuell)
    return zeilen


def _passt(woerter: list[str], max_zeichen: int, max_zeilen: int) -> bool:
    """Passt diese Wortfolge in die erlaubte Zahl Zeilen?"""
    return len(_wrap_zeilen(woerter, max_zeichen)) <= max_zeilen


def _zeilen_umbrechen(woerter: list[str], max_zeichen: int,
                      max_zeilen: int) -> str:
    """Bricht Wörter auf höchstens ``max_zeilen`` Zeilen um."""
    zeilen = _wrap_zeilen(woerter, max_zeichen)
    if len(zeilen) > max_zeilen:
        # Sollte durch die Blockbildung nicht vorkommen; wenn doch, lieber eine
        # zu lange letzte Zeile (die ASS umbricht) als abgeschnittener Text.
        zeilen = zeilen[:max_zeilen - 1] + [" ".join(zeilen[max_zeilen - 1:])]
    return "\n".join(zeilen)


def _saetze(woerter: list[WordTiming]) -> list[list[WordTiming]]:
    """Zerlegt die Wortliste an Satzgrenzen."""
    out: list[list[WordTiming]] = []
    aktuell: list[WordTiming] = []
    for w in woerter:
        if not (w.text or "").strip():
            continue
        aktuell.append(w)
        if _SATZENDE.search(w.text.strip()):
            out.append(aktuell)
            aktuell = []
    if aktuell:
        out.append(aktuell)
    return out


def _teile_ausgewogen(satz: list[WordTiming], max_zeichen: int, max_zeilen: int,
                      max_dauer_s: float, tempo: float) -> list[list[WordTiming]]:
    """Zerlegt einen Satz in möglichst gleich grosse Untertitel-Blöcke.

    Warum nicht einfach gierig auffüllen bis das Limit erreicht ist: dabei
    entstehen Reste. "Die meisten Menschen rechnen" + "linear." — der zweite
    Block steht dann 0.4 Sekunden im Bild und ist nicht lesbar. Wird die Zahl
    der Blöcke vorher bestimmt und der Satz gleichmässig darauf verteilt,
    verschwindet diese Fehlerklasse ganz.
    """
    if not satz:
        return []

    woerter = [w.text.strip() for w in satz]
    gesamt_zeichen = len(" ".join(woerter))
    gesamt_dauer = (satz[-1].end_s - satz[0].start_s) / max(0.01, tempo)

    def fuellen(anzahl: int) -> list[list[WordTiming]]:
        ziel_zeichen = gesamt_zeichen / max(1, anzahl)
        bloecke: list[list[WordTiming]] = []
        aktuell: list[WordTiming] = []
        aktuell_text: list[str] = []
        for i, (w, wort) in enumerate(zip(satz, woerter)):
            rest_bloecke = anzahl - len(bloecke)
            rest_woerter = len(satz) - i
            laenge = len(" ".join(aktuell_text))

            voll = aktuell and not _passt(aktuell_text + [wort], max_zeichen, max_zeilen)
            zu_lang = (aktuell and
                       (w.end_s - aktuell[0].start_s) / max(0.01, tempo) > max_dauer_s)
            # Ausgewogen aufteilen — aber nur, solange noch genug Wörter für
            # die verbleibenden Blöcke übrig sind (sonst entstehen Reste).
            ziel_erreicht = (aktuell and rest_bloecke > 1
                             and laenge >= ziel_zeichen * 0.9
                             and rest_woerter >= rest_bloecke)
            if voll or zu_lang or ziel_erreicht:
                bloecke.append(aktuell)
                aktuell, aktuell_text = [], []

            aktuell.append(w)
            aktuell_text.append(wort)
        if aktuell:
            bloecke.append(aktuell)
        return bloecke

    # Startschätzung aus Zeichen- und Dauerbudget. Sie ist nur eine Untergrenze:
    # ob eine Wortfolge wirklich in die Zeilen passt, weiss erst der echte
    # Umbruch (Wortgrenzen verschenken Platz). Ergibt die Füllung mehr Blöcke
    # als geplant, wird mit der echten Zahl neu verteilt — sonst bleibt der
    # Rest als Ein-Wort-Block mit 0.4 Sekunden Standzeit übrig.
    anzahl = max(1,
                 -(-gesamt_zeichen // max(1, max_zeichen * max_zeilen)),
                 -(-int(gesamt_dauer * 100) // max(1, int(max_dauer_s * 100))))
    bloecke = fuellen(anzahl)
    for _ in range(6):
        if len(bloecke) <= anzahl:
            break
        anzahl = len(bloecke)
        bloecke = fuellen(anzahl)
    return bloecke


def cues_fuer_szene(voice: VoiceResult, fit: SceneFit, start_s: float,
                    cfg: VideoConfig, text_fallback: str = "") -> list[SubtitleCue]:
    """Baut die Untertitel-Blöcke einer Szene in absoluter Videozeit."""
    tempo = max(0.01, fit.audio_tempo)
    offset = start_s + fit.audio_lead_s
    max_zeichen = max_zeichen_pro_zeile(cfg)

    if not voice.woerter:
        # Keine Wort-Timings (z.B. Stimme ohne Boundary-Events): ein Block über
        # die gesamte Sprechdauer. Besser als gar kein Untertitel.
        text = (text_fallback or "").strip()
        if not text:
            return []
        return [SubtitleCue(start_s=offset,
                            end_s=offset + fit.voice_dauer_s,
                            text=_zeilen_umbrechen(text.split(), max_zeichen,
                                                   cfg.untertitel_max_zeilen))]

    cues: list[SubtitleCue] = []
    for satz in _saetze(voice.woerter):
        for block in _teile_ausgewogen(satz, max_zeichen,
                                       cfg.untertitel_max_zeilen,
                                       cfg.untertitel_max_dauer_s, tempo):
            cues.append(SubtitleCue(
                start_s=offset + block[0].start_s / tempo,
                end_s=offset + block[-1].end_s / tempo,
                text=_zeilen_umbrechen([w.text.strip() for w in block],
                                       max_zeichen, cfg.untertitel_max_zeilen)))

    return _mindestdauer(cues, cfg, grenze=start_s + fit.scene_dauer_s)


def _mindestdauer(cues: list[SubtitleCue], cfg: VideoConfig,
                  grenze: float) -> list[SubtitleCue]:
    """Zieht zu kurze Blöcke auf Mindestdauer — ohne den nächsten zu überlappen."""
    for i, c in enumerate(cues):
        wunsch = c.start_s + cfg.untertitel_min_dauer_s
        naechster = cues[i + 1].start_s if i + 1 < len(cues) else grenze
        c.end_s = min(max(c.end_s, wunsch), max(naechster, c.start_s + 0.2))
    return cues


def alle_cues(voices: dict[int, VoiceResult], fits: list[SceneFit],
              starts: list[float], cfg: VideoConfig,
              texte: dict[int, str] | None = None) -> list[SubtitleCue]:
    """Untertitel des gesamten Videos, in Reihenfolge."""
    texte = texte or {}
    out: list[SubtitleCue] = []
    for fit, start in zip(fits, starts):
        voice = voices.get(fit.index)
        if voice is None or fit.voice_dauer_s <= 0:
            continue
        out.extend(cues_fuer_szene(voice, fit, start, cfg,
                                   text_fallback=texte.get(fit.index, "")))
    return out


# ---------------------------------------------------------------------------
# Serialisierung
# ---------------------------------------------------------------------------

def _srt_zeit(s: float) -> str:
    s = max(0.0, s)
    h, rest = divmod(int(s), 3600)
    m, sek = divmod(rest, 60)
    ms = int(round((s - int(s)) * 1000))
    if ms == 1000:                                  # Rundung auf volle Sekunde
        sek, ms = sek + 1, 0
    return f"{h:02d}:{m:02d}:{sek:02d},{ms:03d}"


def _ass_zeit(s: float) -> str:
    s = max(0.0, s)
    h, rest = divmod(int(s), 3600)
    m, sek = divmod(rest, 60)
    cs = int(round((s - int(s)) * 100))
    if cs == 100:
        sek, cs = sek + 1, 0
    return f"{h:d}:{m:02d}:{sek:02d}.{cs:02d}"


def schreibe_srt(cues: list[SubtitleCue], pfad: Path) -> Path:
    bloecke = []
    for i, c in enumerate(cues, start=1):
        bloecke.append(f"{i}\n{_srt_zeit(c.start_s)} --> {_srt_zeit(c.end_s)}\n"
                       f"{c.text}\n")
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text("\n".join(bloecke), encoding="utf-8")
    return pfad


def schreibe_ass(cues: list[SubtitleCue], pfad: Path, cfg: VideoConfig) -> Path:
    """Schreibt gestaltete Untertitel (weiss, schwarze Kontur, unten zentriert).

    PlayResX/Y werden auf die echten Videomasse gesetzt, Schriftgrösse und
    Randabstand aus den Prozentwerten der Config berechnet. Dadurch sieht
    dieselbe Config bei 540p und 1080p identisch aus.
    """
    breite, hoehe = cfg.breite_hoehe
    schrift = schriftgroesse_px(cfg)
    rand = max(10, int(round(hoehe * cfg.untertitel_rand_unten_pct / 100)))
    kontur = max(2, int(round(schrift * 0.09)))

    kopf = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {breite}
PlayResY: {hoehe}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,{schrift},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{kontur},1,2,{int(breite * _RAND_ANTEIL)},{int(breite * _RAND_ANTEIL)},{rand},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    zeilen = [
        f"Dialogue: 0,{_ass_zeit(c.start_s)},{_ass_zeit(c.end_s)},Default,,0,0,0,,"
        + c.text.replace("\n", r"\N")
        for c in cues
    ]
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(kopf + "\n".join(zeilen) + "\n", encoding="utf-8")
    return pfad
