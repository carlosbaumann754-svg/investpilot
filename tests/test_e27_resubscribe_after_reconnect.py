"""Tests fuer R-A40 — E27 orderStatusEvent Re-Subscribe nach Reconnect.

Bug entdeckt waehrend Sprint-Tag-12 (22.05.2026):
Pending-Orders #179 (KO SELL) + #181 (ASML BUY) bleiben 19.6h auf
"PendingSubmit" im pending_orders.json haengen, OBWOHL Trade-History
beide als 'executed' markiert (Order-IDs in history) UND IBKR-Side
ib.openTrades() = 0 pending sagt.

Wurzel: `_e27_subscribed = True` Flag wurde einmal gesetzt aber nie
zurueck. Nach Pool-Invalidate (Stale-Connection-Schutz, Daily-Restart
03:00 UTC, Network-Glitch) bekam die NEUE ib-Instanz KEINE
orderStatusEvent-Subscription mehr. Old Subscription war tot weil ib-
Object disconnected. Resultat: orderStatusEvents (Filled/Cancelled)
kamen nicht beim Tracker an → pending_orders.json blieb auf
"PendingSubmit" haengen.

Fix R-A40: id(ib)-tracking statt globalem Boolean. Bei jeder neuen
ib-Instanz wird neu subscribed.

Source-based Tests (Module-Import wuerde pyotp brauchen).
"""

from pathlib import Path


IBKR_CLIENT_PY = Path(__file__).parent.parent / "app" / "ibkr_client.py"


def test_r_a40_subscribed_ib_id_attribute_exists():
    """Neue Instanz-Variable _e27_subscribed_ib_id muss in __init__ existieren."""
    src = IBKR_CLIENT_PY.read_text(encoding="utf-8")
    assert "self._e27_subscribed_ib_id = None" in src, (
        "R-A40: _e27_subscribed_ib_id init fehlt"
    )


def test_r_a40_maybe_subscribe_uses_id_check():
    """_maybe_subscribe_e27_events MUSS id(ib) checken statt nur Boolean."""
    src = IBKR_CLIENT_PY.read_text(encoding="utf-8")
    fn_start = src.index("def _maybe_subscribe_e27_events(")
    fn_body = src[fn_start:fn_start + 2500]
    assert "current_ib_id = id(ib)" in fn_body, (
        "R-A40: id(ib) lookup fehlt — alter Flag-Pattern wuerde Bug reintroducen"
    )
    assert "self._e27_subscribed_ib_id == current_ib_id" in fn_body, (
        "R-A40: id-vergleich-check fehlt"
    )


def test_r_a40_setter_updates_id_tracking():
    """Nach erfolgreichem Subscribe MUSS _e27_subscribed_ib_id auf id(ib) gesetzt werden."""
    src = IBKR_CLIENT_PY.read_text(encoding="utf-8")
    fn_start = src.index("def _maybe_subscribe_e27_events(")
    fn_body = src[fn_start:fn_start + 2500]
    assert "self._e27_subscribed_ib_id = current_ib_id" in fn_body, (
        "R-A40: id-state-update nach subscribe fehlt"
    )


def test_r_a40_backwards_compat_e27_subscribed_flag_kept():
    """Altes _e27_subscribed Bool-Flag bleibt aus Backwards-Compatibility
    (Tests die _e27_subscribed=True direkt setzen sollen weiter funktionieren).
    Aber Idempotenz-Check basiert jetzt auf id(ib), nicht auf dem Flag."""
    src = IBKR_CLIENT_PY.read_text(encoding="utf-8")
    fn_start = src.index("def _maybe_subscribe_e27_events(")
    fn_body = src[fn_start:fn_start + 2500]
    # Flag wird gesetzt aber NICHT mehr fuer Idempotenz-Check verwendet
    assert "self._e27_subscribed = True" in fn_body, (
        "Backwards-Compat-Flag bleibt"
    )
    # OLD pattern darf nicht zurueckkommen — Idempotenz-Check via id() not flag
    # (manual check: `if ... or self._e27_subscribed:` wuerde Bug reintroducen)
    # Konkret: das alte Pattern war "or self._e27_subscribed):" am Anfang
    assert "or self._e27_subscribed):" not in fn_body, (
        "R-A40 REGRESSION: alter Boolean-Idempotenz-Check ist wieder da"
    )


def test_r_a40_docstring_explains_bug():
    """Docstring MUSS R-A40 + Bug-Erklärung enthalten (gegen versehentliches
    Rueckgaengig-Machen)."""
    src = IBKR_CLIENT_PY.read_text(encoding="utf-8")
    fn_start = src.index("def _maybe_subscribe_e27_events(")
    fn_body = src[fn_start:fn_start + 2500]
    assert "R-A40" in fn_body, "R-A40 Tag fehlt in Docstring"
    assert "Pool-Invalidate" in fn_body or "Daily-Restart" in fn_body, (
        "Bug-Kontext fehlt in Docstring"
    )


def test_r_a40_log_includes_ib_id():
    """Log-Message bei neuem Subscribe muss ib_id enthalten — fuer Debug
    falls Pattern wieder auftritt (man kann dann beweisen dass Subscribe
    nach Reconnect passierte)."""
    src = IBKR_CLIENT_PY.read_text(encoding="utf-8")
    fn_start = src.index("def _maybe_subscribe_e27_events(")
    fn_body = src[fn_start:fn_start + 2500]
    assert "ib_id=%s" in fn_body, (
        "Log-Message ohne ib_id-Identifier — Debug-Visibility-Gap"
    )
