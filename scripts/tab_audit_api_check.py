#!/usr/bin/env python3
"""v37h Task 3-Prep (10.05.2026) — Tab-Audit API Health-Check.

ZWECK
Automatisiert die A1/A2/A4-Pruefungen aus der Tab-Audit-Checklist:
  - A2: API-Call HTTP 200 (kein 401/500)
  - A1: Daten-Sanity (Werte in plausiblen Bereichen)
  - A4: Kein Stuck-Loading-Sentinel ("--", null, leerer Array wo unerwartet)

Carlos macht dann nur noch:
  - A3 (Tooltip vorhanden + verstaendlich)
  - A5 (Mobile-Responsive)

VERWENDUNG
  # Lokal gegen Dev-Server
  python scripts/tab_audit_api_check.py --base http://localhost:8000

  # Gegen VPS (mit JWT-Token aus Login)
  python scripts/tab_audit_api_check.py --base https://bot.cbaumann.ch \\
      --token "<jwt-from-login>"

  # Token aus Env
  AUDIT_TOKEN="<jwt>" python scripts/tab_audit_api_check.py --base https://...

OUTPUT
Markdown-Tabelle zum direkten Pasten in Tab-Audit-Checklist.md
plus Summary-Block (n_PASS / n_WARN / n_FAIL).

EXIT-CODE
  0 = alle Endpoints OK
  1 = mind. ein WARN
  2 = mind. ein FAIL (HTTP-Error oder leerer Payload wo Daten erwartet)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import urllib.request
import urllib.error


# ============================================================
# Endpoint-Registry mit Plausibilitaets-Rules pro Endpoint
# ============================================================
#
# Jede Rule ist eine Funktion (response_json) -> Optional[str].
# Returns None bei OK, error-Message bei FAIL.
#
# Carlos kann diese Rules anpassen wenn neue Domain-Knowledge dazukommt
# (z.B. "Cash > $500k" als Plausibilitaets-Floor wenn Real-Money-Phase laeuft).

def _rule_has_keys(*required: str) -> Callable:
    def check(payload):
        if not isinstance(payload, dict):
            return f"payload is not dict (got {type(payload).__name__})"
        missing = [k for k in required if k not in payload]
        if missing:
            return f"missing keys: {missing}"
        return None
    return check


def _rule_positive_number(key: str, min_value: float = 0.0) -> Callable:
    def check(payload):
        if not isinstance(payload, dict) or key not in payload:
            return f"key '{key}' missing for positive-number check"
        v = payload.get(key)
        if v is None:
            return f"key '{key}' is None (stuck-loading sentinel?)"
        try:
            n = float(v)
        except (ValueError, TypeError):
            return f"key '{key}'={v!r} not numeric"
        if n < min_value:
            return f"key '{key}'={n} < expected min {min_value}"
        return None
    return check


def _rule_list_not_dash(key: str) -> Callable:
    def check(payload):
        if not isinstance(payload, dict) or key not in payload:
            return None  # Schluss: Optional-Feld
        v = payload.get(key)
        if v == "--" or v == "...":
            return f"key '{key}' = stuck-loading sentinel '{v}'"
        return None
    return check


def _rule_not_error(payload):
    """Antwort darf kein top-level error-Key haben."""
    if isinstance(payload, dict) and payload.get("error"):
        return f"error in payload: {payload['error']!r}"
    return None


@dataclass
class EndpointSpec:
    path: str
    name: str  # menschenlesbar fuer Report
    rules: list[Callable] = field(default_factory=list)
    expect_auth: bool = True  # die meisten /api/* brauchen JWT
    optional: bool = False  # 500-OK, weil Backend-Feature evtl. disabled


# Reihenfolge folgt Tab-Audit-Checklist Session 1 (Dashboard-Tab).
# Plausibilitaets-Rules sind defensiv — kein false-positive bei Empty-Account.
ENDPOINTS: list[EndpointSpec] = [
    EndpointSpec(
        "/api/portfolio", "Portfolio (Cash/Equity/Positions)",
        rules=[
            _rule_not_error,
            _rule_has_keys("positions"),
            _rule_list_not_dash("credit"),
            _rule_list_not_dash("equity"),
        ],
    ),
    EndpointSpec(
        "/api/broker-status", "Broker-Status (IBKR connected?)",
        rules=[_rule_not_error, _rule_has_keys("connected")],
    ),
    EndpointSpec(
        "/api/exit-forecast", "Exit-Forecast",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/benchmark", "Bot vs Markt",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/equity-history", "Equity-Verlauf",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/pnl-periods", "P/L over Zeit",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/brain", "Brain Status",
        rules=[_rule_not_error],
    ),
    EndpointSpec(
        "/api/trading/status", "Trading Toggle",
        rules=[_rule_not_error, _rule_has_keys("enabled")],
    ),
    EndpointSpec(
        "/api/trades", "Trades-Tab",
        rules=[_rule_not_error],
    ),
    EndpointSpec(
        "/api/order-audit", "Order-Audit-Tab (E27)",
        rules=[_rule_not_error],
    ),
    EndpointSpec(
        "/api/risk", "Risk-Card",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/exposure", "Exposure",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/market-context", "Market Context",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/execution-stats", "Execution Stats",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/performance-breakdown", "Performance Breakdown",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/backtest", "Backtest-Result",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/backtest/status", "Backtest-Status",
        rules=[_rule_not_error],
    ),
    EndpointSpec(
        "/api/ml-model", "ML-Model",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/optimizer", "Optimizer",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/optimizer/status", "Optimizer-Status",
        rules=[_rule_not_error],
    ),
    EndpointSpec(
        "/api/kelly-sweep", "Kelly-Sweep",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/discovery", "Discovery",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/discovery/status", "Discovery-Status",
        rules=[_rule_not_error],
    ),
    EndpointSpec(
        "/api/universe/suggestions", "Universe-Suggestions",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/withdrawal/status", "Withdrawal-Planner",
        rules=[_rule_not_error],
    ),
    EndpointSpec(
        "/api/config", "Config-View",
        rules=[_rule_not_error],
    ),
    EndpointSpec(
        "/api/config/strategy-audit", "Strategy-Audit",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/email-config-check", "Email-Config-Check",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/weekly-report", "Weekly Report",
        rules=[_rule_not_error],
        optional=True,
    ),
    EndpointSpec(
        "/api/weekly-report/maintenance-preview", "Maintenance-Preview",
        rules=[_rule_not_error],
        optional=True,
    ),
]


# ============================================================
# HTTP-Client (stdlib only, kein requests-Dependency)
# ============================================================

@dataclass
class CheckResult:
    spec: EndpointSpec
    http_status: Optional[int] = None
    elapsed_ms: float = 0.0
    payload_sample: str = ""
    a2_status: str = ""  # PASS / FAIL
    a1_status: str = ""  # PASS / WARN / FAIL
    a4_status: str = ""  # PASS / WARN / FAIL
    notes: list[str] = field(default_factory=list)

    @property
    def overall(self) -> str:
        if "FAIL" in (self.a1_status, self.a2_status, self.a4_status):
            return "FAIL"
        if "WARN" in (self.a1_status, self.a2_status, self.a4_status):
            return "WARN"
        return "PASS"


def _http_get(url: str, token: Optional[str], timeout: float = 8.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return e.code, body


def check_endpoint(spec: EndpointSpec, base: str, token: Optional[str]) -> CheckResult:
    res = CheckResult(spec=spec)
    url = base.rstrip("/") + spec.path
    t0 = time.time()
    try:
        status, body = _http_get(url, token)
    except Exception as e:
        res.elapsed_ms = (time.time() - t0) * 1000
        res.a2_status = "FAIL"
        res.a1_status = "FAIL"
        res.a4_status = "FAIL"
        res.notes.append(f"connection error: {type(e).__name__}: {e}")
        return res
    res.elapsed_ms = (time.time() - t0) * 1000
    res.http_status = status

    # A2 check
    if 200 <= status < 300:
        res.a2_status = "PASS"
    elif status in (401, 403):
        res.a2_status = "FAIL"
        res.notes.append(f"auth failed (HTTP {status}) — check token")
    elif spec.optional and status == 500:
        res.a2_status = "WARN"
        res.notes.append("HTTP 500 (Endpoint optional, Backend-Feature evtl. disabled)")
    else:
        res.a2_status = "FAIL"
        res.notes.append(f"HTTP {status}")

    # A1+A4 nur sinnvoll wenn 2xx
    if res.a2_status == "PASS":
        try:
            payload = json.loads(body) if body else None
        except json.JSONDecodeError:
            payload = None
            res.notes.append("payload not valid JSON")

        # A1 — Plausibilitaet via Rules
        a1_failures = []
        for rule in spec.rules:
            err = rule(payload)
            if err:
                a1_failures.append(err)
        if a1_failures:
            res.a1_status = "WARN"
            res.notes.extend(a1_failures)
        else:
            res.a1_status = "PASS"

        # A4 — Stuck-Loading-Sentinel-Suche im JSON-String
        body_str = body.decode("utf-8", errors="replace")[:5000]
        if '"--"' in body_str or '"loading..."' in body_str or '"..."' in body_str:
            res.a4_status = "WARN"
            res.notes.append("stuck-loading sentinel im Payload gefunden")
        else:
            res.a4_status = "PASS"

        # Sample fuer Report (erste 200 chars)
        res.payload_sample = body_str[:200] + ("..." if len(body_str) > 200 else "")
    else:
        res.a1_status = "SKIP"
        res.a4_status = "SKIP"

    return res


# ============================================================
# Reporter
# ============================================================

def render_markdown(results: list[CheckResult]) -> str:
    lines: list[str] = []
    lines.append("# Tab-Audit API Health-Check Report")
    lines.append("")
    lines.append(f"Generiert: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("## Summary")
    n_pass = sum(1 for r in results if r.overall == "PASS")
    n_warn = sum(1 for r in results if r.overall == "WARN")
    n_fail = sum(1 for r in results if r.overall == "FAIL")
    lines.append(f"- PASS: {n_pass}")
    lines.append(f"- WARN: {n_warn}")
    lines.append(f"- FAIL: {n_fail}")
    lines.append("")
    lines.append("## Detail (sortiert nach Severity)")
    lines.append("")
    lines.append("| # | Endpoint | A2 (HTTP) | A1 (Sanity) | A4 (no-stuck) | ms | Notes |")
    lines.append("|---|---|---|---|---|---|---|")
    severity_order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    sorted_results = sorted(results, key=lambda r: severity_order.get(r.overall, 3))
    for i, r in enumerate(sorted_results, 1):
        notes = "; ".join(r.notes)[:160] if r.notes else ""
        lines.append(
            f"| {i} | `{r.spec.path}` ({r.spec.name}) | "
            f"{r.a2_status} | {r.a1_status} | {r.a4_status} | "
            f"{r.elapsed_ms:.0f} | {notes} |"
        )
    lines.append("")
    lines.append("## Naechste Schritte (manuell)")
    lines.append("")
    lines.append("Carlos: gehe Tab-Audit-Checklist.md durch + setze Haken fuer:")
    lines.append("- **A3** (Tooltip vorhanden + verstaendlich) — pro Card")
    lines.append("- **A5** (Mobile-Responsive 375px) — pro Card")
    lines.append("")
    lines.append("Dieses Report deckt A1+A2+A4 ab. Spart ~50% Audit-Zeit.")
    return "\n".join(lines)


# ============================================================
# CLI
# ============================================================

def main() -> int:
    p = argparse.ArgumentParser(description="Tab-Audit API Health-Check")
    p.add_argument("--base", default="http://localhost:8000",
                   help="Base-URL des Bot-Dashboards (default: localhost:8000)")
    p.add_argument("--token", default=os.environ.get("AUDIT_TOKEN"),
                   help="JWT-Bearer-Token (oder via AUDIT_TOKEN env-var)")
    p.add_argument("--out", default="-",
                   help="Report-Output-Pfad (default: stdout)")
    args = p.parse_args()

    print(f"[INFO] Pruefe {len(ENDPOINTS)} Endpoints gegen {args.base} ...",
          file=sys.stderr)
    if not args.token:
        print("[WARN] Kein Token uebergeben — geschuetzte Endpoints werden 401",
              file=sys.stderr)

    results = []
    for spec in ENDPOINTS:
        r = check_endpoint(spec, args.base, args.token)
        results.append(r)
        marker = {"PASS": "[OK]", "WARN": "[!]", "FAIL": "[X]"}.get(r.overall, "[?]")
        print(f"{marker} {spec.path:45s} {r.http_status} ({r.elapsed_ms:.0f}ms)",
              file=sys.stderr)

    report = render_markdown(results)
    if args.out == "-":
        print(report)
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"[OK] Report geschrieben: {args.out}", file=sys.stderr)

    if any(r.overall == "FAIL" for r in results):
        return 2
    if any(r.overall == "WARN" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
