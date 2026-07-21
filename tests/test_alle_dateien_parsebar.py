"""Jede Python-Datei muss parsebar sein — und web.app importierbar (R-B29).

WOHER DIESER TEST KOMMT (21.07.2026)
=====================================
Ein Kommentarblock wurde mit 4 statt 8 Leerzeichen in ``api_soak_progress``
eingefuegt und brach den umschliessenden try-Block:

    SyntaxError: expected 'except' or 'finally' block

**Die Suite lief mit 1366 gruenen Tests durch.** Kein einziger Test importiert
web.app — die Datei mit dem Dashboard, dem Not-Aus-Endpunkt und der gesamten
Bedienoberflaeche war also nicht durch Tests gedeckt, nicht einmal auf der Ebene
"laesst sich ueberhaupt lesen".

Aufgefallen ist es erst an ``docker ps``: "Restarting (1)" — der Container im
Crash-Loop, auf dem Produktivsystem, 40 Minuten vor Boersenoeffnung.

Die Luecke ist billig zu schliessen und faengt eine ganze Fehlerklasse ab:
Syntaxfehler, kaputte Einrueckung, Tippfehler in Modulnamen — alles, was ein
Deployment zum Absturz bringt, bevor die erste Zeile Logik laeuft.
"""
import ast
import pathlib

import pytest

_WURZEL = pathlib.Path(__file__).resolve().parents[1]

# Ordner, die nicht zum ausgelieferten Code gehoeren.
_IGNORIEREN = {".git", "__pycache__", ".pytest_cache", "venv", ".venv",
               "node_modules", "_vps_backup"}


def _python_dateien():
    for p in _WURZEL.rglob("*.py"):
        if any(teil in _IGNORIEREN or teil.startswith("_vps_backup")
               for teil in p.parts):
            continue
        yield p


@pytest.mark.parametrize("pfad", sorted(_python_dateien(), key=str),
                         ids=lambda p: str(p.relative_to(_WURZEL)))
def test_datei_ist_parsebar(pfad):
    """Reines Parsen — kein Import, also ohne Seiteneffekte und ohne Netzwerk."""
    quelle = pfad.read_text(encoding="utf-8")
    try:
        ast.parse(quelle, filename=str(pfad))
    except SyntaxError as e:
        pytest.fail(f"{pfad.relative_to(_WURZEL)}:{e.lineno} — {e.msg}\n"
                    f"    {(e.text or '').rstrip()}")


def _fehlende_container_abhaengigkeit() -> str | None:
    """Prueft Pakete, die nur im Container installiert sind (z.B. pyotp fuer 2FA).

    Der Import-Test ist der schaerfere von beiden, laeuft aber nur dort, wo die
    Laufzeit-Abhaengigkeiten vorhanden sind. Auf dem Entwicklungsrechner fehlen
    sie — dann greift der Parse-Test oben, der die hier ausloesende Fehlerklasse
    (Syntax/Einrueckung) ohnehin abdeckt.
    """
    import importlib.util
    for paket in ("pyotp", "fastapi", "ib_insync"):
        if importlib.util.find_spec(paket) is None:
            return paket
    return None


_FEHLT = _fehlende_container_abhaengigkeit()
_nur_mit_deps = pytest.mark.skipif(
    _FEHLT is not None,
    reason=f"Laufzeit-Abhaengigkeit '{_FEHLT}' fehlt (nur im Container vorhanden)",
)


@_nur_mit_deps
def test_web_app_ist_importierbar():
    """Der eigentliche Ausloeser: web.app war ungetestet.

    Geht ueber das Parsen hinaus — faengt auch fehlende Importe und Fehler auf
    Modul-Ebene ab. Genau das, was uvicorn beim Start tut; schlaegt es hier fehl,
    startet der Container nicht.
    """
    import importlib
    modul = importlib.import_module("web.app")
    assert hasattr(modul, "app"), "FastAPI-Instanz 'app' fehlt"


@_nur_mit_deps
def test_scheduler_ist_importierbar():
    """Zweiter Prozess im Container — derselbe Absturz waere hier moeglich,
    mit dem Unterschied, dass dann der HANDEL steht statt nur die Anzeige."""
    import importlib
    importlib.import_module("app.scheduler")
