"""
v37cx Anti-Regression Self-Test-System.
============================================================

Idee: Bot prueft sich selbst kontinuierlich. Jede Erweiterung muss
zu einer neuen Self-Test-Methode in dieser Datei fuehren — Tests
wachsen mit dem System mit.

Run-Modi:
  - In-process via run_self_tests() (vom Cron-Skript oder /api/selftest)
  - CLI: `python -m app.self_test`
  - Cron: alle 60 Min, persistiert Ergebnisse in self_test_history.json

Pushover-Trigger:
  - Bei JEDEM neuen FAIL-Event (Diff zum letzten Run) -> CRITICAL
  - Bei 3x FAIL in Folge -> Watchdog-Pause

Test-Klassen (bei Erweiterung des Bots: hier neue Klassen anhaengen):
  TC_BrokerConfig — broker=ibkr, kein eToro-Auth (v37cx)
  TC_TradingFlag  — fail-closed Default + Boot-Init (v37cw)
  TC_FileExist    — kritische Datendateien lesbar
  TC_IBKRConnect  — ib-gateway erreichbar, Account-Summary frisch
  TC_PortfolioSync— positions vs IBKR konsistent
  TC_HealthApi    — /health 200
  TC_Logs         — scheduler.log frisch (<= 15 Min)
  TC_NoLegacyFile — investpilot.py + demo_trader.py nur in legacy/
  TC_AssetClassesSane — enabled_classes ⊆ {stocks, etf, commodities}
  TC_PendingClosesFresh — pending_closes.json nicht > 24h alt-Eintraege
"""
from __future__ import annotations

import json
import logging
import os
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("SelfTest")


@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""
    duration_ms: int = 0
    severity: str = "warning"  # info | warning | critical
    category: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class TestSuiteResult:
    started_at: str
    finished_at: str
    duration_ms: int
    total: int
    passed: int
    failed: int
    results: list = field(default_factory=list)

    @property
    def overall_status(self) -> str:
        if self.failed == 0:
            return "ok"
        critical_fails = [r for r in self.results if not r.passed and r.severity == "critical"]
        return "critical" if critical_fails else "warning"

    def to_dict(self):
        d = asdict(self)
        d["overall_status"] = self.overall_status
        return d


# ============================================================
# TEST-KLASSEN — bei Erweiterung hier anhaengen
# ============================================================

def _get_data_path(name: str) -> Path:
    try:
        from app.config_manager import get_data_path
        return get_data_path(name)
    except Exception:
        return Path("/app/data") / name


def tc_broker_config() -> TestResult:
    """v37cx: broker=ibkr, kein eToro-Auth-Block."""
    try:
        cfg = json.loads(_get_data_path("config.json").read_text())
        broker = cfg.get("broker", "")
        if broker != "ibkr":
            return TestResult("broker_config", False,
                              f"config.broker={broker!r}, erwartet 'ibkr'",
                              severity="critical", category="config")
        if "etoro" in cfg and isinstance(cfg["etoro"], dict) and cfg["etoro"]:
            return TestResult("broker_config", False,
                              "config.etoro Auth-Block existiert noch (v37cx hat ihn entfernt)",
                              severity="warning", category="config")
        return TestResult("broker_config", True, "broker=ibkr, kein etoro-Auth",
                          severity="info", category="config")
    except Exception as e:
        return TestResult("broker_config", False, f"exception: {e!r}",
                          severity="critical", category="config")


def tc_trading_flag_failclosed() -> TestResult:
    """v37cw: trading_enabled.flag muss existieren + lesbar sein."""
    try:
        flag = _get_data_path("trading_enabled.flag")
        if not flag.exists():
            return TestResult("trading_flag", False,
                              "trading_enabled.flag fehlt — fail-closed greift, "
                              "aber Datei sollte vorhanden sein (Boot-Init)",
                              severity="warning", category="trading")
        content = flag.read_text().strip().lower()
        if content not in ("true", "false", "0", "1", ""):
            return TestResult("trading_flag", False,
                              f"trading_enabled.flag enthaelt unerwartetes: {content!r}",
                              severity="critical", category="trading")
        return TestResult("trading_flag", True, f"flag={content}",
                          severity="info", category="trading")
    except Exception as e:
        return TestResult("trading_flag", False, f"exception: {e!r}",
                          severity="critical", category="trading")


