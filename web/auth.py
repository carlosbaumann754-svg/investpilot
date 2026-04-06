"""
InvestPilot - Authentication
JWT-basiertes Login mit bcrypt Passwort-Hashing.
"""

import os
import secrets
import logging
from datetime import datetime, timedelta

from fastapi import Request, HTTPException
from jose import jwt, JWTError
import bcrypt as _bcrypt

log = logging.getLogger("Auth")

# Config aus Umgebungsvariablen
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# JWT Secret: Env-Variable verwenden oder sicheren Fallback generieren
_jwt_env = os.environ.get("JWT_SECRET", "")
if _jwt_env and _jwt_env != "CHANGE_THIS_DEFAULT_SECRET_KEY_NOW":
    JWT_SECRET = _jwt_env
else:
    JWT_SECRET = secrets.token_hex(32)
    log.warning("SICHERHEITSWARNUNG: JWT_SECRET nicht gesetzt! "
                "Verwende zufaellig generierten Key (Sessions ueberleben keinen Restart). "
                "Setze JWT_SECRET als Umgebungsvariable fuer Production!")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24

# Passwort-Hash beim Start generieren
_password_hash = None


def _get_password_hash():
    """Lazy-Init: Hash das Passwort aus der .env beim ersten Aufruf."""
    global _password_hash
    if _password_hash is None and DASHBOARD_PASSWORD:
        _password_hash = _bcrypt.hashpw(
            DASHBOARD_PASSWORD.encode("utf-8"), _bcrypt.gensalt()
        )
    return _password_hash


def verify_password(plain_password: str) -> bool:
    """Pruefe Passwort gegen den Hash."""
    if not DASHBOARD_PASSWORD:
        log.error("DASHBOARD_PASSWORD nicht gesetzt!")
        return False
    stored = _get_password_hash()
    if stored:
        return _bcrypt.checkpw(plain_password.encode("utf-8"), stored)
    return plain_password == DASHBOARD_PASSWORD


def create_token(username: str) -> str:
    """Erstelle JWT Token."""
    expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRY_HOURS)
    payload = {
        "sub": username,
        "exp": expire,
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode und validiere JWT Token."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        return None


async def verify_request(request: Request) -> str:
    """Middleware: Pruefe ob Request authentifiziert ist."""
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token fehlt")

    token = auth_header[7:]
    payload = decode_token(token)

    if not payload:
        raise HTTPException(status_code=401, detail="Ungueltiger oder abgelaufener Token")

    return payload.get("sub", "unknown")


def authenticate_user(username: str, password: str) -> str:
    """Login: Username/Password pruefen, Token zurueckgeben."""
    if username != DASHBOARD_USERNAME:
        return None
    if not verify_password(password):
        return None
    return create_token(username)
