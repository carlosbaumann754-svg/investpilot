"""R-B66: Tests des M2A-Motors gegen das bindende Regelwerk.

Abgedeckt: Handelstage-Zaehlung, Kauf-Fenster, Anlauf-Limit, Geerbt-Schutz
— und die Trader-Integration: Tag 125 haelt, Tag 126 verkauft
(HORIZON_CLOSE), M2a-Position ignoriert SL/Trailing selbst bei -20%,
GEERBTE Position wird weiterhin vom Alt-SL verkauft.
"""
import json
from datetime import date, datetime, timedelta
from unittest import mock

import pytest

from app import m2a_motor


class TestHandelstage:
    def test_wochenende_zaehlt_nicht(self):
        # Fr 2026-08-21 -> Mo 2026-08-24 = 1 Handelstag
        assert m2a_motor.handelstage_zwischen(
            date(2026, 8, 21), date(2026, 8, 24)) == 1

    def test_126_handelstage_sind_rund_26_wochen(self):
        start = date(2026, 1, 5)   # Montag
        ende = start
        while m2a_motor.handelstage_zwischen(start, ende) < 126:
            ende += timedelta(days=1)
        assert 170 <= (ende - start).days <= 182

    def test_handelstag_im_monat(self):
        # 2026-09-01 ist ein Dienstag -> Handelstag 1
        assert m2a_motor.handelstag_im_monat(date(2026, 9, 1)) == 1
        assert m2a_motor.handelstag_im_monat(date(2026, 9, 3)) == 3
        assert m2a_motor.handelstag_im_monat(date(2026, 9, 4)) == 4


class TestKaufFenster:
    def test_fenster_tage_1_bis_3(self):
        assert m2a_motor.im_kauf_fenster(date(2026, 9, 1)) is True
        assert m2a_motor.im_kauf_fenster(date(2026, 9, 3)) is True
        assert m2a_motor.im_kauf_fenster(date(2026, 9, 4)) is False
        assert m2a_motor.im_kauf_fenster(date(2026, 9, 15)) is False

    def test_wochenende_nie_im_fenster(self):
        assert m2a_motor.im_kauf_fenster(date(2026, 9, 6)) is False  # Sonntag

    def test_anlauf_limit_zaehlt_nur_gefuellte_buys(self):
        heute = date(2026, 9, 2)
        hist = [
            {"timestamp": "2026-09-01T16:00", "action": "SCANNER_BUY", "status": "executed"},
            {"timestamp": "2026-09-01T16:05", "action": "SCANNER_BUY", "status": "partial"},
            {"timestamp": "2026-09-01T16:10", "action": "SCANNER_BUY", "status": "cancelled"},
            {"timestamp": "2026-08-29T16:00", "action": "SCANNER_BUY", "status": "executed"},
            {"timestamp": "2026-09-01T17:00", "action": "TRAILING_SL_CLOSE", "status": "executed"},
        ]
        assert m2a_motor.neukaeufe_diesen_monat(hist, heute) == 2


class TestGeerbtSchutz:
    def test_geerbt_liste_wird_gelesen(self, tmp_path, monkeypatch):
        f = tmp_path / "m2a_geerbt.json"
        f.write_text(json.dumps({"position_ids": ["p1", 42]}))
        monkeypatch.setattr(m2a_motor, "_GEERBT_PATH", f)
        m2a_motor._geerbt_cache = {"mtime": None, "ids": frozenset()}
        assert m2a_motor.ist_geerbt("p1") is True
        assert m2a_motor.ist_geerbt(42) is True
        assert m2a_motor.ist_geerbt("neu") is False

    def test_fehlende_datei_heisst_niemand_geerbt(self, monkeypatch, tmp_path):
        monkeypatch.setattr(m2a_motor, "_GEERBT_PATH", tmp_path / "fehlt.json")
        m2a_motor._geerbt_cache = {"mtime": None, "ids": frozenset()}
        assert m2a_motor.ist_geerbt("p1") is False


