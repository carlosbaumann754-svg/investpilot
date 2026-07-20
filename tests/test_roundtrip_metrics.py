"""R-B12 (20.07.2026) — Round-Trip-Oekonomie + Motor-Edge-Zweitsignal.

Kontext: Der WFO-Drift-Watchdog misst das GESAMTE Buch in PROZENT. Am
20.07.2026 meldete er PF 2.02 ("gesund"), waehrend die Round-Trips, die der
neue Motor selbst eroeffnet UND geschlossen hatte, bei PF 0.38 lagen
(netto -7'534 USD). Diese Tests fixieren die Abgrenzung, die diese Luecke
schliesst — und dass das Zweitsignal unabhaengig vom Buch-Signal feuert.
"""
from unittest.mock import patch

import pytest

from app.roundtrip_metrics import build_episodes, clean_roundtrip_stats

SINCE = "2026-07-02T22:00"


def _buy(sym, ts, iid=None):
    return {"action": "SCANNER_BUY", "symbol": sym, "timestamp": ts,
            "instrument_id": iid, "amount_usd": 40000}


def _close(sym, ts, pnl_usd, action="TRAILING_SL_CLOSE", iid=None):
    d = {"action": action, "timestamp": ts, "pnl_usd": pnl_usd,
         "instrument_id": iid}
    if sym:
        d["symbol"] = sym
    return d


def _partial(sym, ts, pnl_usd, iid=None):
    return _close(sym, ts, pnl_usd, action="PARTIAL_CLOSE", iid=iid)


# ============================================================
# build_episodes
# ============================================================

def test_episode_spans_buy_to_full_close():
    trades = [_buy("AAA", "2026-07-05T10:00"),
              _close("AAA", "2026-07-08T10:00", 500.0)]
    closed, still_open = build_episodes(trades)
    assert len(closed) == 1 and not still_open
    assert closed[0]["entry_ts"].startswith("2026-07-05")
    assert closed[0]["exit_ts"].startswith("2026-07-08")
    assert closed[0]["pnl_usd"] == 500.0


def test_partial_closes_fold_into_episode_not_separate():
    """Teil-Closes sind KEINE eigenen Round-Trips — sonst zaehlt man Gewinner doppelt."""
    trades = [_buy("AAA", "2026-07-05T10:00"),
              _partial("AAA", "2026-07-06T10:00", 300.0),
              _partial("AAA", "2026-07-07T10:00", 200.0),
              _close("AAA", "2026-07-08T10:00", -1000.0)]
    closed, _ = build_episodes(trades)
    assert len(closed) == 1
    assert closed[0]["n_partials"] == 2
    assert closed[0]["pnl_usd"] == -500.0  # 300 + 200 - 1000


def test_addon_buy_does_not_reset_entry():
    """Ein Nachkauf darf eine geerbte Position nicht 'frisch' machen."""
    trades = [_buy("AAA", "2026-06-01T10:00"),
              _buy("AAA", "2026-07-10T10:00"),
              _close("AAA", "2026-07-12T10:00", 100.0)]
    closed, _ = build_episodes(trades)
    assert closed[0]["entry_ts"].startswith("2026-06-01")
    assert closed[0]["n_buys"] == 2


def test_rebuy_after_close_is_new_episode():
    trades = [_buy("AAA", "2026-07-03T10:00"),
              _close("AAA", "2026-07-05T10:00", 100.0),
              _buy("AAA", "2026-07-06T10:00"),
              _close("AAA", "2026-07-09T10:00", -50.0)]
    closed, _ = build_episodes(trades)
    assert len(closed) == 2
    assert [e["pnl_usd"] for e in closed] == [100.0, -50.0]


def test_symbol_resolved_via_instrument_id_for_partials():
    """Teil-Closes tragen kein 'symbol' — ohne Aufloesung fielen sie raus."""
    trades = [_buy("AAA", "2026-07-05T10:00", iid=42),
              _partial(None, "2026-07-06T10:00", 300.0, iid=42),
              _close("AAA", "2026-07-08T10:00", 100.0, iid=42)]
    closed, still_open = build_episodes(trades)
    assert len(closed) == 1 and not still_open
    assert closed[0]["pnl_usd"] == 400.0


