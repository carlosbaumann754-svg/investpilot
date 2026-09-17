"""Kommandozeile der Video-Pipeline.

Beispiele::

    # Trockenlauf: Skript, Prompts, Platzhalter-Clips, Stille — kostet nichts
    python -m video_generator "Warum Zinseszins unterschätzt wird" --dry-run

    # Echtes Video im Hochformat für Reels
    python -m video_generator "Warum Zinseszins unterschätzt wird" \\
        --aspect 9:16 --scenes 7 --duration 8 --voice de-CH-LeniNeural

    # Nur das Skript ansehen, bevor Geld fliesst
    python -m video_generator "Thema" --until script

    # Abgebrochenen Lauf fortsetzen (bereits erzeugte Clips bleiben erhalten)
    python -m video_generator --resume video_output/thema-20260917-094500
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import lade_config
from .pipeline import SCHRITTE, BudgetUeberschritten, Pipeline, JobState
from .video_backends import backend_namen


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="video_generator",
        description="Aus einem Thema ein fertig geschnittenes Kurzvideo bauen.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)

    p.add_argument("thema", nargs="?", help="Thema des Videos (Freitext)")

    g = p.add_argument_group("Format")
    g.add_argument("--scenes", type=int, dest="szenen_anzahl", help="Anzahl Szenen (Standard 7)")
    g.add_argument("--duration", type=float, dest="szenen_dauer_s",
                   help="Sekunden pro Szene (Standard 8)")
    g.add_argument("--aspect", dest="aspect_ratio", choices=["9:16", "16:9", "1:1"],
                   help="Seitenverhältnis (Standard 9:16)")
    g.add_argument("--resolution", dest="aufloesung", choices=["540p", "720p", "1080p"],
                   help="Auflösung (Standard 720p)")
    g.add_argument("--language", dest="sprache", help="Sprache des Voiceovers (Standard de)")

    g = p.add_argument_group("Inhalt")
    g.add_argument("--script-file", help="Fertiges Skript (JSON) statt Stufe 1")
    g.add_argument("--extra", default="", help="Zusatzanweisung an den Skript-Generator")
    g.add_argument("--prompt-mode", dest="prompt_modus", choices=["rule_based", "llm"],
                   help="Prompt-Erzeugung (Standard rule_based, kostenlos)")
    g.add_argument("--script-model", dest="script_modell", help="Claude-Modell für Stufe 1")

    g = p.add_argument_group("Video")
    g.add_argument("--backend", dest="video_backend", choices=backend_namen(),
                   help="Video-Modell (Standard pixverse)")
    g.add_argument("--fallback", dest="video_fallback_backend",
                   choices=backend_namen() + ["none"],
                   help="Ersatz-Modell wenn das erste scheitert (Standard kling)")
    g.add_argument("--parallel", type=int, dest="video_parallel",
                   help="Gleichzeitige Video-Calls (Standard 3)")

    g = p.add_argument_group("Ton & Untertitel")
    g.add_argument("--voice", dest="tts_stimme", help="edge-tts-Stimme")
    g.add_argument("--music", dest="musik_pfad", help="Hintergrundmusik (Audiodatei)")
    g.add_argument("--music-db", type=float, dest="musik_lautstaerke_db",
                   help="Musiklautstärke in dB, negativ (Standard -22)")
    g.add_argument("--no-subtitles", action="store_true", help="Keine Untertitel einbrennen")
    g.add_argument("--list-voices", nargs="?", const="de-", metavar="PRAEFIX",
                   help="Verfügbare Stimmen auflisten und beenden")

    g = p.add_argument_group("Ablauf & Kosten")
    g.add_argument("--dry-run", action="store_true",
                   help="Ohne API-Calls und ohne Kosten durchspielen")
    g.add_argument("--max-cost", type=float, dest="max_kosten_usd",
                   help="Kostenlimit in USD (Standard 5.00)")
    g.add_argument("--yes", action="store_true", help="Kostenlimit übergehen")
    g.add_argument("--until", default="assemble", choices=list(SCHRITTE),
                   help="Nach diesem Schritt anhalten (Standard assemble)")
    g.add_argument("--resume", metavar="VERZEICHNIS", help="Lauf fortsetzen")
    g.add_argument("--config", help="Zusätzliche Config-Datei (JSON)")
    g.add_argument("--output-dir", dest="output_dir", help="Ausgabeverzeichnis")
    g.add_argument("-v", "--verbose", action="store_true", help="Debug-Ausgaben")
    return p


def _overrides(args: argparse.Namespace) -> dict:
    felder = ("szenen_anzahl", "szenen_dauer_s", "aspect_ratio", "aufloesung",
              "sprache", "prompt_modus", "script_modell", "video_backend",
              "video_parallel", "tts_stimme", "musik_pfad", "musik_lautstaerke_db",
              "max_kosten_usd", "output_dir")
    out = {f: getattr(args, f) for f in felder if getattr(args, f, None) is not None}
    if args.no_subtitles:
        out["untertitel_aktiv"] = False
    if args.video_fallback_backend:
        out["video_fallback_backend"] = (None if args.video_fallback_backend == "none"
                                         else args.video_fallback_backend)
    return out


def _zusammenfassung(state: JobState) -> str:
    zeilen = ["", "─" * 60, f"Lauf:       {state.verzeichnis}"]
    if state.script:
        scenes = state.script.get("scenes", [])
        zeilen.append(f"Skript:     {len(scenes)} Szenen — "
                      f"{state.script.get('titel') or state.thema}")
    if state.clips:
        zeilen.append(f"Clips:      {len(state.clips)}")
    if state.voices:
        zeilen.append(f"Voiceover:  {len(state.voices)}")
    if state.fits:
        gesamt = sum(f.get("scene_dauer_s", 0) for f in state.fits)
        zeilen.append(f"Länge:      {gesamt:.1f}s")
    zeilen.append(f"Kosten:     {state.kosten_usd:.2f} USD"
                  + ("  (Trockenlauf)" if state.dry_run else ""))
    if state.ergebnis:
        zeilen.append(f"Video:      {state.ergebnis}")
    if state.warnungen:
        zeilen.append("Hinweise:")
        zeilen += [f"  - {w}" for w in state.warnungen]
    zeilen.append("─" * 60)
    return "\n".join(zeilen)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    if args.list_voices:
        from .tts import stimmen_auflisten
        for name in stimmen_auflisten(args.list_voices):
            print(name)
        return 0

    if not args.thema and not args.resume:
        _parser().print_usage(sys.stderr)
        print("\nFehler: entweder ein Thema oder --resume angeben.", file=sys.stderr)
        return 2

    cfg = lade_config(args.config, **_overrides(args))
    probleme = cfg.validate()
    if probleme:
        print("Konfiguration ist nicht nutzbar:", file=sys.stderr)
        for p in probleme:
            print(f"  - {p}", file=sys.stderr)
        return 2

    pipeline = Pipeline(cfg, dry_run=args.dry_run, kosten_bestaetigt=args.yes)

    try:
        if args.resume:
            state = pipeline.resume(Path(args.resume), bis=args.until)
        else:
            state = pipeline.run(args.thema, script_datei=args.script_file,
                                 zusatz=args.extra, bis=args.until)
    except BudgetUeberschritten as e:
        print(f"\nAbgebrochen (Budget): {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nAbgebrochen. Mit --resume <verzeichnis> weitermachen.", file=sys.stderr)
        return 130
    except Exception as e:
        logging.getLogger(__name__).error("Lauf abgebrochen: %s", e, exc_info=args.verbose)
        return 1

    print(_zusammenfassung(state))
    return 0
