"""R-B68 (13.10.2026): Ausfall-Alarm + Anlauf-Limit pro Kauf.

Anlass: IBKR lehnte vom 02. bis 13.10.2026 den Gateway-Login ab. Der Bot
uebersprang 11 Tage lang jeden Zyklus, der Abgleich scheiterte alle 30 Min —
und es kam hoechstens EIN Alarm (Sa 00:00). Ausserdem wurden am 01.10. sechs
statt fuenf Kaeufe ausgefuehrt, weil das Anlauf-Limit nur zu Zyklus-Beginn galt.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app import alerts, m2a_motor
from scripts import ibkr_reconcile


@pytest.fixture
def speicher(monkeypatch):
    """In-Memory-Ersatz fuer load_json/save_json + Aufzeichnung der Alarme."""
    daten = {}
    gesendet = []

    def _load(name):
        v = daten.get(name)
        return dict(v) if isinstance(v, dict) else v

    def _save(name, wert):
        daten[name] = dict(wert)

    def _send(msg, level="INFO", config=None):
        gesendet.append((level, msg))
        return True

    monkeypatch.setattr(alerts, "load_json", _load)
    monkeypatch.setattr(alerts, "save_json", _save)
    monkeypatch.setattr(alerts, "send_alert", _send)
    monkeypatch.setattr("app.config_manager.load_json", _load)
    monkeypatch.setattr("app.config_manager.save_json", _save)
    return daten, gesendet


T0 = datetime(2026, 10, 3, 0, 0)


def _fail(n_min):
    alerts._track_broker_outage(False, "ibkr", "get_equity returned None",
                                jetzt=T0 + timedelta(minutes=n_min))


class TestBrokerAusfall:
    def test_kurzer_aussetzer_bleibt_still(self, speicher):
        """Naechtlicher Gateway-Neustart: 1-2 Fehlzyklen -> kein Alarm."""
        _, gesendet = speicher
        _fail(0)
        _fail(5)
        alerts._track_broker_outage(True, "ibkr", None, jetzt=T0 + timedelta(minutes=10))
        assert gesendet == []

    def test_alarm_ab_drittem_fehlzyklus(self, speicher):
        _, gesendet = speicher
        for m in (0, 5, 10):
            _fail(m)
        assert len(gesendet) == 1
        level, msg = gesendet[0]
        assert level == "ERROR"
        assert "Bot handelt NICHT" in msg and "3 Zyklen" in msg

    def test_wiederholung_erst_nach_6_stunden(self, speicher):
        """DER Kernfehler vom Oktober: Dauerausfall darf nicht verstummen."""
        _, gesendet = speicher
        for m in range(0, 15, 5):
            _fail(m)
        for m in range(15, 6 * 60, 5):          # bis knapp vor 6 Std nach Alarm
            _fail(m)
        assert len(gesendet) == 1
        _fail(10 + 6 * 60)                       # 6 Std nach dem ersten Alarm
        assert len(gesendet) == 2
        for m in range(10 + 6 * 60 + 5, 11 * 24 * 60, 5):   # 11 Tage
            _fail(m)
        assert len(gesendet) >= 11 * 4 - 1       # ~4 Erinnerungen pro Tag

    def test_entwarnung_mit_dauer_nur_nach_alarm(self, speicher):
        daten, gesendet = speicher
        for m in (0, 5, 10):
            _fail(m)
        alerts._track_broker_outage(True, "ibkr", None, jetzt=T0 + timedelta(hours=3))
        assert gesendet[-1][0] == "INFO"
        assert "wieder verbunden" in gesendet[-1][1] and "3.0 Std" in gesendet[-1][1]
        assert daten[alerts.BROKER_OUTAGE_STATE_FILE] == {}

    def test_zustellfehler_wird_im_naechsten_zyklus_wiederholt(self, speicher, monkeypatch):
        daten, gesendet = speicher
        monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: False)
        for m in (0, 5, 10):
            _fail(m)
        assert "last_alert_at" not in daten[alerts.BROKER_OUTAGE_STATE_FILE]
        monkeypatch.setattr(alerts, "send_alert",
                            lambda msg, level="INFO", config=None: gesendet.append((level, msg)) or True)
        _fail(15)
        assert len(gesendet) == 1


class _Client:
    broker_name = "ibkr"

    def __init__(self, equity):
        self._eq = equity

    def get_equity(self):
        return self._eq

    def force_disconnect(self):
        pass


def test_check_broker_health_nutzt_ausfall_zaehler(speicher):
    daten, _ = speicher
    assert alerts.check_broker_health(_Client(None), config={}, retry_wait_s=0) is False
    assert daten[alerts.BROKER_OUTAGE_STATE_FILE]["fails"] == 1
    assert alerts.check_broker_health(_Client(1_000_000), config={}, retry_wait_s=0) is True
    assert daten[alerts.BROKER_OUTAGE_STATE_FILE] == {}


class TestAbgleichDauerfehler:
    def _lauf(self, status="ERROR"):
        rep = {"status": status, "error": "TimeoutError()" if status == "ERROR" else None}
        return ibkr_reconcile._persist_reconcile_status(rep)

    def test_fehlserie_wird_gezaehlt_und_zurueckgesetzt(self, speicher):
        for _ in range(3):
            st = self._lauf()
        assert st["consecutive_errors"] == 3
        st = self._lauf("OK")
        assert st["consecutive_errors"] == 0

    def test_alarm_ab_vierter_fehlschlag_dann_12h_ruhe(self, speicher):
        _, gesendet = speicher
        jetzt = datetime.now(timezone.utc)
        for _ in range(3):
            ibkr_reconcile._alarm_bei_dauerfehler(self._lauf(), jetzt=jetzt)
        assert gesendet == []
        ibkr_reconcile._alarm_bei_dauerfehler(self._lauf(), jetzt=jetzt)
        assert len(gesendet) == 1 and gesendet[0][0] == "ERROR"
        assert "4 Laeufe" in gesendet[0][1]
        ibkr_reconcile._alarm_bei_dauerfehler(self._lauf(), jetzt=jetzt + timedelta(hours=1))
        assert len(gesendet) == 1
        ibkr_reconcile._alarm_bei_dauerfehler(self._lauf(), jetzt=jetzt + timedelta(hours=12, minutes=1))
        assert len(gesendet) == 2

    def test_entwarnung_nach_alarmierter_serie(self, speicher):
        _, gesendet = speicher
        for _ in range(4):
            ibkr_reconcile._alarm_bei_dauerfehler(self._lauf())
        ibkr_reconcile._alarm_bei_dauerfehler(self._lauf("OK"))
        assert gesendet[-1][0] == "INFO" and "laeuft wieder" in gesendet[-1][1]

    def test_keine_entwarnung_ohne_vorherigen_alarm(self, speicher):
        _, gesendet = speicher
        for _ in range(2):
            ibkr_reconcile._alarm_bei_dauerfehler(self._lauf())
        ibkr_reconcile._alarm_bei_dauerfehler(self._lauf("OK"))
        assert gesendet == []


class TestAnlaufLimitProKauf:
    CFG = {"m2a": {"aktiv": True, "max_neukaeufe_pro_monat": 5,
                   "schnitt_datum": "2026-08-31"}}

    def _buy(self, tag, status):
        return {"timestamp": f"2026-10-{tag:02d}T16:00:00", "action": "SCANNER_BUY",
                "symbol": "X", "status": status}

    def test_zaehlregel(self):
        assert m2a_motor.zaehlt_als_neukauf("executed")
        assert m2a_motor.zaehlt_als_neukauf("partial")
        assert m2a_motor.zaehlt_als_neukauf("submitted")
        for s in ("cancelled", "rejected", "failed", "CANCELLED"):
            assert not m2a_motor.zaehlt_als_neukauf(s)

    def test_restkontingent_der_1_oktober(self):
        """Lage nach Zyklus 1 am 01.10.: 3 gefuellt, 2 storniert -> noch 2 frei
        (vorher durfte Zyklus 2 nochmal bis zu 5 Orders legen)."""
        hist = [self._buy(1, s) for s in
                ("executed", "cancelled", "partial", "partial", "cancelled")]
        assert m2a_motor.rest_neukaeufe(self.CFG, hist, heute=date(2026, 10, 1)) == 2

    def test_rest_nie_negativ(self):
        hist = [self._buy(1, "executed") for _ in range(6)]
        assert m2a_motor.rest_neukaeufe(self.CFG, hist, heute=date(2026, 10, 1)) == 0