def test_open_position_not_counted():
    trades = [_buy("AAA", "2026-07-05T10:00")]
    closed, still_open = build_episodes(trades)
    assert not closed and len(still_open) == 1


# ============================================================
# clean_roundtrip_stats — die eigentliche Abgrenzung
# ============================================================

def test_inherited_position_excluded_from_clean():
    """DER KERN: vor dem Fenster gekauft -> zaehlt NICHT zum Motor-Edge.

    Genau dieser Fehler drehte die Zahl am 20.07. faelschlich auf +14.8k.
    """
    trades = [
        _buy("OLD", "2026-06-20T10:00"),                    # vor SINCE
        _close("OLD", "2026-07-17T10:00", 8863.0),          # dicker Gewinn
        _buy("NEW", "2026-07-05T10:00"),                    # im Fenster
        _close("NEW", "2026-07-08T10:00", -5570.0),
    ]
    s = clean_roundtrip_stats(trades, since_iso=SINCE)
    assert s["n"] == 1
    assert s["net_usd"] == -5570.0            # nur der eigene Trade
    assert s["inherited"]["net_usd"] == 8863.0
    assert s["all_in_window"]["net_usd"] == 3293.0   # Kontrollzahl


def test_close_before_window_ignored_entirely():
    trades = [_buy("AAA", "2026-06-01T10:00"),
              _close("AAA", "2026-06-15T10:00", 999.0)]
    s = clean_roundtrip_stats(trades, since_iso=SINCE)
    assert s["n"] == 0 and s["all_in_window"]["n"] == 0


def test_profit_factor_and_winrate():
    trades = []
    for i, pnl in enumerate([300.0, 300.0, -200.0]):
        sym = f"S{i}"
        trades += [_buy(sym, "2026-07-05T10:00"),
                   _close(sym, "2026-07-08T10:00", pnl)]
    s = clean_roundtrip_stats(trades, since_iso=SINCE)
    assert s["n"] == 3
    assert s["pf"] == 3.0                      # 600 / 200
    assert s["win_rate"] == pytest.approx(66.7, abs=0.1)
    assert s["avg_win"] == 300.0
    assert s["avg_loss"] == -200.0


def test_pf_none_when_no_losses():
    """Verlustfreies Fenster -> pf None (nicht inf), Aufrufer wertet als gesund."""
    trades = [_buy("AAA", "2026-07-05T10:00"),
              _close("AAA", "2026-07-08T10:00", 100.0),
              _buy("BBB", "2026-07-05T10:00"),
              _close("BBB", "2026-07-08T10:00", 50.0)]
    s = clean_roundtrip_stats(trades, since_iso=SINCE)
    assert s["pf"] is None and s["net_usd"] == 150.0


def test_empty_history_safe():
    s = clean_roundtrip_stats([], since_iso=SINCE)
    assert s["n"] == 0 and s["pf"] is None and s["net_usd"] == 0.0


# ============================================================
# Zweitsignal im Watchdog
# ============================================================

def _collapsed_book(n_clean):
    """n_clean saubere Round-Trips mit kaputter Oekonomie (PF ~0.3)."""
    trades = []
    for i in range(n_clean):
        sym = f"C{i}"
        pnl = 200.0 if i % 3 else -900.0     # 2/3 kleine Gewinne, 1/3 grosse Verluste
        trades += [_buy(sym, "2026-07-05T10:00"),
                   _close(sym, "2026-07-08T10:00", pnl)]
    return trades


def _signal(trades, **cfg):
    """Ruft das Zweitsignal isoliert auf.

    Alert-State wird gemockt: sonst throttelt ein Test den naechsten aus
    (der Throttle ist persistent) und die Tests haengen voneinander ab.
    """
    from app.wfo_drift_watchdog import _check_roundtrip_signal
    result = {}
    base = {"roundtrip_since": SINCE}
    base.update(cfg)
    alert_state = {}
    with patch("app.alerts.send_alert") as mock_alert, \
         patch("app.wfo_drift_watchdog._load_alert_state",
               side_effect=lambda: alert_state), \
         patch("app.wfo_drift_watchdog._save_alert_state",
               side_effect=lambda s: alert_state.update(s)):
        _check_roundtrip_signal(trades, 1.71, base, result)
    return result, mock_alert


