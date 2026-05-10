"""v37h Pre-Cutover-Task 2+3 (10.05.2026) — Tests fuer
_run_background_maintenance() im scheduler.

Hintergrund: Die 5 GH-Action-Watchdogs (optimizer/backtest/ml_training/
discovery/wfo) liefen frueher NUR im market-hours-Block. Folge: Sonntag-
Optimizer-Pushes (05:00 UTC) wurden vom Bot 7 Tage nicht abgeholt
(market zu am Wochenende). Plus: Brain-Snapshots blieben stundenlang
stale wenn keine Trades passierten -> Reconcile-Drift.

Fix: Hintergrund-Maintenance laeuft IMMER (auch ausserhalb market-hours),
mit Throttle gegen API-Rate-Limits (30 Min Watchdogs, 60 Min Heartbeat).
"""
from unittest.mock import MagicMock, patch
import pytest

# Import original token-check BEFORE any fixture patches it (autouse stub
# unten ersetzt scheduler._check_github_token_validity per default).
from app.scheduler import _check_github_token_validity as _ORIGINAL_TOKEN_CHECK


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    """Reset _BG_MAINT_LAST_RUN zwischen Tests + stub Token-Check (sonst HTTP)."""
    from app import scheduler
    scheduler._BG_MAINT_LAST_RUN.clear()
    scheduler._BG_TOKEN_FAIL_COUNT.update({"count": 0, "alerted_at": 0.0})
    # Stub Token-Check damit Tests keine echten GH-API-Calls machen
    monkeypatch.setattr(scheduler, "_check_github_token_validity",
                        lambda now: None)
    yield
    scheduler._BG_MAINT_LAST_RUN.clear()


# ============================================================
# Watchdog-Throttling: alle 30 Min
# ============================================================

def test_first_call_runs_all_5_watchdogs():
    """Erster Aufruf nach Bot-Start: alle 5 Watchdogs werden gerufen."""
    from app import scheduler
    with patch("app.scheduler.is_market_hours", return_value=True), \
         patch("app.persistence.check_and_reload_optimizer_output", return_value=False) as opt, \
         patch("app.persistence.check_and_reload_backtest_output", return_value=False) as bt, \
         patch("app.persistence.check_and_reload_ml_training_output", return_value=False) as ml, \
         patch("app.persistence.check_and_reload_discovery_output", return_value=False) as disc, \
         patch("app.persistence.check_and_reload_wfo_output", return_value=False) as wfo:
        scheduler._run_background_maintenance()
    opt.assert_called_once()
    bt.assert_called_once()
    ml.assert_called_once()
    disc.assert_called_once()
    wfo.assert_called_once()


def test_second_call_within_throttle_skips_watchdogs():
    """Zweiter Aufruf binnen 30 Min skippt Watchdog-Block."""
    from app import scheduler
    import time as _t

    # Erster Run
    with patch("app.scheduler.is_market_hours", return_value=True), \
         patch("app.persistence.check_and_reload_optimizer_output", return_value=False), \
         patch("app.persistence.check_and_reload_backtest_output", return_value=False), \
         patch("app.persistence.check_and_reload_ml_training_output", return_value=False), \
         patch("app.persistence.check_and_reload_discovery_output", return_value=False), \
         patch("app.persistence.check_and_reload_wfo_output", return_value=False):
        scheduler._run_background_maintenance()

    # Zweiter Run sofort danach (nur 1s spaeter) — Throttle muss greifen
    with patch("app.scheduler.is_market_hours", return_value=True), \
         patch("app.persistence.check_and_reload_optimizer_output") as opt2, \
         patch("app.persistence.check_and_reload_backtest_output") as bt2, \
         patch("app.persistence.check_and_reload_ml_training_output") as ml2, \
         patch("app.persistence.check_and_reload_discovery_output") as disc2, \
         patch("app.persistence.check_and_reload_wfo_output") as wfo2:
        scheduler._run_background_maintenance()

    opt2.assert_not_called()
    bt2.assert_not_called()
    ml2.assert_not_called()
    disc2.assert_not_called()
    wfo2.assert_not_called()


