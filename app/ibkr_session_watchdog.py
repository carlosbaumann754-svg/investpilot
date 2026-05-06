"""
v37cy IBKR-Session-Watchdog mit Smart-Recovery (Carlos-Strategie C).
=========================================================================

Problem: IBKR erlaubt nur 1 Session pro Account. Wenn Carlos sich via
IB-Mobile oder Web-Portal in sein Konto einloggt, wird die Bot-Session
auf dem ib-gateway abgeschossen. Bot kann dann nicht mehr connecten,
SCHLIMM weil:
  - SL/TP wird nicht mehr ueberprueft (Positionen ungeschuetzt)
  - Reconcile alarmiert (False-Positive PHANTOM)
  - Trading-Cycles loggen nur "Connection Refused"

Loesung: Watchdog erkennt Disconnect, restartet ib-gateway-Container
(triggert IBC-Auto-Login mit ENV-Vars), Bot kann reconnecten.

Smart-Recovery (Strategie C — von Carlos gewaehlt):
  1. 2-Strikes-Rule: erst nach 2 aufeinanderfolgenden Fails restarten
     (Schutz gegen transiente Network-Hiccups)
  2. Rate-Limit: max 6 Restarts pro 60-Min-Fenster
     (Schutz gegen Restart-Loop wenn Gateway selbst kaputt ist)
  3. Freshness-Check: Recovery NUR wenn letzter Success < 30 Min her
     (vermeidet Auto-Recovery bei initial-broken-Setup)

Recovery-Trigger (Pattern b — Default):
  - ConnectionRefusedError (Gateway-Process tot)
  - asyncio.TimeoutError (Gateway haengt)
  - andere ib_insync-Connect-Errors mit "Connect call failed"

CLI:
  python -m app.ibkr_session_watchdog        # check, exit 0=ok, 42=restart-needed
  python -m app.ibkr_session_watchdog --reset # state zuruecksetzen
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("IbkrSessionWatchdog")

# Konfig — bei Bedarf via config.json overridebar (siehe _load_config)
DEFAULT_CONFIG = {
    "consecutive_fails_threshold": 2,        # 2-Strikes
    "rate_limit_per_hour": 6,                 # max Restarts/h
    "freshness_minutes": 30,                  # Recovery nur wenn letzter Success in letzten 30 Min
    "connect_timeout_seconds": 8,
    "ibkr_host": "ib-gateway",
    "ibkr_port": 4004,
    "client_id": 197,                         # eigener clientId fuer Watchdog (nicht 1=Bot, nicht 199=SelfTest)
}


@dataclass
class WatchdogState:
    last_check: Optional[str] = None          # ISO-Timestamp
    last_success: Optional[str] = None        # ISO-Timestamp
    consecutive_fails: int = 0
    recovery_attempts: list = field(default_factory=list)  # ISO-Timestamps der letzten Restarts
    last_decision: str = ""                   # "ok" | "transient_fail" | "recovery_triggered" | "rate_limited" | "no_baseline"


def _state_path() -> Path:
    try:
        from app.config_manager import get_data_path
        return get_data_path("ibkr_session_watchdog.json")
    except Exception:
        return Path("/app/data/ibkr_session_watchdog.json")


def _load_state() -> WatchdogState:
    p = _state_path()
    if not p.exists():
        return WatchdogState()
    try:
        data = json.loads(p.read_text())
        # Defensiv: nur bekannte Felder uebernehmen
        return WatchdogState(
            last_check=data.get("last_check"),
            last_success=data.get("last_success"),
            consecutive_fails=int(data.get("consecutive_fails", 0)),
            recovery_attempts=list(data.get("recovery_attempts", [])),
            last_decision=data.get("last_decision", ""),
        )
    except Exception as e:
        log.error(f"State-Load fehlgeschlagen, fresh state: {e}")
        return WatchdogState()


def _save_state(state: WatchdogState) -> None:
    p = _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state), indent=2))
        os.replace(str(tmp), str(p))
    except Exception as e:
        log.error(f"State-Save fehlgeschlagen: {e}")


def _load_config() -> dict:
    """Erlaubt Overrides via config.json -> session_watchdog: { ... }."""
    cfg = dict(DEFAULT_CONFIG)
    try:
        from app.config_manager import load_config
        full = load_config() or {}
        overrides = full.get("session_watchdog") or {}
        for k, v in overrides.items():
            if k in cfg:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def _is_external_login_disconnect(exc: Exception) -> bool:
    """Pattern (b) — Carlos-Default.

    Recovery-Trigger NUR fuer Errors die typisch auf eine externe Session-
    Disconnection hinweisen, NICHT fuer Config-Probleme.
    """
    if isinstance(exc, ConnectionRefusedError):
        return True
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    msg = str(exc).lower()
    if "connect call failed" in msg or "connection refused" in msg:
        return True
    if "timeout" in msg and "connect" in msg:
        return True
    # NICHT recovern bei z.B. "API not enabled" / Auth-Fails
    return False


def _check_connection(cfg: dict) -> tuple[bool, Optional[Exception]]:
    """Versuche kurzen ib_insync-Connect. Returns (ok, exc_or_None)."""
    try:
        from ib_insync import IB
    except Exception as e:
        return False, e
    ib = IB()
    try:
        ib.connect(
            cfg["ibkr_host"],
            cfg["ibkr_port"],
            clientId=cfg["client_id"],
            timeout=cfg["connect_timeout_seconds"],
            readonly=True,
        )
        ok = ib.isConnected()
        try:
            ib.disconnect()
        except Exception:
            pass
        return ok, None
    except Exception as e:
        try:
            ib.disconnect()
        except Exception:
            pass
        return False, e


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _prune_old_recoveries(state: WatchdogState, window_minutes: int = 60) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    keep = []
    for ts in state.recovery_attempts:
        dt = _parse_iso(ts)
        if dt and dt > cutoff:
            keep.append(ts)
    state.recovery_attempts = keep


def evaluate() -> dict:
    """Hauptlogik. Returns {decision, recovery_needed, message}."""
    cfg = _load_config()
    state = _load_state()
    state.last_check = _now_iso()
    _prune_old_recoveries(state)

    ok, exc = _check_connection(cfg)
    now = datetime.now(timezone.utc)

    # ------------- Success-Path -------------
    if ok:
        state.consecutive_fails = 0
        state.last_success = _now_iso()
        state.last_decision = "ok"
        _save_state(state)
        return {
            "decision": "ok",
            "recovery_needed": False,
            "message": "IB-Gateway erreichbar.",
            "state": asdict(state),
        }

    # ------------- Failure-Path -------------
    err_kind = type(exc).__name__ if exc else "Unknown"
    err_msg = str(exc)[:200] if exc else ""

    if exc and not _is_external_login_disconnect(exc):
        state.last_decision = "transient_other_error"
        _save_state(state)
        return {
            "decision": "transient_other_error",
            "recovery_needed": False,
            "message": f"Connect-Fail [{err_kind}], aber kein Login-Disconnect-Pattern: {err_msg}",
            "state": asdict(state),
        }

    state.consecutive_fails += 1

    # 1. 2-Strikes-Rule
    if state.consecutive_fails < cfg["consecutive_fails_threshold"]:
        state.last_decision = "transient_fail"
        _save_state(state)
        return {
            "decision": "transient_fail",
            "recovery_needed": False,
            "message": f"Connect-Fail #{state.consecutive_fails}, warte auf {cfg['consecutive_fails_threshold']}-Strikes.",
            "state": asdict(state),
        }

    # 2. Freshness-Check (Recovery nur wenn vorher SCHON MAL erfolgreich)
    last_success_dt = _parse_iso(state.last_success)
    if last_success_dt is None:
        state.last_decision = "no_baseline"
        _save_state(state)
        return {
            "decision": "no_baseline",
            "recovery_needed": False,
            "message": "Noch nie erfolgreich connected — kein Auto-Recovery (manueller Setup-Check noetig).",
            "state": asdict(state),
        }
    age = (now - last_success_dt).total_seconds() / 60
    if age > cfg["freshness_minutes"]:
        state.last_decision = "stale_baseline"
        _save_state(state)
        return {
            "decision": "stale_baseline",
            "recovery_needed": False,
            "message": (f"Letzter Success vor {age:.1f}min (> {cfg['freshness_minutes']}min) — "
                        "kein Auto-Recovery, vermutlich tieferliegendes Problem."),
            "state": asdict(state),
        }

    # 3. Rate-Limit
    if len(state.recovery_attempts) >= cfg["rate_limit_per_hour"]:
        state.last_decision = "rate_limited"
        _save_state(state)
        return {
            "decision": "rate_limited",
            "recovery_needed": False,
            "message": (f"Rate-Limit erreicht: {len(state.recovery_attempts)} Recoveries in letzten 60min "
                        f"(max {cfg['rate_limit_per_hour']}). Manuell pruefen!"),
            "state": asdict(state),
        }

    # ALLES ok -> Recovery triggern
    state.recovery_attempts.append(_now_iso())
    state.consecutive_fails = 0  # reset after recovery decision
    state.last_decision = "recovery_triggered"
    _save_state(state)
    return {
        "decision": "recovery_triggered",
        "recovery_needed": True,
        "message": (f"Smart-Recovery: {state.consecutive_fails+1} Strikes, letzter Success vor {age:.1f}min, "
                    f"{len(state.recovery_attempts)}/{cfg['rate_limit_per_hour']} Recoveries diese Stunde. "
                    f"Triggere docker restart ib-gateway."),
        "state": asdict(state),
    }


def reset_state() -> None:
    p = _state_path()
    if p.exists():
        p.unlink()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if "--reset" in sys.argv:
        reset_state()
        print("State reset.")
        return 0
    result = evaluate()
    print(json.dumps(result, indent=2, default=str))
    # Exit-Codes:
    #   0  = ok (kein Action)
    #   42 = recovery needed (Cron-Skript macht docker restart)
    #   1  = informational fail (kein Recovery, aber Status nicht ok)
    if result["recovery_needed"]:
        return 42
    if result["decision"] == "ok":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
