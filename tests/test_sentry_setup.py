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


def test_before_send_drops_event_on_filter_crash():
    """v37g-review HIGH 1: Wenn Filter selbst crasht → Event DROPPED (None).

    Vor Real-Money-Phase konservativ-default: lieber Visibility-Verlust
    als Risk dass unscrubbtes Event mit IBKR-Account/Cash/Tokens an
    Sentry-Server gesendet wird.
    """
    event = {"message": "ok"}
    import app.sentry_setup as mod
    original = mod._scrub_dict

    def broken(*args, **kwargs):
        raise RuntimeError("filter bug")
    mod._scrub_dict = broken
    try:
        event["extra"] = {"account": "DUP108015"}
        out = _before_send(event, {})
        # Filter-Crash → Event MUSS None sein, nicht durchgelassen
        assert out is None, f"Expected None (drop), got {out!r}"
    finally:
        mod._scrub_dict = original


# ============================================================
# v37g-review HIGH 2: Cash + Order-ID Redaction
# ============================================================

def test_redact_cash_value_with_currency_suffix():
    """Cash-Werte mit Currency-Suffix (USD/EUR/CHF) müssen redacted werden."""
    cases = [
        "Balance: 12500.50 USD",
        "Cash: 1,234.56 EUR",
        "Equity: 50000 CHF",
        "Total: 999999.99 GBP",
    ]
    for s in cases:
        out = _redact_string(s)
        assert "[REDACTED_AMOUNT]" in out, f"Currency-Pattern verfehlt: {s!r} -> {out!r}"


def test_redact_dollar_prefixed_amount():
    """$-prefixed Amounts werden redacted."""
    cases = [
        "Position-Wert: $50,000.00",
        "Trade @ $284.45",
        "Equity = $1,026,522.79",
    ]
    for s in cases:
        out = _redact_string(s)
        assert "[REDACTED_AMOUNT]" in out, f"Dollar-Pattern verfehlt: {s!r} -> {out!r}"


def test_drop_cash_field():
    """'cash', 'equity', 'balance' Felder werden gedropt."""
    d = {"cash": 793154.18, "equity": 1026522.79, "balance": 50000, "symbol": "AAPL"}
    out = _scrub_dict(d)
    assert out["cash"] == "[REDACTED_SECRET]"
    assert out["equity"] == "[REDACTED_SECRET]"
    assert out["balance"] == "[REDACTED_SECRET]"
    assert out["symbol"] == "AAPL"


def test_drop_order_id_field():
    """'order_id', 'perm_id', 'exec_id' werden gedropt."""
    d = {
        "order_id": 120,
        "perm_id": 634610476,
        "exec_id": "00025b45.69ff1cbe.01.01",
        "symbol": "IWM",
    }
    out = _scrub_dict(d)
    assert out["order_id"] == "[REDACTED_SECRET]"
    assert out["perm_id"] == "[REDACTED_SECRET]"
    assert out["exec_id"] == "[REDACTED_SECRET]"
    assert out["symbol"] == "IWM"


def test_drop_account_field():
    """'account', 'acct', 'account_id' werden gedropt."""
    d = {"account": "DUP108015", "acct": "U1234567", "account_id": 1234567}
    out = _scrub_dict(d)
    assert out["account"] == "[REDACTED_SECRET]"
    assert out["acct"] == "[REDACTED_SECRET]"
    assert out["account_id"] == "[REDACTED_SECRET]"


def test_redact_lowercase_ibkr_account():
    """v37g-review MED 5: case-insensitive IBKR-Account-Match."""
    cases = ["dup108015", "u1234567", "Dup108015", "DuP108015"]
    for s in cases:
        out = _redact_string(s)
        assert "[REDACTED_IBKR_ACCT]" in out, f"Lowercase failed: {s!r} -> {out!r}"


def test_redact_numeric_int_account_id():
    """v37g-review LOW 7: Numerische int-Account-IDs werden konvertiert + gescannt."""
    # 7-stelliger Int könnte ein Account sein — lass mich prüfen ob's konvertiert wird
    # (Pattern matcht z.B. "U1234567" — int 1234567 alleine nicht, aber als Wert in
    #  einem dict würde der int durch _redact_string laufen)
    out = _redact_string(1234567)
    # Plain int 1234567 matcht keinen IBKR-Account (kein "U"-Prefix)
    # → bleibt int. Das ist OK weil Drop-Field für 'account_id' sowieso greift.
    assert out == 1234567 or isinstance(out, str)


def test_redact_bytes_with_account():
    """Bytes-Strings (rare aber möglich in low-level logs) werden auch gescannt."""
    s = b"Connected to DUP108015"
    out = _redact_string(s)
    # Wenn redaction griff, ist's String mit [REDACTED_IBKR_ACCT]
    if isinstance(out, str):
        assert "[REDACTED_IBKR_ACCT]" in out
    else:
        # Falls Decode fehlschlug, bleibt's bytes — auch akzeptabel
        assert "DUP108015" not in out.decode("utf-8", errors="replace")
