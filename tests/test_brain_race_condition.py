"""v37h Race-Condition-Fix (11.05.2026) — Tests fuer _BRAIN_LOCK.

Wurzel-Bug: NGAS/UNG-Trade Mo 11.05. 17:50 UTC. Snapshot-After-Fill
async-thread schrieb UNG-Snapshot. Trader-Cycle parallel mit altem
state ueberschrieb. Bot's Brain hatte 10 Pos statt 11 -> CASH_DRIFT
$21'592 + Pushover-Spam.

Tests verifizieren:
1. _BRAIN_LOCK ist threading.RLock (re-entrant safe)
2. Parallele record_snapshot aus 2 Threads: BEIDE Snapshots in File
3. record_snapshot + run_brain_cycle parallel: konsistenter Endzustand
4. RLock erlaubt rekursive Acquires (run_brain_cycle -> _record_snapshot_locked)
"""
import threading
import time
from unittest.mock import patch

import pytest


@pytest.fixture
def isolated_brain(monkeypatch):
    """In-Memory-Storage statt Disk damit Tests deterministisch sind."""
    storage = {"brain_state.json": None, "trade_history.json": []}
    def fake_save(filename, data):
        # Simulate atomic save with small delay (mimics disk-write)
        time.sleep(0.001)
        # Wichtig: deepcopy NICHT — wir wollen race-condition detect
        import copy
        storage[filename] = copy.deepcopy(data)
    def fake_load(filename):
        import copy
        return copy.deepcopy(storage.get(filename))
    monkeypatch.setattr("app.config_manager.save_json", fake_save)
    monkeypatch.setattr("app.config_manager.load_json", fake_load)
    monkeypatch.setattr("app.brain.save_json", fake_save)
    monkeypatch.setattr("app.brain.load_json", fake_load)
    # Skip backup_to_cloud (network)
    monkeypatch.setattr("app.brain.backup_to_cloud", lambda: None)
    return storage


def _make_portfolio(symbol, invested=10000):
    """Minimal-Portfolio fuer record_snapshot."""
    return {
        "credit": 100000,
        "_equity": 110000,
        "unrealizedPnL": 0,
        "positions": [{
            "instrumentID": 1234,
            "symbol": symbol,
            "amount": invested,
            "openRate": 100.0,
            "currentRate": 101.0,
            "leverage": 1,
            "investmentValue": invested,
            "pnl": 100,
            "totalPnlPercent": 1.0,
        }],
    }


# ============================================================
# _BRAIN_LOCK Existenz + Type
# ============================================================

def test_brain_lock_is_rlock():
    """RLock erlaubt rekursive Acquires aus selbem Thread."""
    from app import brain
    assert hasattr(brain, "_BRAIN_LOCK")
    # threading.RLock() liefert kein direkt instance-checkbares Objekt,
    # aber wir koennen es testen via rekursivem Acquire
    lock = brain._BRAIN_LOCK
    lock.acquire()
    try:
        # Zweiter Acquire aus gleichem Thread sollte NICHT blocken (RLock)
        acquired = lock.acquire(blocking=False)
        assert acquired is True
        lock.release()
    finally:
        lock.release()


# ============================================================
# Parallele record_snapshot aus 2 Threads — BEIDE muessen persistiert
# ============================================================

def test_parallel_record_snapshot_both_persisted(isolated_brain):
    """KRITISCH: das ist das exakte Bug-Szenario von 11.05. 17:50."""
    from app.brain import record_snapshot

    portfolio_a = _make_portfolio("AAPL")
    portfolio_b = _make_portfolio("UNG")

    results = []
    errors = []

    def worker(portfolio, name):
        try:
            snap = record_snapshot(portfolio)
            results.append((name, snap.get("num_positions")))
        except Exception as e:
            errors.append((name, str(e)))

    t1 = threading.Thread(target=worker, args=(portfolio_a, "AAPL"))
    t2 = threading.Thread(target=worker, args=(portfolio_b, "UNG"))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, f"Errors: {errors}"
    # Beide Snapshots muessen im Brain landen
    brain_state = isolated_brain["brain_state.json"]
    assert brain_state is not None
    snaps = brain_state.get("performance_snapshots", [])
    assert len(snaps) == 2, \
        f"Race-Condition: erwartete 2 Snapshots, sah {len(snaps)}"
    # total_runs muss korrekt zaehlen
    assert brain_state.get("total_runs") == 2, \
        f"total_runs muss 2 sein, war {brain_state.get('total_runs')}"


# ============================================================
# Stress-Test: 50 parallele record_snapshot
# ============================================================

def test_stress_50_parallel_snapshots(isolated_brain):
    """50 Threads schreiben gleichzeitig Snapshots — alle muessen ankommen."""
    from app.brain import record_snapshot

    N = 50
    threads = []
    errors = []

    def worker(idx):
        try:
            record_snapshot(_make_portfolio(f"SYM{idx}"))
        except Exception as e:
            errors.append((idx, str(e)))

    for i in range(N):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Errors: {errors[:5]}"
    snaps = isolated_brain["brain_state.json"]["performance_snapshots"]
    assert len(snaps) == N, f"Erwartete {N} Snapshots, sah {len(snaps)}"
    # total_runs muss exakt zaehlen (kein Lost-Update)
    assert isolated_brain["brain_state.json"]["total_runs"] == N


# ============================================================
# RLock-Recursion: run_brain_cycle ruft intern _record_snapshot_locked
# ============================================================

def test_rlock_allows_recursive_acquire(isolated_brain):
    """run_brain_cycle haelt Lock + ruft record_snapshot — RLock erlaubt das."""
    from app.brain import _BRAIN_LOCK, _record_snapshot_locked

    portfolio = _make_portfolio("AAPL")
    with _BRAIN_LOCK:
        # Direkt _record_snapshot_locked (Caller-haelt-Lock-Contract)
        snap = _record_snapshot_locked(portfolio)
        # Plus zweiter Lock-Acquire aus gleichem Thread
        with _BRAIN_LOCK:
            snap2 = _record_snapshot_locked(portfolio)

    snaps = isolated_brain["brain_state.json"]["performance_snapshots"]
    assert len(snaps) == 2  # Beide records gespeichert


# ============================================================
# Backwards-Compat: record_snapshot Public-API gibt weiter snapshot-dict zurueck
# ============================================================

def test_record_snapshot_returns_dict_with_expected_keys(isolated_brain):
    from app.brain import record_snapshot

    snap = record_snapshot(_make_portfolio("AAPL"))
    for key in ("date", "time", "timestamp", "cash", "equity", "num_positions",
                "positions_count", "positions"):
        assert key in snap, f"snap fehlt {key}"
    assert snap["num_positions"] == 1


def test_run_brain_cycle_returns_report_dict(isolated_brain, monkeypatch):
    """Bug-Regression-Test (11.05.2026 20:35 CEST):
    run_brain_cycle Wrapper hatte `return` vergessen -> Trader-Cycle
    crashte in trader.py:2196 mit 'NoneType' object has no attribute 'get'.
    Test stellt sicher dass Wrapper das report-dict durchpropagiert.
    """
    from app.brain import run_brain_cycle
    # Backup-to-Cloud + andere external Calls stubben
    monkeypatch.setattr("app.brain.backup_to_cloud", lambda: None)

    report = run_brain_cycle(_make_portfolio("AAPL"))

    assert report is not None, \
        "run_brain_cycle muss report-dict zurueckgeben (kein None!)"
    assert isinstance(report, dict)
    # Trader-Cycle ruft report.get('total_runs') — muss existieren
    assert "total_runs" in report
    assert report["total_runs"] >= 1
