"""v37g Sentry-Setup-Tests.

Verifiziert:
- Feature-Flag-Schutz (no-op wenn deaktiviert)
- DSN-Schutz (no-op wenn env-var fehlt)
- PII-Filter (IBKR-Accounts, API-Tokens, Emails redacted)
- Drop-Fields (Secrets als [REDACTED_SECRET])
- Crash-Resilience (Init-Failure crasht Bot nicht)
"""
import pytest

from app.sentry_setup import (
    setup_sentry,
    _redact_string,
    _scrub_dict,
    _before_send,
    _DROP_FIELDS,
)


# ============================================================
# Setup-Behavior
# ============================================================

def test_setup_returns_false_when_disabled(monkeypatch):
    """Wenn config.sentry.enabled=false: skip ohne Crash."""
    monkeypatch.setattr("app.sentry_setup._load_sentry_config",
                        lambda: {"enabled": False})
    monkeypatch.setattr("app.sentry_setup._get_dsn", lambda: None)

    assert setup_sentry() is False


def test_setup_returns_false_when_dsn_missing(monkeypatch):
    """Wenn enabled=true aber DSN fehlt: skip mit Warning, kein Crash."""
    monkeypatch.setattr("app.sentry_setup._load_sentry_config",
                        lambda: {"enabled": True})
    monkeypatch.setattr("app.sentry_setup._get_dsn", lambda: None)

    assert setup_sentry() is False


def test_setup_resilient_to_init_failure(monkeypatch):
    """Wenn sentry_sdk-Import oder -Init fehlschlaegt: return False, kein Crash.

    Lokal ist sentry-sdk vermutlich nicht installiert → ModuleNotFoundError
    im setup_sentry's local import. Der äußere except-Block faengt das ab
    und gibt False zurueck. Das ist die kritische Resilience: VPS-Bot soll
    auch dann starten wenn Sentry-SDK fehlt oder Sentry-Server unerreichbar.
    """
    monkeypatch.setattr("app.sentry_setup._load_sentry_config",
                        lambda: {"enabled": True})
    monkeypatch.setattr("app.sentry_setup._get_dsn",
                        lambda: "https://fake@sentry.io/123")

    # Soll NICHT crashen, egal ob sentry_sdk installed oder nicht.
    # Wenn installed: fake DSN führt zu Sentry-Init-Fehler → False
    # Wenn fehlt: ImportError → äußerer except → False
    result = setup_sentry()
    assert result is False


# ============================================================
# PII-Redaction
# ============================================================

def test_redact_ibkr_paper_account():
    """IBKR Paper-Account-Format DU* darf nicht durchsickern."""
    s = "Connected to account DUP108015 successfully"
    out = _redact_string(s)
    assert "DUP108015" not in out
    assert "[REDACTED_IBKR_ACCT]" in out


def test_redact_ibkr_real_account():
    """IBKR Real-Account-Format U******* darf nicht durchsickern."""
    s = "Real-money account U1234567 transferred funds"
    out = _redact_string(s)
    assert "U1234567" not in out
    assert "[REDACTED_IBKR_ACCT]" in out


def test_redact_email():
    """Email-Adressen werden redacted."""
    s = "Contact carlos@example.com for support"
    out = _redact_string(s)
    assert "carlos@example.com" not in out
    assert "[REDACTED_EMAIL]" in out


def test_redact_long_token():
    """Lange alphanumerische Strings (wahrscheinlich Tokens) werden truncated."""
    long_token = "a" * 50  # 50 chars = matched by pattern
    s = f"Auth: {long_token}"
    out = _redact_string(s)
    assert long_token not in out
    assert "[REDACTED]" in out


def test_redact_keeps_short_strings():
    """Kurze Strings (z.B. Symbole) bleiben unverändert."""
    s = "Trade AAPL filled at 285.50"
    out = _redact_string(s)
    assert "AAPL" in out
    assert "285.50" in out


