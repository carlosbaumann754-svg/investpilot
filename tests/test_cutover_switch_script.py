"""Tests fuer R-A31 Cutover-Switch-Script.

Statisch (source-based): script ist Bash, kein Python-Modul. Wir validieren
Struktur + Schluessel-Logik damit das Script am Cutover-Tag funktioniert.

Live-Verifikation: bash scripts/cutover_switch.sh --dry-run auf VPS.
"""

import re
import stat
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "cutover_switch.sh"


def test_script_exists_and_executable_bit_intended():
    """Script existiert (chmod +x via deploy oder bash $script Aufruf)."""
    assert SCRIPT.exists(), f"Script fehlt: {SCRIPT}"
    src = SCRIPT.read_text(encoding="utf-8")
    # Erste Zeile = shebang
    assert src.startswith("#!/usr/bin/env bash"), "Shebang fehlt"
    # set -euo pipefail = Bash-Sicherheit
    assert "set -euo pipefail" in src, "set -euo pipefail fehlt (kein Fail-Fast)"


def test_script_has_all_required_sections():
    """Script muss alle 5 Workflow-Phasen + Rollback + Helpers haben."""
    src = SCRIPT.read_text(encoding="utf-8")
    required = [
        "pre_checks()",
        "do_backup()",
        "show_diff()",
        "apply_switch()",
        "restart_containers()",
        "verify_live()",
        "rollback()",
        "main()",
    ]
    for fn in required:
        assert fn in src, f"Funktion fehlt: {fn}"


def test_script_handles_dry_run_and_force_and_rollback_flags():
    """CLI-Flags: --dry-run, --force, --rollback."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "--dry-run" in src
    assert "--force" in src
    assert "--rollback" in src


def test_script_paths_match_vps_layout():
    """Hardcoded-Pfade muessen mit VPS-Realitaet uebereinstimmen.

    Wenn du diese verschiebst, hier auch updaten — sonst silent-fail am
    Cutover-Tag.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert 'IBG_COMPOSE="/opt/ib-gateway/docker-compose.yml"' in src
    assert 'IP_COMPOSE="/opt/investpilot/docker-compose.vps.yml"' in src
    assert 'IP_CONFIG="/opt/investpilot/data/config.json"' in src


def test_script_uses_correct_socat_bridge_ports():
    """Paper-Port 4004, Live-Port 4001 (gnzsnz/ib-gateway image-Konvention)."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "PORT_PAPER=4004" in src
    assert "PORT_LIVE=4001" in src


def test_script_does_backup_before_change():
    """Backup-Step MUSS vor apply_switch laufen (Rollback-Pfad)."""
    src = SCRIPT.read_text(encoding="utf-8")
    # In main(): backup vor apply, apply vor restart, restart vor verify
    main_start = src.index("main()")
    main_body = src[main_start:main_start + 2000]
    backup_pos = main_body.index("do_backup")
    apply_pos = main_body.index("apply_switch")
    restart_pos = main_body.index("restart_containers")
    verify_pos = main_body.index("verify_live")
    assert backup_pos < apply_pos < restart_pos < verify_pos, (
        "Reihenfolge falsch — backup muss vor apply, apply vor restart, "
        "restart vor verify."
    )


def test_script_auto_rollback_on_verify_fail():
    """Bei verify-fail MUSS automatischer Rollback laufen."""
    src = SCRIPT.read_text(encoding="utf-8")
    # Look for the fallback rollback call in main flow
    assert re.search(r"verify_live.*\n.*rollback", src, re.DOTALL), (
        "Auto-Rollback bei verify-fail fehlt"
    )


def test_script_requires_root():
    """Cutover braucht root (docker compose + /opt/* writes)."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "require_root" in src
    assert "EUID" in src


def test_script_changes_both_files_atomically():
    """Step 1 = ib-gateway compose, Step 2 = config.json — beide muessen
    in apply_switch() vorkommen."""
    src = SCRIPT.read_text(encoding="utf-8")
    apply_start = src.index("apply_switch()")
    apply_end = src.index("\n}", apply_start)
    apply_body = src[apply_start:apply_end]
    # Step 1: sed TRADING_MODE
    assert "TRADING_MODE=paper" in apply_body
    assert "TRADING_MODE=live" in apply_body
    # Step 2: config.json port via python
    assert "$IP_CONFIG" in apply_body
    assert "$PORT_LIVE" in apply_body


def test_script_verifies_live_account_prefix():
    """Verify-Logik muss U-Account-Praefix checken (nicht DU = paper)."""
    src = SCRIPT.read_text(encoding="utf-8")
    verify_start = src.index("verify_live()")
    verify_end = src.index("\n}", verify_start)
    verify_body = src[verify_start:verify_end]
    assert '"mode":"live"' in verify_body, "Mode-Check fehlt"
    assert 'U[0-9]' in verify_body, "U-Praefix-Check fehlt (DU... waere paper)"


def test_runbook_mentions_both_steps_atomically():
    """CUTOVER_RUNBOOK §1 muss BEIDE Steps + Script-Pfad erwaehnen."""
    runbook = Path(__file__).parent.parent / "docs" / "CUTOVER_RUNBOOK.md"
    src = runbook.read_text(encoding="utf-8")
    # Step 1 + Step 2 + Script-Verweis muessen vorhanden sein
    assert "Step 1" in src and "TRADING_MODE" in src
    assert "Step 2" in src and "ibkr.port" in src
    assert "cutover_switch.sh" in src, "Script-Verweis im Runbook fehlt"
    assert "R-A31" in src, "R-A31-Tag im Runbook fehlt"
