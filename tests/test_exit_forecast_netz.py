"""R-B35 (21.07.2026) — Exit-Prognose: "Netz scharf" korrekt gerechnet + markiert.

Kontext: Fuer Positionen UEBER der Trailing-Schwelle (+6%) zeigte die Karte
"Trailing-SL: --" und den -8%-Stop als naechsten Trigger — obwohl der
Ratchet-Stand im State lag (live belegt: AOSL sl_price=32.01, dist=None).
Ursache: die Distanz-Rechnung brauchte current_price, den die IBKR-Position
nicht immer mitliefert; der Fallback des Traders (Kurs aus Einstand+PnL)
fehlte in der Karte.

Hinweis: web.app braucht Container-Abhaengigkeiten (pyotp etc.) — lokal wird
geskippt, im Container laeuft die Pruefung direkt (siehe R-B29-Parse-Guard,
gleiche Begruendung).
"""
import importlib.util

import pytest

_FEHLT = next((p for p in ("pyotp", "fastapi", "ib_insync")
               if importlib.util.find_spec(p) is None), None)
pytestmark = pytest.mark.skipif(
    _FEHLT is not None,
    reason=f"Laufzeit-Abhaengigkeit '{_FEHLT}' fehlt (nur im Container vorhanden)",
)


def _cfg():
    return {
        "demo_trading": {"stop_loss_pct": -8.0, "take_profit_pct": 999},
        "leverage": {"trailing_sl_enabled": True,
                     "trailing_sl_activation_pct": 6.0,
                     "trailing_sl_pct": 4.0,
                     "tp_tranches": []},
        "time_stop": {"enabled": True, "max_days_stale": 30,
                      "min_days_open": 2, "stale_pnl_threshold_pct": 0.5},
    }


def _forecast(position, trailing_state):
    from web.app import _compute_exit_forecast
    return _compute_exit_forecast(position, _cfg(), trailing_state)


def _trail(r):
    return next(t for t in r["triggers"] if t["type"] == "Trailing-SL")


def test_netz_mit_state_aber_ohne_current_price():
    """DER Live-Fall (AOSL): Ratchet im State, current_price fehlt.

    Vorher: dist=None -> Anzeige '--', naechster Trigger faelschlich SL.
    Jetzt: Kurs wird aus Einstand+PnL rekonstruiert, Distanz + gesicherter
    Gewinn berechnet, und der naechste Trigger ist das NETZ."""
    r = _forecast(
        {"position_id": "74820280", "pnl_pct": 7.54,
         "entry_price": 30.9, "current_price": 0},
        {"74820280": {"sl_level": 32.0133}},
    )
    t = _trail(r)
    assert t["armed"] is True
    assert t["estimated"] is False
    assert t["distance_pct"] == pytest.approx(3.7, abs=0.2)     # Kurs ~33.23 -> Netz 32.01
    assert t["locked_pct"] == pytest.approx(3.6, abs=0.2)       # 32.01/30.9 - 1
    assert r["next_trigger"]["type"] == "Trailing-SL"           # nicht mehr SL!


def test_netz_ueber_schwelle_ohne_state_ist_geschaetzt():
    """+6% ueberschritten, Scheduler hat den Ratchet noch nicht geschrieben:
    konservative Schaetzung (aktueller Kurs als Peak), als solche markiert."""
    r = _forecast(
        {"position_id": "1", "pnl_pct": 8.0,
         "entry_price": 100.0, "current_price": 108.0},
        {},
    )
    t = _trail(r)
    assert t["armed"] is True
    assert t["estimated"] is True
    assert t["distance_pct"] == 4.0
    assert t["locked_pct"] == pytest.approx(4.0)                # 8.0 - 4.0


def test_unter_schwelle_kein_netz():
    """Unter +6%: Netz nicht scharf, naechster Trigger bleibt der Stop-Loss."""
    r = _forecast(
        {"position_id": "2", "pnl_pct": 3.76,
         "entry_price": 100.0, "current_price": 103.76},
        {},
    )
    t = _trail(r)
    assert t["armed"] is False
    assert t["distance_pct"] is None
    assert r["next_trigger"]["type"] == "SL"


def test_ratchet_gewinner_kann_nicht_mehr_verlieren():
    """Kern-Eigenschaft des Netzes: einmal scharf, liegt der gesicherte Gewinn
    ueber null — auch wenn der Kurs vom Peak zurueckkommt."""
    r = _forecast(
        {"position_id": "3", "pnl_pct": 6.2,          # vom Peak +10% zurueckgefallen
         "entry_price": 100.0, "current_price": 106.2},
        {"3": {"sl_level": 105.6}},                    # Peak war 110 -> Netz 105.6
    )
    t = _trail(r)
    assert t["locked_pct"] == pytest.approx(5.6)
    assert t["distance_pct"] == pytest.approx(0.56, abs=0.05)  # kurz vor Ausloesung
    assert r["next_trigger"]["type"] == "Trailing-SL"


