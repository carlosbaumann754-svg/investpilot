"""
Insider-Performance-Tracker (v34)
==================================

Ziel: Lernen welche Insider tatsaechlich gute Prognostiker sind.
Bsp.: "Director Smith bei NVDA — seine Open-Market-Buys in den letzten 3 Jahren
fuehrten im Schnitt zu +18% in 6 Monaten. Sein naechster Buy ist mehr wert
als ein Buy von Routine-Kaeufer X."

Das ist der Kern-Mehrwert von CEO-Watcher Premium ($40/Mo). Wir bauen ihn
selbst, on top von Finnhub Free.

DESIGN:
- Daily Job: pro Symbol im Universum die letzten Insider-Buys laden
- Fuer jeden Buy aelter als 90 Tage: Return ab Buy-Datum berechnen (yfinance)
- Performance pro Insider-Name persistieren in data/insider_performance.json
  Schema: {insider_name: {n_buys, avg_30d, avg_90d, avg_180d, hit_rate_180d}}
- Dashboard kann Top-Insider rendern, Score-Logik kann Insider-Buys gewichten

WICHTIG: Anfangs leer. Erste verwertbare Daten nach ~30 Tagen Akkumulation,
robuste Statistiken nach ~6 Monaten. Bauen wir jetzt damit es ab heute
beginnt zu sammeln — nicht weil es morgen fertig ist.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("InsiderTracker")

PERF_PATH = Path(__file__).resolve().parent.parent / "data" / "insider_performance.json"

# Mindest-Zeit-Distanz vor Bewertung (Insider-Buys brauchen Reifezeit)
MIN_TX_AGE_DAYS = 90       # Trades juenger als 90 Tage nicht bewerten
MAX_TX_AGE_DAYS = 365 * 3  # Trades aelter als 3 Jahre ignorieren


def _load_perf() -> dict:
    if not PERF_PATH.exists():
        return {}
    try:
        return json.loads(PERF_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Performance-File korrupt ({e}) — starte leer")
        return {}


def _save_perf(data: dict) -> None:
    try:
        PERF_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PERF_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(PERF_PATH)
    except Exception as e:
        log.error(f"Performance-Save fehlgeschlagen: {e}")


def _compute_return_pct(symbol: str, from_date: datetime, days_forward: int) -> Optional[float]:
    """Pct-Return ab from_date fuer days_forward Tage. None bei Fehler."""
    try:
        import yfinance as yf
        end = min(from_date + timedelta(days=days_forward + 5), datetime.utcnow())
        if end <= from_date + timedelta(days=days_forward - 5):
            return None  # Future-Datum
        df = yf.download(
            symbol,
            start=from_date.date().isoformat(),
            end=end.date().isoformat(),
            interval="1d", progress=False, auto_adjust=True, threads=False,
        )
        if df is None or df.empty or len(df) < 2:
            return None
        closes = df["Close"]
        if hasattr(closes, "values"):
            vals = [float(v) for v in closes.values.flatten() if v == v]
        else:
            vals = list(closes)
        if len(vals) < 2:
            return None
        # Entry = erster Close ab Trade-Datum, Exit = Close nach days_forward
        target_idx = min(days_forward, len(vals) - 1)
        entry = vals[0]
        exit_price = vals[target_idx]
        if entry <= 0:
            return None
        return (exit_price - entry) / entry * 100.0
    except Exception as e:
        log.debug(f"Return-Calc fuer {symbol} {from_date.date()} fehlgeschlagen: {e}")
        return None


def update_insider_performance(symbols: list[str]) -> dict:
    """Zentrale Daily-Job-Funktion. Fuer jedes Symbol die Insider-Buys laden,
    fuer jeden Trade > 90 Tage alt die Returns berechnen, pro Insider aggregieren.

    Returns: Updated Performance-Dict (auch persistiert).
    """
    from app import finnhub_client

    if not finnhub_client.is_available():
        log.info("Finnhub nicht verfuegbar — Tracker pausiert")
        return _load_perf()

    perf = _load_perf()
    now = datetime.utcnow()
    cutoff_min = now - timedelta(days=MIN_TX_AGE_DAYS)
    cutoff_max = now - timedelta(days=MAX_TX_AGE_DAYS)

    new_records = 0
    for sym in symbols:
        try:
            txs = finnhub_client.fetch_insider_transactions(sym)
        except Exception as e:
            log.debug(f"Insider-Fetch {sym} fehler: {e}")
            continue

        for tx in txs:
            code = (tx.get("transactionCode") or "").strip().upper()
            if code != "P":  # Nur Open-Market Purchases bewerten
                continue
            try:
                change = int(tx.get("change", 0) or 0)
            except (ValueError, TypeError):
                continue
            if change <= 0:
                continue
            date_str = tx.get("transactionDate") or tx.get("filingDate") or ""
            try:
                tx_date = datetime.fromisoformat(date_str[:10])
            except (ValueError, TypeError):
                continue
            if tx_date < cutoff_max or tx_date > cutoff_min:
                continue  # Zu alt oder zu jung

            name = (tx.get("name") or "UNKNOWN").strip().upper()
            tx_id = f"{sym}|{name}|{date_str}|{change}"

            entry = perf.setdefault(name, {
                "n_buys": 0, "tx_ids": [],
                "returns_30d": [], "returns_90d": [], "returns_180d": [],
            })
            if tx_id in entry["tx_ids"]:
                continue  # Schon ausgewertet

            r30 = _compute_return_pct(sym, tx_date, 30)
            r90 = _compute_return_pct(sym, tx_date, 90)
            r180 = _compute_return_pct(sym, tx_date, 180) if (now - tx_date).days >= 180 else None

            entry["tx_ids"].append(tx_id)
            entry["n_buys"] += 1
            if r30 is not None: entry["returns_30d"].append(r30)
            if r90 is not None: entry["returns_90d"].append(r90)
            if r180 is not None: entry["returns_180d"].append(r180)
            new_records += 1

    # Aggregierte Stats pro Insider berechnen
    for name, entry in perf.items():
        for window in ("30d", "90d", "180d"):
            rs = entry.get(f"returns_{window}", [])
            if rs:
                entry[f"avg_{window}"] = round(sum(rs) / len(rs), 2)
                entry[f"hit_rate_{window}"] = round(sum(1 for r in rs if r > 0) / len(rs), 3)
            else:
                entry[f"avg_{window}"] = None
                entry[f"hit_rate_{window}"] = None
        entry["last_updated"] = now.isoformat()

    _save_perf(perf)
    log.info(f"Insider-Performance updated: {new_records} neue Records, "
             f"{len(perf)} Insider total")
    return perf


def get_insider_quality(insider_name: str) -> Optional[float]:
    """Liefert Score-Multiplier basierend auf historischer Hit-Rate des Insiders.

    Returns:
        > 1.0 wenn Insider historisch besser als Markt (180d hit_rate > 60%)
        1.0  wenn neutral oder unbekannt
        < 1.0 wenn historisch schlecht (Hit-Rate < 40%)
        None wenn weniger als 3 historische Trades verfuegbar
    """
    perf = _load_perf()
    name_upper = (insider_name or "").strip().upper()
    entry = perf.get(name_upper)
    if not entry:
        return None
    rs = entry.get("returns_180d", [])
    if len(rs) < 3:
        return None
    hit_rate = sum(1 for r in rs if r > 0) / len(rs)
    avg = sum(rs) / len(rs)

    # Multiplier-Logik (konservativ)
    if hit_rate >= 0.6 and avg >= 5.0:
        return 1.5  # Sehr guter Insider
    if hit_rate >= 0.5 and avg >= 0:
        return 1.0  # Durchschnitt
    if hit_rate < 0.4:
        return 0.5  # Schlechter Prognostiker
    return 1.0


def get_top_insiders(n: int = 10, min_trades: int = 5) -> list[dict]:
    """Liefert Top-N Insider nach 180d-Hit-Rate fuer Dashboard-Anzeige."""
    perf = _load_perf()
    candidates = []
    for name, entry in perf.items():
        rs = entry.get("returns_180d", [])
        if len(rs) < min_trades:
            continue
        candidates.append({
            "name": name,
            "n_trades": len(rs),
            "avg_180d_pct": entry.get("avg_180d"),
            "hit_rate_180d": entry.get("hit_rate_180d"),
        })
    candidates.sort(key=lambda x: (x["hit_rate_180d"] or 0, x["avg_180d_pct"] or 0), reverse=True)
    return candidates[:n]
