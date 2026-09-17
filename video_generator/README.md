# Video-Generator

Aus einem Thema wird ein fertig geschnittenes Kurzvideo: Skript, Bildmaterial,
Voiceover, Untertitel, Schnitt — ein Befehl.

```bash
pip install -r video_generator/requirements.txt
sudo apt-get install -y ffmpeg          # ffmpeg + ffprobe werden gebraucht

export ANTHROPIC_API_KEY=...            # Stufe 1: Skript
export FAL_KEY=...                      # Stufe 3: Video

python -m video_generator "Warum Zinseszins unterschätzt wird"
```

Ergebnis liegt unter `video_output/<thema>-<zeitstempel>/video.mp4`, daneben
`video.srt` (zum Nachbearbeiten), `script.json` und `job.json`.

---

## Die sechs Stufen

| # | Modul | Was passiert | Kosten |
|---|---|---|---|
| 1 | `script_generator.py` | Thema → Hook, 6–8 Szenen mit Bild, Kamera und Voiceover (Claude) | ~1 Cent |
| 2 | `prompt_generator.py` | Szene → Video-Prompt | 0 (regelbasiert) |
| 3 | `video_backends.py` | Prompt → Clip (fal.ai: PixVerse v3.5, Fallback Kling 2.5 Turbo Pro) | **Hauptkosten** |
| 4 | `tts.py` | Voiceover-Text → Sprache + Wort-Timings (edge-tts) | 0 |
| 5 | `timing.py` + `subtitles.py` | Ton und Bild aufeinander passen, Untertitel setzen | 0 |
| 6 | `assembler.py` | Schnitt, Musik, Untertitel einbrennen (ffmpeg) | 0 |

Orchestriert von `pipeline.py`, bedient über `cli.py`.

---

## Die drei Stellen, an denen so etwas normalerweise scheitert

### 1. Ton und Bild laufen auseinander

Der Clip ist genau 8 Sekunden lang. Das Voiceover ist so lang, wie die Stimme
für den Satz braucht — das weiss man erst nach der Synthese. Wer beides naiv
aneinanderhängt, hat ab Szene 2 einen Versatz, der sich über sieben Szenen auf
mehrere Sekunden summiert.

`timing.fit_scene()` passt jede Szene einzeln, in dieser Reihenfolge:

| Fall | Mittel | Nebenwirkung |
|---|---|---|
| Ton kürzer als Bild | Stille anhängen, überschüssiges Bild wegschneiden | keine |
| Ton bis ~15 % länger | Stimme bis `max_audio_tempo` straffen | leicht hörbar |
| Ton deutlich länger | letztes Bild einfrieren | sichtbar, wird gewarnt |

Der Voiceover-Text wird **nie** gekürzt. Reicht auch das Standbild-Limit nicht,
steht eine Warnung im Lauf-Protokoll und in `job.json` — mit dem Hinweis, das
Skript zu kürzen oder `--duration` zu erhöhen.

### 2. Untertitel driften

Die Wortzeiten kommen aus den WordBoundary-Events von edge-tts, nicht aus einer
Wörter-pro-Sekunde-Schätzung. Wird die Stimme in Stufe 5 gestaucht, werden alle
Zeiten durch denselben Faktor geteilt.

Die Blöcke werden ausgewogen gefüllt statt gierig — sonst bleibt am Satzende ein
Ein-Wort-Block mit 0.4 Sekunden Standzeit übrig. Das Zeichenlimit pro Zeile wird
aus Bildbreite und Schriftgrösse **berechnet**, nicht konfiguriert: ein fester
Wert passt immer nur zu genau einem Format.

### 3. Der Lauf kostet mehr als gedacht

* `--dry-run` spielt die komplette Kette mit Platzhalter-Clips und Stille durch.
  Kostet nichts, braucht keinen `FAL_KEY` und erzeugt trotzdem ein abspielbares
  MP4 — damit lässt sich Schnitt, Timing und Untertitel-Layout prüfen, bevor
  ein Cent fliesst.
* Vor dem ersten bezahlten Call wird geschätzt. Über `max_kosten_usd` bricht die
  Pipeline ab, statt still das Dreifache auszugeben.