def test_call_after_throttle_window_runs_again():
    """Nach 30+ Min wieder alle Watchdogs aufrufen."""
    from app import scheduler
    import time as _t

    # Erster Run
    with patch("app.scheduler.is_market_hours", return_value=True), \
         patch("app.persistence.check_and_reload_optimizer_output", return_value=False), \
         patch("app.persistence.check_and_reload_backtest_output", return_value=False), \
         patch("app.persistence.check_and_reload_ml_training_output", return_value=False), \
         patch("app.persistence.check_and_reload_discovery_output", return_value=False), \
         patch("app.persistence.check_and_reload_wfo_output", return_value=False):
        scheduler._run_background_maintenance()

    # 31 Min "in der Zukunft" — manuell den last-run zurueckdrehen
    scheduler._BG_MAINT_LAST_RUN["watchdogs"] = _t.time() - 31 * 60

    with patch("app.scheduler.is_market_hours", return_value=True), \
         patch("app.persistence.check_and_reload_optimizer_output", return_value=False) as opt, \
         patch("app.persistence.check_and_reload_backtest_output", return_value=False), \
         patch("app.persistence.check_and_reload_ml_training_output", return_value=False), \
         patch("app.persistence.check_and_reload_discovery_output", return_value=False), \
         patch("app.persistence.check_and_reload_wfo_output", return_value=False):
        scheduler._run_background_maintenance()

    opt.assert_called_once()  # zweiter echter Run


# ============================================================
# Wurzel-Bug-Fix: Watchdogs laufen auch wenn Markt zu
# ============================================================

def test_watchdogs_run_even_when_market_closed():
    """KRITISCH: das war der Wurzel-Bug. Sonntag = market closed = Cycle
    skippte Watchdogs. Pre-Cutover-Fix: Watchdogs laufen UNABHAENGIG."""
    from app import scheduler
    with patch("app.scheduler.is_market_hours", return_value=False), \
         patch("app.persistence.check_and_reload_optimizer_output", return_value=True) as opt, \
         patch("app.persistence.check_and_reload_backtest_output", return_value=False), \
         patch("app.persistence.check_and_reload_ml_training_output", return_value=False), \
         patch("app.persistence.check_and_reload_discovery_output", return_value=False), \
         patch("app.persistence.check_and_reload_wfo_output", return_value=False):
        scheduler._run_background_maintenance()

    opt.assert_called_once()  # Sonntag aber Watchdog liefert!


# ============================================================
# Heartbeat-Snapshot: nur wenn Markt zu, alle 60 Min
# ============================================================

def test_heartbeat_snapshot_when_market_closed():
    """Heartbeat-Snapshot triggert wenn Markt zu (Trader-Cycle pausiert)."""
    from app import scheduler
    fake_portfolio = {"credit": 100000, "positions": [], "_equity": 100000}
    fake_broker = MagicMock()
    fake_broker.get_portfolio.return_value = fake_portfolio

    with patch("app.scheduler.is_market_hours", return_value=False), \
         patch("app.broker_base.get_broker", return_value=fake_broker), \
         patch("app.brain.record_snapshot", return_value={"cash": 100000, "num_positions": 0}) as rec, \
         patch("app.persistence.check_and_reload_optimizer_output", return_value=False), \
         patch("app.persistence.check_and_reload_backtest_output", return_value=False), \
         patch("app.persistence.check_and_reload_ml_training_output", return_value=False), \
         patch("app.persistence.check_and_reload_discovery_output", return_value=False), \
         patch("app.persistence.check_and_reload_wfo_output", return_value=False):
        scheduler._run_background_maintenance()

    rec.assert_called_once_with(fake_portfolio)


