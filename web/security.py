"""
InvestPilot - Security Module
Rate Limiting, IP-Banning, Audit Logging, Security Middleware.
"""

import os
import sqlite3
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config_manager import get_data_path

log = logging.getLogger("Security")

DB_PATH = get_data_path("audit.db")
_db_lock = threading.Lock()

# Konfiguration
MAX_FAILED_LOGINS_SOFT = 5    # -> 1h Ban
MAX_FAILED_LOGINS_HARD = 10   # -> 24h Ban
FAILED_LOGIN_WINDOW = 15      # Minuten
RATE_LIMIT_LOGIN = 5          # pro Minute
RATE_LIMIT_API = 60           # pro Minute


def init_db():
    """Erstelle Security-Tabellen falls noetig."""
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS failed_logins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address TEXT NOT NULL,
                username_attempted TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS banned_ips (
                ip_address TEXT PRIMARY KEY,
                banned_until DATETIME NOT NULL,
                reason TEXT
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                ip_address TEXT,
                username TEXT,
                action TEXT NOT NULL,
                detail TEXT,
                severity TEXT DEFAULT 'INFO'
            );

            CREATE TABLE IF NOT EXISTS rate_limits (
                ip_address TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_failed_ip ON failed_logins(ip_address, timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_rate ON rate_limits(ip_address, endpoint, timestamp);
        """)
        conn.close()


# Initialisierung
init_db()


def _get_conn():
    return sqlite3.connect(str(DB_PATH))


# ============================================================
# IP BANNING
# ============================================================

def is_banned(ip: str) -> bool:
    """Pruefe ob eine IP gebannt ist."""
    with _db_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT banned_until FROM banned_ips WHERE ip_address = ?", (ip,)
        ).fetchone()
        conn.close()

    if not row:
        return False

    banned_until = datetime.fromisoformat(row[0])
    if datetime.utcnow() > banned_until:
        # Ban abgelaufen -> entfernen
        with _db_lock:
            conn = _get_conn()
            conn.execute("DELETE FROM banned_ips WHERE ip_address = ?", (ip,))
            conn.commit()
            conn.close()
        return False
    return True


def ban_ip(ip: str, hours: int, reason: str):
    """IP fuer N Stunden bannen."""
    banned_until = (datetime.utcnow() + timedelta(hours=hours)).isoformat()
    with _db_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO banned_ips (ip_address, banned_until, reason) VALUES (?, ?, ?)",
            (ip, banned_until, reason)
        )
        conn.commit()
        conn.close()
    log.warning(f"IP BANNED: {ip} fuer {hours}h - {reason}")


# ============================================================
# FAILED LOGIN TRACKING
# ============================================================

def record_failed_login(ip: str, username: str):
    """Fehlgeschlagenen Login aufzeichnen und ggf. IP bannen."""
    with _db_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO failed_logins (ip_address, username_attempted) VALUES (?, ?)",
            (ip, username)
        )
        conn.commit()

        # Zaehle Fehlversuche in den letzten N Minuten
        cutoff = (datetime.utcnow() - timedelta(minutes=FAILED_LOGIN_WINDOW)).isoformat()
        count = conn.execute(
            "SELECT COUNT(*) FROM failed_logins WHERE ip_address = ? AND timestamp > ?",
            (ip, cutoff)
        ).fetchone()[0]
        conn.close()

    log.warning(f"Failed login from {ip} (User: {username}) - Count: {count}/{MAX_FAILED_LOGINS_SOFT}")

    if count >= MAX_FAILED_LOGINS_HARD:
        ban_ip(ip, 24, f"24h Ban: {count} fehlgeschlagene Logins")
        _trigger_alert(ip, count, 24)
    elif count >= MAX_FAILED_LOGINS_SOFT:
        ban_ip(ip, 1, f"1h Ban: {count} fehlgeschlagene Logins")
        _trigger_alert(ip, count, 1)
    elif count >= 3:
        _trigger_alert(ip, count, 0)


def _trigger_alert(ip: str, count: int, ban_hours: int):
    """Sicherheits-Alert ausloesen."""
    try:
        from web.alerts import send_security_alert
        if ban_hours > 0:
            send_security_alert(
                f"IP BANNED: {ip}",
                f"IP {ip} wurde fuer {ban_hours}h gebannt nach {count} fehlgeschlagenen Login-Versuchen."
            )
        else:
            send_security_alert(
                f"Verdaechtige Login-Versuche: {ip}",
                f"{count} fehlgeschlagene Login-Versuche von IP {ip} in den letzten {FAILED_LOGIN_WINDOW} Minuten."
            )
    except Exception as e:
        log.error(f"Alert senden fehlgeschlagen: {e}")


# ============================================================
# RATE LIMITING
# ============================================================

def check_rate_limit(ip: str, endpoint: str, max_per_minute: int) -> bool:
    """Pruefe Rate Limit. Returns True wenn erlaubt, False wenn ueberschritten."""
    cutoff = (datetime.utcnow() - timedelta(minutes=1)).isoformat()

    with _db_lock:
        conn = _get_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM rate_limits WHERE ip_address = ? AND endpoint = ? AND timestamp > ?",
            (ip, endpoint, cutoff)
        ).fetchone()[0]

        if count >= max_per_minute:
            conn.close()
            return False

        conn.execute(
            "INSERT INTO rate_limits (ip_address, endpoint) VALUES (?, ?)",
            (ip, endpoint)
        )
        # Alte Eintraege aufraumen (aelter als 5 Min)
        old_cutoff = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
        conn.execute("DELETE FROM rate_limits WHERE timestamp < ?", (old_cutoff,))
        conn.commit()
        conn.close()

    return True


# ============================================================
# AUDIT LOG
# ============================================================

async def log_audit(username: str, action: str, detail: str = "", severity: str = "INFO", ip: str = ""):
    """Eintrag ins Audit-Log schreiben."""
    with _db_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO audit_log (ip_address, username, action, detail, severity) VALUES (?, ?, ?, ?, ?)",
            (ip, username, action, detail, severity)
        )
        conn.commit()
        conn.close()


def get_audit_log(limit: int = 50):
    """Audit-Log lesen."""
    with _db_lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT timestamp, ip_address, username, action, detail, severity "
            "FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()

    return [{
        "timestamp": r[0], "ip": r[1], "user": r[2],
        "action": r[3], "detail": r[4], "severity": r[5],
    } for r in rows]


# ============================================================
# SECURITY MIDDLEWARE
# ============================================================

async def security_middleware(request: Request, call_next):
    """Middleware: IP-Ban Check, Rate Limiting, Security Headers."""
    ip = request.client.host if request.client else "unknown"

    # 1. IP Ban Check
    if is_banned(ip):
        await log_audit("", "BLOCKED_BANNED_IP", f"Gebannte IP versuchte Zugriff: {ip}", "WARNING", ip)
        return JSONResponse(status_code=403, content={"detail": "IP gesperrt"})

    # 2. Rate Limiting
    path = request.url.path
    if path == "/api/auth/login":
        if not check_rate_limit(ip, "login", RATE_LIMIT_LOGIN):
            return JSONResponse(status_code=429, content={"detail": "Zu viele Anfragen. Bitte warten."})
    elif path.startswith("/api/"):
        if not check_rate_limit(ip, "api", RATE_LIMIT_API):
            return JSONResponse(status_code=429, content={"detail": "Rate Limit ueberschritten"})

    # 3. Request ausfuehren
    response = await call_next(request)

    # 4. Security Headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    return response
