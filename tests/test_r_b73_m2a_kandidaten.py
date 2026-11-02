"""R-B73 (02.11.2026): M2a geht die Rangliste weiter herunter und ignoriert
die M0-Filter "Quartalszahlen" und "Nachrichtenstimmung".

Anlass Kauf-Fenster 02.11.: nur 1 von 5 moeglichen Neukaeufen. Die Kauf-
Schleife schnitt die Kandidaten VOR den Filtern auf die Top 5 — INSP, ARI,
KMT (Earnings-Blackout) und ETD (negatives Sentiment) fielen durch, Rang 6+
wurde nie geprueft. Das validierte M2a-Modell (signal_stack_backtester) kennt
weder Earnings- noch Sentiment-Filter. Carlos-Entscheid 02.11.2026 ("Wir
machen B" = Ersatz-Kandidaten + beide Filter fuer M2a aus).
"""
from pathlib import Path

from app import trader

C = [{"symbol": s, "score": sc} for s, sc in
     (("INSP", 48), ("ARI", 46), ("KMT", 45), ("EZPW", 44), ("ETD", 43),
      ("G6", 42), ("G7", 41))]


def test_m2a_bekommt_ersatz_kandidaten():
    plan, liste = trader._kauf_kandidaten(C, n_plan=5, m2a=True)
    assert [c["symbol"] for c in plan] == ["INSP", "ARI", "KMT", "EZPW", "ETD"]
    assert [c["symbol"] for c in liste] == [c["symbol"] for c in C]


def test_alt_motor_unveraendert_top_n():
    plan, liste = trader._kauf_kandidaten(C, n_plan=5, m2a=False)
    assert liste == plan and len(liste) == 5


def test_plan_bleibt_fuer_budget_und_gewichte():
    """Das Budget rechnet weiter mit den geplanten N — sonst wuerden die
    Positionen bei 40 Kandidaten auf einen Bruchteil schrumpfen."""
    plan, _ = trader._kauf_kandidaten(C, n_plan=3, m2a=True)
    assert len(plan) == 3


def _schleife():
    src = (Path(__file__).parent.parent / "app" / "trader.py").read_text(encoding="utf-8")
    start = src.find("top_buys, _kauf_liste = _kauf_kandidaten(")
    assert start != -1, "Kauf-Schleife nutzt _kauf_kandidaten nicht"
    return src[start:src.find('log.info(f"  SCANNER BUY: {symbol}', start)]


def test_schleife_iteriert_ersatzliste_und_stoppt_nach_n_kaeufen():
    s = _schleife()
    assert "for candidate in _kauf_liste:" in s
    assert "_m2a_gebucht >= len(top_buys)" in s


def test_m0_filter_fuer_m2a_aus():
    s = _schleife()
    blackout = s[s.find("# Earnings Blackout Check"):s.find("# Sentiment Filter")]
    sentiment = s[s.find("# Sentiment Filter"):s.find("# Earnings Surprise Score")]
    surprise = s[s.find("# Earnings Surprise Score"):]
    for teil in (blackout, sentiment, surprise[:400]):
        assert "not _m2a_kauf" in teil
