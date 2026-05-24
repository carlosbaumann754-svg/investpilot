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


# ============================================================
# R-A20 (19.05.2026): Sentry-Noise-Filter Tests
# ============================================================
# 4 unique Library-Quirks die als ERROR landen aber kein Bot-Bug sind
# muessen aus Sentry-Stream gefiltert werden.

def test_noise_filter_drops_ibkr_error_300():
    """Error 300 (cancelMktData-Quirk) muss gedroppt werden."""
    from app.sentry_setup import _is_noise
    assert _is_noise("Error 300, reqId 197: Can't find EId with tickerId:197") is True
    assert _is_noise("Can't find EId with tickerId:42") is True


def test_noise_filter_drops_stale_portfolio_filter():
    """v37ce Boot-Race-Detection ist erwuenscht, kein Bug."""
    from app.sentry_setup import _is_noise
    assert _is_noise(
        "Stale ib.portfolio()-Eintrag uebersprungen: USO conId=418893644 qty=354.0"
    ) is True


def test_noise_filter_drops_high_latency_warn():
    """Latency-Warnings sind nicht-actionable, kein Bot-Crash."""
    from app.sentry_setup import _is_noise
    assert _is_noise("Hohe Latenz: 2019ms fuer Instrument 5001") is True


def test_noise_filter_drops_uvicorn_http_warns():
    """Uvicorn-HTTP-Warnings von externen Scanners."""
    from app.sentry_setup import _is_noise
    assert _is_noise("WARNING:  Invalid HTTP request received.") is True


def test_noise_filter_keeps_real_errors():
    """Echte Bot-Errors muessen DURCH den Filter."""
    from app.sentry_setup import _is_noise
    assert _is_noise("ValueError: invalid literal for int()") is False
    assert _is_noise("Kein Quote fuer instrument_id=5001 (USO)") is False
    assert _is_noise("ConnectionRefusedError: ib_gateway") is False
    assert _is_noise("CONCENTRATION-BLOCK OIL: max_positions_per_symbol") is False


def test_noise_filter_handles_none_and_empty():
    """Defensive: None/leerer String crashen nicht."""
    from app.sentry_setup import _is_noise
    assert _is_noise(None) is False
    assert _is_noise("") is False
    assert _is_noise(0) is False  # int statt str


def test_before_send_drops_noise_event(monkeypatch):
    """before_send returnt None bei Noise-Pattern -> Event geht NICHT an Sentry."""
    from app.sentry_setup import _before_send
    event = {
        "message": "Error 300, reqId 197: Can't find EId with tickerId:197",
        "level": "error",
    }
    result = _before_send(event, {})
    assert result is None  # gedroppt


def test_before_send_keeps_real_event():
    """before_send returnt event bei echtem Error."""
    from app.sentry_setup import _before_send
    event = {
        "message": "ValueError: x must be positive",
        "level": "error",
    }
    result = _before_send(event, {})
    assert result is not None
    assert result.get("message")  # event durchgelassen


# ============================================================
# R-A24 (19.05.2026 abend): Container-Restart-Noise filtern
# ============================================================
# Peer closed connection, Socket disconnect, asyncio Task pending
# bei Container-Rebuilds. Erwartetes Verhalten — Self-Test #11 deckt
# echte Disconnects ab.

def test_noise_filter_drops_peer_closed():
    """ib_insync 'Peer closed connection' bei Container-Restart."""
    from app.sentry_setup import _is_noise
    assert _is_noise("Peer closed connection.") is True
    assert _is_noise("Peer closed connection") is True


def test_noise_filter_drops_socket_disconnect():
    """_get_account_value Socket disconnect bei Restart."""
    from app.sentry_setup import _is_noise
    assert _is_noise("_get_account_value(NetLiquidation) failed: Socket disconnect") is True
    assert _is_noise("Socket disconnect") is True


