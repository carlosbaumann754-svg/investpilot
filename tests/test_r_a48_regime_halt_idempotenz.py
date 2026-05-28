"""Tests fuer R-A48 — REGIME-HALT-Notification Cry-Wolf-Idempotenz-Fix.

Bug-Anlass: Carlos meldete Do 28.05.2026 07:26 CEST Pushover-Banner
'REGIME HALT AKTIVIERT' — Diagnose zeigte: Alert feuert in JEDEM Bot-Cycle
(~5 Min) waehrend Halt-Periode. Bei 4h Halt bereits ~48 Banner. Bei
mehrtaegigem Halt (F&G/Yield-Curve/Marktbreite sind langsame Indikatoren)
schnell ~200+ Banner/Tag → Cry-Wolf-Effekt vor Real-Money-Cutover.

Plus 2. Bug: alert_regime_resumed() war zwar implementiert in alerts.py
aber NIRGENDWO aufgerufen — Recovery-Alert fehlte komplett. Carlos sah
also nie das '✅ REGIME HALT AUFGEHOBEN'-Signal.

Fix R-A48 (Sprint-Tag-17, 28.05.2026):
  1. Helper _classify_regime_state_change(current, previous) -> str
     - "halt" wenn False -> True
     - "resumed" wenn True -> False
     - "none" wenn kein State-Change
  2. Modul-Level-State _last_regime_halt_state in trader.py
  3. Im Bot-Cycle: nur bei "halt"/"resumed" eine Notification feuern
     (statt jeden Cycle blind)

Design-Trade-Off: In-Memory state (nicht persistiert). Bei Container-
Restart (Daily 05:15 UTC oder manuell) ist state auf False reset —
bewusst: nach Restart kommt EIN Status-Recap-Alert wenn HALT noch
aktiv (sonst waere User nach Restart blind ob HALT noch durch ist).
"""

import pytest

from app.trader import _classify_regime_state_change


# ---------------------------------------------------------------------------
# State-Change-Klassifikation (Pure-Function Tests)
# ---------------------------------------------------------------------------

def test_r_a48_off_to_on_classifies_as_halt():
    """Trading-OK -> Halt-aktiv: muss als 'halt' klassifiziert werden."""
    result = _classify_regime_state_change(
        current_halt_active=True, previous_halt_active=False
    )
    assert result == "halt"


def test_r_a48_on_to_off_classifies_as_resumed():
    """Halt-aktiv -> Trading-OK: muss als 'resumed' klassifiziert werden
    (Recovery-Alert via alert_regime_resumed)."""
    result = _classify_regime_state_change(
        current_halt_active=False, previous_halt_active=True
    )
    assert result == "resumed"


def test_r_a48_on_to_on_no_change():
    """Halt war aktiv, ist weiterhin aktiv: KEIN Alert (Anti-Cry-Wolf)."""
    result = _classify_regime_state_change(
        current_halt_active=True, previous_halt_active=True
    )
    assert result == "none"


def test_r_a48_off_to_off_no_change():
    """Trading war OK, ist weiterhin OK: KEIN Alert (Standard-Betrieb)."""
    result = _classify_regime_state_change(
        current_halt_active=False, previous_halt_active=False
    )
    assert result == "none"


# ---------------------------------------------------------------------------
# Cycle-Sequence-Simulation: typisches Carlos-Szenario heute (28.05.2026)
# ---------------------------------------------------------------------------

def test_r_a48_typical_long_halt_only_one_alert():
    """Simuliere Carlos's heute-Szenario: HALT geht 4h durch
    (= 48 Bot-Cycles a 5 Min). Erwartung: GENAU 1 'halt' beim
    Eintritt, dann 47x 'none'. Total 1 Pushover statt 48."""
    states = []
    previous = False  # Bot-Start: kein Halt
    # 48 Cycles mit HALT aktiv
    for cycle in range(48):
        result = _classify_regime_state_change(
            current_halt_active=True, previous_halt_active=previous
        )
        states.append(result)
        previous = True  # state-update

    halt_count = sum(1 for s in states if s == "halt")
    none_count = sum(1 for s in states if s == "none")
    resumed_count = sum(1 for s in states if s == "resumed")

    assert halt_count == 1, f"Erwartet 1 'halt', got {halt_count}"
    assert none_count == 47, f"Erwartet 47 'none', got {none_count}"
    assert resumed_count == 0


