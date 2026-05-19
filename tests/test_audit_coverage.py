"""Tests fuer R-A12 Audit-Coverage-Reflection (Sprint-Tag-9, 19.05.2026).

Reflection-basiertes Health-Audit: jedes "AUDIT_REQUIRED"-Modul muss
AUDIT_METADATA-Block haben. Audit liest via AST (keine Side-Effects)
und generiert Cross-Validation-Checks automatisch.

Forcing Function: Pre-Commit-Hook + Self-Test #16 + Sa-Audit-Run.
"""

import ast
import pytest
from pathlib import Path


# ============================================================
# 1. AST-Discovery — Foundation
# ============================================================

def test_discover_module_metadata_finds_required_modules():
    """_discover_module_metadata findet alle AUDIT_REQUIRED-Module."""
    from app.health_audit import _discover_module_metadata, AUDIT_REQUIRED_MODULES
    meta = _discover_module_metadata()
    missing = AUDIT_REQUIRED_MODULES - set(meta.keys())
    assert not missing, (
        f"Required modules ohne AUDIT_METADATA: {sorted(missing)}. "
        "Loesung: AUDIT_METADATA-Block am Top der Datei hinzufuegen."
    )


def test_discover_no_side_effects_on_broken_module(tmp_path):
    """AST-Parse darf nicht crashen bei Syntax-Errors."""
    from app.health_audit import _extract_audit_metadata_via_ast
    broken = tmp_path / "broken.py"
    broken.write_text("this is not valid python !!! ", encoding="utf-8")
    result = _extract_audit_metadata_via_ast(broken)
    assert result is None  # graceful degrade


def test_discover_returns_none_for_module_without_marker(tmp_path):
    """Modul ohne AUDIT_METADATA-Konstante -> None."""
    from app.health_audit import _extract_audit_metadata_via_ast
    no_marker = tmp_path / "no_marker.py"
    no_marker.write_text(
        'import logging\nlog = logging.getLogger("X")\n# no AUDIT_METADATA here\n',
        encoding="utf-8")
    assert _extract_audit_metadata_via_ast(no_marker) is None


def test_discover_extracts_valid_dict(tmp_path):
    """Valider Marker wird korrekt geparsed."""
    from app.health_audit import _extract_audit_metadata_via_ast
    valid = tmp_path / "valid.py"
    valid.write_text(
        'AUDIT_METADATA = {\n'
        '    "purpose": "test module",\n'
        '    "added_in": "Sprint-Tag-9",\n'
        '    "state_files": ["test.json"],\n'
        '}\n', encoding="utf-8")
    meta = _extract_audit_metadata_via_ast(valid)
    assert meta == {
        "purpose": "test module",
        "added_in": "Sprint-Tag-9",
        "state_files": ["test.json"],
    }


# ============================================================
# 2. Schema-Validation
# ============================================================

def test_schema_validation_passes_for_valid_marker():
    from app.health_audit import _validate_metadata_schema
    meta = {
        "purpose": "ok",
        "added_in": "v1",
        "config_section": "demo_trading",
        "state_files": ["x.json"],
        "self_tests": ["tc_x"],
        "scheduler_hooks": ["_BG_X_S"],
        "health_check": "audit_health_check",
    }
    errors = _validate_metadata_schema("test", meta)
    assert errors == []


def test_schema_rejects_missing_purpose():
    from app.health_audit import _validate_metadata_schema
    meta = {"added_in": "v1"}
    errors = _validate_metadata_schema("test", meta)
    assert any("purpose" in e for e in errors)


def test_schema_rejects_wrong_type():
    from app.health_audit import _validate_metadata_schema
    meta = {"purpose": "ok", "added_in": "v1", "state_files": "not-a-list"}
    errors = _validate_metadata_schema("test", meta)
    assert any("state_files" in e for e in errors)


def test_schema_rejects_unknown_key():
    from app.health_audit import _validate_metadata_schema
    meta = {"purpose": "ok", "added_in": "v1", "random_unknown_key": "x"}
    errors = _validate_metadata_schema("test", meta)
    assert any("random_unknown_key" in e for e in errors)


# ============================================================
# 3. check_module_coverage Output-Quality
# ============================================================

def test_check_module_coverage_passes_when_all_required_have_markers():
    """Wenn alle Required-Module Marker haben, Master-Check passed."""
    from app.health_audit import check_module_coverage
    results = check_module_coverage()
    master = next((r for r in results
                   if r.name == "module_coverage_required_complete"), None)
    assert master is not None
    assert master.passed, master.message


def test_check_module_coverage_returns_per_module_checks():
    """Pro registriertem Modul werden mehrere Sub-Checks generiert."""
    from app.health_audit import check_module_coverage, _discover_module_metadata
    results = check_module_coverage()
    meta = _discover_module_metadata()
    # Per Modul werden mehrere Checks generiert (mind. master + 1 per Modul mit Config)
    assert len(results) > len(meta), (
        f"Erwarte >{len(meta)} checks (1 master + per-module subchecks), "
        f"got {len(results)}"
    )


# ============================================================
# 4. Pre-Commit-Hook Integration (Self-Test #16)
# ============================================================

def test_self_test_audit_coverage_complete_passes():
    """Self-Test #16 muss aktuell gruen sein."""
    from app.self_test import tc_audit_coverage_complete
    result = tc_audit_coverage_complete()
    assert result.passed, result.detail


def test_self_test_in_registry():
    """tc_audit_coverage_complete ist in ALL_TESTS registriert."""
    from app.self_test import ALL_TESTS, tc_audit_coverage_complete
    assert tc_audit_coverage_complete in ALL_TESTS


# ============================================================
# 5. AUDIT_REQUIRED_MODULES Konsistenz
# ============================================================

def test_audit_required_modules_all_exist():
    """Alle Module in AUDIT_REQUIRED_MODULES haben tatsaechlich
    eine app/*.py Datei (Tippfehler-Schutz)."""
    from app.health_audit import AUDIT_REQUIRED_MODULES
    app_dir = Path(__file__).parent.parent / "app"
    existing = {p.stem for p in app_dir.glob("*.py") if not p.stem.startswith("_")}
    missing = AUDIT_REQUIRED_MODULES - existing
    assert not missing, f"AUDIT_REQUIRED-Modul fehlt in app/: {sorted(missing)}"
