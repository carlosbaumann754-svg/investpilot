"""Tests fuer app/bootstrap_v12.py — speziell die v37du Boot-Invarianten
(Seed-Drift-Schutz). migrate() ist pur (dict -> (dict, changes)) und damit
ohne I/O testbar.
"""
from app import bootstrap_v12 as b


# --- use_signal_stack: HARTER Invariant (immer True erzwingen) --------------
def test_use_signal_stack_forced_when_missing():
    cfg, changes = b.migrate({"some": "thing"})
    assert cfg["use_signal_stack"] is True
    assert any("use_signal_stack" in c for c in changes)


def test_use_signal_stack_forced_when_false():
    # Genau der gefaehrliche Fall: ein alter/Stale-Seed mit explizit False
    cfg, changes = b.migrate({"use_signal_stack": False})
    assert cfg["use_signal_stack"] is True
    assert any("use_signal_stack" in c for c in changes)


def test_use_signal_stack_noop_when_already_true():
    cfg, changes = b.migrate({"use_signal_stack": True})
    assert cfg["use_signal_stack"] is True
    assert not any("use_signal_stack" in c for c in changes)


# --- optimizer.enabled: inject-if-missing, NICHT ueberschreiben -------------
def test_optimizer_enabled_injected_false_when_section_missing():
    cfg, _ = b.migrate({})
    assert cfg["optimizer"]["enabled"] is False


def test_optimizer_enabled_injected_when_key_missing():
    cfg, _ = b.migrate({"optimizer": {"interval_days": 7}})
    assert cfg["optimizer"]["enabled"] is False
    assert cfg["optimizer"]["interval_days"] == 7  # bestehendes bleibt


def test_optimizer_enabled_true_is_NOT_overridden():
    # Bewusstes post-Soak Re-Enable darf nicht zurueckgesetzt werden
    cfg, changes = b.migrate({"optimizer": {"enabled": True}})
    assert cfg["optimizer"]["enabled"] is True
    assert not any("optimizer.enabled" in c for c in changes)


# --- risk_management Sicherheits-/Cap-Invarianten (inject-if-missing) -------
def test_risk_management_invariants_injected_when_missing():
    cfg, _ = b.migrate({})
    rm = cfg["risk_management"]
    assert rm["catastrophic_stop"] == {"enabled": True, "pct": 20}
    assert rm["max_positions_per_class"] == 20
    assert rm["max_class_allocation_pct"] == 1000


def test_risk_management_existing_values_not_overridden():
    cfg, _ = b.migrate({"risk_management": {
        "max_positions_per_class": 5,            # Live-abweichend -> bleibt
        "catastrophic_stop": {"enabled": False, "pct": 10},
    }})
    rm = cfg["risk_management"]
    assert rm["max_positions_per_class"] == 5          # NICHT ueberschrieben
    assert rm["catastrophic_stop"]["enabled"] is False  # NICHT ueberschrieben
    assert rm["max_class_allocation_pct"] == 1000       # fehlte -> injiziert


# --- aktualisierte Baselines (Reconstruction-aus-Null) ----------------------
def test_reconstruction_from_empty_has_current_exit_baselines():
    cfg, _ = b.migrate({})
    assert cfg["time_stop"]["max_days_stale"] == 30
    lev = cfg.get("leverage")
    # leverage-Subkeys werden nur bei existierender Parent-Section injiziert;
    # bei {} ist leverage nicht da -> kein Inject. Wir pruefen die Baseline-Konstante.
    # R-B12 (20.07.2026): Die Umstellung auf 10/12 + TP aus + Tranchen aus wurde
    # noch am selben Abend zurueckgedreht — der Backtest, der sie empfahl, holt
    # ~86 % seines Gewinns aus monatlichem Rebalancing, das der Live-Bot nicht hat.
    # Details im Kommentar an V12_SUBKEY_INJECT. Baselines daher wieder wie zuvor.
    assert b.V12_SUBKEY_INJECT["leverage"]["trailing_sl_activation_pct"] == 6.0
    assert b.V12_SUBKEY_INJECT["leverage"]["trailing_sl_pct"] == 4.0
    tranches = b.V12_SUBKEY_INJECT["leverage"]["tp_tranches"]
    assert [t["profit_target_pct"] for t in tranches] == [8, 16, 30]


def test_leverage_subkeys_injected_when_parent_present():
    cfg, _ = b.migrate({"leverage": {"enabled": True}})
    lev = cfg["leverage"]
    assert lev["trailing_sl_activation_pct"] == 6.0
    assert lev["trailing_sl_pct"] == 4.0


def test_empty_tranche_list_not_reinjected():
    """R-B12: Die leere Tranchen-Liste muss als 'vorhanden' gelten.

    Wuerde die Injection sie als fehlend behandeln (z.B. via Falsy-Pruefung
    statt 'key not in parent'), kaeme der Gewinner-Deckel bei JEDEM Boot
    zurueck — die Rekalibrierung waere still wirkungslos.
    """
    cfg, _ = b.migrate({"leverage": {"enabled": True, "tp_tranches": []}})
    assert cfg["leverage"]["tp_tranches"] == []


# --- Idempotenz: ein bereits korrekter Live-aehnlicher Config -> keine Changes
def test_idempotent_on_correct_config():
    base, _ = b.migrate({})              # erzeugt eine v12+invariant-konforme Basis
    # Sektionen, die migrate nur-bei-Parent injiziert, ergaenzen:
    # Werte aus den Konstanten ziehen statt hart zu verdrahten — sonst laeuft
    # dieser "live-aehnliche" Config bei jeder Baseline-Aenderung aus dem Ruder.
    base["leverage"] = dict(b.V12_SUBKEY_INJECT["leverage"])
    base["disabled_symbols"] = list(b.V12_DISABLED_SYMBOLS)
    again, changes2 = b.migrate(base)
    assert changes2 == [], f"erwartete Idempotenz, bekam Changes: {changes2}"


# --- disabled_symbols bleibt git-managed (bestehendes Verhalten) ------------
def test_disabled_symbols_forced_from_git_list():
    cfg, _ = b.migrate({"disabled_symbols": ["FOO"]})
    assert set(cfg["disabled_symbols"]) == set(b.V12_DISABLED_SYMBOLS)
    assert "SILVER" in cfg["disabled_symbols"]