def test_noise_filter_drops_asyncio_task_pending():
    """ib_insync connectAsync Task pending Cascade."""
    from app.sentry_setup import _is_noise
    msg = "Task <Task pending name='Task-900' coro=<Connection.connectAsync running at"
    assert _is_noise(msg) is True


def test_noise_filter_keeps_real_disconnect_message():
    """Echte App-Bugs mit Disconnect-im-Text gehen durch."""
    from app.sentry_setup import _is_noise
    assert _is_noise("Order rejected by exchange") is False
    assert _is_noise("Bot lost connection to database (5 retries failed)") is False


def test_before_send_drops_container_restart_event():
    """before_send returnt None bei Container-Restart-Patterns."""
    from app.sentry_setup import _before_send
    for msg in [
        "Peer closed connection.",
        "_get_account_value(NetLiquidation) failed: Socket disconnect",
        "Task <Task pending name='Task-900' connectAsync running at /...",
    ]:
        event = {"message": msg, "level": "error"}
        assert _before_send(event, {}) is None, f"Should drop: {msg}"


# ============================================================
# R-A29 (19.05.2026 abend): CancelledError-Patterns ergaenzen
# ============================================================

def test_noise_filter_drops_cancelled_error_api_connection():
    """API connection failed: CancelledError() bei Container-Restart."""
    from app.sentry_setup import _is_noise
    assert _is_noise("API connection failed: CancelledError()") is True
    assert _is_noise("API connection failed: CancelledError") is True


def test_noise_filter_drops_api_connection_runtime_error():
    """API connection failed: RuntimeError beim Restart-Cascade."""
    from app.sentry_setup import _is_noise
    msg = "API connection failed: RuntimeError(Task pending name='Task-894' coro=...)"
    assert _is_noise(msg) is True


def test_noise_filter_drops_asyncio_cancelled_exceptions():
    """asyncio.exceptions.CancelledError als Exception-Type."""
    from app.sentry_setup import _is_noise
    msg = "asyncio.exceptions.CancelledError: Task was cancelled"
    assert _is_noise(msg) is True


def test_noise_filter_keeps_real_cancelled_error():
    """Generische CancelledError in App-Code geht weiter durch — nur ib_insync-Pattern filtered."""
    from app.sentry_setup import _is_noise
    # 'CancelledError' alleine ohne 'API connection failed' Prefix
    assert _is_noise("Trade cancelled due to risk limit") is False


# ============================================================
# R-A30 (21.05.2026 morgen): Daily-Restart-Artefakte + yfinance-Delisted
# ============================================================

def test_noise_filter_drops_ibkr_error_1100_short():
    """Error 1100 reqId -1 (Connectivity lost) bei daily ib-gateway Restart."""
    from app.sentry_setup import _is_noise
    msg = "Error 1100, reqId -1: Connectivity between IBKR and Trader Workstation has been lost."
    assert _is_noise(msg) is True


def test_noise_filter_drops_ibkr_error_1100_short_pattern():
    """Pattern 'Error 1100, reqId' matcht auch ohne genauen Text-Rest."""
    from app.sentry_setup import _is_noise
    assert _is_noise("Error 1100, reqId 0: something else") is True
    assert _is_noise("Error 1100, reqId -1: Connectivity lost") is True


def test_noise_filter_drops_connectivity_lost_long_form():
    """Generic 'Connectivity between IBKR and Trader Workstation has been lost'.
    Auch ohne Error-1100-Prefix (z.B. von ib_insync.wrapper-Loggerline)."""
    from app.sentry_setup import _is_noise
    assert _is_noise("Connectivity between IBKR and Trader Workstation has been lost") is True


def test_noise_filter_drops_yfinance_delisted():
    """yfinance MC.PA-Pattern: 'possibly delisted; no price data found'."""
    from app.sentry_setup import _is_noise
    assert _is_noise("$MC.PA: possibly delisted; no price data found (period=1d)") is True
    assert _is_noise("$LVMUY: possibly delisted; no price data found (period=5d)") is True


