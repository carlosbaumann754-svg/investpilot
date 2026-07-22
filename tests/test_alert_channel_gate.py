"""R-B36 (22.07.2026) — Tages-Zusammenfassung darf nicht am Telegram-Kanal haengen.

Kontext: send_daily_summary lief durch `_tg_notify_enabled`, und das verlangt
telegram.enabled=True. Telegram ist seit dem 28.04.2026 aus — die Zusammenfassung
kam damit auf KEINEM Kanal mehr an, obwohl Pushover aktiv ist und send_alert die
Kanaele selbst routet. Der R-B14-Fix am Zeitfenster (21.07.) lief deshalb ins
Leere: der zweite Blocker sass davor. Gefunden am Morgen des 22.07., als die
"erste Zusammenfassung seit April" wieder ausblieb.

WICHTIGE ABGRENZUNG (die erste Fassung dieses Fixes war zu breit und wurde von
der Suite gestoppt): Fuer Trade-Erfolgs-, Weekly- und Optimizer-Meldungen ist
die Stille bei Pushover-only GEWOLLT (Cry-Wolf-Disziplin, siehe
test_alert_regime_bypass.py / test_alert_failed_bypass.py). Nur der bewusst
bestellte Tages-Digest ist kanal-neutral.
"""
from unittest.mock import patch

from app.alerts import _tg_notify_enabled, send_daily_summary


def _cfg(tg_enabled=False, notify_daily=None):
    tg = {"enabled": tg_enabled}
    if notify_daily is not None:
        tg["notify_daily_summary"] = notify_daily
    return {"alerts": {"telegram": tg,
                       "pushover": {"enabled": True, "user_key": "u",
                                    "api_token": "t"}}}


def test_summary_geht_raus_obwohl_telegram_aus():
    """DER Live-Fall: Telegram aus, Pushover an -> Zusammenfassung + Stempel."""
    with patch("app.alerts.send_alert") as mock_send, \
         patch("app.alerts._load_alert_state", return_value={}), \
         patch("app.alerts._save_alert_state") as mock_save:
        send_daily_summary(1_000_000, 0.5, 5000, 3, "sideways",
                           config=_cfg(tg_enabled=False))
    mock_send.assert_called_once()
    stamped = mock_save.call_args[0][0]
    assert "last_daily_summary" in stamped     # Stempel gesetzt -> kein Doppelversand


def test_explizites_opt_out_gilt_weiterhin():
    """Wer den Digest bewusst abschaltet, bekommt ihn nicht — kanal-unabhaengig."""
    with patch("app.alerts.send_alert") as mock_send:
        send_daily_summary(1_000_000, 0.5, 5000, 3, "sideways",
                           config=_cfg(tg_enabled=True, notify_daily=False))
    mock_send.assert_not_called()


def test_info_noise_gate_bleibt_unangetastet():
    """Regressionsschutz fuer die GEWOLLTE Stille: Trade-/Weekly-/Optimizer-Info
    haengt weiterhin am Telegram-Gate (Pushover-only bleibt dort still)."""
    cfg = _cfg(tg_enabled=False)
    assert _tg_notify_enabled("trades", cfg) is False
    assert _tg_notify_enabled("weekly_report", cfg) is False
    assert _tg_notify_enabled("optimizer", cfg) is False
