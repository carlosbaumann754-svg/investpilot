"""Tests fuer den Meilenstein-Wecker 25/50/80 (R-B53/58) — jeder decide()-Zweig
plus die bindende Futility-Botschaft bei 50."""
from scripts.zwischencheck_trigger import (
    FUTILITY_GRENZE_PF_PCT, decide, meldung_fuer)


class TestDecide:
    def test_unter_erster_marke_wartet(self):
        aktion, grund, ms = decide(19, {})
        assert (aktion, ms) == ("waiting", None)
        assert "19/25" in grund

    def test_25_feuert(self):
        aktion, _, ms = decide(25, {})
        assert (aktion, ms) == ("fire", 25)

    def test_legacy_marker_zaehlt_als_fired_25(self):
        # Alter 25er-State {"fired_at": ...} darf 25 nicht erneut feuern.
        aktion, grund, ms = decide(30, {"fired_at": "2026-08-26T22:15"})
        assert (aktion, ms) == ("waiting", None)
        assert "30/50" in grund

    def test_50_feuert_nach_25(self):
        aktion, _, ms = decide(50, {"fired_25": "x"})
        assert (aktion, ms) == ("fire", 50)

    def test_uebersprungene_marke_feuert_niedrigste_zuerst(self):
        # Zaehler springt 24 -> 52 (Batch-Nachtrag): erst 25 melden, nicht 50.
        aktion, _, ms = decide(52, {})
        assert (aktion, ms) == ("fire", 25)

    def test_80_feuert_und_danach_stille(self):
        aktion, _, ms = decide(83, {"fired_25": "x", "fired_50": "x"})
        assert (aktion, ms) == ("fire", 80)
        aktion, _, ms = decide(90, {"fired_25": "x", "fired_50": "x",
                                    "fired_80": "x"})
        assert (aktion, ms) == ("silent", None)

    def test_unlesbarer_zaehler_wartet(self):
        aktion, _, ms = decide(None, {"fired_25": "x"})
        assert (aktion, ms) == ("waiting", None)


class TestFutilityBotschaft:
    def test_grenze_ist_p01_bei_50(self):
        # Bindend 27.08. (R-B58, p01-Regel, damals 0.48 aus Close-Referenz);
        # korrigiert 28.08. (R-B64c): validiertes p01@50 der OHLC-Referenz.
        # Zwischenschritt 0.41 war faelschlich p05 — gleicher Tag korrigiert.
        assert FUTILITY_GRENZE_PF_PCT == 0.31

    def test_unter_grenze_stopp_signal_mit_prio_1(self):
        titel, text, prio = meldung_fuer(50, 50, {"pf_pct": 0.25, "net_usd": -20000})
        assert "FUTILITY-STOPP" in titel
        assert "STOPPEN" in text
        assert prio == 1

    def test_ueber_grenze_bestanden_prio_0(self):
        titel, _, prio = meldung_fuer(50, 50, {"pf_pct": 0.99, "net_usd": -1000})
        assert "BESTANDEN" in titel
        assert prio == 0

    def test_pf_fehlt_manuell_pruefen(self):
        titel, _, prio = meldung_fuer(50, 50, {"pf_pct": None, "net_usd": 0})
        assert "nicht berechenbar" in titel
        assert prio == 1

    def test_80_verweist_auf_go_nogo_protokoll(self):
        titel, text, prio = meldung_fuer(80, 80, {"pf_pct": 1.2, "net_usd": 5000})
        assert "GO/NO-GO" in titel
        assert "R-B57" in text
        assert prio == 1