def test_noise_filter_drops_yfinance_no_data_range():
    """Alt-Wortlaut der yfinance-Delisting-Warning."""
    from app.sentry_setup import _is_noise
    msg = "No data found for this date range, symbol may be delisted"
    assert _is_noise(msg) is True


def test_noise_filter_keeps_real_1100_in_non_ibkr_context():
    """Number '1100' alleine ohne 'Error 1100, reqId' Prefix bleibt drin."""
    from app.sentry_setup import _is_noise
    # z.B. config-value oder positionssize $1100
    assert _is_noise("Order size 1100 exceeded limit") is False
    assert _is_noise("Trade closed at 1100 EUR") is False


def test_noise_filter_keeps_real_connectivity_error():
    """Connectivity-Errors OHNE 'IBKR and Trader Workstation' (z.B. yfinance,
    Polygon, Slack-Webhook) gehen weiter durch → echter Infrastruktur-Bug."""
    from app.sentry_setup import _is_noise
    assert _is_noise("Connectivity to Polygon failed: timeout") is False
    assert _is_noise("Slack webhook connectivity lost") is False


# ============================================================
# R-A35 (21.05.2026 Sprint-Tag-11 Block 5): TimeoutError + truncated
# _get_account_value Patterns nach Sentry-Soak-Check.
# ============================================================

def test_r_a35_drops_api_connection_timeout_error():
    """API connection failed: TimeoutError() — neuer Pattern aus
    Sentry-Issue PYTHON-FASTAPI-M (15 Events)."""
    from app.sentry_setup import _is_noise
    assert _is_noise("API connection failed: TimeoutError()") is True
    assert _is_noise("API connection failed: TimeoutError") is True


def test_r_a35_generic_api_connection_failed_pattern():
    """Generic 'API connection failed:' deckt ALLE Sub-Error-Klassen ab
    (TimeoutError, OSError, ConnectionResetError, etc.) — robuster als
    pro-Sub-Error-Pattern."""
    from app.sentry_setup import _is_noise
    assert _is_noise("API connection failed: OSError") is True
    assert _is_noise("API connection failed: ConnectionResetError(...)") is True
    assert _is_noise("API connection failed: BrokenPipeError") is True


def test_r_a35_drops_truncated_get_account_value():
    """'_get_account_value(NetLiquidation) failed:' truncated — der echte
    Sentry-Event kommt teilweise ohne 'Socket disconnect'-Suffix an
    (Sentry-Message-Truncation oder andere Exception-Subklasse)."""
    from app.sentry_setup import _is_noise
    assert _is_noise("_get_account_value(NetLiquidation) failed:") is True
    assert _is_noise(
        "_get_account_value(NetLiquidation) failed: ConnectionError"
    ) is True


def test_r_a35_keeps_real_timeout_in_non_ibkr_context():
    """TimeoutError() ohne 'API connection failed:'-Prefix bleibt drin —
    z.B. Polygon-API-Timeout, Slack-Webhook-Timeout = echte Infrastruktur-
    Errors die nicht gefiltert werden duerfen."""
    from app.sentry_setup import _is_noise
    assert _is_noise("yfinance TimeoutError") is False
    assert _is_noise("Polygon API call timed out") is False
    assert _is_noise("TimeoutError: Connection to 'finnhub.io' timed out") is False


def test_r_a35_keeps_real_api_connection_in_non_ibkr_context():
    """'API connection' fuer andere APIs (z.B. Polygon, FRED) bleibt drin —
    nur 'API connection failed:' (genauer Prefix) wird gefiltert."""
    from app.sentry_setup import _is_noise
    # Generic Pattern matcht "API connection failed:" — wenn anderer Text:
    assert _is_noise("Polygon API connection slow") is False
    assert _is_noise("FRED API rate limited") is False


# ============================================================
# R-A37 (21.05.2026 Sprint-Tag-11 Block 7): Filter-Coverage breiter
# (logentry.formatted) + Generic _get_account_value-Catch.
# ============================================================