def test_signal_alerts_on_collapsed_motor_edge():
    result, mock_alert = _signal(_collapsed_book(15), roundtrip_min_n=12)
    rt = result["roundtrip"]
    assert rt["n"] == 15
    assert rt["pf"] < 1.0
    assert rt["drift_pct"] < -30
    assert result.get("roundtrip_alert") is True
    mock_alert.assert_called_once()
    assert "MOTOR-EDGE-ALARM" in mock_alert.call_args[0][0]


def test_signal_reports_but_stays_silent_below_min_sample():
    """Mini-Sample: berichten ja, alarmieren nein (Cry-Wolf-Schutz)."""
    result, mock_alert = _signal(_collapsed_book(9), roundtrip_min_n=12)
    rt = result["roundtrip"]
    assert rt["n"] == 9
    assert rt["pf"] < 1.0                      # Wert wird berichtet
    assert "Zu wenig saubere Round-Trips" in rt["skip_reason"]
    assert result.get("roundtrip_alert") is not True
    mock_alert.assert_not_called()


def test_signal_silent_when_motor_healthy():
    trades = []
    for i in range(15):
        sym = f"H{i}"
        trades += [_buy(sym, "2026-07-05T10:00"),
                   _close(sym, "2026-07-08T10:00", 400.0 if i % 4 else -100.0)]
    result, mock_alert = _signal(trades, roundtrip_min_n=12)
    assert result["roundtrip"]["pf"] > 1.71
    mock_alert.assert_not_called()


def test_signal_can_be_disabled():
    result, mock_alert = _signal(_collapsed_book(15),
                                 roundtrip_signal_enabled=False)
    assert "roundtrip" not in result
    mock_alert.assert_not_called()


def test_signal_never_raises_on_garbage():
    """Ein Fehler im Zweitsignal darf den Haupt-Check nicht kippen."""
    result, _ = _signal([{"action": "SCANNER_BUY"}, None, 42, "kaputt"])
    assert isinstance(result, dict)   # kein Throw


def test_signal_alert_is_throttled():
    """Zweiter Alert innerhalb des Fensters wird unterdrueckt (Cry-Wolf-Schutz).

    Eigener Throttle-Key: das Zweitsignal darf das Buch-Signal nicht
    aussperren und umgekehrt.
    """
    from app.wfo_drift_watchdog import _check_roundtrip_signal
    trades = _collapsed_book(15)
    cfg = {"roundtrip_since": SINCE, "roundtrip_min_n": 12}
    alert_state = {}
    with patch("app.alerts.send_alert") as mock_alert, \
         patch("app.wfo_drift_watchdog._load_alert_state",
               side_effect=lambda: alert_state), \
         patch("app.wfo_drift_watchdog._save_alert_state",
               side_effect=lambda s: alert_state.update(s)):
        r1, r2 = {}, {}
        _check_roundtrip_signal(trades, 1.71, cfg, r1)
        _check_roundtrip_signal(trades, 1.71, cfg, r2)
    assert r1.get("roundtrip_alert") is True
    assert r2.get("roundtrip_alert") is not True
    assert "gethrottlet" in r2["roundtrip"]["skip_reason"]
    assert mock_alert.call_count == 1
    # eigener Key -> Buch-Signal-Throttle bleibt unberuehrt
    assert "last_roundtrip_alert_at" in alert_state
    assert "last_alert_at" not in alert_state


def test_signal_ignores_inherited_winners():
    """Buch gruen durch Alt-Gewinner, Motor trotzdem kaputt -> Alarm."""
    trades = _collapsed_book(15)
    trades += [_buy("OLD", "2026-06-01T10:00"),
               _close("OLD", "2026-07-17T10:00", 50000.0)]
    result, mock_alert = _signal(trades, roundtrip_min_n=12)
    assert result["roundtrip"]["inherited_net_usd"] == 50000.0
    assert result["roundtrip"]["pf"] < 1.0
    mock_alert.assert_called_once()
