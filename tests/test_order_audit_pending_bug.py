"""Tests fuer R-A32 — /api/order-audit Top-Level-Keys-Iterieren-Bug.

Bug entdeckt waehrend Sprint-Tag-11 Block 3 Audit (21.05.2026):
Frueher zeigte UI "1 Pending Order" mit Order-ID "pending" und leeren
"--" Feldern, weil der API-Code direkt durch pending_raw.items()
iterierte (also auch durch "version", "saved_at", "_audit" und die
"pending"-Sub-Dict selbst). Nach Purge der 9 Stale-Orders wurde
das Bug-Symptom deutlich: "2 Pending Orders" mit Order-IDs "pending"
und "_audit".

Source-based Tests (Module-Import wuerde pyotp brauchen, lokal nicht da).
"""

import re
from pathlib import Path


APP_PY = Path(__file__).parent.parent / "web" / "app.py"


def _extract_api_order_audit_body() -> str:
    """Extract the api_order_audit function body for inspection."""
    src = APP_PY.read_text(encoding="utf-8")
    start = src.find("async def api_order_audit(")
    assert start != -1, "api_order_audit endpoint not found"
    # Read ~4000 chars (function body)
    return src[start:start + 4000]


def test_r_a32_reads_pending_sub_dict_not_top_level():
    """Pending-Read MUSS pending_raw['pending'] nutzen, nicht pending_raw.items()."""
    body = _extract_api_order_audit_body()
    # Must extract the pending sub-dict
    assert 'pending_raw.get("pending"' in body, (
        "R-A32 Fix fehlt: muss pending_raw.get('pending', {}) nutzen "
        "statt direkt pending_raw.items() zu iterieren"
    )


def test_r_a32_iterates_correct_dict_not_top_level():
    """Iteration MUSS ueber pending_dict (= das sub-dict) gehen, nicht
    direkt ueber pending_raw — sonst landen 'version', 'saved_at', '_audit'
    als Phantom-Rows im UI."""
    body = _extract_api_order_audit_body()
    # Look for the for-loop in the new fixed version
    assert "for order_id, entry in pending_dict.items()" in body, (
        "R-A32: Iteration muss pending_dict.items() nutzen"
    )
    # The OLD buggy pattern must NOT exist anymore in this scope
    # (we check only the relevant function body, not other code)
    # Old buggy: "for order_id, entry in pending_raw.items()"
    assert "for order_id, entry in pending_raw.items()" not in body, (
        "R-A32 REGRESSION: alter Bug-Pfad (pending_raw.items()) ist zurueck"
    )


def test_r_a32_explanation_comment_present():
    """Bug-Fix-Doku muss im Code stehen damit es nicht versehentlich
    rueckgaengig gemacht wird."""
    body = _extract_api_order_audit_body()
    assert "R-A32" in body, "R-A32 Tag fehlt im Code"
    assert "Top-Level-Keys" in body or "version" in body, (
        "Bug-Explanation-Kommentar fehlt"
    )


def test_r_a32_filter_final_statuses_unchanged():
    """Die Final-Status-Filterung (Filled/Cancelled/etc.) bleibt unveraendert
    — gehoert nicht zum R-A32-Fix."""
    body = _extract_api_order_audit_body()
    assert '"Filled"' in body
    assert '"Cancelled"' in body
    assert '"Stale"' in body
    assert '"Rejected"' in body


def test_r_a32_handles_old_flat_format_gracefully():
    """Edge-Case: wenn pending_raw ein altes Flat-Format hat (vor _audit
    war drin), muss die neue Logik graceful 0 Pending zurueckgeben statt
    crashen. Test: pending_raw.get('pending', {}) defaultet zu {} wenn
    Key fehlt."""
    body = _extract_api_order_audit_body()
    # Defensive default {}: get('pending', {}) muss explicit default haben
    assert "get(\"pending\", {})" in body, (
        "Defensive {}-Default fehlt — alte Format-Files wuerden crashen"
    )


def test_r_a32_runbook_or_changelog_mentions_fix():
    """Fix muss dokumentiert sein (Audit-Trail)."""
    # Wir akzeptieren den Tag im Code-Kommentar als Dokumentation
    body = _extract_api_order_audit_body()
    assert "21.05.2026" in body or "R-A32" in body, (
        "Datum oder R-A32-Tag im Code fehlt"
    )