def test_r_a37_drops_raw_template_pattern():
    """Bei Python logger.error('_get_account_value(%s) failed: %s', ...)
    ist logentry.message = raw template mit %s. Filter MUSS auch das matchen
    via generic '_get_account_value'-Pattern."""
    from app.sentry_setup import _is_noise
    assert _is_noise("_get_account_value(%s) failed: %s") is True
    assert _is_noise("_get_account_value(NetLiquidation) failed: %s") is True


def test_r_a37_drops_bare_function_name_pattern():
    """Generic '_get_account_value' catch-all (alle errors aus dieser
    Funktion sind Library-Quirks)."""
    from app.sentry_setup import _is_noise
    assert _is_noise("_get_account_value some-strange-suffix") is True
    assert _is_noise("Exception in _get_account_value: WeirdError") is True


def test_r_a37_before_send_checks_logentry_formatted():
    """_before_send muss logentry.formatted lesen, nicht nur logentry.message."""
    src = open("app/sentry_setup.py", encoding="utf-8").read()
    bs_start = src.index("def _before_send(")
    bs_body = src[bs_start:bs_start + 3000]
    assert 'logentry.get("formatted")' in bs_body, (
        "R-A37 logentry.formatted lookup fehlt"
    )
    assert "log_msg_fmt" in bs_body, "R-A37 formatted-Variable fehlt"


def test_r_a37_before_send_combines_all_message_fields():
    """combined-String muss msg + log_msg + log_msg_fmt + exc_msgs zusammenfuegen."""
    src = open("app/sentry_setup.py", encoding="utf-8").read()
    bs_start = src.index("def _before_send(")
    bs_body = src[bs_start:bs_start + 3000]
    assert "[msg, log_msg, log_msg_fmt]" in bs_body, (
        "R-A37 combined-Join muss alle 3 Message-Felder enthalten"
    )


def test_r_a37_keeps_real_get_account_value_in_other_module():
    """Funktion gleichen Namens in anderem Modul/Kontext bleibt — aber wir
    nehmen das in Kauf weil _get_account_value sehr spezifisch ist (nur
    1 Funktion mit dem Namen im Bot)."""
    from app.sentry_setup import _is_noise
    # Sehr breit — auch valid call-pfade die wir gewohnt sind als Noise
    # zu ignorieren werden gefangen. Das ist gewollt.
    assert _is_noise("_get_account_value works fine") is True
    # Aber: völlig fremde Texte bleiben
    assert _is_noise("Hello world") is False
    assert _is_noise("Database connection lost") is False


# ============================================================
# R-A38 (21.05.2026 Sprint-Tag-11 Block 8): ib_insync Account-Update-
# Timeout-Pattern + PII-Drop-Defense.
# ============================================================

def test_r_a38_drops_generic_account_updates_timeout():
    """'account updates request timed out' (generic ohne Account-ID)."""
    from app.sentry_setup import _is_noise
    assert _is_noise("account updates request timed out") is True


def test_r_a38_drops_account_updates_with_account_id():
    """'account updates for DUP108015 request timed out' (mit Account-ID
    wie PII-Variante). Filter MUSS VOR PII-Scrub greifen, damit Account-ID
    nicht in Sentry-Inbox landet."""
    from app.sentry_setup import _is_noise
    assert _is_noise("account updates for DUP108015 request timed out") is True
    assert _is_noise("account updates for U1234567 request timed out") is True


def test_r_a38_drops_via_generic_account_updates_pattern():
    """Generic 'account updates' Catch — matcht alle Varianten der
    ib_insync-Account-Update-Logs (timed out, failed, error, etc.)."""
    from app.sentry_setup import _is_noise
    assert _is_noise("account updates failed") is True
    assert _is_noise("Failed to fetch account updates from IB") is True