# ============================================================
# Dict-Scrubbing
# ============================================================

def test_scrub_drops_secret_fields():
    """Secret-Felder werden auf [REDACTED_SECRET] gesetzt."""
    d = {
        "user_key": "abc123def456",
        "api_token": "verysecret",
        "symbol": "AAPL",  # nicht-secret bleibt
    }
    out = _scrub_dict(d)
    assert out["user_key"] == "[REDACTED_SECRET]"
    assert out["api_token"] == "[REDACTED_SECRET]"
    assert out["symbol"] == "AAPL"


def test_scrub_handles_nested_dicts():
    """Verschachtelte Dicts werden rekursiv gescrubt."""
    d = {
        "config": {
            "alerts": {
                "pushover_user_key": "secret123",
                "enabled": True,
            }
        }
    }
    out = _scrub_dict(d)
    assert out["config"]["alerts"]["pushover_user_key"] == "[REDACTED_SECRET]"
    assert out["config"]["alerts"]["enabled"] is True


def test_scrub_handles_lists():
    """Listen werden auch gescrubt."""
    d = {
        "trades": [
            {"symbol": "AAPL", "api_token": "secret"},
            {"symbol": "TSLA", "api_token": "another"},
        ]
    }
    out = _scrub_dict(d)
    assert out["trades"][0]["api_token"] == "[REDACTED_SECRET]"
    assert out["trades"][1]["api_token"] == "[REDACTED_SECRET]"
    assert out["trades"][0]["symbol"] == "AAPL"


def test_scrub_max_depth_protection():
    """Schutz vor circular refs / deeply nested structures."""
    deep = {"level": 1}
    cur = deep
    for i in range(20):
        cur["nested"] = {"level": i + 2}
        cur = cur["nested"]

    # Soll nicht stack overflow werfen
    out = _scrub_dict(deep)
    assert isinstance(out, dict)


# ============================================================
# before_send Hook
# ============================================================

def test_before_send_redacts_message():
    """Event-message mit IBKR-Account wird redacted."""
    event = {"message": "Connected to DUP108015"}
    out = _before_send(event, {})
    assert "DUP108015" not in out["message"]


def test_before_send_redacts_extra():
    """Event-extra-context wird gescrubt."""
    event = {
        "extra": {
            "account": "DUP108015",
            "api_token": "secret",
        }
    }
    out = _before_send(event, {})
    assert out["extra"]["api_token"] == "[REDACTED_SECRET]"


def test_before_send_redacts_exception_local_vars():
    """Local-Variables in Stack-Frames werden gescrubt."""
    event = {
        "exception": {
            "values": [{
                "stacktrace": {
                    "frames": [{
                        "vars": {
                            "ibkr_password": "supersecret",
                            "user_key": "abc",
                            "symbol": "AAPL",
                        }
                    }]
                }
            }]
        }
    }
    out = _before_send(event, {})
    frame_vars = out["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
    assert frame_vars["ibkr_password"] == "[REDACTED_SECRET]"
    assert frame_vars["user_key"] == "[REDACTED_SECRET]"
    assert frame_vars["symbol"] == "AAPL"


def test_before_send_resilient_to_filter_error():
    """Wenn Filter selbst crasht: Event durchlassen, nicht None."""
    # Event mit invalid type → Scrub-Exception möglich
    event = {"message": "ok"}
    # Mocken um Scrub-Fehler zu erzwingen
    import app.sentry_setup as mod
    original = mod._scrub_dict

    def broken(*args, **kwargs):
        raise RuntimeError("filter bug")
    mod._scrub_dict = broken
    try:
        # Setze extra damit Scrub-Path getriggert wird
        event["extra"] = {"x": 1}
        out = _before_send(event, {})
        # Trotz Scrub-Fehler: Event nicht None, message unverändert
        assert out is not None
    finally:
        mod._scrub_dict = original
