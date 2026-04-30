"""
Earnings-Exit-Filter (v37v) — schliesst Positionen vor Earnings.
================================================================

Problem: ROKU hatte am 30.04.2026 Earnings AfterClose mit Implied Move +/-12.6%
und 15% Portfolio-Position. Bestehende ``events_calendar.is_earnings_blackout``
blockt nur NEUE BUYs, NICHT die laufende Position. Stop-Loss=-3 schuetzt nicht
vor After-Hours-Earnings-Gap weil Bot nur RTH-Quotes prueft.

Loesung: Combined-Filter (Variante E aus User-Diskussion 30.04.):
  Trigger wenn Earnings in <= MAX_DAYS_BEFORE
  UND (Position > MIN_POSITION_PCT_PORTFOLIO
       ODER Volatility-Proxy > MIN_VOLA_PCT)

Source-of-Truth fuer Earnings-Termine: yfinance via events_calendar._fetch_earnings_date.

Volatility-Proxy: historische 30-Tage-Standard-Deviation der Daily-Returns
als Stand-In fuer Implied Move (aus Optionsmarkt). yfinance liefert
historische Daten gratis, Implied Volatility waere Finnhub Premium.
Empirische Korrelation hist-Vola ↔ Earnings-Implied-Move ist ~0.7-0.8 fuer
Mid+Large-Caps — gut genug als Filter-Trigger.

Public API
----------
check_earnings_exit(
    symbol: str,
    position_value_usd: float,
    portfolio_value_usd: float,
    config: dict,
) -> tuple[bool, str | None]

Returns (should_exit, reason).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# Defaults (per config.market_context.earnings_exit_* override)
# ============================================================
DEFAULT_MAX_DAYS_BEFORE = 1            # Trigger wenn Earnings <= 1 Tag entfernt
DEFAULT_MIN_POSITION_PCT = 10.0        # Position > 10% Portfolio
DEFAULT_MIN_VOLA_PCT = 8.0             # 30d-Std > 8% (annualisiert ueberproportional)
DEFAULT_LOOKBACK_DAYS_VOLA = 30        # Lookback fuer Volatility-Proxy

#: Exemption-Liste-Datei (v37x): Symbole die der User bewusst durch
#: Earnings halten will. Filter ueberspringt diese.
EXEMPTIONS_FILE = "earnings_exit_exemptions.json"


# ============================================================
# Volatility-Proxy via yfinance
# ============================================================

_vola_cache: dict[str, tuple[float, float]] = {}  # symbol -> (vola_pct, fetched_at_ts)
_VOLA_CACHE_TTL_SEC = 3600  # 1h cache, Earnings-Filter laeuft pro Cycle


def _fetch_volatility_proxy(symbol: str, lookback_days: int = DEFAULT_LOOKBACK_DAYS_VOLA) -> Optional[float]:
    """30-Tage Standard-Deviation der Daily-Returns in Prozent.

    Returns None wenn yfinance nicht verfuegbar oder kein Datum.
    Cache 1h damit Cycle-Loop nicht jeden Symbol neu fragt.
    """
    now_ts = datetime.now(timezone.utc).timestamp()
    cached = _vola_cache.get(symbol)
    if cached and (now_ts - cached[1]) < _VOLA_CACHE_TTL_SEC:
        return cached[0]

    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=f"{lookback_days + 5}d")
        if hist is None or len(hist) < lookback_days // 2:
            return None
        # Daily-Returns
        closes = hist["Close"].pct_change().dropna()
        if len(closes) < 5:
            return None
        # Std in % (nicht annualisiert)
        vola_pct = float(closes.std() * 100)
        _vola_cache[symbol] = (vola_pct, now_ts)
        return vola_pct
    except Exception as e:
        logger.debug(f"Vola-Proxy fuer {symbol} fehlgeschlagen: {e}")
        return None


# ============================================================
# Hauptfunktion
# ============================================================

def load_exemptions() -> set[str]:
    """v37x: Symbole die vom Earnings-Exit-Filter ausgenommen sind.

    User kann bewusst Position halten (z.B. wenn er auf positive Earnings spielt).
    Persistiert in data/earnings_exit_exemptions.json mit Audit-Trail.

    v37y: vor jedem Read laeuft Auto-Cleanup — entfernt Symbols mit
    Earnings in der Vergangenheit (One-shot-Exemption-Pattern).
    """
    cleanup_expired_exemptions()  # v37y: stille Auto-Cleanup
    try:
        from app.config_manager import load_json
        data = load_json(EXEMPTIONS_FILE) or {}
        return set(data.get("exempt_symbols", []) or [])
    except Exception as e:
        logger.debug(f"Exemption-Liste nicht ladbar: {e}")
        return set()


def add_exemption(
    symbol: str,
    reason: str = "manual",
    auto_cleanup_after_earnings: bool = True,
    earnings_date: Optional[datetime] = None,
) -> None:
    """Fuegt ein Symbol zur Exemption-Liste hinzu (idempotent).

    v37y: One-shot-Pattern (Default):
        auto_cleanup_after_earnings=True (default) -> Exemption gilt nur fuer
        DAS naechste Earnings. Sobald earnings_date in der Vergangenheit ist,
        wird Symbol automatisch entfernt + Pushover-Alert.

        auto_cleanup_after_earnings=False -> Exemption ist persistent (legacy).
        User muss selber via remove_exemption() entfernen.

    Args:
        symbol: Ticker (case-insensitive)
        reason: Audit-Trail-Begruendung
        auto_cleanup_after_earnings: One-shot-Default (empfohlen).
        earnings_date: Wenn None, wird via _fetch_earnings_date geholt.
                       Notwendig fuer auto_cleanup zum Auto-Remove-Trigger.
    """
    from app.config_manager import load_json, save_json
    data = load_json(EXEMPTIONS_FILE) or {}
    exempt = set(data.get("exempt_symbols", []) or [])
    exempt.add(symbol.upper())
    data["exempt_symbols"] = sorted(exempt)

    # v37y: Auto-Cleanup-Metadata
    if auto_cleanup_after_earnings:
        if earnings_date is None:
            try:
                from app.events_calendar import _fetch_earnings_date
                earnings_date = _fetch_earnings_date(symbol)
            except Exception:
                pass
        if earnings_date is not None:
            auto_cleanup = data.setdefault("auto_cleanup", {})
            auto_cleanup[symbol.upper()] = {
                "earnings_date": earnings_date.strftime("%Y-%m-%d"),
                "set_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
            }

    audit = data.setdefault("audit", [])
    audit.append({
        "symbol": symbol.upper(),
        "added_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "action": "ADD",
        "auto_cleanup": auto_cleanup_after_earnings,
        "earnings_date": (earnings_date.strftime("%Y-%m-%d")
                          if earnings_date else None),
    })
    save_json(EXEMPTIONS_FILE, data)


def remove_exemption(symbol: str, reason: str = "manual") -> None:
    """Entfernt ein Symbol von der Exemption-Liste — Filter wird wieder aktiv."""
    from app.config_manager import load_json, save_json
    data = load_json(EXEMPTIONS_FILE) or {}
    exempt = set(data.get("exempt_symbols", []) or [])
    exempt.discard(symbol.upper())
    data["exempt_symbols"] = sorted(exempt)

    # v37y: auch aus auto_cleanup-Map entfernen
    auto_cleanup = data.get("auto_cleanup", {}) or {}
    if symbol.upper() in auto_cleanup:
        del auto_cleanup[symbol.upper()]
        data["auto_cleanup"] = auto_cleanup

    audit = data.setdefault("audit", [])
    audit.append({
        "symbol": symbol.upper(),
        "added_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "action": "REMOVE",
    })
    save_json(EXEMPTIONS_FILE, data)


def cleanup_expired_exemptions() -> list[str]:
    """v37y: One-shot-Cleanup — entfernt Exemptions deren Earnings vorbei sind.

    Wird automatisch beim load_exemptions() aufgerufen (vor jedem Filter-Check).
    Returns: Liste der entfernten Symbols.
    """
    try:
        from app.config_manager import load_json, save_json
    except Exception:
        return []

    try:
        data = load_json(EXEMPTIONS_FILE) or {}
    except Exception:
        return []

    auto_cleanup = data.get("auto_cleanup", {}) or {}
    if not auto_cleanup:
        return []

    today = datetime.now(timezone.utc).date()
    removed = []

    for symbol, meta in list(auto_cleanup.items()):
        ed_str = meta.get("earnings_date")
        if not ed_str:
            continue
        try:
            earnings_date = datetime.fromisoformat(ed_str).date()
        except Exception:
            continue

        # Earnings vorbei? -> auto-remove
        # +1 Tag Puffer damit Earnings-Tag selbst noch exempt bleibt
        # (Earnings-AfterClose = Bot kann erst naechsten Tag reagieren)
        if earnings_date < today:
            removed.append(symbol)
            # Symbol aus exempt_symbols entfernen
            exempt = set(data.get("exempt_symbols", []) or [])
            exempt.discard(symbol)
            data["exempt_symbols"] = sorted(exempt)
            # Aus auto_cleanup entfernen
            del auto_cleanup[symbol]
            # Audit-Trail
            audit = data.setdefault("audit", [])
            audit.append({
                "symbol": symbol,
                "added_at": datetime.now(timezone.utc).isoformat(),
                "reason": f"auto-cleanup-after-earnings-{ed_str}",
                "action": "AUTO_REMOVE",
            })

    if removed:
        data["auto_cleanup"] = auto_cleanup
        try:
            save_json(EXEMPTIONS_FILE, data)
        except Exception as e:
            logger.warning(f"Auto-Cleanup save fehlgeschlagen: {e}")

        # Pushover-Alert
        try:
            from app.alerts import send_alert
            sym_list = ", ".join(removed)
            send_alert(
                f"Earnings-Exemption automatisch entfernt: {sym_list}. "
                f"Earnings vorbei -> Filter ist wieder aktiv fuer naechstes Quartal.",
                level="INFO",
            )
        except Exception as e:
            logger.debug(f"Auto-Cleanup-Pushover fehlgeschlagen: {e}")

        logger.info(f"Auto-Cleanup: {len(removed)} Exemption(s) entfernt: {sym_list}")

    return removed


def check_earnings_exit(
    symbol: str,
    position_value_usd: float,
    portfolio_value_usd: float,
    config: Optional[dict] = None,
) -> tuple[bool, Optional[str]]:
    """Pruefe ob eine offene Position vor Earnings geschlossen werden sollte.

    Args:
        symbol: Ticker (z.B. "ROKU")
        position_value_usd: aktueller Marktwert der Position
        portfolio_value_usd: Gesamtportfolio-Wert (fuer Position-Size-Pct)
        config: Bot-Config (liest market_context.earnings_exit_*)

    Returns:
        (should_exit, reason). reason ist None wenn nicht trigger.

    Variante-E-Logik:
        - Earnings <= max_days_before AND
        - (position_pct > min_position_pct OR vola_pct > min_vola_pct)
    """
    cfg = (config or {}).get("market_context", {}) if config else {}

    # Master-Switch (default: aktiv)
    if not cfg.get("earnings_exit_enabled", True):
        return False, None

    # v37x: Symbol-Exemption (User-Override "halten trotz Earnings")
    if symbol.upper() in load_exemptions():
        return False, None

    max_days = int(cfg.get("earnings_exit_max_days_before", DEFAULT_MAX_DAYS_BEFORE))
    min_pos_pct = float(cfg.get("earnings_exit_min_position_pct", DEFAULT_MIN_POSITION_PCT))
    min_vola = float(cfg.get("earnings_exit_min_vola_pct", DEFAULT_MIN_VOLA_PCT))

    # 1. Earnings-Datum holen
    try:
        from app.events_calendar import _fetch_earnings_date
        earnings_dt = _fetch_earnings_date(symbol)
    except Exception as e:
        logger.debug(f"Earnings-Date Lookup fehlgeschlagen fuer {symbol}: {e}")
        return False, None

    if earnings_dt is None:
        return False, None  # kein Earnings-Termin bekannt

    # Tage bis Earnings (negativ = Earnings schon vorbei)
    now = datetime.now()
    if earnings_dt.tzinfo is not None and now.tzinfo is None:
        # earnings_dt ist tz-aware, now nicht — angleichen
        now = datetime.now(earnings_dt.tzinfo)
    days_until = (earnings_dt - now).days

    if days_until < 0 or days_until > max_days:
        return False, None  # ausserhalb Trigger-Fenster

    # 2. Trigger-Kriterien Variante E
    pos_pct = (position_value_usd / portfolio_value_usd * 100) if portfolio_value_usd > 0 else 0

    vola_pct = _fetch_volatility_proxy(symbol)
    vola_str = f"{vola_pct:.1f}%" if vola_pct is not None else "n/a"

    pos_trigger = pos_pct > min_pos_pct
    vola_trigger = vola_pct is not None and vola_pct > min_vola

    if not (pos_trigger or vola_trigger):
        return False, None  # kein Trigger

    triggers = []
    if pos_trigger:
        triggers.append(f"Position {pos_pct:.1f}% > {min_pos_pct}%")
    if vola_trigger:
        triggers.append(f"Vola {vola_str} > {min_vola}%")

    reason = (
        f"Earnings in {days_until} Tag(en) ({earnings_dt.strftime('%Y-%m-%d')}) "
        f"+ {' AND '.join(triggers)}"
    )
    return True, reason


# ============================================================
# Status-Lookup fuer Dashboard
# ============================================================

def get_pending_earnings_for_positions(
    positions: list[dict],
    portfolio_value_usd: float,
    config: Optional[dict] = None,
) -> list[dict]:
    """Liefert Liste der Positionen mit anstehenden Earnings.

    Fuer Dashboard-Card 'Earnings-Watchlist'. Returnt **alle** Positionen
    mit Earnings <= 7 Tage entfernt, auch ohne Trigger — UI-Zwecke.
    """
    out = []
    for p in positions:
        symbol = p.get("symbol")
        if not symbol:
            continue
        try:
            from app.events_calendar import _fetch_earnings_date
            earnings_dt = _fetch_earnings_date(symbol)
            if earnings_dt is None:
                continue
            now = datetime.now()
            if earnings_dt.tzinfo is not None and now.tzinfo is None:
                now = datetime.now(earnings_dt.tzinfo)
            days_until = (earnings_dt - now).days
            if days_until < 0 or days_until > 7:
                continue
            pos_value = abs(float(p.get("amount", 0) or 0))
            pos_pct = (pos_value / portfolio_value_usd * 100) if portfolio_value_usd > 0 else 0
            vola_pct = _fetch_volatility_proxy(symbol)
            would_exit, reason = check_earnings_exit(
                symbol, pos_value, portfolio_value_usd, config
            )
            out.append({
                "symbol": symbol,
                "earnings_date": earnings_dt.strftime("%Y-%m-%d"),
                "days_until": days_until,
                "position_value_usd": round(pos_value, 2),
                "position_pct": round(pos_pct, 2),
                "vola_pct_30d": round(vola_pct, 2) if vola_pct else None,
                "would_exit": would_exit,
                "reason": reason,
            })
        except Exception as e:
            logger.debug(f"Earnings-Watchlist Fehler {symbol}: {e}")
    return out