def test_no_heartbeat_when_market_open():
    """Markt offen -> Trader-Cycle macht eigene Snapshots, kein Heartbeat noetig."""
    from app import scheduler
    with patch("app.scheduler.is_market_hours", return_value=True), \
         patch("app.broker_base.get_broker") as br, \
         patch("app.brain.record_snapshot") as rec, \
         patch("app.persistence.check_and_reload_optimizer_output", return_value=False), \
         patch("app.persistence.check_and_reload_backtest_output", return_value=False), \
         patch("app.persistence.check_and_reload_ml_training_output", return_value=False), \
         patch("app.persistence.check_and_reload_discovery_output", return_value=False), \
         patch("app.persistence.check_and_reload_wfo_output", return_value=False):
        scheduler._run_background_maintenance()

    br.assert_not_called()
    rec.assert_not_called()


def test_heartbeat_throttled_to_60min():
    """Heartbeat zwei Aufrufe binnen 60 Min: nur einmal Snapshot."""
    from app import scheduler
    import time as _t
    fake_portfolio = {"credit": 1, "positions": []}
    fake_broker = MagicMock()
    fake_broker.get_portfolio.return_value = fake_portfolio

    with patch("app.scheduler.is_market_hours", return_value=False), \
         patch("app.broker_base.get_broker", return_value=fake_broker), \
         patch("app.brain.record_snapshot", return_value={"cash": 1, "num_positions": 0}) as rec, \
         patch("app.persistence.check_and_reload_optimizer_output", return_value=False), \
         patch("app.persistence.check_and_reload_backtest_output", return_value=False), \
         patch("app.persistence.check_and_reload_ml_training_output", return_value=False), \
         patch("app.persistence.check_and_reload_discovery_output", return_value=False), \
         patch("app.persistence.check_and_reload_wfo_output", return_value=False):
        scheduler._run_background_maintenance()
        # Zweiter Call sofort: kein Heartbeat-Snapshot
        scheduler._run_background_maintenance()

    assert rec.call_count == 1  # nur 1x trotz 2 Calls


# ============================================================
# Defensiv: Errors duerfen Cycle nicht brechen
# ============================================================

def test_watchdog_exception_does_not_stop_others():
    """Wenn 1 Watchdog raised, laufen die anderen 4 trotzdem."""
    from app import scheduler
    with patch("app.scheduler.is_market_hours", return_value=True), \
         patch("app.persistence.check_and_reload_optimizer_output",
               side_effect=RuntimeError("network blip")), \
         patch("app.persistence.check_and_reload_backtest_output", return_value=False) as bt, \
         patch("app.persistence.check_and_reload_ml_training_output", return_value=False) as ml, \
         patch("app.persistence.check_and_reload_discovery_output", return_value=False) as disc, \
         patch("app.persistence.check_and_reload_wfo_output", return_value=False) as wfo:
        # darf NICHT raisen
        scheduler._run_background_maintenance()
    bt.assert_called_once()
    ml.assert_called_once()
    disc.assert_called_once()
    wfo.assert_called_once()


def test_heartbeat_exception_does_not_stop_watchdogs():
    """Heartbeat-Snapshot crash darf Watchdogs nicht beeinflussen."""
    from app import scheduler

    with patch("app.scheduler.is_market_hours", return_value=False), \
         patch("app.broker_base.get_broker", side_effect=RuntimeError("ib down")), \
         patch("app.persistence.check_and_reload_optimizer_output", return_value=False) as opt, \
         patch("app.persistence.check_and_reload_backtest_output", return_value=False), \
         patch("app.persistence.check_and_reload_ml_training_output", return_value=False), \
         patch("app.persistence.check_and_reload_discovery_output", return_value=False), \
         patch("app.persistence.check_and_reload_wfo_output", return_value=False):
        # darf NICHT raisen
        scheduler._run_background_maintenance()

    opt.assert_called_once()


# ============================================================
# Review-Fix #2: Heartbeat skippt bei IB-Disconnect (vermeidet 30-60s Block)
# ============================================================