def test_einstand_kommt_notfalls_aus_dem_trailing_state():
    """Der volle Live-Fall (AOSL, 2. Iteration): die IBKR-Position liefert WEDER
    current_price NOCH entry_price — aber der State hat beides. Ohne den Griff
    in den State bliebe die Anzeige auf 'geschaetzt', obwohl der exakte
    Ratchet-Stand vorliegt."""
    r = _forecast(
        {"position_id": "74820280", "pnl_pct": 7.48,
         "entry_price": 0, "current_price": 0},
        {"74820280": {"sl_level": 32.0133, "entry_price": 30.9}},
    )
    t = _trail(r)
    assert t["estimated"] is False                              # exakt, nicht geschaetzt
    assert t["locked_pct"] == pytest.approx(3.6, abs=0.1)
    assert t["distance_pct"] == pytest.approx(3.6, abs=0.2)
    assert r["next_trigger"]["type"] == "Trailing-SL"


# --- R-B66c (29.08.2026): Exit-Forecast unter M2a ------------------------
# Eine M2a-Position hat genau ZWEI Ausstiege (Zeit-Horizont + Kat-Stop).
# Die Karte darf ihr KEINE M0-Trigger (SL/Trailing/TP/Time-Stop) andichten —
# und Geerbte muessen die Alt-Anzeige behalten (+ Badge-Flag).

def _forecast_m2a(position, m2a_ctx, open_dt=None, age=45.0):
    from datetime import datetime
    from unittest import mock
    from web.app import _compute_exit_forecast
    open_dt = open_dt or datetime(2026, 9, 1, 15, 45)
    with mock.patch("app.trader._find_position_open_time",
                    return_value=(open_dt, age)):
        return _compute_exit_forecast(position, _cfg(), {}, m2a_ctx)


def _ctx(geerbt=frozenset()):
    return {"horizon": 126, "geerbt": geerbt, "kat_stop_pct": -40.0}


def test_m2a_position_zeigt_nur_horizont_und_katstop():
    from datetime import date, datetime
    from unittest import mock
    from app import m2a_motor
    open_dt = datetime(2026, 9, 1, 15, 45)
    r = _forecast_m2a({"position_id": "99001", "pnl_pct": -12.0}, _ctx(),
                      open_dt=open_dt)
    assert r["m2a"] is True and r["geerbt"] is False
    typen = {t["type"] for t in r["triggers"]}
    assert typen == {"HORIZONT", "E6-Kat-Stop"}, (
        f"M2a-Position zeigt falsche Trigger: {typen}")
    e6 = next(t for t in r["triggers"] if t["type"] == "E6-Kat-Stop")
    assert e6["distance_pct"] == pytest.approx(28.0)  # -12 -> -40
    hz = next(t for t in r["triggers"] if t["type"] == "HORIZONT")
    erwartet = m2a_motor.handelstage_seit(open_dt)
    assert hz["handelstage"] == erwartet
    assert hz["handelstage_rest"] == max(0, 126 - erwartet)
    assert r["next_trigger"]["type"] == "HORIZONT"
    assert r["next_trigger"]["handelstage_rest"] == hz["handelstage_rest"]


def test_m2a_kurz_vor_katstop_wird_der_zum_naechsten_trigger():
    r = _forecast_m2a({"position_id": "99002", "pnl_pct": -37.0}, _ctx())
    assert r["next_trigger"]["type"] == "E6-Kat-Stop"
    assert r["next_trigger"]["distance_pct"] == pytest.approx(3.0)


def test_geerbte_behalten_alt_anzeige_und_flag():
    r = _forecast_m2a({"position_id": "74820280", "pnl_pct": 2.0},
                      _ctx(geerbt=frozenset({"74820280"})))
    assert r["geerbt"] is True and r["m2a"] is False
    typen = {t["type"] for t in r["triggers"]}
    assert "SL" in typen and "Trailing-SL" in typen
    assert "HORIZONT" not in typen


def test_ohne_m2a_ctx_alles_wie_vorher():
    """Vor dem Schnitt (m2a_ctx=None) muss die Karte exakt M0 zeigen."""
    r = _forecast_m2a({"position_id": "1", "pnl_pct": 1.0}, None)
    assert r["m2a"] is False and r["geerbt"] is False
    assert "SL" in {t["type"] for t in r["triggers"]}