class TestTraderIntegration:
    def _lauf(self, monkeypatch, tmp_path, alter_handelstage, geerbt,
              pnl_pct=-20.0):
        """Fahrt check_stop_loss_take_profit mit EINER Position."""
        from app import trader

        cfg = {"m2a": {"aktiv": True},
               "demo_trading": {"stop_loss_pct": -8, "take_profit_pct": 999},
               "time_stop": {"enabled": False}}
        open_dt = datetime.now() - timedelta(days=1)  # echter Wert egal:
        # handelstage_seit wird direkt gemockt (deterministisch).
        monkeypatch.setattr(m2a_motor, "handelstage_seit",
                            lambda *_a, **_k: alter_handelstage)
        gf = tmp_path / "m2a_geerbt.json"
        gf.write_text(json.dumps(
            {"position_ids": ["p1"] if geerbt else []}))
        monkeypatch.setattr(m2a_motor, "_GEERBT_PATH", gf)
        m2a_motor._geerbt_cache = {"mtime": None, "ids": frozenset()}

        pos = {"positionID": "p1", "instrumentID": 7, "symbol": "TST",
               "amount": 40000, "pnl": 40000 * pnl_pct / 100,
               "pnl_pct": pnl_pct, "leverage": 1,
               "openDateTime": open_dt.isoformat()}
        client = mock.Mock()
        client.get_portfolio.return_value = {"positions": [pos],
                                             "credit": 10000, "_equity": 50000}
        closes = []
        monkeypatch.setattr(
            trader, "_close_position_safe",
            lambda c, pid, iid, tag: closes.append(tag) or
            {"orderForOpen": {"orderID": "1", "statusID": "executed",
                              "filledQuantity": 1, "avgFillPrice": 1.0,
                              "intendedPrice": 1.0, "refQuote": 1.0}})
        monkeypatch.setattr(trader, "save_trade", lambda *_a, **_k: None)
        monkeypatch.setattr(trader, "_import_alerts", lambda: None)
        monkeypatch.setattr(trader, "_import_leverage_manager", lambda: None)
        monkeypatch.setattr(trader, "_find_position_open_time",
                            lambda *_a, **_k: (open_dt, 1.0))
        # Markt-Zu-Guard + Netz-Aufrufe neutralisieren (Test laeuft jederzeit)
        import app.asset_classes as _ac
        monkeypatch.setattr(_ac, "is_asset_class_tradeable",
                            lambda *_a, **_k: True)
        import app.earnings_exit as _ee
        monkeypatch.setattr(_ee, "check_earnings_exit",
                            lambda *_a, **_k: (False, None))
        trader.check_stop_loss_take_profit(client, cfg)
        return closes

    def test_tag_125_haelt_auch_bei_minus_20(self, monkeypatch, tmp_path):
        closes = self._lauf(monkeypatch, tmp_path, 125, geerbt=False,
                            pnl_pct=-20.0)
        assert closes == [], ("M2a-Position wurde vor Tag 126 verkauft — "
                              "Regelwerk-Verstoss (kein Software-SL!)")

    def test_tag_126_verkauft(self, monkeypatch, tmp_path):
        closes = self._lauf(monkeypatch, tmp_path, 126, geerbt=False,
                            pnl_pct=3.0)
        assert closes == ["HORIZON"]

    def test_geerbte_position_nutzt_alt_sl(self, monkeypatch, tmp_path):
        closes = self._lauf(monkeypatch, tmp_path, 200, geerbt=True,
                            pnl_pct=-9.0)
        assert "HORIZON" not in closes
        assert len(closes) >= 1, ("Geerbte Position bei -9% wurde nicht vom "
                                  "Alt-SL geschlossen — Bestandsschutz kaputt")


class TestWatchdogUnterM2a:
    """R-B66c: Unter M2a muss der WFO-Drift-Watchdog (inkl. Roundtrip-
    Zweitsignal) SCHWEIGEN — beide Baselines sind M0-Artefakte, Alarme
    dagegen waeren systematische Fehlalarme (Cry-Wolf). Rollback
    (aktiv=false) reaktiviert ihn automatisch."""

    def test_m2a_aktiv_pausiert_watchdog(self):
        from app.wfo_drift_watchdog import check_wfo_drift
        r = check_wfo_drift(config={"m2a": {"aktiv": True},
                                    "wfo_drift_watchdog": {"enabled": True}})
        assert r["alert_triggered"] is False
        assert r["skip_reason"] and "M2a" in r["skip_reason"]

    def test_m2a_inaktiv_laesst_watchdog_weiterlaufen(self):
        from app.wfo_drift_watchdog import check_wfo_drift
        r = check_wfo_drift(config={"m2a": {"aktiv": False},
                                    "wfo_drift_watchdog": {"enabled": True}})
        # kein M2a-Skip — was danach passiert (Daten fehlen etc.) ist egal,
        # nur der Grund darf nicht M2a sein
        assert not (r.get("skip_reason") and "M2a" in r["skip_reason"])


class TestWeeklyReportUnterM2a:
    """R-B66d: Die Wochen-Vorschlaege des Alt-Motors (SL/TP anpassen,
    min_score senken, Brain-Scores, mehr Asset-Klassen) sind unter M2a
    REGELWIDRIG — der Generator muss sie kappen und auf die Gates verweisen."""

    def test_m2a_kappt_alt_vorschlaege(self, monkeypatch):
        from app import weekly_report as wr
        monkeypatch.setattr("app.m2a_motor.ist_aktiv", lambda cfg: True)
        stats = {"sl_closes": 10, "tp_closes": 1, "total_trades": 0,
                 "scanner_trades": 0, "asset_class_breakdown": {"stocks": 20}}
        brain = {"instrument_scores": {"1": {"score": -99.0}}}
        sug = wr._generate_improvement_suggestions(brain, stats, [], [])
        bereiche = {s["bereich"] for s in sug}
        assert bereiche == {"M2a-Regelwerk"}, bereiche
        assert "Gates" in sug[0]["vorschlag"]

    def test_ohne_m2a_alles_wie_vorher(self, monkeypatch):
        from app import weekly_report as wr
        monkeypatch.setattr("app.m2a_motor.ist_aktiv", lambda cfg: False)
        stats = {"sl_closes": 10, "tp_closes": 1, "total_trades": 5,
                 "scanner_trades": 5, "asset_class_breakdown": {"stocks": 20}}
        sug = wr._generate_improvement_suggestions({}, stats, [], [])
        assert any(s["bereich"] == "Trading" for s in sug)
