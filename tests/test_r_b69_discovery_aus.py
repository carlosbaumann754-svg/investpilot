"""R-B69 (13.10.2026): Woechentliche Asset-Discovery unter dem Signal-Stack aus.

Anlass (Sentry): 48x "search_instrument failed" (Fr 09.10., Discovery lief mitten
im IBKR-Login-Ausfall, je Suchbegriff ein Fehler) + "$MLO: No data found, symbol
may be delisted" (jeden Freitag 19:02 seit Mai — Discovery bewertet Muell-Symbole).

Der Signal-Stack kauft ausschliesslich aus dem festen sp600-Universum; Discovery
fuettert nur das alte TA-Universum. Am 16.07.2026 wurde deshalb die Dashboard-
Karte ausgeblendet ("Discovery obsolet") — der automatische Freitagslauf lief
aber weiter. R-B69 schaltet den Lauf selbst ab und stellt den Selbsttest um.
"""
from unittest.mock import patch

import pytest

from app import asset_discovery, scheduler, self_test


@pytest.mark.parametrize("cfg, erwartet", [
    ({"use_signal_stack": True}, False),
    ({"use_signal_stack": False}, True),
    ({}, True),                      # Default wie market_scanner: TA-Pfad
])
def test_automatik_folgt_dem_signal_stack(cfg, erwartet):
    assert asset_discovery.automatik_aktiv(cfg) is erwartet


def test_automatik_aus_wenn_config_unlesbar():
    """Fail-safe: Discovery erzeugt nur Rauschen -> im Zweifel nicht laufen."""
    with patch.object(asset_discovery, "load_config", side_effect=RuntimeError("kaputt")):
        assert asset_discovery.automatik_aktiv() is False


@pytest.fixture
def freitag_17utc(monkeypatch, tmp_path):
    from app import config_manager
    monkeypatch.setattr(config_manager, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "_check_github_token_validity", lambda now: None)
    monkeypatch.setattr(scheduler, "is_friday_discovery_time", lambda: True)
    scheduler._BG_MAINT_LAST_RUN.clear()
    # nur den Discovery-Block pruefen: alle anderen Bloecke als "gerade gelaufen"
    import time as _t
    jetzt = _t.time()
    for k in ("watchdogs", "heartbeat", "wfo_drift", "universe_health"):
        scheduler._BG_MAINT_LAST_RUN[k] = jetzt
    yield
    scheduler._BG_MAINT_LAST_RUN.clear()


def test_freitagslauf_startet_nicht_unter_signal_stack(freitag_17utc, monkeypatch):
    monkeypatch.setattr(asset_discovery, "automatik_aktiv", lambda config=None: False)
    with patch.object(scheduler.threading, "Thread") as thread:
        scheduler._run_background_maintenance()
    gestartet = [c for c in thread.call_args_list
                 if c.kwargs.get("target") is scheduler._run_live_discovery_safely]
    assert gestartet == []


def test_freitagslauf_startet_im_ta_modus(freitag_17utc, monkeypatch):
    monkeypatch.setattr(asset_discovery, "automatik_aktiv", lambda config=None: True)
    with patch.object(scheduler.threading, "Thread") as thread:
        scheduler._run_background_maintenance()
    gestartet = [c for c in thread.call_args_list
                 if c.kwargs.get("target") is scheduler._run_live_discovery_safely]
    assert len(gestartet) == 1


def test_selbsttest_meldet_pausiert_statt_veraltet(monkeypatch, tmp_path):
    """Ohne Anpassung wuerde der Selbsttest ab 14 Tagen ohne Lauf WARNEN."""
    (tmp_path / "discovery_status.json").write_text(
        '{"finished_at": "2026-07-01T19:00:00"}', encoding="utf-8")
    monkeypatch.setattr(self_test, "_get_data_path", lambda name: tmp_path / name)
    monkeypatch.setattr(asset_discovery, "automatik_aktiv", lambda config=None: False)
    r = self_test.tc_discovery_freshness()
    assert r.passed is True
    assert r.severity == "info"
    assert "pausiert" in r.detail.lower()


def test_selbsttest_prueft_weiter_im_ta_modus(monkeypatch, tmp_path):
    (tmp_path / "discovery_status.json").write_text(
        '{"finished_at": "2026-07-01T19:00:00"}', encoding="utf-8")
    monkeypatch.setattr(self_test, "_get_data_path", lambda name: tmp_path / name)
    monkeypatch.setattr(asset_discovery, "automatik_aktiv", lambda config=None: True)
    r = self_test.tc_discovery_freshness()
    assert r.passed is False and "> 14d" in r.detail