def tc_critical_files() -> TestResult:
    """Kritische State-Dateien lesbar."""
    files = ["config.json", "brain_state.json"]
    missing = []
    for f in files:
        p = _get_data_path(f)
        if not p.exists() or p.stat().st_size == 0:
            missing.append(f)
    if missing:
        return TestResult("critical_files", False,
                          f"missing/empty: {missing}",
                          severity="critical", category="state")
    return TestResult("critical_files", True, f"{len(files)} files OK",
                      severity="info", category="state")


def tc_ibkr_connect() -> TestResult:
    """IB-Gateway erreichbar — via IbkrBroker mit dedizierter Self-Test-clientId.

    v37h Tab-Audit-Day-2 (12.05.2026): vorher direkter IB().connect()-Call
    crashte im FastAPI AnyIO-Worker-Thread mit 'no current event loop'.
    v37h Tab-Audit-Day-3 (13.05.2026): Self-Test nutzte cfg.ibkr.client_id
    was zu Konflikten mit Bot-Cycle/Watchdog/Reconcile-Slots fuehrte
    (TimeoutError nach 14h Uptime). Fix: dedizierte clientId=199 fuer
    Self-Test reservieren — kein Pool-Konflikt mehr.

    Fallback bei "already_in_use": IbkrBroker._get_ib hat schon eingebaute
    Retry-Logik mit random clientId (siehe _get_ib Retry-Block).
    """
    t0 = time.time()
    try:
        from app.ibkr_client import IbkrBroker
        from app.config_manager import load_config
        cfg = load_config()
        ibkr_cfg = dict(cfg.get("ibkr") or {})
        # v37h: dedizierte Self-Test-clientId. 199 ist reserviert fuer
        # Self-Test (Bot's Pool nutzt typisch 1-99 default, oder 100-999
        # nach Retry). Falls 199 auch belegt: _get_ib's eingebauter Retry
        # mit random clientId greift automatisch.
        ibkr_cfg["client_id"] = 199
        # Shorter timeout damit Self-Test nicht 30s blockt bei IBG-Problem
        ibkr_cfg["timeout"] = 8
        broker = IbkrBroker(ibkr_cfg, readonly=True)
        ib = broker._get_ib()
        connected = ib.isConnected()
        tags = ib.accountSummary()
        # KEIN ib.disconnect() — Pool-Sharing, Connection bleibt aktiv
        if not connected:
            return TestResult("ibkr_connect", False, "isConnected=False after _get_ib()",
                              severity="critical", category="ibkr",
                              duration_ms=int((time.time() - t0) * 1000))
        if not tags:
            return TestResult("ibkr_connect", False, "leere accountSummary",
                              severity="warning", category="ibkr",
                              duration_ms=int((time.time() - t0) * 1000))
        return TestResult("ibkr_connect", True, f"{len(tags)} account-tags (clientId=199)",
                          severity="info", category="ibkr",
                          duration_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        return TestResult("ibkr_connect", False, f"exception: {type(e).__name__}: {e}",
                          severity="critical", category="ibkr",
                          duration_ms=int((time.time() - t0) * 1000))


def tc_no_legacy_active() -> TestResult:
    """v37cx: investpilot.py + demo_trader.py duerfen nicht im Hauptpfad sein."""
    root = Path("/app")
    bad = []
    for f in ("investpilot.py", "demo_trader.py"):
        if (root / f).exists():
            bad.append(f)
    if bad:
        return TestResult("no_legacy_active", False,
                          f"Legacy-Files im Hauptpfad: {bad}. Nach legacy/ verschieben.",
                          severity="warning", category="cleanup")
    return TestResult("no_legacy_active", True,
                      "Legacy-Files korrekt archiviert",
                      severity="info", category="cleanup")


def tc_asset_classes_sane() -> TestResult:
    """v37cv: enabled_classes ⊆ {stocks, etf, commodities} (IBKR-Universe)."""
    try:
        cfg = json.loads(_get_data_path("config.json").read_text())
        # Suche enabled_classes an mehreren ueblichen Pfaden
        ec = (cfg.get("market_scanner", {}).get("enabled_classes")
              or cfg.get("scanner", {}).get("enabled_classes")
              or cfg.get("trading", {}).get("enabled_classes")
              or cfg.get("enabled_classes")
              or [])
        if not ec:
            return TestResult("asset_classes_sane", True,
                              "enabled_classes leer/default (Code-Default greift)",
                              severity="info", category="config")
        allowed = {"stocks", "etf", "commodities"}
        bad = [c for c in ec if c not in allowed]
        if bad:
            return TestResult("asset_classes_sane", False,
                              f"unzulaessige Klassen aktiv: {bad}. Carlos hat seit "
                              f"v37cv nur {sorted(allowed)} freigegeben.",
                              severity="critical", category="config")
        return TestResult("asset_classes_sane", True,
                          f"enabled_classes={sorted(ec)}",
                          severity="info", category="config")
    except Exception as e:
        return TestResult("asset_classes_sane", False, f"exception: {e!r}",
                          severity="warning", category="config")


def tc_scheduler_heartbeat() -> TestResult:
    """Scheduler-Heartbeat soll <= 20 Min alt sein (v37co-Threshold).

    v37cx: actual heartbeat-File ist alert_state.json (siehe app/alerts.py:22).
    """
    try:
        hb = _get_data_path("alert_state.json")
        if not hb.exists():
            return TestResult("scheduler_heartbeat", False,
                              "alert_state.json fehlt — Scheduler hat noch nie Heartbeat geschrieben",
                              severity="warning", category="scheduler")
        data = json.loads(hb.read_text() or "{}")
        ts = data.get("last_heartbeat")
        if not ts:
            return TestResult("scheduler_heartbeat", False,
                              "alert_state.json hat kein last_heartbeat",
                              severity="critical", category="scheduler")
        # parse ISO
        if ts.endswith("Z"):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
        if age_min > 15:
            return TestResult("scheduler_heartbeat", False,
                              f"Heartbeat {age_min:.1f}min alt (> 15)",
                              severity="critical", category="scheduler")
        return TestResult("scheduler_heartbeat", True,
                          f"Heartbeat {age_min:.1f}min alt",
                          severity="info", category="scheduler")
    except Exception as e:
        return TestResult("scheduler_heartbeat", False, f"exception: {e!r}",
                          severity="warning", category="scheduler")


def tc_health_endpoint() -> TestResult:
    """/health muss 200 returnen (intern via http)."""
    t0 = time.time()
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8000/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            ok = r.status == 200
            body = r.read(200).decode("utf-8", errors="replace")
        return TestResult("health_endpoint", ok,
                          f"HTTP {r.status} body={body[:80]}",
                          severity="critical", category="api",
                          duration_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        return TestResult("health_endpoint", False, f"exception: {e!r}",
                          severity="critical", category="api",
                          duration_ms=int((time.time() - t0) * 1000))


def tc_pending_closes_fresh() -> TestResult:
    """v37cu: pending_closes.json darf keine Eintraege > 24h enthalten."""
    try:
        f = _get_data_path("pending_closes.json")
        if not f.exists():
            return TestResult("pending_closes_fresh", True,
                              "keine pending_closes.json (= sauber)",
                              severity="info", category="trading")
        data = json.loads(f.read_text() or "{}")
        old = []
        cutoff = datetime.now() - timedelta(hours=24)
        for k, v in data.items():
            ts = v.get("submitted_at", "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", ""))
                if dt < cutoff:
                    old.append(k)
            except Exception:
                old.append(k)
        if old:
            return TestResult("pending_closes_fresh", False,
                              f"{len(old)} Eintraege > 24h alt: {old[:5]}",
                              severity="warning", category="trading")
        return TestResult("pending_closes_fresh", True,
                          f"{len(data)} Eintraege, alle frisch",
                          severity="info", category="trading")
    except Exception as e:
        return TestResult("pending_closes_fresh", False, f"exception: {e!r}",
                          severity="warning", category="trading")


def tc_no_etoro_in_logs() -> TestResult:
    """v37cx: keine eToro-Trade-Calls in den letzten 1000 log-Lines."""
    try:
        log_path = _get_data_path("logs/scheduler.log")
        if not log_path.exists():
            return TestResult("no_etoro_in_logs", True,
                              "kein log-File — skip",
                              severity="info", category="cleanup")
        # Letzte 1000 Zeilen lesen (effizient via tail)
        with open(log_path, "rb") as f:
            f.seek(0, 2)  # end
            size = f.tell()
            chunk = min(size, 200_000)  # 200KB
            f.seek(size - chunk)
            tail = f.read().decode("utf-8", errors="replace").splitlines()
        recent = tail[-1000:]
        bad = [l for l in recent if "EtoroClient" in l and "parse_position" not in l
               and "_v37cx" not in l and "deprecated" not in l.lower()]
        if bad:
            return TestResult("no_etoro_in_logs", False,
                              f"{len(bad)} eToro-Refs in Recent-Logs (z.B. {bad[0][:80]})",
                              severity="warning", category="cleanup")
        return TestResult("no_etoro_in_logs", True,
                          f"clean (von {len(recent)} log-lines)",
                          severity="info", category="cleanup")
    except Exception as e:
        return TestResult("no_etoro_in_logs", False, f"exception: {e!r}",
                          severity="info", category="cleanup")


# ============================================================
# RUNNER
# ============================================================



def tc_ibkr_session_watchdog() -> TestResult:
    """v37cy: Watchdog-State darf keine offenen Recovery-Probleme zeigen."""
    try:
        f = _get_data_path("ibkr_session_watchdog.json")
        if not f.exists():
            return TestResult("ibkr_session_watchdog", True,
                              "State-File noch nicht initialisiert (erster Run kommt)",
                              severity="info", category="ibkr")
        data = json.loads(f.read_text() or "{}")
        decision = data.get("last_decision", "")
        recoveries = len(data.get("recovery_attempts", []))
        consecutive_fails = data.get("consecutive_fails", 0)
        if decision == "rate_limited":
            return TestResult("ibkr_session_watchdog", False,
                              f"Rate-Limit erreicht ({recoveries} Restarts/h) — manuell pruefen!",
                              severity="critical", category="ibkr")
        if decision in ("no_baseline", "stale_baseline"):
            return TestResult("ibkr_session_watchdog", False,
                              f"Watchdog kann nicht auto-recovern: {decision}",
                              severity="warning", category="ibkr")
        if consecutive_fails >= 1:
            return TestResult("ibkr_session_watchdog", True,
                              f"Aktuelle Fail-Streak: {consecutive_fails} (noch unter 2-Strikes-Threshold)",
                              severity="warning", category="ibkr")
        return TestResult("ibkr_session_watchdog", True,
                          f"decision={decision}, recoveries letzte 60min={recoveries}",
                          severity="info", category="ibkr")
    except Exception as e:
        return TestResult("ibkr_session_watchdog", False, f"exception: {e!r}",
                          severity="warning", category="ibkr")




def tc_discovery_freshness() -> TestResult:
    """v37cz: Discovery soll innerhalb der letzten 14 Tage gelaufen sein."""
    try:
        f = _get_data_path("discovery_status.json")
        if not f.exists():
            return TestResult("discovery_freshness", True,
                              "discovery_status.json fehlt — vermutlich noch kein Run",
                              severity="info", category="scheduler")
        data = json.loads(f.read_text() or "{}")
        last = data.get("finished_at") or data.get("started_at")
        if not last:
            return TestResult("discovery_freshness", False,
                              "discovery_status.json hat kein finished_at",
                              severity="warning", category="scheduler")
        from datetime import datetime, timezone
        ts = last.replace("Z", "+00:00") if last.endswith("Z") else last
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        # Discovery soll Freitags laufen, also max 9d (Toleranz fuer Verzoegerungen)
        if days > 14:
            return TestResult("discovery_freshness", False,
                              f"Letzter Discovery-Run vor {days:.1f} Tagen (> 14d) — GH-Action defekt?",
                              severity="warning", category="scheduler")
        if days > 9:
            return TestResult("discovery_freshness", True,
                              f"Letzter Discovery-Run vor {days:.1f}d (>9d, leicht stale)",
                              severity="info", category="scheduler")
        return TestResult("discovery_freshness", True,
                          f"Letzter Discovery-Run vor {days:.1f}d",
                          severity="info", category="scheduler")
    except Exception as e:
        return TestResult("discovery_freshness", False, f"exception: {e!r}",
                          severity="warning", category="scheduler")


def tc_yfinance_freshness() -> TestResult:
    """v37h Tab-Audit-Day-2 (12.05.2026, Q3-2): yfinance liefert frische Daten?

    Hintergrund: market_scanner + market_context nutzen yfinance.history()
    direkt ohne Stale-Detection. Wenn yfinance-Outage stale Daten liefert
    (z.B. Wochenend-'possibly delisted'-Spam, oder yfinance-CDN-Cache-
    Hickup), wuerde Bot's Scoring auf alten Werten basieren ohne Warnung.

    Check: SPY (most-traded ETF) ueber yfinance laden, letzten Bar-Date
    pruefen. Wenn > 4 Tage alt (=Wochenende + 1 Toleranz-Tag) -> FAIL +
    Pushover-Alert via _log_close_failure-Pattern (siehe alerts.py).
    """
    t0 = time.time()
    try:
        import yfinance as yf
    except ImportError:
        return TestResult("yfinance_freshness", False,
                          "yfinance nicht installiert",
                          severity="warning", category="data",
                          duration_ms=int((time.time() - t0) * 1000))
    try:
        from datetime import datetime, timezone, timedelta
        ticker = yf.Ticker("SPY")
        hist = ticker.history(period="5d")
        if hist.empty:
            return TestResult("yfinance_freshness", False,
                              "yfinance SPY-history leer",
                              severity="warning", category="data",
                              duration_ms=int((time.time() - t0) * 1000))
        last_bar = hist.index[-1]
        # pandas DatetimeIndex -> Python datetime
        if hasattr(last_bar, "to_pydatetime"):
            last_bar = last_bar.to_pydatetime()
        if last_bar.tzinfo is None:
            last_bar = last_bar.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - last_bar).total_seconds() / 86400
        # Toleranz: 4 Tage (Wochenende + Feiertag + 1 Tag Puffer)
        if age_days > 4:
            return TestResult("yfinance_freshness", False,
                              f"SPY-Last-Bar vor {age_days:.1f}d (> 4d Toleranz)"
                              f" - yfinance liefert stale Daten!",
                              severity="critical", category="data",
                              duration_ms=int((time.time() - t0) * 1000))
        return TestResult("yfinance_freshness", True,
                          f"SPY-Last-Bar vor {age_days:.1f}d (fresh)",
                          severity="info", category="data",
                          duration_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        return TestResult("yfinance_freshness", False,
                          f"exception: {type(e).__name__}: {e}",
                          severity="warning", category="data",
                          duration_ms=int((time.time() - t0) * 1000))


def tc_cash_reserve_sane() -> TestResult:
    """v37h+1 (14.05.2026): Cash-Reserve-Module + Config-Werte konsistent.

    Prueft:
      - app.cash_reserve importierbar + get_required_reserve_chf liefert > 0
      - Config-Keys risk_management.min_cash_reserve_chf + _pct vorhanden
      - Floor + Pct innerhalb Sicherheits-Grenzen
      - Hybrid-Logik korrekt (max(floor, pct*equity) bei Test-Equity 50000)
    """
    t0 = time.time()
    try:
        from app.cash_reserve import (
            get_required_reserve_chf,
            MIN_PCT,
            MAX_PCT,
            MIN_FLOOR_CHF,
            MAX_FLOOR_CHF,
        )
        from app.config_manager import load_config
    except Exception as e:
        return TestResult("cash_reserve_sane", False,
                          f"Import-Fehler: {type(e).__name__}: {e}",
                          severity="critical", category="config",
                          duration_ms=int((time.time() - t0) * 1000))
    try:
        cfg = load_config() or {}
        risk = cfg.get("risk_management", {})
        floor = risk.get("min_cash_reserve_chf")
        pct = risk.get("min_cash_reserve_pct")
        if floor is None or pct is None:
            return TestResult("cash_reserve_sane", False,
                              "min_cash_reserve_chf oder _pct fehlen in config.risk_management",
                              severity="warning", category="config",
                              duration_ms=int((time.time() - t0) * 1000))
        if not (MIN_FLOOR_CHF <= float(floor) <= MAX_FLOOR_CHF):
            return TestResult("cash_reserve_sane", False,
                              f"floor {floor} ausserhalb [{MIN_FLOOR_CHF},{MAX_FLOOR_CHF}]",
                              severity="critical", category="config",
                              duration_ms=int((time.time() - t0) * 1000))
        if not (MIN_PCT <= float(pct) <= MAX_PCT):
            return TestResult("cash_reserve_sane", False,
                              f"pct {pct} ausserhalb [{MIN_PCT},{MAX_PCT}]",
                              severity="critical", category="config",
                              duration_ms=int((time.time() - t0) * 1000))
        # Hybrid-Logik bei Test-Equity 50'000: pct=0.10 -> 5000 > floor 500 -> 5000
        req = get_required_reserve_chf(50000.0)
        expected = max(float(floor), float(pct) * 50000.0)
        if abs(req - expected) > 0.01:
            return TestResult("cash_reserve_sane", False,
                              f"Hybrid-Berechnung falsch: got {req}, expected {expected}",
                              severity="critical", category="config",
                              duration_ms=int((time.time() - t0) * 1000))
        return TestResult("cash_reserve_sane", True,
                          f"floor={floor} CHF + pct={pct} (Hybrid bei 50k Equity -> {req:.0f} CHF)",
                          severity="info", category="config",
                          duration_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        return TestResult("cash_reserve_sane", False,
                          f"exception: {type(e).__name__}: {e}",
                          severity="critical", category="config",
                          duration_ms=int((time.time() - t0) * 1000))


def tc_symbol_concentration_sane() -> TestResult:
    """R-A10 (Sprint-Tag-9, 19.05.2026): Symbol-Konzentrations-Hard-Cap Config-Check.

    Prueft dass:
      - risk_management.symbol_concentration Section existiert
      - enabled ist boolean
      - max_positions_per_symbol ist int 1-5 (sane range)
      - max_exposure_per_symbol_pct ist 1-50 (sane range)
      - pushover_threshold_per_day ist int 1-20

    Anlass: 18.05.2026 5x SCANNER_BUY OIL — Schutz dass Config nicht
    versehentlich auf weichere Werte geaendert wird (z.B. max_pos=99
    waere de-facto disabled).
    """
    t0 = time.time()
    try:
        from app.config_manager import load_config
        cfg = load_config()
        sc = cfg.get("risk_management", {}).get("symbol_concentration")
        if not isinstance(sc, dict):
            return TestResult(
                "symbol_concentration_sane", False,
                "Section risk_management.symbol_concentration fehlt komplett",
                severity="critical", category="config",
                duration_ms=int((time.time() - t0) * 1000))

        enabled = sc.get("enabled")
        if not isinstance(enabled, bool):
            return TestResult(
                "symbol_concentration_sane", False,
                f"enabled muss bool sein, ist {type(enabled).__name__}",
                severity="critical", category="config",
                duration_ms=int((time.time() - t0) * 1000))

        max_pos = sc.get("max_positions_per_symbol", 1)
        if not isinstance(max_pos, int) or not (1 <= max_pos <= 5):
            return TestResult(
                "symbol_concentration_sane", False,
                f"max_positions_per_symbol={max_pos} ausserhalb sane range 1-5",
                severity="critical", category="config",
                duration_ms=int((time.time() - t0) * 1000))

        max_exp = sc.get("max_exposure_per_symbol_pct", 15)
        if not isinstance(max_exp, (int, float)) or not (1 <= max_exp <= 50):
            return TestResult(
                "symbol_concentration_sane", False,
                f"max_exposure_per_symbol_pct={max_exp} ausserhalb sane range 1-50",
                severity="critical", category="config",
                duration_ms=int((time.time() - t0) * 1000))

        push_thr = sc.get("pushover_threshold_per_day", 3)
        if not isinstance(push_thr, int) or not (1 <= push_thr <= 20):
            return TestResult(
                "symbol_concentration_sane", False,
                f"pushover_threshold_per_day={push_thr} ausserhalb sane range 1-20",
                severity="warning", category="config",
                duration_ms=int((time.time() - t0) * 1000))

        return TestResult(
            "symbol_concentration_sane", True,
            f"enabled={enabled}, max_pos={max_pos}, max_exp={max_exp}%, "
            f"pushover_at={push_thr}/Tag",
            severity="info", category="config",
            duration_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        return TestResult(
            "symbol_concentration_sane", False,
            f"exception: {type(e).__name__}: {e}",
            severity="critical", category="config",
            duration_ms=int((time.time() - t0) * 1000))


ALL_TESTS: list[Callable[[], TestResult]] = [
    tc_broker_config,
    tc_trading_flag_failclosed,
    tc_critical_files,
    tc_ibkr_connect,
    tc_no_legacy_active,
    tc_asset_classes_sane,
    tc_scheduler_heartbeat,
    tc_health_endpoint,
    tc_pending_closes_fresh,
    tc_no_etoro_in_logs,
    tc_ibkr_session_watchdog,
    tc_discovery_freshness,
    tc_yfinance_freshness,  # v37h Q3-2: Stale-yfinance-Detection
    tc_cash_reserve_sane,   # v37h+1: Cash-Reserve-Config + Hybrid-Logik
    tc_symbol_concentration_sane,  # R-A10 Sprint-Tag-9 (19.05.2026)
]


def run_self_tests(skip_ibkr: bool = False) -> TestSuiteResult:
    """Run alle Tests. Returns TestSuiteResult.

    Args:
        skip_ibkr: Wenn True, ueberspringt IBKR-Connect (z.B. wenn IB-Gateway
                   bekanntermassen down ist und nicht nochmal alarmieren soll).
    """
    started = datetime.now(timezone.utc)
    results = []
    for fn in ALL_TESTS:
        if skip_ibkr and "ibkr" in fn.__name__:
            continue
        t0 = time.time()
        try:
            r = fn()
        except Exception as e:
            r = TestResult(fn.__name__, False, f"unhandled: {e!r}\n{traceback.format_exc()}",
                           severity="critical", category="meta")
        if not r.duration_ms:
            r.duration_ms = int((time.time() - t0) * 1000)
        results.append(r)
        log.info(f"  [{ 'OK' if r.passed else 'FAIL' }] {r.name} — {r.detail[:80]}")
    finished = datetime.now(timezone.utc)
    suite = TestSuiteResult(
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_ms=int((finished - started).total_seconds() * 1000),
        total=len(results),
        passed=sum(1 for r in results if r.passed),
        failed=sum(1 for r in results if not r.passed),
        results=results,
    )
    return suite


def persist_result(suite: TestSuiteResult, history_max: int = 200) -> None:
    """Speichert in self_test_history.json (max history_max Eintraege)."""
    try:
        path = _get_data_path("self_test_history.json")
        history = []
        if path.exists():
            history = json.loads(path.read_text() or "[]")
        history.append(suite.to_dict())
        history = history[-history_max:]
        path.write_text(json.dumps(history, indent=2, default=str))
    except Exception as e:
        log.error(f"persist_result fehlgeschlagen: {e}")


def run_and_alert() -> TestSuiteResult:
    """Cron-Entry-Point: run, persist, alert bei NEUEN Fails."""
    suite = run_self_tests()
    persist_result(suite)

    # Diff zum letzten Run: alarmiere nur bei NEUEN Fails
    try:
        path = _get_data_path("self_test_history.json")
        history = json.loads(path.read_text() or "[]")
        if len(history) >= 2:
            prev = history[-2]
            prev_failed = {r["name"] for r in prev.get("results", []) if not r["passed"]}
            current_failed = {r.name for r in suite.results if not r.passed}
            new_fails = current_failed - prev_failed
            if new_fails:
                _alert_new_fails(suite, new_fails)
    except Exception as e:
        log.error(f"diff/alert fehlgeschlagen: {e}")

    return suite


def _alert_new_fails(suite: TestSuiteResult, new_fail_names: set) -> None:
    """Pushover-Alert bei neuen Test-Fails."""
    try:
        from app.alerts import send_alert
        details = [r for r in suite.results if r.name in new_fail_names]
        msg_lines = [f"<b>SelfTest neue Fails ({len(new_fail_names)})</b>"]
        for r in details:
            msg_lines.append(f"• {r.name} [{r.severity}]: {r.detail[:120]}")
        crit = any(r.severity == "critical" for r in details)
        send_alert(
            title="InvestPilot: Self-Test Regression",
            message="\n".join(msg_lines),
            level="ERROR" if crit else "WARNING",
        )
    except Exception as e:
        log.error(f"Pushover-Alert fehlgeschlagen: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    suite = run_and_alert()
    print(json.dumps(suite.to_dict(), indent=2, default=str))