def test_heartbeat_skips_when_ib_disconnected():
    """Wenn broker._ib.isConnected() False -> Skip get_portfolio (kein 30-60s Block)."""
    from app import scheduler

    fake_broker = MagicMock()
    fake_broker._ib.isConnected.return_value = False  # disconnected

    with patch("app.scheduler.is_market_hours", return_value=False), \
         patch("app.broker_base.get_broker", return_value=fake_broker), \
         patch("app.brain.record_snapshot") as rec, \
         patch("app.persistence.check_and_reload_optimizer_output", return_value=False), \
         patch("app.persistence.check_and_reload_backtest_output", return_value=False), \
         patch("app.persistence.check_and_reload_ml_training_output", return_value=False), \
         patch("app.persistence.check_and_reload_discovery_output", return_value=False), \
         patch("app.persistence.check_and_reload_wfo_output", return_value=False):
        scheduler._run_background_maintenance()

    fake_broker.get_portfolio.assert_not_called()  # KEIN Roundtrip
    rec.assert_not_called()


# ============================================================
# Review-Fix #1: Token-Validity-Check bei 401/403 -> Pushover-Alert
# ============================================================

def test_token_validity_401_triggers_pushover(monkeypatch):
    """GitHub-Token expired (HTTP 401) -> Pushover-Alert mit priority=1."""
    from app import scheduler
    # Bypass den autouse-Stub fuer diesen Test
    # Restore die echte Funktion (autouse-Stub hat sie zu no-op gesetzt)
    monkeypatch.setattr(scheduler, "_check_github_token_validity",
                        _ORIGINAL_TOKEN_CHECK)

    fake_resp = MagicMock()
    fake_resp.status_code = 401

    pushovers = []
    monkeypatch.setattr("app.alerts.send_pushover",
                        lambda msg, *a, **kw: pushovers.append({"msg": msg, "kw": kw}))

    with patch("app.persistence._get_token", return_value="badtoken"), \
         patch("requests.get", return_value=fake_resp):
        _ORIGINAL_TOKEN_CHECK(now=1000.0)

    assert len(pushovers) == 1
    assert "GITHUB_TOKEN" in pushovers[0]["msg"] or "401" in pushovers[0]["msg"]
    assert pushovers[0]["kw"].get("priority") == 1


def test_token_validity_200_resets_fail_counter(monkeypatch):
    """HTTP 200 -> reset _BG_TOKEN_FAIL_COUNT auf 0."""
    from app import scheduler
    # Restore die echte Funktion (autouse-Stub hat sie zu no-op gesetzt)
    monkeypatch.setattr(scheduler, "_check_github_token_validity",
                        _ORIGINAL_TOKEN_CHECK)

    scheduler._BG_TOKEN_FAIL_COUNT["count"] = 5  # vorher gefailed

    fake_resp = MagicMock()
    fake_resp.status_code = 200

    with patch("app.persistence._get_token", return_value="goodtoken"), \
         patch("requests.get", return_value=fake_resp):
        _ORIGINAL_TOKEN_CHECK(now=1000.0)

    assert scheduler._BG_TOKEN_FAIL_COUNT["count"] == 0


def test_token_validity_throttled_6h(monkeypatch):
    """Token-Check 2× sofort: nur 1× echter HTTP-Request (6h Throttle)."""
    from app import scheduler
    # Restore die echte Funktion (autouse-Stub hat sie zu no-op gesetzt)
    monkeypatch.setattr(scheduler, "_check_github_token_validity",
                        _ORIGINAL_TOKEN_CHECK)

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    request_count = {"n": 0}

    def fake_get(*a, **kw):
        request_count["n"] += 1
        return fake_resp

    with patch("app.persistence._get_token", return_value="t"), \
         patch("requests.get", side_effect=fake_get):
        _ORIGINAL_TOKEN_CHECK(now=1000.0)
        _ORIGINAL_TOKEN_CHECK(now=1001.0)  # 1s spaeter

    assert request_count["n"] == 1  # nur 1 echter Call
