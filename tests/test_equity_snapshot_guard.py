"""R-B36 (22.07.2026) — manueller Snapshot darf den Tagesend-Snapshot nicht fressen.

Live-Fall 21.07.: Ein manueller Dashboard-Snapshot um 12:08 CEST schrieb den
Tages-Guard und legte einen History-Eintrag an. Der geplante Abend-Lauf fand
beides vor und uebersprang — in der Monatstabelle stand ein Mittagswert statt
des Tagesend-Werts.

Drei Schichten desselben Fehlers, alle drei hier fixiert:
  1. take_snapshot brach bei JEDEM heutigen Eintrag ab (auch manuellen)
  2. der Guard wurde von JEDEM Trigger geschrieben (auch manuellen)
  3. Eintraege wurden appended statt ge-upserted (Duplikat-Risiko)
"""
from unittest.mock import patch

import pytest

from app import equity_snapshot as es


@pytest.fixture()
def welt(tmp_path, monkeypatch):
    """Kleine Welt: History im Speicher, Guard im tmp_path, Netz/Broker gemockt."""
    zustand = {"history": []}

    monkeypatch.setattr(es, "_load_history", lambda: list(zustand["history"]))
    monkeypatch.setattr(es, "_save_history",
                        lambda h: zustand.__setitem__("history", list(h)))
    monkeypatch.setattr(es, "_fetch_portfolio_components",
                        lambda: {"portfolio_total_value": 1_000_000.0,
                                 **{k: 0.0 for k in es._COMPONENT_KEYS}})
    monkeypatch.setattr(es, "_fetch_latest_close", lambda sym: 100.0)
    guard = tmp_path / "equity_snapshot_last.flag"
    monkeypatch.setattr(es, "get_data_path", lambda name: guard)
    # Cloud-Backup im Test stilllegen (wird in take_snapshot lazy importiert)
    import app.persistence as persistence
    monkeypatch.setattr(persistence, "backup_to_cloud", lambda *a, **k: None)
    return zustand, guard


def test_manueller_snapshot_schreibt_keinen_guard(welt):
    zustand, guard = welt
    snap = es.take_snapshot(triggered_by="manual-dashboard")
    assert snap is not None
    assert len(zustand["history"]) == 1
    assert not guard.exists()          # Tages-Slot NICHT verbraucht


def test_abendlauf_ersetzt_mittagswert(welt):
    """Der Kern des Live-Falls: Scheduler-Lauf nach manuellem Klick muss laufen
    und den Mittagswert ERSETZEN (ein Eintrag, Quelle Scheduler, Guard gesetzt)."""
    zustand, guard = welt
    es.take_snapshot(triggered_by="manual-dashboard")
    snap = es.take_snapshot(triggered_by="scheduler-daily-2230")
    assert snap is not None
    assert len(zustand["history"]) == 1                    # ersetzt, nicht dupliziert
    assert zustand["history"][0]["source"] == "scheduler-daily-2230"
    assert guard.read_text().strip() == snap["date"]       # jetzt ist der Tag zu


def test_scheduler_eintrag_beendet_den_tag(welt):
    """Nach dem Scheduler-Lauf: weitere Laeufe (egal ob manuell oder geplant)
    skippen — der Tagesend-Wert ist kanonisch und bleibt stehen."""
    zustand, _ = welt
    es.take_snapshot(triggered_by="scheduler-daily-2230")
    assert es.take_snapshot(triggered_by="scheduler-daily-2230") is None
    assert es.take_snapshot(triggered_by="manual-dashboard") is None
    assert len(zustand["history"]) == 1
