"""R-B25 (21.07.2026) — Score-Historie + Ranking-Auswertung.

Kontext: Der Shadow-Runner bewertet taeglich ~309 Aktien und ueberschrieb die
Datei bei jedem Lauf; in keinem der 32 Backups war sie enthalten. Damit wurde
die schnellste Antwort auf "funktioniert das Signal" jeden Tag geloescht,
waehrend die Validierung auf ~0.6 Round-Trips pro Tag wartete (~8 Monate bis zu
einer belastbaren Aussage).

Diese Tests fixieren zwei Dinge:
  1. Die Statistik ist korrekt — eine falsche Rangkorrelation waere schlimmer
     als gar keine Messung, weil sie Vertrauen erzeugt.
  2. Die Auswertung sagt "noch zu frueh", solange sie zu frueh ist. Genau daran
     ist das Motor-Edge-Signal am 20.07. gescheitert (41.5 % Fehlalarme durch
     eine geratene Mindestmenge).
"""
from unittest.mock import patch

import pytest

from app.signal_ic_tracker import (
    _period_pairs,
    _quintile_spread,
    bewertung,
    compute_ic,
    spearman,
)
from app.signal_score_history import append_snapshot, build_snapshot


# --------------------------------------------------------------- Statistik ---

def test_spearman_perfekte_rangfolge():
    """Gleiche Rangfolge -> +1, auch wenn der Zusammenhang nicht linear ist."""
    xs = [1, 2, 3, 4, 5]
    ys = [10, 20, 30, 40, 5000]  # monoton, aber stark nichtlinear
    assert spearman(xs, ys) == pytest.approx(1.0)


def test_spearman_inverse_rangfolge():
    assert spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_gleichstand_wird_gemittelt():
    """Bei Gleichstand Durchschnittsrang — sonst haengt das Ergebnis an der
    zufaelligen Eingabereihenfolge."""
    a = spearman([1, 1, 2, 3], [1, 2, 3, 4])
    b = spearman([1, 1, 2, 3], [2, 1, 3, 4])  # nur die zwei Gleichstaende getauscht
    assert a == pytest.approx(b)


def test_spearman_konstante_eingabe_ist_none():
    """Ohne Streuung ist keine Korrelation definiert — None statt 0.0, damit die
    Periode verworfen und nicht als 'kein Zusammenhang' gezaehlt wird."""
    assert spearman([5, 5, 5, 5], [1, 2, 3, 4]) is None


def test_spearman_zu_wenig_punkte():
    assert spearman([1, 2], [1, 2]) is None


def test_quintils_spanne():
    """10 Namen -> Fuenftel = 2. Bestes Paar gegen schlechtestes Paar."""
    scores = list(range(10))
    rets = [x / 100 for x in range(10)]  # 0 % .. 9 %, gleiche Rangfolge
    q = _quintile_spread(scores, rets)
    assert q["n_pro_quintil"] == 2
    assert q["top_quintil_ret_pct"] == pytest.approx(8.5)    # (8+9)/2
    assert q["bottom_quintil_ret_pct"] == pytest.approx(0.5)  # (0+1)/2
    assert q["spread_pct"] == pytest.approx(8.0)


def test_quintils_spanne_zu_wenig_namen():
    assert _quintile_spread([1, 2, 3], [0.1, 0.2, 0.3]) is None


# ------------------------------------------------------ Perioden-Bildung ---

def test_perioden_nicht_ueberlappend_ist_voreinstellung():
    """Der entscheidende Punkt: nicht-ueberlappende Perioden sind unabhaengig.
    Ueberlappende teilen fast den ganzen Kursverlauf und taeuschen Sicherheit vor.
    """
    tage = [f"2026-01-{d:02d}" for d in range(1, 11)]  # 10 Schnappschuesse
    ohne = _period_pairs(tage, horizon=3, overlap=False)
    mit = _period_pairs(tage, horizon=3, overlap=True)

    assert ohne == [("2026-01-01", "2026-01-04"),
                    ("2026-01-04", "2026-01-07"),
                    ("2026-01-07", "2026-01-10")]
    assert len(mit) == 7  # jeder Starttag -> viel mehr, aber abhaengige Messpunkte


def test_perioden_zu_kurze_historie():
    assert _period_pairs(["2026-01-01", "2026-01-02"], horizon=5, overlap=False) == []


