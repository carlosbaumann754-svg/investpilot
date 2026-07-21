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

EXIT_CFG = {"sl_pct": -8, "tp_pct": None, "trail_act_pct": 6.0,
            "trail_pct": 4.0, "tranches": None}

# Referenzverteilung: p05 = "so schlecht ist ein GESUNDES System noch in 5 %
# der Faelle". Steigt mit n, weil die Streuung abnimmt.
REFERENZ = {
    "exit_config": EXIT_CFG,
    "by_n": {"5": {"p05": 0.10}, "10": {"p05": 0.30},
             "20": {"p05": 0.50}, "40": {"p05": 0.80}},
}

LIVE_CFG = {
    "demo_trading": {"stop_loss_pct": -8, "take_profit_pct": 999},
    "leverage": {"trailing_sl_activation_pct": 6.0, "trailing_sl_pct": 4.0,
                 "tp_tranches": []},
}


def _signal(trades, referenz=REFERENZ, live_cfg=LIVE_CFG, **cfg):
    """Ruft das Zweitsignal isoliert auf.

    Gemockt werden: Alert-State (sonst throttelt ein Test den naechsten aus),
    die Referenzverteilung und die Live-Config (fuer den Staleness-Guard).
    """
    from app.wfo_drift_watchdog import _check_roundtrip_signal
    result = {}
    base = {"roundtrip_since": SINCE}
    base.update(cfg)
    alert_state = {}

    def fake_load_json(name):
        return referenz if name == "roundtrip_pf_reference.json" else None

    with patch("app.alerts.send_alert") as mock_alert, \
         patch("app.config_manager.load_json", side_effect=fake_load_json), \
         patch("app.config_manager.load_config", return_value=live_cfg), \
         patch("app.wfo_drift_watchdog._load_alert_state",
               side_effect=lambda: alert_state), \
         patch("app.wfo_drift_watchdog._save_alert_state",
               side_effect=lambda s: alert_state.update(s)):
        _check_roundtrip_signal(trades, 1.71, base, result)
    return result, mock_alert


def _book(n, gewinn_usd, verlust_usd, einsatz=40000.0):
    """n Round-Trips: 2 von 3 gewinnen. Einsatz gleich -> Prozent = USD/Einsatz."""
    trades = []
    for i in range(n):
        sym = f"C{i}"
        pnl = gewinn_usd if i % 3 else verlust_usd
        trades += [dict(_buy(sym, "2026-07-05T10:00"), amount_usd=einsatz),
                   _close(sym, "2026-07-08T10:00", pnl)]
    return trades


def _kaputt(n=15):
    """PF% ~0.20 — klar unter dem p05 (0.40 bei n=15)."""
    return _book(n, 200.0, -2000.0)


def _gesund(n=15):
    """PF% ~2.0 — klar ueber jedem p05 der Referenz."""
    return _book(n, 900.0, -900.0)


# ============================================================
# Referenz-Schwelle: Interpolation
# ============================================================

def test_threshold_interpolates_between_grid_points():
    """n=15 liegt zwischen 10 (p05 0.30) und 20 (p05 0.50) -> 0.40."""
    from app.wfo_drift_watchdog import _reference_threshold
    with patch("app.config_manager.load_json", return_value=REFERENZ):
        grenze, _ = _reference_threshold(15)
    assert grenze == pytest.approx(0.40)


def test_threshold_clamps_below_and_above_grid():
    from app.wfo_drift_watchdog import _reference_threshold
    with patch("app.config_manager.load_json", return_value=REFERENZ):
        unten, _ = _reference_threshold(2)
        oben, _ = _reference_threshold(500)
    assert unten == pytest.approx(0.10)
    assert oben == pytest.approx(0.80)


def test_threshold_none_without_reference():
    from app.wfo_drift_watchdog import _reference_threshold
    with patch("app.config_manager.load_json", return_value=None):
        grenze, ref = _reference_threshold(15)
    assert grenze is None and ref is None


# ============================================================
# Alarm-Verhalten
# ============================================================

def test_alerts_when_below_reference_quantile():
    """DER KERN: Alarm nur, wenn schlechter als 95 % der gesunden Phasen."""
    result, mock_alert = _signal(_kaputt(15))
    rt = result["roundtrip"]
    assert rt["n"] == 15
    assert rt["pf_pct"] < rt["alarm_grenze"]
    assert result.get("roundtrip_alert") is True
    mock_alert.assert_called_once()
    assert "MOTOR-EDGE-ALARM" in mock_alert.call_args[0][0]


def test_silent_when_above_reference_quantile():
    result, mock_alert = _signal(_gesund(15))
    assert result["roundtrip"]["pf_pct"] > result["roundtrip"]["alarm_grenze"]
    mock_alert.assert_not_called()


def test_small_sample_reports_with_wide_tolerance():
    """Bei kleinem n wird berichtet — und die Toleranz ist entsprechend weit.

    Der alte Entwurf schwieg unter n=12 komplett. Jetzt darf frueh gesprochen
    werden, weil die Grenze mit n mitwaechst (bei n=5 nur 0.10).
    """
    result, mock_alert = _signal(_gesund(6))
    rt = result["roundtrip"]
    assert rt["n"] == 6
    assert rt["alarm_grenze"] < 0.31, "Toleranz bei kleinem n muss weit sein"
    mock_alert.assert_not_called()


