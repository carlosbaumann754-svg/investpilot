"""R-B11 (07.06.2026) — Cutover-Infra-Fixes aus dem LLM-Deep-Audit.

C1: cutover_switch.sh verify greppt mode=real + account_type=live (nicht "live").
C2: session_watchdog defaultet host/port auf die Haupt-ibkr-Config (nicht 4004).
C3: /api/broker-status strippt account+equity fuer UNAUTH-Anfragen (kein Leak).
C5: _broker_status mode="unknown" bei Connect-Fail (nicht faelschlich "paper").
C6: Reconcile schreibt Heartbeat (reconcile_status.json), Gate #1 liest ihn.

web/app.py + scripts sind lokal nicht importierbar (pyotp/ib_insync) -> source-
basierte Tests fuer diese; ibkr_session_watchdog ist leicht -> echter Unit-Test.
"""
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── C5/C3: mode + account_type Ableitung ────────────────────────────────────
def test_c5_derive_mode_unknown_on_no_account():
    """C5: ohne Account (Connect-Fail) -> mode 'unknown', nicht 'paper'."""
    src = _read("web/app.py")
    start = src.find("def _derive_account_mode")
    assert start != -1, "_derive_account_mode fehlt"
    body = src[start:start + 1600]
    assert 'return "unknown", None' in body, "C5: unknown-Fallback fehlt"
    # Real-Account -> real/live, Paper-Account (DU/DUP) -> paper/paper
    assert '("paper" if is_paper else "real")' in body
    assert '("paper" if is_paper else "live")' in body
    assert '("DU", "DUP")' in body, "Paper-Prefix-Check fehlt"


def test_c3_public_strip_removes_account_and_equity():
    """C3: _public_broker_status entfernt account + equity, behaelt account_type."""
    src = _read("web/app.py")
    start = src.find("def _public_broker_status")
    assert start != -1, "_public_broker_status fehlt"
    body = src[start:start + 600]
    assert 'safe.pop("account", None)' in body
    assert 'safe.pop("equity", None)' in body
    # account_type/mode duerfen NICHT gestrippt werden
    assert 'pop("account_type"' not in body
    assert 'pop("mode"' not in body


def test_c3_endpoint_strips_for_unauthenticated():
    """C3: Endpoint ruft _request_is_authenticated + _public_broker_status."""
    src = _read("web/app.py")
    start = src.find("async def api_broker_status")
    body = src[start:start + 3500]
    assert "_request_is_authenticated(request)" in body, "Auth-Check fehlt"
    assert "_public_broker_status(result)" in body, "Strip-Aufruf fehlt"
    # request-Param muss injiziert werden
    assert "async def api_broker_status(request: Request)" in src


def test_c3_optional_auth_helper_does_not_raise():
    """C3: _request_is_authenticated faengt ab (try/except) -> wirft nie 401."""
    src = _read("web/app.py")
    start = src.find("async def _request_is_authenticated")
    assert start != -1, "_request_is_authenticated fehlt"
    body = src[start:start + 600]
    assert "try:" in body and "except Exception:" in body
    assert "return True" in body and "return False" in body


def test_c3c5_sync_returns_account_type():
    """C3/C5: _broker_status_sync nimmt account_type in die Antwort auf."""
    src = _read("web/app.py")
    start = src.find("def _broker_status_sync")
    body = src[start:start + 3000]
    assert '"account_type": account_type' in body
    assert "_derive_account_mode(broker_name, account, etoro_env)" in body


# ── C1: Cutover-Skript-Verify ───────────────────────────────────────────────
def test_c1_verify_uses_real_and_account_type():
    """C1: verify_live greppt mode=real + account_type=live + connected=true."""
    src = _read("scripts/cutover_switch.sh")
    vs = src.index("verify_live()")
    ve = src.index("\n}", vs)
    body = src[vs:ve]
    assert '"mode":"real"' in body
    assert '"account_type":"live"' in body
    assert '"connected":true' in body
    assert '"mode":"live"' not in body, "alter Bug-String darf nicht zurueck"


# ── C2: Session-Watchdog Port-Fallback ──────────────────────────────────────
def test_c2_watchdog_defaults_to_main_ibkr_port(monkeypatch):
    """C2: ohne expliziten Override defaultet ibkr_port auf config.ibkr.port."""
    import app.config_manager as cm
    monkeypatch.setattr(
        cm, "load_config",
        lambda: {"ibkr": {"host": "ib-gateway", "port": 4001}},
    )
    from app import ibkr_session_watchdog as w
    cfg = w._load_config()
    assert cfg["ibkr_port"] == 4001, "C2: Haupt-ibkr.port nicht uebernommen"
    assert cfg["ibkr_host"] == "ib-gateway"


def test_c2_explicit_override_still_wins(monkeypatch):
    """C2: explizites session_watchdog.ibkr_port ueberschreibt den Default."""
    import app.config_manager as cm
    monkeypatch.setattr(
        cm, "load_config",
        lambda: {"ibkr": {"port": 4001},
                 "session_watchdog": {"ibkr_port": 9999}},
    )
    from app import ibkr_session_watchdog as w
    cfg = w._load_config()
    assert cfg["ibkr_port"] == 9999


def test_c2_no_config_keeps_safe_default(monkeypatch):
    """C2: ohne ibkr-Config bleibt der DEFAULT_CONFIG-Wert (kein Crash)."""
    import app.config_manager as cm
    monkeypatch.setattr(cm, "load_config", lambda: {})
    from app import ibkr_session_watchdog as w
    cfg = w._load_config()
    assert cfg["ibkr_port"] == w.DEFAULT_CONFIG["ibkr_port"]


# ── C6: Reconcile-Heartbeat + Gate #1 ───────────────────────────────────────
def test_c6_reconcile_persists_status():
    """C6: ibkr_reconcile schreibt reconcile_status.json mit status+drift_count."""
    src = _read("scripts/ibkr_reconcile.py")
    assert "def _persist_reconcile_status" in src
    body = src[src.find("def _persist_reconcile_status"):][:900]
    assert 'save_json("reconcile_status.json"' in body
    assert '"status"' in body and '"drift_count"' in body
    # Wird im Erfolgs- UND im Fehlerpfad aufgerufen
    assert src.count("_persist_reconcile_status(") >= 2


def test_c6_gate1_reads_heartbeat():
    """C6: Readiness Gate #1 liest reconcile_status.json statt hartkodiert green."""
    src = _read("web/app.py")
    gs = src.find("Gate #1: Reconciliation")
    body = src[gs:gs + 1800]
    assert 'load_json' in body and 'reconcile_status.json' in body
    assert '"passive"' not in body, "Gate #1 darf nicht mehr hartkodiert passive sein"
    # echte Status-Verzweigung
    assert '"OK"' in body and '"ERROR"' in body
