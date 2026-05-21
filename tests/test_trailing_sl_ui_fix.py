"""Tests fuer R-A33 — Trailing-SL Display-Bug.

Bug entdeckt waehrend Sprint-Tag-11 Block 3 Audit (21.05.2026):
Positionen-Tabelle zeigte "Trail SL: --" obwohl trailing_sl_state.json
einen aktiven sl_level fuer Position #8894 (KO) hatte (sl_level=81.0313,
entry=78.605, activated=12.05.).

Wurzel: 2 Bugs in einer Stelle:
  1. Backend-Endpoint /api/trailing-sl returnt `{"positions": [...]}`
     ABER Frontend-app.js liest `td.active` -> trailData immer leer.
  2. Frontend nutzt `pos.position_id` (Number) als Dict-Key,
     trailing_sl_state.json keys sind aber Strings ("8894").

Fix R-A33:
  - Frontend liest `td.positions || td.active` (Fallback fuer alte Server).
  - String-Cast: trailData[String(t.position_id)] und lookup mit
    String(pos.position_id).
  - Plus: Backend /api/portfolio reichert pos.trailing_sl direkt an
    (Belt-and-Suspenders falls /api/trailing-sl mal failt).

Source-based Tests (Module-Import wuerde pyotp brauchen, lokal nicht da).
"""

from pathlib import Path


APP_JS = Path(__file__).parent.parent / "web" / "static" / "app.js"
APP_PY = Path(__file__).parent.parent / "web" / "app.py"


def test_r_a33_frontend_reads_positions_field_not_active():
    """app.js MUSS td.positions lesen (mit Fallback td.active fuer alte Server)."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "td.positions || td.active" in src, (
        "R-A33 Frontend-Fix fehlt: muss td.positions || td.active nutzen"
    )


def test_r_a33_frontend_uses_string_key_for_position_id():
    """app.js MUSS String(pos.position_id) als Lookup-Key nutzen — sonst
    Type-Mismatch mit dict-keys aus trailing_sl_state.json."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "trailData[String(pos.position_id)]" in src, (
        "R-A33 String-Cast bei trail-lookup fehlt"
    )
    assert "String(t.position_id)" in src, (
        "R-A33 String-Cast beim Befuellen von trailData fehlt"
    )


def test_r_a33_backend_enriches_portfolio_positions():
    """Backend /api/portfolio reichert pos.trailing_sl direkt mit den
    Daten aus trailing_sl_state.json an. Belt-and-Suspenders falls
    Frontend nur /api/portfolio aufruft."""
    src = APP_PY.read_text(encoding="utf-8")
    # Look for the R-A33 backend enrichment block
    assert "R-A33" in src, "R-A33 backend tag fehlt"
    assert 'trailing_sl_state.json' in src, "trailing-state-file load fehlt"
    # Sucht das spezifische Pattern wo wir trailing_sl pro Position setzen
    assert '_pos["trailing_sl"]' in src, (
        "trailing_sl enrichment in /api/portfolio fehlt"
    )


def test_r_a33_backend_enrichment_includes_required_fields():
    """Backend liefert sl_level + entry_price + activated_at + updated_at."""
    src = APP_PY.read_text(encoding="utf-8")
    # The dict that gets assigned to _pos["trailing_sl"]
    assert '"sl_level": entry.get("sl_level")' in src
    assert '"entry_price": entry.get("entry_price")' in src
    assert '"activated_at": entry.get("activated")' in src
    assert '"updated_at": entry.get("updated")' in src


def test_r_a33_backend_handles_missing_state_file_gracefully():
    """Wenn trailing_sl_state.json fehlt oder leer ist, MUSS portfolio
    nicht crashen, sondern pos.trailing_sl = None setzen."""
    src = APP_PY.read_text(encoding="utf-8")
    # Look for the defensive try/except + None fallback
    assert "trailing_state = {}" in src, "Defensive {}-Default fehlt"
    assert "_pos[\"trailing_sl\"] = None" in src, (
        "None-Fallback wenn pid nicht im state-file"
    )


def test_r_a33_no_regression_existing_trail_render_path():
    """Existing render-path im app.js mit `trail.sl_level` darf nicht
    geaendert worden sein — nur die Lookup-Stelle."""
    src = APP_JS.read_text(encoding="utf-8")
    # Render-side bleibt: fmtUsd(trail.sl_level)
    assert "fmtUsd(trail.sl_level)" in src, (
        "Render-Pfad geaendert — sollte unveraendert sein"
    )