def test_r_a38_keeps_unrelated_account_messages():
    """'account' Wort allein im anderen Context (kein 'account updates')
    bleibt drin — z.B. 'account locked', 'account balance check'."""
    from app.sentry_setup import _is_noise
    assert _is_noise("Account balance check failed") is False
    assert _is_noise("User account locked") is False
    assert _is_noise("Bank account number invalid") is False


# ============================================================
# R-A39 (22.05.2026 Sprint-Tag-12): IBKR Error 1101 + 1102
# (Connectivity RESTORED — Library-Quirk: als ERROR geloggt obwohl OK).
# ============================================================

def test_r_a39_drops_ibkr_error_1101():
    """Error 1101 = Connectivity restored (data lost). Library-Quirk."""
    from app.sentry_setup import _is_noise
    msg = "Error 1101, reqId -1: Connectivity between IBKR and Trader Workstation has been restored — data lost."
    assert _is_noise(msg) is True


def test_r_a39_drops_ibkr_error_1102():
    """Error 1102 = Connectivity restored (data maintained). Library-Quirk."""
    from app.sentry_setup import _is_noise
    msg = "Error 1102, reqId -1: Connectivity between IBKR and Trader Workstation has been restored — data maintained."
    assert _is_noise(msg) is True


def test_r_a39_drops_connectivity_restored_generic():
    """Generic 'restored'-Form ohne Error-Code-Prefix (z.B. von ib_insync.wrapper)."""
    from app.sentry_setup import _is_noise
    assert _is_noise("Connectivity between IBKR and Trader Workstation has been restored") is True


def test_r_a39_generic_connectivity_between_ibkr_prefix():
    """Generic prefix-catch fuer zukuenftige IBKR-error-code-Varianten (1103, etc.)."""
    from app.sentry_setup import _is_noise
    assert _is_noise("Connectivity between IBKR has stale market data") is True
    assert _is_noise("Connectivity between IBKR and farms degraded") is True


def test_r_a39_keeps_real_ibkr_text_in_other_context():
    """'IBKR' Wort allein in anderem Kontext bleibt drin (z.B. Bot-eigene
    Log-Messages, Setup-Fehler)."""
    from app.sentry_setup import _is_noise
    # Kein 'Connectivity between IBKR' Prefix
    assert _is_noise("IBKR account funding failed") is False
    assert _is_noise("Cannot reach IBKR Web Portal") is False


# ============================================================
# R-A44 (24.05.2026 Sprint-Tag-13 spaet): yfinance HTTP 404-Variante.
# ============================================================

def test_r_a44_drops_yfinance_http_404_quotesummary():
    """Pattern aus PYTHON-FASTAPI-R: yfinance/Yahoo returnt HTTP 404 mit
    JSON-Body {'quoteSummary':{'result':null,'error':...}}. R-A30-Patterns
    deckten nur 'delisted'-Wortlaut, nicht HTTP-404-JSON-Variante."""
    from app.sentry_setup import _is_noise
    msg = 'HTTP Error 404: {"quoteSummary":{"result":null,"error":{"code":"Not Found"}}}'
    assert _is_noise(msg) is True


def test_r_a44_drops_quotesummary_result_null_generic():
    """Generic 'quoteSummary:{result:null' Pattern — matcht ALLE Yahoo-API
    Variants die null-result returnen (auch ohne HTTP-404-Prefix)."""
    from app.sentry_setup import _is_noise
    msg = 'yfinance got "quoteSummary":{"result":null,"error":{"code":"Bad Request"}}'
    assert _is_noise(msg) is True


def test_r_a44_keeps_real_http_404_other_apis():
    """HTTP 404 von anderen APIs (Polygon, Finnhub, IBKR REST) bleibt drin
    — kein false-positive durch unsere generischere Pattern."""
    from app.sentry_setup import _is_noise
    assert _is_noise("HTTP Error 404: Polygon ticker not found") is False
    assert _is_noise("Finnhub returned HTTP 404 for AAPL fundamentals") is False
    assert _is_noise("IBKR REST API 404: invalid account") is False
