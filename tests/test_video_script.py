"""Skript-Generator: Antwort-Parsing und Wortbudget.

Zwei Stellen, an denen ein Sprachmodell die Pipeline zuverlässig sabotiert:
es rahmt sein JSON in Markdown ein, und es schreibt längere Voiceover-Texte
als in die Clip-Länge passen. Beides wird hier abgefangen, nicht im Schnitt.
"""
import json

import pytest

from video_generator.config import lade_config
from video_generator.models import Scene, Script
from video_generator.script_generator import (_json_aus_antwort, _wortzahl,
                                              generate_script, lade_script)


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Antwort:
    def __init__(self, text):
        self.content = [_Block(text)]


class FakeClient:
    """Gibt der Reihe nach vorbereitete Antworten zurück."""

    def __init__(self, *antworten):
        self.antworten = list(antworten)
        self.aufrufe = []
        self.messages = self

    def create(self, **kw):
        self.aufrufe.append(kw)
        # Der Prefill "{" wird vom Aufrufer wieder vorangestellt.
        return _Antwort(self.antworten.pop(0).lstrip("{"))


def _skript_json(voiceover: str, n: int = 4) -> str:
    return json.dumps({
        "titel": "T", "hook": "H", "cta": "C", "hashtags": ["a"],
        "scenes": [{"index": i, "titel": f"S{i}", "visual": "a table, side light",
                    "kamera": "slow push-in", "voiceover": voiceover}
                   for i in range(1, n + 1)]})


@pytest.fixture
def cfg():
    return lade_config(szenen_anzahl=4)


# -- Parsing ----------------------------------------------------------------

def test_json_in_markdown_fence_wird_erkannt():
    assert _json_aus_antwort('```json\n{"a": 1}\n```')["a"] == 1


def test_nachgeplapper_hinter_dem_json_stoert_nicht():
    daten = _json_aus_antwort('{"a": 1}\n\nIch hoffe, das passt so!')
    assert daten == {"a": 1}


def test_geschweifte_klammern_in_strings_verwirren_den_parser_nicht():
    daten = _json_aus_antwort('{"t": "ein } Zeichen", "b": 2}')
    assert daten["b"] == 2


def test_abgeschnittenes_json_meldet_sich():
    with pytest.raises(ValueError, match="unvollständig"):
        _json_aus_antwort('{"a": {"b": 1}')


def test_antwort_ohne_json_meldet_sich():
    with pytest.raises(ValueError, match="Keine JSON-Antwort"):
        _json_aus_antwort("Tut mir leid, das kann ich nicht.")


# -- Skript-Erzeugung -------------------------------------------------------

def test_skript_wird_vollstaendig_uebernommen(cfg):
    client = FakeClient(_skript_json("Kurzer Satz hier."))
    script = generate_script("Zinseszins", cfg, client=client)

    assert len(script.scenes) == 4
    assert script.thema == "Zinseszins"
    assert script.hook == "H"
    assert all(s.dauer_s == cfg.szenen_dauer_s for s in script.scenes)


def test_wortbudget_steht_im_prompt(cfg):
    client = FakeClient(_skript_json("Kurz."))
    generate_script("Thema", cfg, client=client)

    system = client.aufrufe[0]["system"]
    assert str(cfg.woerter_budget_pro_szene) in system


def test_zu_langes_voiceover_wird_einmal_nachgebessert(cfg):
    lang = " ".join(["Wort"] * 60)
    reparatur = json.dumps({"scenes": [{"index": i, "voiceover": "Jetzt kurz."}
                                       for i in range(1, 5)]})
    client = FakeClient(_skript_json(lang), reparatur)

    script = generate_script("Thema", cfg, client=client)

    assert len(client.aufrufe) == 2, "Reparaturrunde wurde nicht ausgelöst"
    assert all(_wortzahl(s.voiceover) <= cfg.woerter_budget_pro_szene
               for s in script.scenes)


def test_knapp_ueber_budget_loest_keine_reparatur_aus(cfg):
    """Bis 15% Überhang fängt das der Schnitt sauber ab — kein Extra-Call."""
    knapp = " ".join(["Wort"] * (cfg.woerter_budget_pro_szene + 1))
    client = FakeClient(_skript_json(knapp))

    generate_script("Thema", cfg, client=client)

    assert len(client.aufrufe) == 1


def test_gescheiterte_reparatur_bricht_den_lauf_nicht_ab(cfg, caplog):
    """Ein Komfort-Call darf das bereits erzeugte Skript nicht vernichten."""
    lang = " ".join(["Wort"] * 60)
    client = FakeClient(_skript_json(lang), "kein json hier")

    script = generate_script("Thema", cfg, client=client)

    assert len(script.scenes) == 4
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_szene_ohne_bildbeschreibung_ist_ein_fehler(cfg):
    kaputt = json.dumps({"scenes": [{"index": 1, "visual": "", "voiceover": "x"}]})
    with pytest.raises(ValueError, match="fehlt"):
        generate_script("Thema", cfg, client=FakeClient(kaputt))


def test_zu_wenige_szenen_sind_ein_fehler(cfg):
    with pytest.raises(ValueError, match="zu wenig"):
        generate_script("Thema", cfg, client=FakeClient(_skript_json("Kurz.", n=2)))


def test_antwort_ohne_szenen_ist_ein_fehler(cfg):
    with pytest.raises(ValueError, match="keine Szenen"):
        generate_script("Thema", cfg, client=FakeClient('{"titel": "T"}'))


# -- Serialisierung ---------------------------------------------------------

def test_skript_ueberlebt_den_weg_ueber_die_platte(tmp_path):
    original = Script(thema="T", hook="H", titel="Titel",
                      scenes=[Scene(1, "S", "a table", "push-in", "Text hier.")],
                      cta="C", hashtags=["a"])
    pfad = tmp_path / "s.json"
    pfad.write_text(json.dumps(original.to_dict(), ensure_ascii=False),
                    encoding="utf-8")

    geladen = lade_script(str(pfad))

    assert geladen.to_dict() == original.to_dict()