# ============================================================
# Schutzmechanismen
# ============================================================

def test_no_reference_means_report_but_no_alert():
    """Ohne Tabelle keine Grenze -> lieber schweigen als falsch alarmieren."""
    result, mock_alert = _signal(_kaputt(15), referenz=None)
    rt = result["roundtrip"]
    assert rt["pf_pct"] is not None, "Wert muss trotzdem berichtet werden"
    assert "Referenzverteilung fehlt" in rt["skip_reason"]
    mock_alert.assert_not_called()


def test_stale_reference_blocks_alert_and_flags():
    """Referenz zu anderer Config -> falsche Grenzen -> kein Alarm, aber laut.

    Ein still gewordenes Signal war der Fehler bei der Tages-Zusammenfassung
    (R-B14) — hier wird der Grund explizit im Ergebnis vermerkt.
    """
    andere_cfg = {"demo_trading": {"stop_loss_pct": -5, "take_profit_pct": 12},
                  "leverage": {"trailing_sl_activation_pct": 6.0,
                               "trailing_sl_pct": 4.0, "tp_tranches": []}}
    result, mock_alert = _signal(_kaputt(15), live_cfg=andere_cfg)
    rt = result["roundtrip"]
    assert rt.get("referenz_veraltet") is True
    assert "passt nicht zur Live-Config" in rt["skip_reason"]
    mock_alert.assert_not_called()


def test_alert_is_throttled():
    from app.wfo_drift_watchdog import _check_roundtrip_signal
    trades = _kaputt(15)
    cfg = {"roundtrip_since": SINCE}
    alert_state = {}

    def fake_load_json(name):
        return REFERENZ if name == "roundtrip_pf_reference.json" else None

    with patch("app.alerts.send_alert") as mock_alert, \
         patch("app.config_manager.load_json", side_effect=fake_load_json), \
         patch("app.config_manager.load_config", return_value=LIVE_CFG), \
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
    assert "last_roundtrip_alert_at" in alert_state
    assert "last_alert_at" not in alert_state, "eigener Key, sperrt das Buch-Signal nicht"


def test_signal_can_be_disabled():
    result, mock_alert = _signal(_kaputt(15), roundtrip_signal_enabled=False)
    assert "roundtrip" not in result
    mock_alert.assert_not_called()


def test_signal_never_raises_on_garbage():
    result, _ = _signal([{"action": "SCANNER_BUY"}, None, 42, "kaputt"])
    assert isinstance(result, dict)


def test_ignores_inherited_winners():
    """Buch gruen durch Alt-Gewinner, Motor trotzdem kaputt -> Alarm."""
    trades = _kaputt(15)
    trades += [dict(_buy("OLD", "2026-06-01T10:00"), amount_usd=40000.0),
               _close("OLD", "2026-07-17T10:00", 50000.0)]
    result, mock_alert = _signal(trades)
    assert result["roundtrip"]["inherited_net_usd"] == 50000.0
    mock_alert.assert_called_once()


def test_reports_both_usd_and_percent():
    """USD = Gatekeeper (oekonomisch), Prozent = Alarm (vergleichbar)."""
    result, _ = _signal(_kaputt(15))
    rt = result["roundtrip"]
    assert rt["pf"] is not None and rt["pf_pct"] is not None
    assert rt["net_usd"] < 0
    assert rt["avg_ret_pct"] is not None


# ============================================================
# Prozent-Basis in den Kennzahlen
# ============================================================

def test_invested_usd_accumulated_from_buys():
    trades = [dict(_buy("AAA", "2026-07-05T10:00"), amount_usd=30000.0),
              dict(_buy("AAA", "2026-07-06T10:00"), amount_usd=10000.0),
              _close("AAA", "2026-07-08T10:00", 4000.0)]
    s = clean_roundtrip_stats(trades, since_iso=SINCE)
    assert s["n"] == 1
    assert s["avg_ret_pct"] == pytest.approx(10.0)   # 4000 / 40000


def test_pf_percent_differs_from_usd_when_sizes_differ():
    """Grosser Verlierer neben kleinen Gewinnern: USD-PF schlechter als Prozent.

    Genau dieser Effekt erklaerte am 21.07. einen Teil der Luecke zwischen
    gemessenem USD-PF (0.38) und der Backtest-Basis (0.45).
    """
    trades = []
    for i, (einsatz, pnl) in enumerate([(10000.0, 1000.0), (10000.0, 1000.0),
                                        (100000.0, -5000.0)]):
        sym = f"S{i}"
        trades += [dict(_buy(sym, "2026-07-05T10:00"), amount_usd=einsatz),
                   _close(sym, "2026-07-08T10:00", pnl)]
    s = clean_roundtrip_stats(trades, since_iso=SINCE)
    # USD: 2000 / 5000 = 0.40 ; Prozent: (10+10) / 5 = 4.00
    assert s["pf"] == pytest.approx(0.40)
    assert s["pf_pct"] == pytest.approx(4.00)