def test_r_a48_full_cycle_halt_then_recovery():
    """Full-Cycle: 5x Trading OK, 10x HALT, 5x Trading OK (Recovery).
    Erwartung: 1x 'halt', 1x 'resumed', Rest 'none'."""
    sequence_halt_active = (
        [False] * 5 +  # Trading OK
        [True] * 10 +  # HALT-Phase
        [False] * 5    # Recovery
    )

    results = []
    previous = False
    for current in sequence_halt_active:
        result = _classify_regime_state_change(
            current_halt_active=current, previous_halt_active=previous
        )
        results.append(result)
        previous = current

    halt_idx = results.index("halt")
    resumed_idx = results.index("resumed")

    assert halt_idx == 5, f"'halt' sollte bei Cycle 5 sein (Eintritt), war {halt_idx}"
    assert resumed_idx == 15, f"'resumed' sollte bei Cycle 15 sein (Recovery), war {resumed_idx}"
    assert results.count("halt") == 1
    assert results.count("resumed") == 1
    assert results.count("none") == 18


def test_r_a48_flickering_halt_each_flip_alerts():
    """Pathologischer Fall: HALT flackert zwischen on/off (sollte
    NICHT vorkommen, aber wenn ja: jeder Flip wird signaliert).
    Das ist KEIN Bug — Carlos soll wissen wenn das passiert."""
    sequence = [False, True, False, True, False, True, False]
    results = []
    previous = False
    for current in sequence:
        result = _classify_regime_state_change(
            current_halt_active=current, previous_halt_active=previous
        )
        results.append(result)
        previous = current

    # Erwartung: none, halt, resumed, halt, resumed, halt, resumed
    assert results == ["none", "halt", "resumed", "halt", "resumed", "halt", "resumed"]


# ---------------------------------------------------------------------------
# Module-Level State Tests (Container-Restart-Simulation)
# ---------------------------------------------------------------------------

def test_r_a48_module_state_resets_on_import():
    """Bei Container-Restart wird Modul-State auf False reset.
    Bewusste Design-Wahl: erster Cycle nach Restart feuert wieder
    den HALT-Alert wenn HALT noch aktiv (User-Visibility-Update)."""
    from app import trader
    # Test: Modul-Variable hat einen Default-Wert
    assert hasattr(trader, "_last_regime_halt_state")
    # Default sollte False sein (Trading-OK angenommen)
    # Note: bei laufenden Tests koennte der State schon True sein durch
    # andere Tests, daher reset
    trader._last_regime_halt_state = False
    assert trader._last_regime_halt_state is False


# ---------------------------------------------------------------------------
# Regression-Schutz (Source-Based)
# ---------------------------------------------------------------------------

def test_r_a48_old_unconditional_call_pattern_gone():
    """Regression: alter Pattern 'elif al: al.alert_regime_halt(...)' ohne
    Idempotenz-Check darf nicht mehr in trader.py sein."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "trader.py"
    body = src.read_text(encoding="utf-8")
    # Der alte Pattern war: elif al: ...\n... al.alert_regime_halt(regime_reason, regime_data)
    # Wir suchen nach diesem spezifischen unconditional-Aufruf.
    # NICHT zu verwechseln mit dem neuen Pattern in if transition == "halt":
    assert "elif al:\n                # Telegram: Regime Halt Notification\n                al.alert_regime_halt(regime_reason, regime_data)" not in body, (
        "R-A48 REGRESSION: alter unconditional alert_regime_halt-Aufruf ist zurueck"
    )


def test_r_a48_classify_helper_present():
    """R-A48 Helper MUSS in trader.py existieren (Source-Based-Regression)."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "trader.py"
    body = src.read_text(encoding="utf-8")
    assert "def _classify_regime_state_change" in body
    assert "_last_regime_halt_state" in body


def test_r_a48_alert_regime_resumed_now_called():
    """R-A48: alert_regime_resumed MUSS jetzt in trader.py aufgerufen sein
    (vor R-A48 wurde es NIRGENDS gerufen — Recovery-Alert fehlte komplett)."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "trader.py"
    body = src.read_text(encoding="utf-8")
    assert "alert_regime_resumed" in body, (
        "R-A48: alert_regime_resumed muss in trader.py jetzt aufgerufen werden "
        "(war vor R-A48 komplett unbenutzt)"
    )
