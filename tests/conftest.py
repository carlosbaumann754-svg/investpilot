"""Global pytest configuration für InvestPilot Tests.

CRITICAL — v37e Tag 6 Lessons-Learned (07.05.2026):
==================================================
Beim Test-Run von test_order_status_tracker.py wurden REAL Pushover-Alerts
auf das Live-Handy gesendet, weil:

  1. _mark_stale() ruft send_pushover() direkt auf (ab Tag 6)
  2. Existing Tests (Order-IDs 555, 777, 888) triggerten _mark_stale via
     recover_from_ibkr ohne explizites monkeypatch
  3. Lokale config.json hatte Live-Pushover-Credentials → API-Call ging durch

Resultat: Carlos bekam 3 STALE_ORDER-Pushover die KEINE echten Anomalien
waren, sondern Test-Garbage. Direkter Verstoß gegen "Stille = OK"-Doktrin
und Cry-Wolf-Effect-Risiko.

Fix: Autouse-Fixture die send_pushover global stub'd. Tests können das
explizit überschreiben wenn sie das Verhalten testen wollen.
"""
import os
import pytest
from unittest.mock import MagicMock


@pytest.fixture(autouse=True)
def _block_real_pushover(monkeypatch):
    """Verhindert ECHTE Pushover-API-Calls in jeglichen Tests.

    Wirkt automatisch auf JEDEN Test (autouse=True). Tests die das Verhalten
    von send_pushover spezifisch testen wollen, können eigenes
    monkeypatch.setattr("app.alerts.send_pushover", ...) verwenden — pytest's
    monkeypatch ist stack-based, der lokale Patch überschreibt diesen hier.

    Defense-in-Depth: Auch wenn ein Entwickler vergisst zu mock'en, fliesst
    KEIN Test-Pushover auf User-Handys. Die "Stille = OK"-Watchdog-Disziplin
    bleibt geschützt.

    Returns:
        Der MagicMock — Tests können assert_called(), call_args etc. nutzen
        falls sie via Fixture-Injection darauf zugreifen wollen
        (z.B. `def test_x(_block_real_pushover): ...`).
    """
    mock = MagicMock(return_value=False, name="send_pushover (test-blocked)")
    monkeypatch.setattr("app.alerts.send_pushover", mock)
    yield mock


@pytest.fixture(autouse=True)
def _enforce_no_pushover_credentials_in_test_env(monkeypatch):
    """Defense-in-Depth Layer 2: Falls send_pushover doch irgendwie aufgerufen
    wird (z.B. via dynamischem Import, anderer Code-Pfad), blocken wir
    zusätzlich die Credentials. Der echte send_pushover-Code returnt dann
    False ohne API-Call.

    Setzt PUSHOVER_USER_KEY und PUSHOVER_API_TOKEN auf empty strings, falls
    sie im Test-Env gesetzt sein sollten.
    """
    monkeypatch.setenv("PUSHOVER_USER_KEY", "")
    monkeypatch.setenv("PUSHOVER_API_TOKEN", "")
    yield
