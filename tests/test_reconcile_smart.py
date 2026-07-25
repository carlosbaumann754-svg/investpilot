"""Tests fuer Smart-Reconcile Accept-Phantom-Logik (v37w)."""

from __future__ import annotations

import pytest


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    """Isoliertes data/-Verzeichnis pro Test — via DATA_DIR-Attribut-Patch.

    Warum setattr statt setenv+importlib.reload: reload() liess
    config_manager.DATA_DIR nach dem Teardown auf dem tmp_path des
    VORHERIGEN Tests haengen — fixture-lose Tests lasen fremden Test-State.
    Das fruehere del sys.modules["scripts.ibkr_reconcile"] ist damit
    obsolet: das Modul importiert config_manager nur funktions-lokal
    (zur Laufzeit aufgeloest) und haelt keinen eigenen DATA_DIR-Stand.
    Details: temp_data_dir in tests/test_earnings_exit.py (Fix 25.07.2026).
    """
    from app import config_manager
    monkeypatch.setattr(config_manager, "DATA_DIR", tmp_path)
    yield tmp_path


def test_accept_phantom_persists(temp_data_dir):
    from scripts.ibkr_reconcile import _add_accepted_phantom, _load_accepted_phantoms
    _add_accepted_phantom("CPER", reason="test")
    _add_accepted_phantom("USO", reason="test")
    accepted = _load_accepted_phantoms()
    assert accepted == {"CPER", "USO"}


def test_accept_phantom_idempotent(temp_data_dir):
    """Zweifaches add ergibt nur einen Eintrag in accepted_symbols."""
    from scripts.ibkr_reconcile import _add_accepted_phantom, _load_accepted_phantoms
    _add_accepted_phantom("CPER")
    _add_accepted_phantom("CPER")
    _add_accepted_phantom("CPER")
    accepted = _load_accepted_phantoms()
    assert accepted == {"CPER"}
    # Audit-Trail haelt aber alle 3 Eintraege fest
    from app.config_manager import load_json
    data = load_json("reconcile_accepted_phantoms.json") or {}
    assert len(data.get("audit", [])) == 3


def test_load_empty_returns_empty_set(temp_data_dir):
    from scripts.ibkr_reconcile import _load_accepted_phantoms
    assert _load_accepted_phantoms() == set()


def test_audit_contains_reason(temp_data_dir):
    from scripts.ibkr_reconcile import _add_accepted_phantom
    from app.config_manager import load_json
    _add_accepted_phantom("XYZ", reason="initial-account-setup")
    data = load_json("reconcile_accepted_phantoms.json") or {}
    last_audit = data["audit"][-1]
    assert last_audit["symbol"] == "XYZ"
    assert last_audit["reason"] == "initial-account-setup"
    assert "accepted_at" in last_audit
