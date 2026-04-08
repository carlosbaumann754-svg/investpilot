"""
InvestPilot - 2FA / TOTP Modul

Verwaltet TOTP-Secrets, Recovery-Codes und Verifikation.
Persistenz in data/auth_2fa.json (Gist-gesichert).
"""

import json
import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional

import bcrypt as _bcrypt
import pyotp
import qrcode
from qrcode.image.svg import SvgImage
from io import BytesIO
import base64

from app.config_manager import get_data_path

log = logging.getLogger("Auth2FA")

_2FA_FILE = "auth_2fa.json"
ISSUER = "InvestPilot"
RECOVERY_CODE_COUNT = 8


def _get_2fa_path() -> Path:
    return Path(get_data_path(_2FA_FILE))


def _read_2fa() -> dict:
    """Liest 2FA-State aus der JSON. Leeres dict wenn nicht vorhanden."""
    path = _get_2fa_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"2FA-State Lesefehler: {e}")
        return {}


def _write_2fa(data: dict) -> bool:
    """Schreibt 2FA-State atomar."""
    path = _get_2fa_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)
        return True
    except Exception as e:
        log.error(f"2FA-State Schreibfehler: {e}")
        return False


def is_enabled() -> bool:
    """True wenn 2FA aktiviert und einsatzbereit."""
    state = _read_2fa()
    return bool(state.get("enabled")) and bool(state.get("totp_secret"))


def get_status() -> dict:
    """Status-Info fuer das Frontend (ohne Secret)."""
    state = _read_2fa()
    return {
        "enabled": bool(state.get("enabled")),
        "setup_at": state.get("setup_at"),
        "recovery_codes_remaining": len(state.get("recovery_codes", [])),
    }


def _generate_recovery_codes(n: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Erzeugt n menschenfreundliche Recovery-Codes (XXXX-XXXX Format)."""
    codes = []
    for _ in range(n):
        raw = secrets.token_hex(4).upper()  # 8 hex chars
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes


def _hash_recovery_code(code: str) -> str:
    """bcrypt-Hash eines Recovery-Codes (Case-insensitive: vorher uppercased)."""
    normalized = code.strip().upper().replace(" ", "")
    return _bcrypt.hashpw(normalized.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def _verify_recovery_code(code: str, hashes: list[str]) -> Optional[int]:
    """Prueft ob code zu einem der hashes passt. Returns Index des Treffers oder None."""
    normalized = code.strip().upper().replace(" ", "")
    for idx, h in enumerate(hashes):
        try:
            if _bcrypt.checkpw(normalized.encode("utf-8"), h.encode("utf-8")):
                return idx
        except Exception:
            continue
    return None


def begin_setup(username: str) -> dict:
    """Startet Setup: erzeugt neues Secret + Recovery-Codes, ABER setzt enabled=False
    bis confirm_setup() mit gueltigem TOTP-Code aufgerufen wurde.

    Returns:
        {
          "secret": "BASE32...",       # nur fuer QR-Code Anzeige
          "provisioning_uri": "...",   # otpauth:// URI
          "qr_svg_b64": "...",         # base64-encoded SVG
          "recovery_codes": [...]      # plain, einmalig zur Anzeige
        }
    """
    secret = pyotp.random_base32()
    recovery_codes = _generate_recovery_codes()

    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=username, issuer_name=ISSUER)

    # SVG QR-Code erzeugen (kein PIL noetig)
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgImage)
    buf = BytesIO()
    img.save(buf)
    svg_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    # Pending-State persistieren (enabled=False bis confirmed)
    state = _read_2fa()
    state["pending_secret"] = secret
    state["pending_recovery_hashes"] = [_hash_recovery_code(c) for c in recovery_codes]
    state["pending_started_at"] = datetime.utcnow().isoformat()
    _write_2fa(state)

    return {
        "secret": secret,
        "provisioning_uri": uri,
        "qr_svg_b64": svg_b64,
        "recovery_codes": recovery_codes,
    }


def confirm_setup(code: str) -> bool:
    """Bestaetigt Setup mit erstem TOTP-Code. Aktiviert 2FA wenn gueltig."""
    state = _read_2fa()
    secret = state.get("pending_secret")
    hashes = state.get("pending_recovery_hashes")
    if not secret or not hashes:
        return False

    totp = pyotp.TOTP(secret)
    if not totp.verify(code.strip(), valid_window=1):
        return False

    # Pending → Active
    state["totp_secret"] = secret
    state["recovery_codes"] = hashes
    state["enabled"] = True
    state["setup_at"] = datetime.utcnow().isoformat()
    state.pop("pending_secret", None)
    state.pop("pending_recovery_hashes", None)
    state.pop("pending_started_at", None)
    _write_2fa(state)
    return True


def verify_totp(code: str) -> bool:
    """Verifiziert einen 6-stelligen TOTP-Code."""
    state = _read_2fa()
    secret = state.get("totp_secret")
    if not secret or not state.get("enabled"):
        return False
    totp = pyotp.TOTP(secret)
    return totp.verify(code.strip(), valid_window=1)


def verify_recovery_code(code: str) -> bool:
    """Verifiziert einen Recovery-Code und verbraucht ihn (one-time use)."""
    state = _read_2fa()
    hashes = state.get("recovery_codes", [])
    if not hashes or not state.get("enabled"):
        return False
    idx = _verify_recovery_code(code, hashes)
    if idx is None:
        return False
    # Code verbraucht — entfernen
    hashes.pop(idx)
    state["recovery_codes"] = hashes
    _write_2fa(state)
    log.info(f"Recovery-Code verwendet, {len(hashes)} verbleiben")
    return True


def disable(totp_code: str) -> bool:
    """Deaktiviert 2FA. Erfordert gueltigen TOTP-Code als Bestaetigung."""
    if not verify_totp(totp_code):
        return False
    state = _read_2fa()
    state["enabled"] = False
    state.pop("totp_secret", None)
    state.pop("recovery_codes", None)
    _write_2fa(state)
    return True