# ------------------------------------------------------------- compute_ic ---

def _hist(tage_scores_preise):
    """{datum: {symbol: [score, preis]}} aus kompakter Testangabe."""
    return {d: {s: [sc, pr] for s, (sc, pr) in eintraege.items()}
            for d, eintraege in tage_scores_preise.items()}


def test_compute_ic_perfektes_signal():
    """Hoher Score -> hohe Folgerendite. IC muss +1 sein."""
    n = 40
    start = {f"S{i}": (float(i), 100.0) for i in range(n)}
    # Kurs steigt proportional zum Score -> gleiche Rangfolge
    ende = {f"S{i}": (0.0, 100.0 * (1 + i / 100.0)) for i in range(n)}
    hist = _hist({"2026-01-01": start, "2026-01-02": ende})

    erg = compute_ic(hist, horizon=1)
    assert erg["n_perioden"] == 1
    assert erg["perioden"][0]["rang_ic"] == pytest.approx(1.0)
    assert erg["perioden"][0]["n_symbole"] == n
    assert erg["perioden"][0]["quintile"]["spread_pct"] > 0


def test_compute_ic_inverses_signal():
    """Das Szenario, das wir wirklich finden wollen: Signal zeigt falsch herum."""
    n = 40
    start = {f"S{i}": (float(i), 100.0) for i in range(n)}
    ende = {f"S{i}": (0.0, 100.0 * (1 + (n - i) / 100.0)) for i in range(n)}
    hist = _hist({"2026-01-01": start, "2026-01-02": ende})

    erg = compute_ic(hist, horizon=1)
    assert erg["perioden"][0]["rang_ic"] == pytest.approx(-1.0)
    assert erg["mittlerer_ic"] < 0


def test_compute_ic_ueberspringt_symbole_ohne_folgekurs():
    """Ein Symbol, das spaeter fehlt (Delisting, kein Kurs), darf die Periode
    nicht kippen — es wird ausgelassen, der Rest wird gemessen."""
    n = 40
    start = {f"S{i}": (float(i), 100.0) for i in range(n)}
    ende = {f"S{i}": (0.0, 100.0 * (1 + i / 100.0)) for i in range(n) if i != 7}
    hist = _hist({"2026-01-01": start, "2026-01-02": ende})

    erg = compute_ic(hist, horizon=1)
    assert erg["perioden"][0]["n_symbole"] == n - 1


def test_compute_ic_verwirft_zu_kleine_perioden():
    """Unter der Mindestmenge wird die Periode verworfen statt mitgezaehlt.
    Bei n=30 liegt die Streuung des IC schon bei ~0.19 — eine kleine Periode
    saehe auch bei reinem Zufall oft nach starkem Signal aus."""
    start = {f"S{i}": (float(i), 100.0) for i in range(10)}
    ende = {f"S{i}": (0.0, 101.0) for i in range(10)}
    hist = _hist({"2026-01-01": start, "2026-01-02": ende})

    erg = compute_ic(hist, horizon=1)
    assert erg["n_perioden"] == 0
    assert erg["mittlerer_ic"] is None


def test_compute_ic_leere_historie():
    erg = compute_ic({}, horizon=21)
    assert erg["n_perioden"] == 0
    assert erg["t_wert"] is None


# -------------------------------------------------------------- bewertung ---

def test_bewertung_zu_wenig_perioden_ist_unklar():
    """Der wichtigste Test des Moduls: bei duenner Datenlage KEIN Urteil.
    Zwei Perioden mit hohem IC duerfen nicht als 'Signal gefunden' durchgehen."""
    b = bewertung({"n_perioden": 2, "mittlerer_ic": 0.35, "t_wert": 9.9})
    assert b["status"] == "unklar"
    assert b["farbe"] == "grau"


def test_bewertung_signal_erkannt():
    b = bewertung({"n_perioden": 12, "mittlerer_ic": 0.04, "t_wert": 2.5})
    assert b["status"] == "signal"
    assert b["farbe"] == "gruen"


def test_bewertung_inverses_signal():
    b = bewertung({"n_perioden": 12, "mittlerer_ic": -0.05, "t_wert": -3.1})
    assert b["status"] == "invers"
    assert b["farbe"] == "rot"


