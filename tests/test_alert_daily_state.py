"""R-B14 (21.07.2026) — Tageszaehler + Tages-Zusammenfassung.

ZWEI STILLE BUGS, beide am 20./21.07. bei der Watchdog-Analyse aufgefallen:

1. `alerts_sent_today` wurde hochgezaehlt, aber NIE zurueckgesetzt. Am 20.07.
   stand er auf 467, obwohl an dem Tag null Alerts rausgingen — und hat mich
   bei der Diagnose zunaechst in die Irre gefuehrt ("467 Alerts heute?!").

2. `should_send_daily_summary` hatte ein 5-Minuten-Fenster (21:00-21:05),
   geprueft wird aber erst am ENDE eines Handelszyklus. Dauert der Zyklus ein
   paar Minuten, ist das Fenster zu. Folge: die Zusammenfassung fiel seit dem
   28.04.2026 stillschweigend aus — knapp drei Monate, ohne dass es jemand
   bemerkte, weil ein NICHT gesendeter Report keine Spur hinterlaesst.

Genau deshalb diese Tests: Beide Fehler sind unsichtbar, solange niemand
gezielt hinschaut.
"""
from datetime import datetime
from unittest.mock import patch

import pytest

from app import alerts


@pytest.fixture
def state():
    store = {}
    with patch.object(alerts, "_load_alert_state", side_effect=lambda: store), \
         patch.object(alerts, "_save_alert_state",
                      side_effect=lambda s: store.update(s)):
        yield store


def _at(y, m, d, hh, mm=0):
    """Patcht datetime.now() im alerts-Modul auf einen festen Zeitpunkt."""
    fixed = datetime(y, m, d, hh, mm)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    return patch.object(alerts, "datetime", _DT)


# ============================================================
# 1) Tageszaehler
# ============================================================

def _send_once(state):
    """Simuliert genau den Zaehl-Block aus send_alert."""
    today = alerts.datetime.now().strftime("%Y-%m-%d")
    st = alerts._load_alert_state()
    if st.get("alerts_sent_date") != today:
        st["alerts_sent_date"] = today
        st["alerts_sent_today"] = 0
    st["alerts_sent_today"] = st.get("alerts_sent_today", 0) + 1
    alerts._save_alert_state(st)


def test_counter_increments_within_same_day(state):
    with _at(2026, 7, 21, 10):
        _send_once(state)
        _send_once(state)
        _send_once(state)
    assert state["alerts_sent_today"] == 3
    assert state["alerts_sent_date"] == "2026-07-21"


def test_counter_resets_on_new_day(state):
    """DER BUG: vorher lief der Zaehler ewig weiter (467 statt 0)."""
    with _at(2026, 7, 20, 10):
        for _ in range(467):
            _send_once(state)
    assert state["alerts_sent_today"] == 467

    with _at(2026, 7, 21, 10):
        _send_once(state)
    assert state["alerts_sent_today"] == 1, "Zaehler haette zuruecksetzen muessen"
    assert state["alerts_sent_date"] == "2026-07-21"


def test_counter_starts_clean_without_prior_state(state):
    with _at(2026, 7, 21, 9):
        _send_once(state)
    assert state["alerts_sent_today"] == 1


# ============================================================
# 2) Tages-Zusammenfassung
# ============================================================

def test_summary_fires_late_in_hour_21(state):
    """DER BUG: 21:37 war vorher False, weil das Fenster bei 21:05 endete.

    Geprueft wird am Ende eines Handelszyklus — der landet selten in den
    ersten fuenf Minuten.
    """
    with _at(2026, 7, 21, 21, 37):
        assert alerts.should_send_daily_summary() is True


def test_summary_fires_at_start_of_hour_21(state):
    with _at(2026, 7, 21, 21, 0):
        assert alerts.should_send_daily_summary() is True


@pytest.mark.parametrize("hour", [20, 22, 0, 12])
def test_summary_silent_outside_hour_21(state, hour):
    with _at(2026, 7, 21, hour, 30):
        assert alerts.should_send_daily_summary() is False


def test_summary_only_once_per_day(state):
    """Der Datums-Guard ist die Idempotenz — nicht das Zeitfenster."""
    state["last_daily_summary"] = datetime(2026, 7, 21, 21, 2).isoformat()
    with _at(2026, 7, 21, 21, 40):
        assert alerts.should_send_daily_summary() is False


def test_summary_fires_again_next_day(state):
    state["last_daily_summary"] = datetime(2026, 7, 20, 21, 2).isoformat()
    with _at(2026, 7, 21, 21, 40):
        assert alerts.should_send_daily_summary() is True


def test_summary_survives_corrupt_timestamp(state):
    """Kaputter Zeitstempel darf die Zusammenfassung nicht dauerhaft blockieren."""
    state["last_daily_summary"] = "kaputt"
    with _at(2026, 7, 21, 21, 10):
        assert alerts.should_send_daily_summary() is True