* Nach **jedem** bezahlten Clip wird `job.json` geschrieben. Bricht der Lauf
  später ab, setzt `--resume <verzeichnis>` genau dort wieder an.

---

## Rezepte

```bash
# Erst das Skript ansehen, bevor irgendetwas generiert wird
python -m video_generator "Thema" --until script

# Komplette Kette kostenlos durchspielen
python -m video_generator "Thema" --dry-run

# Querformat für YouTube, andere Stimme, mit Musik
python -m video_generator "Thema" --aspect 16:9 --resolution 1080p \
    --voice de-CH-LeniNeural --music musik/beat.mp3

# Eigenes Skript verwenden (Stufe 1 überspringen)
python -m video_generator "Thema" --script-file mein_skript.json

# Abgebrochenen Lauf fortsetzen — bezahlte Clips bleiben erhalten
python -m video_generator --resume video_output/thema-20260917-094500

# Verfügbare Stimmen anzeigen
python -m video_generator --list-voices de-
```

---

## Konfiguration

Defaults in `config.default.json`, überschreibbar per `--config datei.json` und
per CLI-Flag (in dieser Reihenfolge). Die wichtigsten Schrauben:

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `szenen_anzahl` / `szenen_dauer_s` | 7 / 8 | Länge des Videos |
| `woerter_pro_sekunde` | 2.4 | bestimmt das Wortbudget pro Szene |
| `aspect_ratio` / `aufloesung` | `9:16` / `720p` | Zielformat |
| `video_backend` / `video_fallback_backend` | `pixverse` / `kling` | Modellwahl |
| `pixverse_model_id` / `kling_model_id` | s. Datei | fal.ai versioniert Modelle im Pfad — Wechsel ohne Code-Änderung |
| `max_kosten_usd` | 5.00 | Budget-Bremse |
| `max_audio_tempo` | 1.15 | wie stark die Stimme gestrafft werden darf |
| `max_video_hold_s` | 2.5 | ab wann ein Standbild als Problem gilt |
| `untertitel_schriftgroesse_pct` | 4.5 | Prozent der Bildhöhe |
| `untertitel_max_zeichen` | `null` | `null` = aus der Geometrie berechnen |

Secrets kommen ausschliesslich aus der Umgebung (`ANTHROPIC_API_KEY`,
`FAL_KEY`) und landen nie in `job.json`.

---

## Bekannte Grenzen

* **Clip-Längen sind durch die Modelle vorgegeben.** PixVerse liefert 5 s oder
  8 s, Kling 5 s oder 10 s. `--duration 7` wird auf die nächste unterstützte
  Länge aufgerundet; die Feinanpassung macht Stufe 5.
* **Kling als Fallback ist teurer** und liefert bei 8-Sekunden-Szenen einen
  10-Sekunden-Clip, von dem gekürzt wird. Über `--fallback none` abschaltbar.
* **Die Preistabelle in `video_backends.py` ist ein Richtwert** (Stand Anfang
  2026) und dient nur der Budget-Warnung. Abgerechnet wird bei fal.ai.
* **Die Request-Schemata der Modelle sind nicht versionsstabil.** Ändert fal.ai
  ein Feld, ist die Anpassung auf `payload()` der jeweiligen Backend-Klasse
  begrenzt; die Modell-Kennung selbst steht schon in der Config.
* **Keine Bild-zu-Video-Kontinuität.** Jede Szene wird unabhängig generiert,
  Figuren sehen zwischen Szenen unterschiedlich aus. Für Erklärvideos mit
  wechselnden Motiven ist das egal, für Erzählungen mit einer Hauptfigur nicht.

---

## Tests

```bash
python -m pytest tests/test_video_*.py -q
```

68 Tests, ohne Netzzugriff und ohne API-Keys. Der Test der Gesamtkette
überspringt sich selbst, wenn ffmpeg fehlt.

---

## Verhältnis zum Trading-Bot

Dieses Paket ist vom übrigen Repository unabhängig: eigene Konfiguration, eigene
`requirements.txt`, keine gemeinsamen Datendateien, kein Import in beide
Richtungen. Es landet bewusst **nicht** im Docker-Image des Bots.