def test_bewertung_kein_nachweis():
    """Genug Daten, aber kein Zusammenhang — das ist ein Ergebnis, kein Fehler."""
    b = bewertung({"n_perioden": 12, "mittlerer_ic": 0.008, "t_wert": 0.4})
    assert b["status"] == "kein_nachweis"
    assert b["farbe"] == "gelb"


def test_bewertung_hoher_ic_ohne_t_wert_ist_kein_signal():
    """Ohne t-Wert (nur eine Periode) darf auch ein IC von 0.5 kein Gruen geben."""
    b = bewertung({"n_perioden": 8, "mittlerer_ic": 0.5, "t_wert": None})
    assert b["status"] == "unklar"


# --------------------------------------------------------- Score-Historie ---

def test_build_snapshot_preis_als_tupel():
    """Der Preis-Provider liefert (kurs_jetzt, kurs_vor_21d) — nur der erste zaehlt."""
    snap = build_snapshot({"AAA": {"score": 53.264}}, {"AAA": (12.3456, 11.0)})
    assert snap == {"AAA": [53.26, 12.3456]}


def test_build_snapshot_preis_als_float():
    snap = build_snapshot({"AAA": {"score": 50.0}}, {"AAA": 10.0})
    assert snap == {"AAA": [50.0, 10.0]}


def test_build_snapshot_ohne_kurs_wird_ausgelassen():
    """Ohne Kurs keine spaetere Folgerendite -> der Eintrag waere nur Ballast."""
    snap = build_snapshot({"AAA": {"score": 50.0}, "BBB": {"score": 60.0}},
                          {"AAA": (10.0, 9.0)})
    assert list(snap) == ["AAA"]


def test_build_snapshot_kurs_null_wird_ausgelassen():
    """Kurs 0 wuerde bei der Renditerechnung durch null teilen."""
    snap = build_snapshot({"AAA": {"score": 50.0}}, {"AAA": (0.0, 9.0)})
    assert snap == {}


def test_append_snapshot_ist_idempotent():
    """Ein zweiter Lauf am selben Tag ersetzt, statt zu duplizieren — der
    Shadow-Scan muss nach einem Fehlschlag von Hand nachziehbar sein."""
    gespeichert = {}

    def fake_load(name):
        return gespeichert.get(name)

    def fake_save(name, payload):
        gespeichert[name] = payload

    with patch("app.config_manager.load_json", side_effect=fake_load), \
         patch("app.config_manager.save_json", side_effect=fake_save):
        append_snapshot("2026-07-21", {"AAA": {"score": 50.0}}, {"AAA": (10.0, 9.0)})
        r = append_snapshot("2026-07-21", {"AAA": {"score": 55.0}}, {"AAA": (11.0, 9.0)})

    snaps = gespeichert["signal_score_history.json"]["snapshots"]
    assert list(snaps) == ["2026-07-21"]
    assert snaps["2026-07-21"]["AAA"] == [55.0, 11.0]  # der neuere Wert gewinnt
    assert r["replaced"] is True


def test_append_snapshot_haelt_aufbewahrungsgrenze():
    gespeichert = {}

    with patch("app.config_manager.load_json", side_effect=lambda n: gespeichert.get(n)), \
         patch("app.config_manager.save_json",
               side_effect=lambda n, p: gespeichert.__setitem__(n, p)):
        for tag in range(1, 8):
            append_snapshot(f"2026-07-{tag:02d}", {"AAA": {"score": 50.0}},
                            {"AAA": (10.0, 9.0)}, max_snapshots=3)

    snaps = gespeichert["signal_score_history.json"]["snapshots"]
    assert list(snaps) == ["2026-07-05", "2026-07-06", "2026-07-07"]  # aelteste raus


def test_append_snapshot_leer_wird_nicht_gespeichert():
    """Ein Lauf ohne verwertbare Kurse darf keinen leeren Tag ins Archiv legen —
    sonst entsteht eine Luecke, die spaeter wie ein Handelstag aussieht."""
    gespeichert = {}
    with patch("app.config_manager.load_json", side_effect=lambda n: gespeichert.get(n)), \
         patch("app.config_manager.save_json",
               side_effect=lambda n, p: gespeichert.__setitem__(n, p)):
        r = append_snapshot("2026-07-21", {"AAA": {"score": 50.0}}, {})

    assert r["ok"] is False
    assert gespeichert == {}
