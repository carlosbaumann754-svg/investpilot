"""R-B13 (21.07.2026) — Equity-Snapshot: Bestandteile + FX persistieren.

WARUM ES DIESE TESTS GIBT
-------------------------
Bei der 3-Monats-Analyse am 20.07.2026 liess sich die Ergebnis-Bruecke
(Wertaenderung = realisierte Trades + Aenderung der Buchgewinne + Rest) NICHT
bauen: `unrealized_pnl` war nirgends historisiert, `brain_state` deckt nur die
letzten 1-2 Tage ab. Der Restposten von -87k war ein reines Daten-Artefakt.
Ausserdem musste der USD/CHF-Kurs ad hoc nachgeladen werden, obwohl das Depot in
CHF, die Benchmarks aber in USD notieren.

Faellt eines dieser Felder still wieder weg, ist dieselbe Analyse in drei
Monaten erneut unmoeglich — und man merkt es erst dann. Diese Tests nageln fest,
dass sie geschrieben werden.
"""
from unittest.mock import MagicMock, patch

import pytest

from app import equity_snapshot as es

BRAIN = {"performance_snapshots": [{
    "date": "2026-07-21", "total_value": 1_080_812.93,
    "unrealized_pnl": 8087.06, "base_unrealized_pnl": 6549.61,
    "base_realized_pnl": 0.0, "cash": 694_380.70, "invested": 468_529.32,
    "num_positions": 15, "base_currency": "CHF",
}]}

CLOSES = {"SPY": 750.0, "QQQ": 700.0, "AGG": 100.0, "IWM": 200.0, "CHF=X": 0.8103}


@pytest.fixture
def snap_env():
    """Isoliert: kein echtes Datei-IO, kein Netz, kein Cloud-Backup."""
    store = {"brain_state.json": BRAIN, es.EQUITY_FILE: []}

    def fake_load(name):
        return store.get(name)

    def fake_save(name, data):
        store[name] = data

    with patch.object(es, "load_json", side_effect=fake_load), \
         patch.object(es, "save_json", side_effect=fake_save), \
         patch.object(es, "_fetch_latest_close", side_effect=lambda s: CLOSES.get(s)), \
         patch.object(es, "get_data_path", return_value=MagicMock()), \
         patch("app.persistence.backup_to_cloud", MagicMock()):
        yield store


# ============================================================
# R-B13: Bestandteile
# ============================================================

def test_snapshot_persists_components_for_bridge(snap_env):
    """DER KERN: ohne diese Felder ist die Ergebnis-Bruecke nicht baubar."""
    snap = es.take_snapshot("test")
    assert snap is not None
    assert snap["unrealized_pnl"] == 8087.06
    assert snap["base_unrealized_pnl"] == 6549.61
    assert snap["cash"] == 694_380.70
    assert snap["invested"] == 468_529.32
    assert snap["num_positions"] == 15
    assert snap["base_currency"] == "CHF"


def test_snapshot_persists_fx_rate(snap_env):
    """Depot in CHF, Benchmarks in USD -> ohne Kurs Aepfel/Birnen."""
    snap = es.take_snapshot("test")
    assert snap["usdchf_close"] == 0.8103


def test_benchmarks_still_written(snap_env):
    snap = es.take_snapshot("test")
    assert snap["spy_close"] == 750.0
    assert snap["iwm_close"] == 200.0
    assert snap["portfolio_total_value"] == 1_080_812.93


def test_bridge_is_computable_from_two_snapshots(snap_env):
    """Integrationsprobe: aus zwei Snapshots muss die Bruecke rechenbar sein.

    Delta(Wert) = realisiert + Delta(Buchgewinn) + Rest
    Hier: Wert +10'000, Buchgewinn 8'087 -> 9'000, realisiert 5'000
          -> Rest = 10'000 - 5'000 - 913 = 4'087 (Gebuehren/FX/Ein-Auszahlungen)
    Der Test prueft nicht die Zahlen des Bots, sondern dass die BENOETIGTEN
    Felder vorhanden und numerisch sind.
    """
    s1 = es.take_snapshot("test")
    snap_env["brain_state.json"]["performance_snapshots"][0].update(
        {"total_value": 1_090_812.93, "unrealized_pnl": 9000.0})
    snap_env[es.EQUITY_FILE] = []          # Idempotenz-Guard umgehen
    s2 = es.take_snapshot("test")

    d_wert = s2["portfolio_total_value"] - s1["portfolio_total_value"]
    d_buch = s2["unrealized_pnl"] - s1["unrealized_pnl"]
    realisiert = 5000.0
    rest = d_wert - realisiert - d_buch
    assert d_wert == pytest.approx(10_000.0)
    assert d_buch == pytest.approx(912.94)
    assert isinstance(rest, float)


# ============================================================
# Robustheit
# ============================================================

def test_missing_components_do_not_crash(snap_env):
    """Alt-Snapshots ohne die Felder -> Snapshot trotzdem schreiben."""
    snap_env["brain_state.json"] = {"performance_snapshots": [
        {"date": "2026-07-21", "total_value": 500_000.0}]}
    snap = es.take_snapshot("test")
    assert snap is not None
    assert snap["portfolio_total_value"] == 500_000.0
    # Nicht vorhandene Bestandteile werden als None gefuehrt, nicht erfunden
    assert snap.get("unrealized_pnl") is None


def test_fx_unavailable_is_none_not_crash(snap_env):
    with patch.object(es, "_fetch_latest_close", side_effect=lambda s: None):
        snap = es.take_snapshot("test")
    assert snap is not None
    assert snap["usdchf_close"] is None


def test_no_portfolio_value_aborts(snap_env):
    snap_env["brain_state.json"] = {"performance_snapshots": []}
    with patch.object(es, "_fetch_portfolio_components", return_value=None):
        assert es.take_snapshot("test") is None


def test_one_snapshot_per_day(snap_env):
    first = es.take_snapshot("test")
    second = es.take_snapshot("test")
    assert first is not None
    assert second is None, "zweiter Snapshot am selben Tag muss geskippt werden"


def test_history_is_capped(snap_env):
    snap_env[es.EQUITY_FILE] = [{"date": f"2020-01-{i:02d}"} for i in range(1, 29)]
    es.take_snapshot("test")
    assert len(snap_env[es.EQUITY_FILE]) <= es.MAX_HISTORY_DAYS
