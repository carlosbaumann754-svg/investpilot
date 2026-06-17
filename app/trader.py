"""
InvestPilot - Trading Engine (v2)
Automatisches Portfolio-Management: Aufbau, Rebalancing, SL/TP.
Integriert: Risk Manager, Leverage Manager, Asset Filters,
Market Context, Execution Tracking, Alerts.
"""

import logging
import time
from datetime import datetime

from app.config_manager import load_config, save_json, load_json, _get_file_lock
from app.etoro_client import EtoroClient  # noqa: F401  — static parse_position() bleibt genutzt
from app.broker_base import get_broker

log = logging.getLogger("Trader")

# R-B11/S2 (07.06.2026): trade_history.json wird im selben Prozess von MEHREREN
# Threads geschrieben — dem Bot-Main-Loop (save_trade) UND dem ib_insync-
# orderStatusEvent-Callback (OrderStatusTracker._update_trade_history). Ohne Lock
# ueber die GESAMTE load->append->save-Sequenz gehen Updates verloren (Lost
# Update). record_snapshot bekam dafuer schon _BRAIN_LOCK (11.05. $21k-Drift),
# save_trade blieb offen. Wir nutzen den GETEILTEN per-File-RLock aus
# config_manager, damit BEIDE Schreiber (hier + Tracker) dasselbe Lock-Objekt
# halten; der RLock ist reentrant, daher deadlockt der innere load_json/save_json
# (das den Lock erneut nimmt) im selben Thread nicht.
_TRADE_HISTORY_LOCK = _get_file_lock("trade_history.json")


# R-A48 (28.05.2026 Sprint-Tag-17): Idempotenz-State fuer REGIME-HALT-Notifications.
# Vor R-A48 feuerte alert_regime_halt() bei JEDEM Bot-Cycle (~5 Min) in dem
# buy_allowed=False — bei mehrstuendigem HALT ~12 Pushes/h Cry-Wolf-Spam.
# Plus: alert_regime_resumed() wurde NIRGENDWO aufgerufen (Recovery-Alert
# fehlte komplett).
# Fix: Modul-Level-State trackt letzten Halt-Zustand. Bei Container-Restart
# resetted auf False — bewusst: nach Restart soll EIN Status-Recap-Alert
# kommen wenn HALT noch aktiv (sonst blind nach Daily-Restart 05:15 UTC).
_last_regime_halt_state = False


def _classify_regime_state_change(current_halt_active: bool,
                                   previous_halt_active: bool) -> str:
    """R-A48: Idempotenz-Klassifikation fuer REGIME-HALT-Notifications.

    Pure function — testbar ohne Bot-Context.

    Args:
        current_halt_active: True wenn HALT in diesem Cycle aktiv.
        previous_halt_active: True wenn HALT im letzten Cycle aktiv war.

    Returns:
        "halt"    — neuer Wechsel False -> True: alert_regime_halt feuern.
        "resumed" — Wechsel True -> False: alert_regime_resumed feuern.
        "none"    — kein State-Change: nichts tun (Anti-Cry-Wolf).
    """
    if current_halt_active and not previous_halt_active:
        return "halt"
    if not current_halt_active and previous_halt_active:
        return "resumed"
    return "none"


# v37h+3 (Sprint-Tag-9, 19.05.2026): Audit-Coverage-Marker
AUDIT_METADATA = {
    "purpose": "Trading-Engine: Buy/Sell-Loop, SL/TP-Exits, Trailing-SL, Time-Stop, Partial-Close, existing_symbols-Filter, Cooldown-Filter, R-A10 Symbol-Concentration-Hook",
    "config_section": "demo_trading",
    "state_files": ["trade_history.json", "buy_cooldown.json", "pending_closes.json", "partial_close_state.json"],
    "self_tests": ["tc_pending_closes_fresh", "tc_trading_flag_failclosed"],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "v1 (Foundation)",
}


def _canon_instrument_key(ref):
    """R-B1 Phase 3a (29.05.2026 Soak): kanonischer Instrument-Key.

    Symbol bevorzugt (stabil, broker-agnostisch, eindeutig auch fuer
    Discovery-Assets), Zahl-Fallback. Macht ALTE (etoro_id-Zahl) und NEUE
    (Symbol) Instrument-Referenzen vergleichbar → Position-Matching +
    Cooldown werden symbol-primaer OHNE persistierte Daten zu migrieren.

    Akzeptiert:
      - Bot-Symbol-str (in ASSET_UNIVERSE) -> direkt zurueck (eindeutig)
      - etoro_id Zahl/numerischer str -> Reverse-Lookup zu Symbol
      - sonst -> str(ref) (stabiler Fallback, nie Crash)

    Backward-Compat: alte Cooldown-Keys "6408" und neue "AAPL" normalisieren
    beide zu "AAPL" → interoperieren nahtlos, keine Migration noetig.
    """
    try:
        from app.market_scanner import ASSET_UNIVERSE, symbol_for_instrument_id
    except Exception:
        return str(ref)
    # Symbol direkt (haeufigster Fall: candidate["symbol"])
    if isinstance(ref, str) and ref in ASSET_UNIVERSE:
        return ref
    # Zahl/numerischer-str -> Reverse zu Symbol (eindeutig fuer echte IDs)
    sym = symbol_for_instrument_id(ref)
    if sym:
        return sym
    # Fallback: stabiler String (Discovery -1, unbekannte conIds, etc.)
    return str(ref)


def save_trade(trade_entry):
    """Trade-Historie persistent speichern.

    R-A41 (22.05.2026 Sprint-Tag-12): Bei Close-Trades mit pnl_pct triggert
    automatisch Meta-Labeler outcome-Update. Vorher landete kein outcome je
    in meta_labeling_shadow.json → Treffer-Quote war immer 0%.
    """
    # S2: gesamte load->append->save-Sequenz unter Lock (Lost-Update-Schutz
    # gegen den parallel schreibenden orderStatusEvent-Thread).
    with _TRADE_HISTORY_LOCK:
        history = load_json("trade_history.json") or []
        history.append(trade_entry)
        save_json("trade_history.json", history)

    # R-A41: Meta-Labeler outcome-Hook fuer Close-Events
    try:
        action = str(trade_entry.get("action", "")).upper()
        pnl_pct = trade_entry.get("pnl_pct")
        symbol = trade_entry.get("symbol")
        # Nur Close-Events mit bekanntem PnL + Symbol
        if (pnl_pct is not None and symbol and
            any(s in action for s in ("CLOSE", "STOP_LOSS", "TAKE_PROFIT",
                                       "TIME_STOP", "TRAILING"))):
            from app.meta_labeler import update_outcome_for_close
            update_outcome_for_close(
                symbol=symbol,
                pnl_pct=float(pnl_pct),
                close_timestamp=trade_entry.get("timestamp"),
                position_id=trade_entry.get("position_id"),
            )
    except Exception as e:
        # Non-fatal: trade-history-save soll nicht fail wegen meta-hook
        from app.config_manager import logging
        logging.getLogger(__name__).debug(
            f"R-A41 meta-outcome-hook fehlgeschlagen (non-fatal): {e}"
        )

    # R-A45 (24.05.2026): Discovery-Persist mark_traded-Hook fuer ALLE Trades
    # (BUY + CLOSE). Schutz vor 90d-Cleanup eines aktiv-getradeten Symbols.
    try:
        symbol = trade_entry.get("symbol")
        if symbol:
            from app.discovery_persist import mark_traded
            mark_traded(symbol)  # no-op wenn symbol nicht in persisted-Discoveries
    except Exception as e:
        from app.config_manager import logging
        logging.getLogger(__name__).debug(
            f"R-A45 mark_traded hook failed (non-fatal): {e}"
        )


def _trade_status_from_result(result) -> str:
    """v37dh (07.05.2026): Trade-Status aus IBKR-Result-Dict ableiten.

    Helper fuer alle 8 trade_entry-Stellen in trader.py. Vorher: hardcoded
    'executed'. Bug: heute morgen 10:13 COPPER-MISSED_FILL trotz v37df —
    Fix war nur in einem Code-Pfad (Portfolio-Aufbau). Scanner-Buy +
    SL/TP/Trailing/Earnings/Recovery-Pfade hatten weiter hardcoded 'executed'.

    Returns:
        'executed' wenn Result mit Filled-Status,
        'submitted' bei Pending,
        'cancelled' wenn IBKR Order zurueckgezogen,
        'rejected' wenn IBKR abgelehnt,
        'partial' bei Teilfill,
        'failed' wenn result is None (Order konnte nicht placed werden),
        'executed' Default fuer eToro/Backward-Compat.
    """
    if result is None:
        return "failed"
    if not isinstance(result, dict):
        return "executed"
    order = result.get("orderForOpen") or {}
    bot_status = _map_ibkr_status_to_bot_status(order.get("statusID"))
    # v37dj (10.06.2026): IBKR sendet NIE den Literal-Status "PartiallyFilled" —
    # ein teilgefuelltes Order, das (nach Fill-Timeout) storniert wird, endet
    # API-seitig als "Cancelled" mit filledQuantity>0. Der "partial"-Branch in
    # _map_ibkr_status_to_bot_status war damit toter Code. Robuste Erkennung
    # hier ueber die TATSAECHLICHEN Fills: cancelled/rejected MIT Fills => echte
    # Position => "partial", nicht "cancelled". Sonst: (a) Dashboard zeigt
    # gehaltene Position als "failed trade", (b) Zyklus-Budget wird nicht
    # dekrementiert (s.u. ~Z.2490), (c) Soak-Zaehler unterzaehlt echte Trades.
    # Fund 10.06.: CALM (774/912 gefuellt) + GIII (590/1836) -> "Cancelled".
    if bot_status in ("cancelled", "rejected"):
        try:
            filled = order.get("filledQuantity")
            avg = order.get("avgFillPrice")
            if (filled and float(filled) > 0) or (avg and float(avg) > 0):
                return "partial"
        except (TypeError, ValueError):
            pass
    return bot_status


def _ibkr_status_raw_from_result(result) -> str:
    """v37dh: Audit-Trail-Helper. Returnt original IBKR-statusID oder None."""
    if not isinstance(result, dict):
        return None
    order = result.get("orderForOpen") or {}
    return order.get("statusID")


def _map_ibkr_status_to_bot_status(ibkr_status: str) -> str:
    """v37df (07.05.2026): IBKR-Order-Status -> Bot-Trade-Status.

    Behebt das 'intent vs reality'-Logging-Pattern (3 Bugs heute Mittag/Abend
    gefunden: MISSED_FILL bei Cancelled, Leverage-Inkonsistenz, Position-Age).
    Statt hardcoded 'executed' im Trade-Log nutzen wir IBKR's tatsaechlichen
    Status. Reconcile-Code (v37db) skipt cancelled/rejected — damit greift
    der Filter automatisch.

    Mapping:
      Filled, ApiPending          -> 'executed'  (echte Execution)
      Submitted, PreSubmitted,
      PendingSubmit, PendingCancel -> 'submitted' (pending, kein MissedFill)
      Cancelled, ApiCancelled,
      Inactive                     -> 'cancelled' (Order zurueckgezogen)
      Rejected                     -> 'rejected'  (IBKR hat abgelehnt)
      PartiallyFilled              -> 'partial'   (Teilfill)
      Sonst                        -> 'executed'  (Backward-Compat eToro)
    """
    if not ibkr_status:
        return "executed"  # Backward-Compat (eToro liefert keinen statusID)
    s = str(ibkr_status).strip()
    if s == "Filled":
        return "executed"
    if s in ("Submitted", "PreSubmitted", "PendingSubmit", "PendingCancel",
             "ApiPending"):
        return "submitted"
    if s in ("Cancelled", "ApiCancelled", "Inactive"):
        return "cancelled"
    if s == "Rejected":
        return "rejected"
    if s == "PartiallyFilled":
        return "partial"
    return "executed"  # Default — unbekannter Status, eToro-Compat


def _has_recent_earnings_close(symbol: str, hours: int = 24) -> bool:
    """v37aa: Prueft ob fuer dieses Symbol in den letzten N Stunden bereits
    ein EARNINGS_BLACKOUT_CLOSE in trade_history.json geschrieben wurde.

    Verhindert Duplikate wenn close_position() submitted ist aber IBKR-Fill
    erst Pre-Market kommt — naechster Cycle wuerde sonst nochmal triggern.
    """
    try:
        from datetime import datetime, timezone, timedelta
        history = load_json("trade_history.json") or []
        cutoff_ts = datetime.now(timezone.utc) - timedelta(hours=hours)
        for t in reversed(history[-200:]):  # nur die letzten 200 reichen
            if t.get("action") != "EARNINGS_BLACKOUT_CLOSE":
                continue
            if (t.get("symbol") or "").upper() != symbol.upper():
                continue
            ts_str = t.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff_ts:
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False  # bei Fehler nicht blockieren


def _attach_fill_prices(trade_entry: dict, broker_result: dict | None) -> dict:
    """v37j: Reichert ein trade_entry um avg_fill_price + intended_price an.

    Diese Felder werden vom Cost-Model-Calibrator (E2) konsumiert, um
    die realisierte IBKR-Slippage pro Asset-Klasse zu schaetzen und
    nach 20+ Fills die hardcodierten Defaults aus app/cost_model.py
    zu ueberschreiben.

    Args:
        trade_entry: bestehender dict (wird in-place erweitert + zurueckgegeben).
        broker_result: das von client.buy/sell/close_position zurueckgegebene
                       dict. Sucht orderForOpen.avgFillPrice/intendedPrice.

    Werte=0 oder None werden NICHT geschrieben (Calibrator filtert sowieso).
    """
    if not isinstance(broker_result, dict):
        return trade_entry
    order = broker_result.get("orderForOpen") or {}
    if not isinstance(order, dict):
        return trade_entry
    avg = order.get("avgFillPrice")
    intended = order.get("intendedPrice")
    ref_quote = order.get("refQuote")
    try:
        if avg and float(avg) > 0:
            trade_entry["avg_fill_price"] = float(avg)
        if intended and float(intended) > 0:
            trade_entry["intended_price"] = float(intended)
        if ref_quote and float(ref_quote) > 0:
            trade_entry["ref_quote"] = float(ref_quote)
    except (TypeError, ValueError):
        pass
    return trade_entry


def _is_already_closed(result) -> bool:
    """v37ce: True wenn close_position Sentinel {_already_closed: True} zurueckgab.
    Heisst: Position war bei IBKR schon geschlossen (stale ib.portfolio()-Eintrag).
    -> KEIN FAILED loggen, KEIN erneuter Versuch im naechsten Cycle."""
    if isinstance(result, dict) and result.get("_already_closed"):
        return True
    return False


# ============================================================
# v37cu (04.05.2026): Anti-Loop-Idempotency-Schutz
# ============================================================
# CRITICAL-BUG-FIX nach Episode 04.05. mit BA + CPER:
# Bot machte 19x SELL fuer BA und 55x SELL fuer CPER innerhalb eines Tages.
# IBKR fillte ALLE Orders -> BA wurde von +238 LONG zu -1373 SHORT,
# CPER von +1408 zu -21'120 SHORT. v37ce Stale-Filter griff nicht weil
# sowohl ib.portfolio() als auch ib.positions() denselben stale-state
# hatten (beide einig auf altem qty>0, Cross-Check fand keine Diskrepanz).
#
# Loesung: file-based Pending-Close-Tracker. Vor jedem close_position-Call
# pruefen wir:
#   1. Hat IB gerade eine open Trade-Order fuer diese Position? -> skip
#   2. Haben wir in den letzten 60 Sek schon eine Close-Order rausgeschickt
#      fuer diese Position? -> skip
# Persistenz via data/pending_closes.json damit auch Cycle-uebergreifend
# (alle 5 Min) funktioniert.

_PENDING_CLOSE_COOLDOWN_SEC = 300  # 5 Min — eine ganze Cycle-Periode


def _parse_iso_safe(ts: str):
    """v37h Tab-Audit-Day-4 (14.05.2026): robust ISO-parse mit Z-Suffix-Support.

    Vorher: fromisoformat() crasht bei 'Z' (Python<3.11). Resultat: Eintraege
    mit ISO-Z-Format ueberlebten den Cleanup-Filter (= silent state-bloat).
    Jetzt: Z -> +00:00 ersetzen, dann tz-naive zurueck (Carlos-Bot nutzt
    durchgaengig naive datetimes fuer pending_closes).
    """
    from datetime import datetime
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _cleanup_pending_closes(
    max_age_hours: int = 24,
    presubmitted_stuck_hours: float = 4.0,
) -> int:
    """v37h Tab-Audit-Day-4 (14.05.2026): Periodischer Cleanup-Helper.
    v37h+2 (15.05.2026): erweitert um PreSubmitted-Stuck-Detection (Q3-7).

    Carlos's Self-Test-FAIL 14.05.: 2 stale-Eintraege in pending_closes
    blieben 28-30h drin weil Cleanup nur bei neuer Close-Order lief.
    Plus ISO-Z-Format crashte den Filter silent.

    Carlos's Self-Test-FAIL 15.05.: 1 stale 'PreSubmitted' mit
    filledQuantity=0 hing 23h drin weil PreSubmitted nicht in der
    Final-Status-Liste war und das Age-Gate (24h) noch nicht griff.
    IBKR-Standard: PreSubmitted-Orders ohne Fortschritt sind nach
    ~4h faktisch tot (Cancellation-Window).

    Cleanup-Kriterien (entfernt Eintrag wenn EINS davon zutrifft):
      1. submitted_at > max_age_hours her (Default 24h)
      2. result_summary enthaelt 'Filled' (Order ist final, kein Loop-Risk)
      3. result_summary enthaelt 'Cancelled' / 'Rejected' (analog final)
      4. submitted_at unparseable (=alter Bug, sicher zu entfernen)
      5. NEU v37h+2 (Q3-7): 'PreSubmitted' + filledQuantity=0 +
         age > presubmitted_stuck_hours -> entfernen (stuck order)

    Returns:
        Anzahl entfernter Eintraege.

    Aufruf-Stellen:
      - run_trading_cycle() bei Cycle-Start (NEU v37h+2: periodischer GC)
      - _check_close_idempotent() bei Close-Attempt (existierend)
      - _track_pending_close() nach Close-Submit (existierend)
    Idempotent — kann mehrmals pro Cycle laufen ohne Side-Effects.
    """
    try:
        pending = load_json("pending_closes.json") or {}
        if not pending:
            return 0
        from datetime import datetime as _dt, timedelta as _td
        cutoff_age = _dt.now() - _td(hours=max_age_hours)
        cutoff_presubmitted = _dt.now() - _td(hours=presubmitted_stuck_hours)
        kept = {}
        for k, v in pending.items():
            if not isinstance(v, dict):
                continue  # garbage entry
            summary = str(v.get("result_summary", ""))
            # Kriterium 2-3: Final-Status -> entfernen
            if any(s in summary for s in ("'Filled'", "'Cancelled'",
                                            "'Rejected'", "'ApiCancelled'")):
                continue
            # Kriterium 4: submitted_at unparseable -> weg
            dt = _parse_iso_safe(v.get("submitted_at"))
            if dt is None:
                continue
            # Kriterium 1: 24h Age-Limit (Default-Hard-Cap)
            if dt < cutoff_age:
                continue
            # Kriterium 5 (Q3-7): stuck PreSubmitted mit filledQuantity=0 + age > 4h
            if "'PreSubmitted'" in summary:
                if _is_zero_filled(summary) and dt < cutoff_presubmitted:
                    continue
            kept[k] = v
        removed = len(pending) - len(kept)
        if removed > 0:
            save_json("pending_closes.json", kept)
            log.debug(f"pending_closes Cleanup: {removed} stale-Eintraege entfernt")
        return removed
    except Exception as e:
        log.debug(f"pending_closes Cleanup fehlgeschlagen (non-fatal): {e}")
        return 0


def _is_zero_filled(summary: str) -> bool:
    """v37h+2 (Q3-7): Robust-Detection von 'filledQuantity: 0' im result_summary.

    Das result_summary-Feld ist ein str() vom IBKR-Order-Dict, daher
    diverse Quoting-Stile moeglich. Tolerant gegen Leerzeichen, Quotes,
    int vs float.

    Returns True wenn 'filledQuantity' explizit als 0 (oder 0.0) gefunden.
    Returns False wenn nicht gefunden oder > 0 (sicherer Default — bleibt
    drin solange wir nicht sicher sind dass nichts gefuellt wurde).
    """
    import re
    if not summary:
        return False
    # Matches: 'filledQuantity': 0, "filledQuantity":0, 'filledQuantity' : 0.0
    m = re.search(r"['\"]filledQuantity['\"]\s*:\s*([\d.]+)", summary)
    if not m:
        return False
    try:
        return float(m.group(1)) == 0.0
    except ValueError:
        return False


def _check_close_idempotent(client, instrument_id, is_partial=False) -> tuple[bool, str]:
    """v37cu: Pre-Close-Check vor jeder close_position-Aufruf.

    v37h (14.05.2026): ruft jetzt _cleanup_pending_closes() vorab auf
    um stale-Eintraege zu raeumen — verhindert Self-Test-Warnings und
    state-file-bloat.

    Returns:
        (skip: bool, reason: str)
        skip=True -> Bot soll close_position NICHT aufrufen.
    """
    if instrument_id is None:
        return False, ""

    # v37h: periodischer Cleanup vor Pre-Check
    _cleanup_pending_closes()

    # Check 1: persistent pending-close-tracker
    try:
        pending = load_json("pending_closes.json") or {}
        key = str(instrument_id)
        if key in pending:
            last_ts = pending[key].get("submitted_at")
            if last_ts:
                last_dt = _parse_iso_safe(last_ts)
                if last_dt is not None:
                    from datetime import datetime as _dt2
                    age_sec = (_dt2.now() - last_dt).total_seconds()
                    if age_sec < _PENDING_CLOSE_COOLDOWN_SEC:
                        # R-B11/E2: ein VOLLER/Risk-Close (is_partial=False) darf
                        # NICHT von einem pending PARTIAL-Close geblockt werden —
                        # sonst verzoegert sich ein Stop-Loss bis zu Cooldown-Sek,
                        # waehrend die Position weiter faellt. Nur gleichartige
                        # Closes blocken (partial-nach-partial, full-nach-full).
                        pending_is_partial = bool(pending[key].get("is_partial"))
                        if not (pending_is_partial and not is_partial):
                            return True, (
                                f"Anti-Loop: Close fuer {instrument_id} bereits "
                                f"vor {age_sec:.0f}s submitted (Cooldown "
                                f"{_PENDING_CLOSE_COOLDOWN_SEC}s)"
                            )
                        # else: voller Close nach pending Partial -> durchlassen
    except Exception:
        pass

    # Check 2: Live IB-Gateway Open-Trades (am verlaesslichsten)
    try:
        if hasattr(client, "_get_ib"):
            ib = client._get_ib()
            # v37do (11.06.2026): E6-Katastrophen-Stop AUSSCHLIESSEN — er ist eine
            # dauerhaft ruhende GTC-Schutzorder (Status PreSubmitted), KEIN laufender
            # Close. Ohne diesen Skip blockiert er den Anti-Loop und damit Trailing-SL
            # / Partial-Close / Stop-Loss fuer JEDE Position mit E6-Stop. Regression
            # nach dem E6-Backfill 10.06.: CERT-Trailing-SL + GIII-Tranche feuerten,
            # wurden aber alle als 'Anti-Loop: bereits PreSubmitted' uebersprungen.
            try:
                from app.ibkr_client import _CATASTROPHIC_ORDER_REF as _E6_REF
            except Exception:
                _E6_REF = "E6_CATASTROPHIC"
            for t in (ib.openTrades() or []):
                if not (t.contract and t.order and t.orderStatus):
                    continue
                if getattr(t.order, "orderRef", "") == _E6_REF:
                    continue  # Schutz-Stop, kein pending Close -> nicht mitzaehlen
                con_id = getattr(t.contract, "conId", None)
                if con_id and str(con_id) == str(instrument_id):
                    status = t.orderStatus.status or ""
                    if status in ("Submitted", "PreSubmitted",
                                   "PendingSubmit", "PendingCancel"):
                        return True, (
                            f"Anti-Loop: Open Trade fuer {instrument_id} "
                            f"bereits in Status '{status}' bei IBKR"
                        )
    except Exception:
        # Wenn IB-Check fehlschlaegt: nicht skippen, Trader sollte trotzdem
        # versuchen (Fallback auf normalen Pfad). Pending-Tracker oben
        # ist primaerer Schutz.
        pass

    return False, ""


def _track_pending_close(instrument_id, result, is_partial=False):
    """v37cu: Nach erfolgreicher close_position()-Aufruf das Submitted-
    Timestamp persistieren. Cleanup von >24h alten Eintraegen.

    R-B11/E2: is_partial wird mitgespeichert, damit ein spaeterer voller/Risk-
    Close nicht von einem pending Partial-Close geblockt wird.
    """
    if instrument_id is None or not result:
        return
    if isinstance(result, dict) and result.get("_already_closed"):
        return  # Schon geschlossen, kein neuer Submit
    try:
        from datetime import datetime as _dt3
        pending = load_json("pending_closes.json") or {}
        key = str(instrument_id)
        pending[key] = {
            "submitted_at": _dt3.now().isoformat(),
            "result_summary": str(result)[:200],
            "is_partial": bool(is_partial),
        }
        save_json("pending_closes.json", pending)
        # v37h (14.05.2026): Cleanup via _cleanup_pending_closes() — ISO-Z-
        # robust + status-aware (Filled/Cancelled sofort raus statt 24h warten).
        _cleanup_pending_closes()
    except Exception as e:
        log.debug(f"Pending-Close-Track fehlgeschlagen (non-fatal): {e}")


def _close_position_safe(client, position_id, instrument_id, action_name="CLOSE"):
    """v37cu: Idempotency-safe Wrapper um client.close_position().

    Prueft Anti-Loop-Schutz BEFORE submit, traked submitted-Order AFTER.
    Returns gleiches wie close_position() oder {_skipped_idempotent: True, reason: ...}
    bei Skip.
    """
    skip, reason = _check_close_idempotent(client, instrument_id)
    if skip:
        log.warning(f"  {action_name} SKIP fuer {instrument_id}: {reason}")
        return {"_skipped_idempotent": True, "reason": reason}

    result = client.close_position(position_id, instrument_id)
    _track_pending_close(instrument_id, result)
    return result


def _partial_close_safe(client, position_id, close_pct, instrument_id, action_name="PARTIAL_CLOSE"):
    """v37h+2 (Q3-16, 15.05.2026): Idempotency-safe Wrapper um partial_close().

    Carlos's Trade-Logic-Audit (15.05.) hat Risk R4 identifiziert:
    PARTIAL_CLOSE rief client.partial_close DIREKT, ohne _check_close_idempotent
    -> kann doppelte Tranchen-Close-Orders feuern wenn vorheriger Versuch
    noch pending. Plus: tranche_consumed wurde bei Submitted-Result True
    gesetzt, wenn IBKR spaeter Cancelled meldet ist State falsch (Tranche
    dauerhaft 'verbrannt').

    Returns:
        Dict wie partial_close() oder {_skipped_idempotent: True, reason: ...}.
    """
    skip, reason = _check_close_idempotent(client, instrument_id, is_partial=True)
    if skip:
        log.warning(f"  {action_name} SKIP fuer {instrument_id}: {reason}")
        return {"_skipped_idempotent": True, "reason": reason}

    if not hasattr(client, "partial_close"):
        return {"_unsupported": True, "_broker": getattr(client, "broker_name", "?")}

    result = client.partial_close(position_id, close_pct, instrument_id)
    _track_pending_close(instrument_id, result, is_partial=True)
    return result


def _is_skipped_idempotent(result) -> bool:
    """v37cu: True wenn close_position vom Anti-Loop-Wrapper geskipped wurde."""
    return isinstance(result, dict) and result.get("_skipped_idempotent") is True


def _log_close_failure(action_name: str, p: dict, alerts_mod=None, extra: dict | None = None):
    """Protokolliere + persistiere eine fehlgeschlagene close_position()-Antwort.

    Zuvor gab es 4 Stellen in der SL/TP-Logik wo `client.close_position()` einen
    falsy-Wert zurueckgeben konnte und wir nur in den Sonnenschein-Fall (`if result:`)
    eingetreten sind. Folge: Position blieb am eToro offen, aber lokal dachten wir
    'geschlossen' (oder noch schlimmer: kein Log). Das kostet echtes Geld
    (Overnight-Fees, verpasster Exit). Dieser Helper wird jetzt im else-Fall
    aufgerufen und:
      1) loggt als ERROR (sichtbar im Render-Log)
      2) persistiert einen `<action>_FAILED`-Eintrag in trade_history.json
         (damit das Dashboard den Fehlversuch sehen kann)
      3) schickt Telegram-Alert falls das alerts-Modul verfuegbar ist
    """
    pnl_pct = p.get("pnl_pct", 0)
    pos_id = p.get("position_id")
    instr_id = p.get("instrument_id")
    log.error(
        f"  CLOSE FAILED: {action_name} fuer Position {pos_id} (Instrument {instr_id}) — "
        f"eToro hat keinen Erfolg gemeldet. PnL war {pnl_pct:+.2f}%. "
        f"Position bleibt OFFEN — naechster Zyklus wird erneut versuchen."
    )
    try:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "action": f"{action_name}_FAILED",
            "instrument_id": instr_id,
            "position_id": pos_id,
            "pnl_pct": pnl_pct,
            "status": "close_failed",
        }
        if extra:
            entry.update(extra)
        save_trade(entry)
    except Exception as e:
        log.warning(f"  Konnte {action_name}_FAILED nicht in trade_history persistieren: {e}")
    if alerts_mod:
        try:
            alerts_mod.alert_trade_executed({
                "action": f"{action_name}_FAILED",
                "position_id": pos_id,
                "instrument_id": instr_id,
                "pnl_pct": pnl_pct,
                "status": "close_failed",
            })
        except Exception as e:
            log.debug(f"Alert-Dispatch fehlgeschlagen: {e}")


def _find_position_open_time(position_id, api_open_time=None, symbol=None):
    """Ermittle wie lange eine Position offen ist (in Tagen).

    Priority:
    1) api_open_time aus eToro API (wenn Feld vorhanden)
    2) trade_history.json Lookup nach position_id (erster BUY-Entry)
    3) v37dd Fallback: trade_history.json Lookup nach symbol (letzter BUY)
       fuer alte Brain-Cache-Snapshots ohne position_id

    Returns (open_datetime, age_days) oder (None, None) wenn unbekannt.
    """
    from datetime import datetime, timezone

    def _parse(ts):
        """Parse ISO-Timestamp -> timezone-aware UTC datetime.

        Wichtig: eToro liefert 'Z'-Suffix (UTC), trade_history.json schreibt
        lokale naive Timestamps (datetime.now().isoformat()). Fruehere Version
        hat tzinfo einfach gestripped und mit datetime.now() verglichen -
        das gab bei UTC-Inputs +1/+2h falsche Alter. Jetzt: alles nach UTC
        normalisieren, naive Inputs als lokale Zeit interpretieren.
        """
        if not ts:
            return None
        try:
            s = str(ts).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                # Naive = lokale Zeit (wie trade_history.json via datetime.now())
                # -> Python 3.6+ astimezone() interpretiert naive als local
                dt = dt.astimezone(timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt
        except Exception:
            return None

    # 1) eToro API
    dt = _parse(api_open_time)

    # 2) Fallback trade_history lookup
    # R-A23 (Sprint-Tag-9 abend, 19.05.2026): KRITISCHER BUG-FIX.
    # Vorher: erster Match nach position_id+BUY -> dt = ÄLTESTE Buy.
    # Bei IBKR ist position_id = conId (Contract-ID), die UEBER Buy-Sell-
    # Zyklen wiederverwendet wird. USO conId=418893644 ist konstant,
    # egal wann/wie oft die Position gekauft wird.
    # Resultat: nach Re-Buy zeigte _find_position_open_time die ALTE
    # erstmals-Buy-Zeit (z.B. vor 20 Tagen) statt die aktuelle (Sekunden).
    # Time-Stop feuerte sofort -> Close-Buy-Loop alle ~1h: kostete heute
    # 4 Trades Slippage in 3h.
    # Fix: State-Machine durch History — letzte BUY NACH letztem CLOSE
    # mit dieser conId/position_id.
    BUY_ACTIONS = ("BUY", "OPEN", "buy", "open", "SCANNER_BUY", "MANUAL_BUY")
    CLOSE_PATTERNS = ("CLOSE", "SELL", "STOP", "TRAIL", "TIME_STOP", "COVER")

    if dt is None and position_id is not None:
        history = load_json("trade_history.json") or []
        latest_buy_dt = None
        for entry in history:
            if str(entry.get("position_id")) != str(position_id):
                continue
            action = (entry.get("action") or "").upper()
            status = (entry.get("status") or "").lower()
            # FAILED-Close zaehlt nicht als Reset (Position bleibt offen)
            if "FAILED" in action or status == "close_failed":
                continue
            # CLOSE-Event -> Reset: vorherige BUYs nicht mehr relevant
            if any(p in action for p in CLOSE_PATTERNS):
                latest_buy_dt = None
                continue
            # BUY-Event -> jüngsten Wert merken
            if entry.get("action") in BUY_ACTIONS:
                buy_dt = _parse(entry.get("timestamp"))
                if buy_dt is not None:
                    latest_buy_dt = buy_dt
        dt = latest_buy_dt

    # 3) v37dd Fallback: Symbol-basierter Lookup wenn position_id fehlt
    # (z.B. brain_state-Snapshots vor v37dd droppten position_id beim Persist).
    # Status-Filter: nur 'executed' (cancelled/rejected zaehlen nicht).
    # R-A23: auch hier State-Machine — letzte BUY NACH letztem CLOSE
    # damit Symbol-Lookup-Pfad ebenfalls korrekt arbeitet.
    if dt is None and symbol:
        history = load_json("trade_history.json") or []
        latest_buy = None
        for entry in history:
            if entry.get("symbol") != symbol:
                continue
            action = (entry.get("action") or "").upper()
            status = (entry.get("status") or "").lower()
            if "FAILED" in action or status == "close_failed":
                continue
            if any(p in action for p in CLOSE_PATTERNS):
                latest_buy = None
                continue
            if (entry.get("action") in BUY_ACTIONS
                    and entry.get("status") in (None, "executed", "filled",
                                                "partial")):  # v37dj: Teilfill = echte Position
                ts = _parse(entry.get("timestamp"))
                if ts is not None:
                    latest_buy = ts
        dt = latest_buy

    if dt is None:
        return None, None

    age = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    return dt, age


# ============================================================
# SAFE MODULE IMPORTS (Graceful Degradation)
# ============================================================

def _import_risk_manager():
    try:
        from app import risk_manager
        return risk_manager
    except ImportError:
        log.warning("Risk Manager nicht verfuegbar")
        return None

def _import_leverage_manager():
    try:
        from app import leverage_manager
        return leverage_manager
    except ImportError:
        log.warning("Leverage Manager nicht verfuegbar")
        return None

def _import_asset_filters():
    try:
        from app import asset_filters
        return asset_filters
    except ImportError:
        log.warning("Asset Filters nicht verfuegbar")
        return None

def _import_market_context():
    try:
        from app import market_context
        return market_context
    except ImportError:
        log.warning("Market Context nicht verfuegbar")
        return None

def _import_execution():
    try:
        from app import execution
        return execution
    except ImportError:
        log.warning("Execution Tracker nicht verfuegbar")
        return None

def _import_alerts():
    try:
        from app import alerts
        return alerts
    except ImportError:
        log.debug("Alerts nicht verfuegbar")
        return None

def _import_events_calendar():
    try:
        from app import events_calendar
        return events_calendar
    except ImportError:
        log.debug("Events Calendar nicht verfuegbar")
        return None

def _import_sentiment():
    try:
        from app import sentiment
        return sentiment
    except ImportError:
        log.debug("Sentiment-Analyse nicht verfuegbar")
        return None

def _import_hedging():
    try:
        from app import hedging
        return hedging
    except ImportError:
        log.debug("Hedging nicht verfuegbar")
        return None


# ============================================================
# PORTFOLIO STATUS
# ============================================================

def show_portfolio_status(client):
    """Aktuellen Portfolio-Status anzeigen und zurueckgeben."""
    log.info("=" * 55)
    log.info("PORTFOLIO STATUS")
    log.info("=" * 55)

    portfolio = client.get_portfolio()
    if not portfolio:
        log.error("Portfolio nicht verfuegbar")
        return None

    credit = portfolio.get("credit", 0)
    positions = portfolio.get("positions", [])
    unrealized_pnl = portfolio.get("unrealizedPnL", 0)

    total_invested = 0
    parsed_positions = []
    for pos in positions:
        p = EtoroClient.parse_position(pos)
        parsed_positions.append(p)
        total_invested += p["invested"]

    # v36g — gleicher Fix wie in brain.record_snapshot:
    # IBKR liefert _equity (NetLiquidation) — das ist die korrekte Equity.
    # credit + invested + pnl ueberzaehlt bei IBKR, weil credit dort
    # AvailableFunds (Cash MINUS Margin-Reserve) ist.
    ibkr_equity = portfolio.get("_equity")
    if ibkr_equity is not None and ibkr_equity > 0:
        total_value = float(ibkr_equity)
    else:
        total_value = total_invested + unrealized_pnl + credit

    log.info(f"  Credit (Cash):     ${credit:>12,.2f}")
    log.info(f"  Investiert:        ${total_invested:>12,.2f}")
    log.info(f"  Unrealized P/L:    ${unrealized_pnl:>12,.2f}")
    log.info(f"  Gesamtwert:        ${total_value:>12,.2f}")
    log.info(f"  Positionen:        {len(positions)}")

    for p in parsed_positions:
        log.info(f"    #{p['instrument_id']}: ${p['invested']:,.0f} -> "
                 f"P/L: ${p['pnl']:+,.2f} ({p['pnl_pct']:+.1f}%) {p['leverage']}x")

    # Risk Manager: Drawdown-Tracking
    rm = _import_risk_manager()
    if rm:
        # C4: Base-Currency als Wechsel-Schluessel -> Re-Baseline am Cutover
        # (USD-Paper -> CHF-Live) statt FX-Sprung als Drawdown zu werten.
        state = rm.update_portfolio_tracking(
            total_value, account_key=portfolio.get("_base_currency"))
        log.info(f"  Tages-P/L:         {state['daily_pnl_pct']:+.2f}% (${state['daily_pnl_usd']:+,.2f})")
        log.info(f"  Wochen-P/L:        {state['weekly_pnl_pct']:+.2f}% (${state['weekly_pnl_usd']:+,.2f})")

    return portfolio


# ============================================================
# PORTFOLIO AUFBAU
# ============================================================

def build_initial_portfolio(client, config):
    """Portfolio nach Ziel-Allokation aufbauen."""
    log.info("=" * 55)
    log.info("PORTFOLIO AUFBAU")
    log.info("=" * 55)

    dt_config = config.get("demo_trading", {})
    targets = dt_config.get("portfolio_targets", {})
    max_trade = dt_config.get("max_single_trade_usd", 5000)
    default_leverage = dt_config.get("default_leverage", 1)

    rm = _import_risk_manager()
    lm = _import_leverage_manager()
    af = _import_asset_filters()
    ex = _import_execution()

    portfolio = client.get_portfolio()
    if not portfolio:
        log.error("Konnte Portfolio nicht laden")
        return []

    credit = portfolio.get("credit", 0)
    positions = portfolio.get("positions", [])
    log.info(f"  Verfuegbar: ${credit:,.2f}")
    log.info(f"  Positionen: {len(positions)}")

    if credit < 100:
        log.warning("  Zu wenig Credit fuer neue Trades")
        return []

    # Bestehende Positionen nach InstrumentID mappen
    existing = {}
    for pos in positions:
        p = EtoroClient.parse_position(pos)
        iid = p["instrument_id"]
        existing[iid] = existing.get(iid, 0) + p["invested"]

    total_portfolio = credit + sum(existing.values())
    log.info(f"  Portfolio-Gesamtwert: ${total_portfolio:,.2f}")

    trades_executed = []
    for symbol, target in targets.items():
        iid = target["instrument_id"]
        target_pct = target["allocation_pct"]
        target_value = total_portfolio * target_pct / 100
        current_value = existing.get(iid, 0)
        diff = target_value - current_value

        log.info(f"  {symbol}: Soll=${target_value:,.0f} Ist=${current_value:,.0f} Diff=${diff:,.0f}")

        if diff > 50:
            amount = min(diff, max_trade, credit * 0.9)
            if amount < 50:
                continue

            leverage = target.get("leverage", default_leverage)

            # Leverage Manager: Auf erlaubte Stufe pruefen
            asset_class = target.get("class", "stocks")
            if lm:
                leverage = lm.snap_to_allowed(leverage, asset_class, symbol)

            # Risk Manager: Position Sizing
            if rm:
                stop_loss_pct = dt_config.get("stop_loss_pct", -3)
                max_risk_size = rm.calculate_leveraged_position_size(
                    total_portfolio, stop_loss_pct, leverage, config)
                amount = min(amount, max_risk_size)

            if amount < 50:
                log.info(f"    -> Skip (Risk-Sizing reduziert auf ${amount:.0f})")
                continue

            # Execution Tracking
            start_time = time.time()
            result = client.buy(iid, round(amount, 2), leverage=leverage)

            if ex and result:
                ex.track_execution(None, result, iid, "BUY", amount, asset_class, start_time)

            if result:
                order = result.get("orderForOpen", {})
                # v37q: position_id mitschreiben fuer Meta-Labeler-Training-Match.
                # IBKR: conId aus result._contract. eToro: order_id ist gleichzeitig
                # die position_id (UUID).
                contract_info = result.get("_contract") or {}
                pid = contract_info.get("conId") or order.get("orderID")
                # v37df: Status aus IBKR-Reality statt hardcoded "executed".
                # IbkrBroker.buy() gibt orderForOpen.statusID zurueck = echtes
                # IBKR-orderStatus.status. Mapping ueber _map_ibkr_status_to_bot_status.
                # Wenn Order pending/cancelled/rejected wird, Reconcile-Code v37db
                # filtert cancelled/rejected raus -> kein MissedFill-False-Positive.
                ibkr_status_id = order.get("statusID")
                bot_status = _map_ibkr_status_to_bot_status(ibkr_status_id)
                trade_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "action": "BUY",
                    "symbol": symbol,
                    "name": target["name"],
                    "instrument_id": iid,
                    "position_id": str(pid) if pid is not None else None,
                    "asset_class": asset_class,
                    "amount_usd": round(amount, 2),
                    "leverage": leverage,
                    "order_id": order.get("orderID"),
                    "status": bot_status,             # v37df: Reality-Aware
                    "ibkr_status_raw": ibkr_status_id, # v37df: Audit-Trail
                }
                # v37df: Wenn Order nicht erfolgreich, im Log klar machen
                if bot_status not in ("executed", "partial"):
                    log.warning(
                        "    Order NICHT ausgefuehrt: status=%s (IBKR-statusID=%s) "
                        "— Trade-Log mit korrektem Status, Position wird NICHT "
                        "weiterverfolgt", bot_status, ibkr_status_id)

                # Leverage logging
                if lm:
                    trade_entry = lm.log_leverage_trade(trade_entry, total_portfolio)

                save_trade(_attach_fill_prices(trade_entry, result))
                trades_executed.append(trade_entry)
                # R-B11/E5: nur dekrementieren wenn der Buy Kapital gebunden hat
                # (executed/partial/submitted) — nicht bei rejected/cancelled.
                if bot_status in ("executed", "partial", "submitted"):
                    credit -= amount
                log.info(f"    -> GEKAUFT: ${amount:,.2f} {leverage}x (Order: {order.get('orderID')})")

                # Alert
                al = _import_alerts()
                if al:
                    al.alert_trade_executed(trade_entry)
            else:
                log.error(f"    -> FEHLER bei {symbol}")

    log.info(f"\n  {len(trades_executed)} Trades ausgefuehrt")
    return trades_executed


# ============================================================
# STOP-LOSS / TAKE-PROFIT
# ============================================================

def check_stop_loss_take_profit(client, config):
    """Stop-Loss und Take-Profit pruefen (inkl. Trailing SL)."""
    log.info("=" * 55)
    log.info("STOP-LOSS / TAKE-PROFIT CHECK")
    log.info("=" * 55)

    dt_config = config.get("demo_trading", {})
    sl_pct = dt_config.get("stop_loss_pct", -10)
    tp_pct = dt_config.get("take_profit_pct", 25)

    # v12: Time-Stop Exit Config
    ts_cfg = config.get("time_stop", {}) or {}
    ts_enabled = ts_cfg.get("enabled", False)
    ts_max_days = ts_cfg.get("max_days_stale", 10)
    ts_stale_thr = ts_cfg.get("stale_pnl_threshold_pct", 0.5)
    ts_min_days = ts_cfg.get("min_days_open", 2)

    lm = _import_leverage_manager()
    al = _import_alerts()

    portfolio = client.get_portfolio()
    if not portfolio:
        return []

    # v37i: Market-Hours-Guard pro Position
    # Verhindert STOP_LOSS_CLOSE_FAILED-Spam-Loops wenn der Bot waehrend
    # geschlossener Boersen versucht eine Position zu schliessen (z.B. ROKU
    # in US-Pre-Market 03-09 EST). IBKR liefert dann keinen Live-Quote ->
    # close_position() returned None -> trader logged FAILED -> trade_history
    # bekommt im 5-Min-Takt einen Fehler-Eintrag, bis der Markt aufmacht.
    # Loesung: vor jedem Close-Versuch pruefen ob Asset-Klasse jetzt
    # tradeable ist. Wenn nicht: einmal INFO-loggen und ueberspringen.
    try:
        from app.asset_classes import is_asset_class_tradeable
    except ImportError:
        is_asset_class_tradeable = None
    skipped_off_hours: set[str] = set()  # nur einmal pro Klasse loggen

    # v37v: Earnings-Exit-Filter (vor SL/TP-Loop)
    # Prueft pro Position ob Earnings <= 1 Tag entfernt + Position > 10%
    # ODER Vola > 8%. Wenn ja: schliesse Position vor Earnings-Gap.
    # Loest das ROKU-30.04.-Problem (15% Portfolio + Implied-Move 12.6%).
    try:
        from app.earnings_exit import check_earnings_exit
    except ImportError:
        check_earnings_exit = None

    portfolio_value_usd = 0
    try:
        portfolio_value_usd = float(portfolio.get("creditByRealizedEquity")
                                    or portfolio.get("_equity") or 0)
    except (TypeError, ValueError):
        pass

    actions = []
    for pos in portfolio.get("positions", []):
        p = EtoroClient.parse_position(pos)
        if p["invested"] <= 0:
            continue

        # Earnings-Exit-Filter (v37v): vor allen anderen Checks
        # — wenn Earnings naht und Risk-Profil triggert, sofort schliessen.
        if check_earnings_exit is not None and portfolio_value_usd > 0:
            sym = pos.get("symbol")
            if sym:
                try:
                    pos_value = float(pos.get("amount", 0) or p.get("invested", 0))
                    should_exit, reason = check_earnings_exit(
                        sym, pos_value, portfolio_value_usd, config,
                    )
                    # v37aa: Duplicate-Prevention. Heute morgen 30.04. wurden zwei
                    # EARNINGS_BLACKOUT_CLOSE fuer ROKU geschrieben (09:10 + 10:21
                    # CEST), weil close_position() submitted aber IBKR erst Pre-
                    # Market fillt. Naechster Cycle sieht Position immer noch open
                    # -> Filter triggered erneut. Skip wenn bereits in den letzten
                    # N Stunden ein EARNINGS_BLACKOUT_CLOSE fuer dieses Symbol da war.
                    if should_exit and _has_recent_earnings_close(sym, hours=24):
                        log.info(f"  EARNINGS-EXIT skip {sym}: bereits in den "
                                 f"letzten 24h geschlossen (vermeidet Duplikat)")
                        should_exit = False
                    if should_exit:
                        log.warning(f"  EARNINGS-EXIT: {sym} — {reason}")
                        result = _close_position_safe(
                            client, p["position_id"], p.get("instrument_id"),
                            "EARNINGS-EXIT")
                        if _is_skipped_idempotent(result):
                            continue
                        if _is_already_closed(result):
                            log.info(f"  EARNINGS-EXIT skip {sym}: bei IBKR bereits "
                                     f"geschlossen (stale Bot-Cache)")
                            continue
                        if result:
                            trade_entry = {
                                "timestamp": datetime.now().isoformat(),
                                "action": "EARNINGS_BLACKOUT_CLOSE",
                                "symbol": sym,
                                "instrument_id": p["instrument_id"],
                                "position_id": p["position_id"],
                                "pnl_pct": p["pnl_pct"],
                                "pnl_usd": p["pnl"],
                                "leverage": p["leverage"],
                                "earnings_reason": reason,
                                "status": _trade_status_from_result(result),  # v37dh
                                "ibkr_status_raw": _ibkr_status_raw_from_result(result),
                            }
                            save_trade(_attach_fill_prices(trade_entry, result))
                            actions.append("EARNINGS_BLACKOUT_CLOSE")
                            if al:
                                al.alert_trade_executed(trade_entry)
                            continue  # Position zu, naechste Pruefung skippen
                        else:
                            _log_close_failure("EARNINGS_BLACKOUT_CLOSE", p, al,
                                               extra={"earnings_reason": reason})
                            # Kein continue — bei Close-Failure den Rest weiterpruefen
                except Exception as e:
                    log.debug(f"Earnings-Exit-Check {sym} fehlgeschlagen: {e}")

        # Market-Hours Guard
        if is_asset_class_tradeable is not None:
            asset_class = _lookup_asset_class(p.get("instrument_id"))
            if not is_asset_class_tradeable(asset_class):
                if asset_class not in skipped_off_hours:
                    log.info(
                        f"  Markt geschlossen fuer Klasse '{asset_class}' — "
                        f"SL/TP-Checks fuer betroffene Positionen werden uebersprungen "
                        f"(Position bleibt offen, retry zum naechsten RTH-Open)."
                    )
                    skipped_off_hours.add(asset_class)
                continue

        # Trailing SL: Fallback current_price aus PnL + entry_price berechnen
        if not p.get("current_price") and p.get("entry_price") and p["invested"] > 0:
            # current_price = entry_price * (1 + pnl_pct/100)
            p["current_price"] = round(p["entry_price"] * (1 + p["pnl_pct"] / 100), 6)
        if not p.get("entry_price") and p.get("current_price") and p["invested"] > 0 and p["pnl_pct"] != 0:
            # entry_price = current_price / (1 + pnl_pct/100)
            p["entry_price"] = round(p["current_price"] / (1 + p["pnl_pct"] / 100), 6)

        # Trailing SL Update + Trigger Check
        if lm and p.get("current_price"):
            lm.update_trailing_stop_loss(
                p["position_id"], p["current_price"], p.get("entry_price", p["current_price"]),
                p["leverage"], config)

            # Pruefe ob Trailing SL ausgeloest wurde
            triggered = lm.check_trailing_stop_losses([{
                "position_id": p["position_id"],
                "instrument_id": p["instrument_id"],
                "current_price": p["current_price"],
            }])
            if triggered:
                result = _close_position_safe(client, p["position_id"], p["instrument_id"], "TRAILING-SL")
                if _is_skipped_idempotent(result):
                    continue
                if _is_already_closed(result):
                    log.info(f"  TRAILING-SL skip {p['instrument_id']}: bei IBKR bereits "
                             f"geschlossen (stale Bot-Cache)")
                    continue
                if result:
                    trade_entry = {
                        "timestamp": datetime.now().isoformat(),
                        "action": "TRAILING_SL_CLOSE",
                        "symbol": p.get("symbol"),  # Q3-15: fuer Reports/Filter
                        "instrument_id": p["instrument_id"],
                        "position_id": p["position_id"],
                        "pnl_pct": p["pnl_pct"],
                        "pnl_usd": p["pnl"],
                        "leverage": p["leverage"],
                        "trailing_sl_level": triggered[0]["sl_level"],
                        "status": _trade_status_from_result(result),  # v37dh
                        "ibkr_status_raw": _ibkr_status_raw_from_result(result),
                    }
                    save_trade(_attach_fill_prices(trade_entry, result))
                    actions.append("TRAILING_SL_CLOSE")
                    if al:
                        al.alert_trade_executed(trade_entry)
                else:
                    _log_close_failure("TRAILING_SL_CLOSE", p, al,
                                       extra={"trailing_sl_level": triggered[0]["sl_level"]})
                continue  # Trailing SL hat Prioritaet, Skip fixed SL/TP

        # --- v12: Time-Stop / Staleness Exit ---
        # Schliesst Positionen die zu lange "stuck" sind und kaum P/L generieren.
        # Opportunitaetskosten-Schutz: gebundenes Kapital waere woanders besser.
        if ts_enabled:
            _, age_days = _find_position_open_time(
                p["position_id"], p.get("open_time"), symbol=p.get("symbol"))
            if age_days is not None and age_days >= ts_max_days \
                    and age_days >= ts_min_days \
                    and abs(p["pnl_pct"]) < ts_stale_thr:
                log.info(f"  TIME_STOP: Position {p['position_id']} "
                         f"(Instrument {p['instrument_id']}) — "
                         f"{age_days:.1f}d offen, PnL {p['pnl_pct']:+.2f}% < {ts_stale_thr}%")
                # v37h+2 (Q3-11, 15.05.2026): TIME_STOP nutzt jetzt
                # _close_position_safe statt direktem close_position-Call.
                # Vorher: kein _check_close_idempotent / pending_closes-Tracker
                # -> bei langsamem Fill (off-hours, partial) feuerte Time-Stop
                # alle 5 Min eine neue Close-Order. Gleicher Anti-Loop-Bug-
                # Pattern wie BA/CPER vom 04.05.
                result = _close_position_safe(
                    client, p["position_id"], p["instrument_id"], "TIME-STOP")
                if _is_skipped_idempotent(result):
                    continue  # Anti-Loop-Skip
                if _is_already_closed(result):
                    log.info(f"  TIME_STOP skip {p['instrument_id']}: bei IBKR bereits "
                             f"geschlossen (stale Bot-Cache)")
                    continue
                if result:
                    trade_entry = {
                        "timestamp": datetime.now().isoformat(),
                        "action": "TIME_STOP_CLOSE",
                        "symbol": p.get("symbol"),  # Q3-15: fuer Reports/Filter
                        "instrument_id": p["instrument_id"],
                        "position_id": p["position_id"],
                        "pnl_pct": p["pnl_pct"],
                        "pnl_usd": p["pnl"],
                        "leverage": p["leverage"],
                        "age_days": round(age_days, 2),
                        "status": _trade_status_from_result(result),  # v37dh
                        "ibkr_status_raw": _ibkr_status_raw_from_result(result),
                    }
                    save_trade(_attach_fill_prices(trade_entry, result))
                    actions.append("TIME_STOP_CLOSE")
                    if al:
                        al.alert_trade_executed(trade_entry)
                    continue  # Position ist zu, Rest skippen
                else:
                    _log_close_failure("TIME_STOP_CLOSE", p, al,
                                       extra={"age_days": round(age_days, 2)})
                    # WICHTIG: KEIN continue — naechster Zyklus versucht erneut,
                    # aber innerhalb dieses Zyklus nicht noch SL/TP drueberlegen.

        # --- Profit-Locking: Partial Close (TP-Tranchen) ---
        lev_cfg = config.get("leverage", {})
        tp_tranches = lev_cfg.get("tp_tranches", [])
        if tp_tranches and p["pnl_pct"] > 0:
            partial_state = load_json("partial_close_state.json") or {}
            pid_key = str(p["position_id"])
            triggered_tranches = partial_state.get(pid_key, {}).get("triggered", [])

            for tranche_idx, tranche in enumerate(tp_tranches):
                target_pct = tranche.get("profit_target_pct", 0)
                close_pct = tranche.get("pct_of_position", 0)

                if tranche_idx in triggered_tranches:
                    continue  # Diese Tranche wurde bereits ausgeloest

                if p["pnl_pct"] >= target_pct:
                    close_amount = round(p["invested"] * close_pct / 100, 2)
                    if close_amount >= 1:
                        log.info(f"  PARTIAL_CLOSE: Position {p['position_id']} "
                                 f"(Instrument {p['instrument_id']}) — "
                                 f"Tranche {tranche_idx+1}: {close_pct}% bei +{target_pct}% "
                                 f"(PnL: {p['pnl_pct']:+.1f}%, Betrag: ${close_amount:,.2f})")

                        # Berechne neue kumulierte Summe (noch nicht persistiert!)
                        prev_total = partial_state.get(pid_key, {}).get("total_closed_pct", 0)
                        new_total = prev_total + close_pct

                        # v37h Tab-Audit-Day-2 (12.05.2026): bei IBKR jetzt
                        # ECHTER partial_close statt nur Log-Signal! eToro-
                        # Fallback bleibt fuer Legacy-Pfad. Bei kumuliert
                        # >=100% nutzen wir close_position() (komplett raus),
                        # sonst partial_close(close_pct) (Teil-Schliessung).
                        action_kind = None
                        if new_total >= 100:
                            log.info(f"  PROFIT_LOCK_CLOSE: Alle Tranchen erreicht "
                                     f"({new_total}%) — schliesse Position komplett")
                            result = _close_position_safe(client, p["position_id"], p["instrument_id"], "PROFIT_LOCK")
                            if _is_skipped_idempotent(result):
                                continue
                            trade_status = _trade_status_from_result(result) if result else "failed"
                            action_kind = "PROFIT_LOCK_CLOSE"
                            if not result:
                                log.warning(f"  PROFIT_LOCK_CLOSE FEHLGESCHLAGEN — "
                                            f"Tranche wird NICHT als erledigt markiert")
                        elif hasattr(client, "partial_close"):
                            # IBKR-Pfad: echter Teil-Verkauf via partial_close
                            # v37h+2 (Q3-16, 15.05.2026): nutzt _partial_close_safe
                            # Wrapper statt direkten Call — _check_close_idempotent
                            # verhindert doppelte Tranchen-Close-Orders.
                            try:
                                result = _partial_close_safe(
                                    client, p["position_id"], close_pct,
                                    p["instrument_id"], "PARTIAL_CLOSE",
                                )
                            except Exception as e:
                                log.error(f"  PARTIAL_CLOSE Exception: {e}")
                                result = None
                            if _is_skipped_idempotent(result):
                                # Anti-Loop: vorheriger Versuch ist noch pending,
                                # Tranche NICHT als verbraucht markieren
                                log.info(f"  PARTIAL_CLOSE skip (idempotent): "
                                         f"Tranche {tranche_idx+1} wartet auf "
                                         f"pending close-Resolution")
                                continue
                            if result and result.get("_unsupported"):
                                # eToro-Fallback: nur Signal loggen
                                log.info(f"  PARTIAL_SIGNAL: Tranche {tranche_idx+1} "
                                         f"erreicht (Broker unterstuetzt kein partial-close — "
                                         f"kumuliert {new_total}%)")
                                trade_status = "signal_logged"
                                result = True
                                action_kind = "PARTIAL_SIGNAL"
                            elif result and result.get("_skipped_too_small"):
                                log.info(f"  PARTIAL_CLOSE skipped (qty too small): "
                                         f"Tranche {tranche_idx+1}, full_qty="
                                         f"{result.get('_full_qty')}, pct={close_pct}")
                                trade_status = "skipped_small"
                                action_kind = "PARTIAL_SKIP"
                            elif result and result.get("_already_closed"):
                                log.info(f"  PARTIAL_CLOSE skip: Position bereits geschlossen")
                                trade_status = "already_closed"
                                action_kind = "PARTIAL_SKIP"
                            elif result:
                                # Echter partial-close erfolgreich
                                close_qty = result.get("_close_qty", "?")
                                remaining = result.get("_remaining_qty", "?")
                                log.info(f"  PARTIAL_CLOSE OK: {close_qty} shares "
                                         f"verkauft, {remaining} bleiben (Tranche "
                                         f"{tranche_idx+1}, kumuliert {new_total}%)")
                                trade_status = _trade_status_from_result(result)
                                action_kind = "PARTIAL_CLOSE"
                            else:
                                # v37h Pushover-Eskalation: nutze _log_close_failure
                                # statt nur Warning. Bringt: ERROR-Log + Trade-History-
                                # Persist mit _FAILED-Suffix + Pushover als WARNING-
                                # Level via alerts_mod.alert_trade_executed. Wichtig
                                # weil PARTIAL_CLOSE_FAILED = Real-Money-Risiko
                                # (Tranche blockt sich selbst, Position bleibt offen
                                # mit zu wenig Profit-Locking).
                                _log_close_failure("PARTIAL_CLOSE", p, al, extra={
                                    "tranche_index": tranche_idx,
                                    "tranche_close_pct": close_pct,
                                    "tranche_target_pct": target_pct,
                                    "total_closed_pct": new_total,
                                    "reason": "client.partial_close returnte falsy/None",
                                })
                                trade_status = "failed"
                                action_kind = "PARTIAL_CLOSE_FAILED"
                        else:
                            # Legacy-Pfad (eToro-Client ohne partial_close-Methode)
                            log.info(f"  PARTIAL_SIGNAL: Tranche {tranche_idx+1} erreicht "
                                     f"(legacy: kumuliert {new_total}%)")
                            trade_status = "signal_logged"
                            result = True
                            action_kind = "PARTIAL_SIGNAL"

                        # Tranche NUR bei Erfolg/Signal als ausgeloest markieren
                        # (bei API-Fehler/Skip bleibt Tranche offen fuer naechsten Zyklus)
                        tranche_consumed = (
                            action_kind in ("PROFIT_LOCK_CLOSE", "PARTIAL_CLOSE", "PARTIAL_SIGNAL")
                            and result is not None and result is not False
                        )
                        if tranche_consumed:
                            if pid_key not in partial_state:
                                partial_state[pid_key] = {"triggered": [], "total_closed_pct": 0}
                            partial_state[pid_key]["triggered"].append(tranche_idx)
                            partial_state[pid_key]["total_closed_pct"] = new_total
                            save_json("partial_close_state.json", partial_state)

                        trade_entry = {
                            "timestamp": datetime.now().isoformat(),
                            "action": action_kind,
                            "instrument_id": p["instrument_id"],
                            "position_id": p["position_id"],
                            "pnl_pct": p["pnl_pct"],
                            "pnl_usd": round(p["pnl"] * close_pct / 100, 2),
                            "leverage": p["leverage"],
                            "tranche_index": tranche_idx,
                            "tranche_close_pct": close_pct,
                            "tranche_target_pct": target_pct,
                            "close_amount_usd": close_amount,
                            "total_closed_pct": new_total,
                            "status": trade_status,
                        }
                        save_trade(_attach_fill_prices(trade_entry, result))
                        actions.append(action_kind)

                        # Bei voller Schliessung: Rest der Tranchen-Pruefung ueberspringen
                        if new_total >= 100 and result:
                            break

                        # v37h: alert_trade_executed nur bei Erfolg-Pfaden senden.
                        # PARTIAL_CLOSE_FAILED ruft schon _log_close_failure auf
                        # (ERROR-Alert via WARNING-Level) — kein Doppel-Push.
                        # PARTIAL_SKIP ist ein "saubere Skip", kein Alert noetig.
                        if al and action_kind in ("PROFIT_LOCK_CLOSE", "PARTIAL_CLOSE", "PARTIAL_SIGNAL"):
                            al.alert_trade_executed(trade_entry)

        # Stop-Loss Check
        if p["pnl_pct"] <= sl_pct:
            log.warning(f"  STOP-LOSS: Position {p['position_id']} "
                        f"(Instrument {p['instrument_id']}) bei {p['pnl_pct']:+.1f}%")
            result = _close_position_safe(client, p["position_id"], p["instrument_id"], "STOP-LOSS")
            if _is_skipped_idempotent(result):
                continue  # Anti-Loop-Skip, naechster Position pruefen
            if _is_already_closed(result):
                log.info(f"  STOP-LOSS skip {p['instrument_id']}: bei IBKR bereits "
                         f"geschlossen (stale Bot-Cache)")
            elif result:
                trade_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "action": "STOP_LOSS_CLOSE",
                    "symbol": p.get("symbol"),  # Q3-15: fuer Reports/Filter
                    "instrument_id": p["instrument_id"],
                    "position_id": p["position_id"],
                    "pnl_pct": p["pnl_pct"],
                    "pnl_usd": p["pnl"],
                    "leverage": p["leverage"],
                    "status": _trade_status_from_result(result),  # v37dh
                    "ibkr_status_raw": _ibkr_status_raw_from_result(result),
                }
                save_trade(_attach_fill_prices(trade_entry, result))
                actions.append("STOP_LOSS_CLOSE")
                if al:
                    al.alert_trade_executed(trade_entry)
            else:
                _log_close_failure("STOP_LOSS_CLOSE", p, al)

        # Take-Profit Check (nur fuer verbleibende Position nach Partial Closes)
        elif p["pnl_pct"] >= tp_pct:
            partial_state_tp = load_json("partial_close_state.json") or {}
            pid_tp_key = str(p["position_id"])
            closed_pct_total = partial_state_tp.get(pid_tp_key, {}).get("total_closed_pct", 0)
            remaining_label = f" (Rest nach {closed_pct_total}% Partial Close)" if closed_pct_total > 0 else ""
            log.info(f"  TAKE-PROFIT: Position {p['position_id']} "
                     f"(Instrument {p['instrument_id']}) bei {p['pnl_pct']:+.1f}%{remaining_label}")
            result = _close_position_safe(client, p["position_id"], p["instrument_id"], "TAKE-PROFIT")
            if _is_skipped_idempotent(result):
                continue
            if _is_already_closed(result):
                log.info(f"  TAKE-PROFIT skip {p['instrument_id']}: bei IBKR bereits "
                         f"geschlossen (stale Bot-Cache)")
            elif result:
                trade_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "action": "TAKE_PROFIT_CLOSE",
                    "symbol": p.get("symbol"),  # Q3-15: fuer Reports/Filter
                    "instrument_id": p["instrument_id"],
                    "position_id": p["position_id"],
                    "pnl_pct": p["pnl_pct"],
                    "pnl_usd": p["pnl"],
                    "leverage": p["leverage"],
                    "status": _trade_status_from_result(result),  # v37dh
                    "ibkr_status_raw": _ibkr_status_raw_from_result(result),
                }
                save_trade(_attach_fill_prices(trade_entry, result))
                actions.append("TAKE_PROFIT_CLOSE")
                if al:
                    al.alert_trade_executed(trade_entry)
            else:
                _log_close_failure("TAKE_PROFIT_CLOSE", p, al)

    # Partial-Close State bereinigen fuer geschlossene Positionen
    _cleanup_partial_close_state(portfolio)

    log.info(f"  {len(actions)} SL/TP Aktionen")
    return actions


def _cleanup_partial_close_state(portfolio):
    """Entferne Partial-Close-State fuer Positionen die nicht mehr offen sind.

    v37h+2 (15.05.2026, Q3-8): Defensive-Guard gegen None / leeres portfolio.
    Vorher: Wenn portfolio=None (IBKR-Portfolio-Fetch-Fail, z.B. waehrend
    der 08:33 CEST IBKR-Connectivity-Drops vom 15.05.), wurde open_ids leer
    -> cleaned={} -> ALLE partial_state-Eintraege geloescht. Worst-Case:
    Bot vergisst dass Tranche 1 (50%) schon getriggert wurde, schliesst bei
    Tranche 3 die volle 50% statt der geplanten 20%.
    Fix: bei nicht-validem portfolio den Cleanup SKIPPEN statt zu clearen.
    """
    partial_state = load_json("partial_close_state.json") or {}
    if not partial_state:
        return

    # Defensive: kein Cleanup wenn Portfolio-Fetch unzuverlaessig war.
    # Besser ein paar stale-Eintraege als verlorene Tranchen-State.
    if not portfolio or not isinstance(portfolio, dict):
        log.warning(
            "  partial_close_state-Cleanup geskippt: portfolio=None/invalid "
            f"({type(portfolio).__name__}). {len(partial_state)} Eintraege "
            "bleiben unveraendert (Safety-Guard gegen falsche Tranchen-Resets)."
        )
        return
    positions = portfolio.get("positions")
    if positions is None:
        log.warning(
            "  partial_close_state-Cleanup geskippt: portfolio['positions']=None. "
            f"{len(partial_state)} Eintraege bleiben unveraendert."
        )
        return

    open_ids = set()
    for pos in positions:
        try:
            p = EtoroClient.parse_position(pos)
            open_ids.add(str(p["position_id"]))
        except Exception as e:
            log.debug(f"parse_position skipped (non-fatal): {e}")
            continue

    # Zweite Defensive-Schicht: wenn nach Parsing immer noch keine open_ids
    # (z.B. alle Position-Parse-Fails), nicht clearen — wahrscheinlich
    # Schema-Drift / Edge-Case, kein legitimer "alle Positionen zu" Fall.
    if not open_ids and len(positions) > 0:
        log.warning(
            f"  partial_close_state-Cleanup geskippt: {len(positions)} Positionen "
            "im Portfolio aber 0 erfolgreich geparsed. Schema-Drift? "
            f"{len(partial_state)} Eintraege bleiben unveraendert."
        )
        return

    cleaned = {pid: data for pid, data in partial_state.items() if pid in open_ids}
    if len(cleaned) != len(partial_state):
        save_json("partial_close_state.json", cleaned)


# ============================================================
# REBALANCING
# ============================================================

def rebalance_portfolio(client, config):
    """Portfolio rebalancieren wenn Abweichung zu gross."""
    log.info("=" * 55)
    log.info("REBALANCING CHECK")
    log.info("=" * 55)

    dt_config = config.get("demo_trading", {})
    targets = dt_config.get("portfolio_targets", {})
    threshold = dt_config.get("rebalance_threshold_pct", 5)

    portfolio = client.get_portfolio()
    if not portfolio:
        return

    credit = portfolio.get("credit", 0)
    positions = portfolio.get("positions", [])

    pos_by_instrument = {}
    total_invested = 0
    for pos in positions:
        p = EtoroClient.parse_position(pos)
        iid = p["instrument_id"]
        current_val = p["invested"] + p["pnl"]
        pos_by_instrument[iid] = pos_by_instrument.get(iid, 0) + current_val
        total_invested += current_val

    # v37cd: NetLiq als Total-Quelle (analog brain.py:71). Bei IBKR-Margin-
    # Konten gilt NICHT total = credit + invested + pnl (siehe ibkr_client.py
    # v37cd Cash-Mapping-Fix). NetLiq ist die ehrliche Portfolio-Summe.
    equity = portfolio.get("_equity")
    total = equity if equity else (total_invested + credit)
    if total <= 0:
        log.info("  Portfolio leer - kein Rebalancing noetig")
        return

    needs_rebalance = False
    for symbol, target in targets.items():
        iid = target["instrument_id"]
        target_pct = target["allocation_pct"]
        current_val = pos_by_instrument.get(iid, 0)
        current_pct = (current_val / total * 100) if total > 0 else 0
        deviation = current_pct - target_pct

        status = "OK" if abs(deviation) <= threshold else "REBALANCE"
        if status == "REBALANCE":
            needs_rebalance = True

        log.info(f"  {symbol}: Soll={target_pct}% Ist={current_pct:.1f}% "
                 f"Abw={deviation:+.1f}% [{status}]")

    if needs_rebalance and credit > 100:
        log.info("  -> Rebalancing wird ausgefuehrt...")
        build_initial_portfolio(client, config)
    else:
        log.info("  -> Kein Rebalancing noetig")


# ============================================================
# SCANNER-BASIERTES TRADING (v2 mit allen Filtern)
# ============================================================

def execute_scanner_trades(client, config, scan_results):
    """Trades basierend auf Scanner-Ergebnissen mit vollen Safety-Checks."""
    log.info("=" * 55)
    log.info("DYNAMISCHES TRADING (Scanner-basiert)")
    log.info("=" * 55)

    dt_config = config.get("demo_trading", {})
    # v15: max_trade wird pro Trade aus dem Portfolio-Wert resolved (siehe unten),
    # max_positions skaliert mit Portfolio via Tier-Map.
    min_score = dt_config.get("min_scanner_score", 15)
    stop_loss_pct = dt_config.get("stop_loss_pct", -3)

    rm = _import_risk_manager()
    lm = _import_leverage_manager()
    af = _import_asset_filters()
    mc = _import_market_context()
    ex = _import_execution()
    al = _import_alerts()
    ec = _import_events_calendar()
    sent = _import_sentiment()
    hdg = _import_hedging()

    portfolio = client.get_portfolio()
    if not portfolio:
        log.error("Portfolio nicht verfuegbar")
        return []

    credit = portfolio.get("credit", 0)
    positions = portfolio.get("positions", [])
    parsed_positions = [EtoroClient.parse_position(pos) for pos in positions]
    # v36g — IBKR-Korrektur: credit ist AvailableFunds, nicht reines Cash.
    # NetLiquidation aus _equity nutzen wenn verfuegbar (siehe brain.py).
    ibkr_equity_pre = portfolio.get("_equity")
    if ibkr_equity_pre is not None and ibkr_equity_pre > 0:
        total_value = float(ibkr_equity_pre)
    else:
        total_value = credit + sum(p["invested"] for p in parsed_positions)

    # v15: Portfolio-Sizing skaliert mit Portfolio-Wert (Tier-Map + Prozent-basiert).
    if rm:
        max_positions = rm.resolve_max_positions(total_value, config)
        max_trade = rm.resolve_max_single_trade_usd(total_value, config)
    else:
        max_positions = config.get("risk_management", {}).get(
            "max_open_positions", dt_config.get("max_positions", 10))
        max_trade = dt_config.get("max_single_trade_usd", 3000)

    # v15: Cash-Deposit-DCA — staffel neue Einzahlungen ueber N Zyklen,
    # damit Market-Timing-Risiko reduziert wird. `effective_cash` ist der
    # fuer diesen Zyklus deploy-bare Betrag; `credit` bleibt unveraendert
    # fuer Logging und echte Safety-Checks.
    dca_info = None
    effective_cash = credit
    if rm:
        try:
            # v37h+2 (R-A2): currency-Hint mitgeben damit DCA Phantom-Plans
            # bei FX-Reval verhindert. Bei IBKR Multi-Currency-Konten ist
            # portfolio['_base_currency'] (= z.B. 'CHF') der zuverlaessige Hint;
            # Top-Level 'credit' kann USD oder BASE sein (siehe Q3-13).
            _base_curr = portfolio.get("_base_currency")
            dca_info = rm.detect_cash_deposit(credit, config, currency=_base_curr)
            effective_cash = dca_info.get("remaining_budget_usd", credit)
            if dca_info.get("dca_active"):
                log.info(
                    f"  Cash-DCA aktiv: ${effective_cash:,.2f}/{credit:,.2f} "
                    f"verfuegbar ({dca_info.get('remaining_cycles')} Zyklen verbleibend)"
                )
        except Exception as e:
            log.warning(f"Cash-DCA Detection fehlgeschlagen (non-fatal): {e}", exc_info=True)

    # v37h+1 (14.05.2026): Cash-Reserve-Guard (Hybrid Floor + %).
    # v37h+2 (15.05.2026, Q3-13): explizit BASE-Currency-Quellen lesen statt
    # `credit`/`_equity`. Verhindert dass Cash-Reserve gegen USD-Cash
    # verglichen wird waehrend Reserve in CHF gerechnet ist (~14% Drift bei
    # FX-Bewegungen). Wenn _base_*-Felder fehlen (eToro-Fallback, alte
    # Snapshots): fallback auf Top-Level Werte.
    # Reduziert effective_cash um die geforderte Reserve in BASE-Currency (CHF).
    # Wenn cash < reserve: deployable = 0 -> Bot pausiert Buys ohne Notverkauf.
    # Auto-Refill durch normale TP/SL/Dividenden-Sells.
    try:
        from app.cash_reserve import (
            get_required_reserve_chf,
            compute_deployable_cash_chf,
        )
        # Q3-13: BASE-Werte explizit fuer Cash-Reserve. Fallback auf Top-Level.
        equity_chf = portfolio.get("_base_net_liquidation") or total_value
        cash_chf = portfolio.get("_base_total_cash")
        # Wenn _base_total_cash fehlt UND `credit` in derselben Currency wie
        # equity_chf ist (typisch CHF=BASE-Konto ohne Multi-Currency-Reporting):
        # nutze credit als Fallback. Sonst lieber konservativ 0.
        if cash_chf is None:
            # R-B11/R4 (07.06.2026): Fallback KONSISTENT in derselben Currency wie
            # effective_cash (USD/Top-Level). Frueher blieb equity_chf evtl. in CHF
            # (_base_net_liquidation) waehrend cash auf USD-effective_cash fiel ->
            # Reserve (%-von-CHF-Equity) gegen USD-Cash -> Buffer um die FX-Drift
            # (~10%) unterschritten. Konservativ: beide auf Top-Level/USD.
            cash_chf = effective_cash  # Best-Effort Fallback (USD)
            equity_chf = total_value   # gleiche Currency wie cash_chf
        required_reserve_chf = get_required_reserve_chf(equity_chf, config)
        if required_reserve_chf > 0:
            deployable_chf = compute_deployable_cash_chf(
                cash_chf, equity_chf, config
            )
            # effective_cash ist in USD/Top-Level — wir muessen die Reserve-
            # Kuerzung in derselben Currency anwenden. Annahme: cash_chf und
            # effective_cash haben dasselbe Verhaeltnis (= IBKR-FX-Rate).
            # Wenn cash_chf > 0: scale down effective_cash by ratio.
            if cash_chf > 0:
                deploy_ratio = deployable_chf / cash_chf
                pre_reserve_cash = effective_cash
                effective_cash = max(0.0, effective_cash * deploy_ratio)
                if abs(pre_reserve_cash - effective_cash) > 0.01:
                    log.info(
                        f"  Cash-Reserve aktiv: {required_reserve_chf:,.2f} CHF "
                        f"reserviert (Equity {equity_chf:,.0f} CHF, "
                        f"Cash {cash_chf:,.0f} CHF, "
                        f"deploy-Ratio {deploy_ratio:.3f}, "
                        f"effective_cash {effective_cash:,.2f}/{pre_reserve_cash:,.2f})"
                    )
                    if effective_cash <= 0.01:
                        log.info(
                            "  Cash-Reserve: Cash unter Reserve-Schwelle -> "
                            "Buys pausiert, warte auf TP/SL/Dividenden-Refill"
                        )
    except Exception as e:
        log.warning(f"Cash-Reserve-Guard fehlgeschlagen (non-fatal): {e}", exc_info=True)

    # Risk Manager: Drawdown-Check
    if rm:
        dd_ok, dd_reason = rm.check_drawdown_limits()
        if not dd_ok:
            log.warning(f"  TRADING PAUSIERT: {dd_reason}")
            if al:
                al.alert_drawdown(
                    rm.get_risk_summary().get("daily_pnl_pct", 0),
                    rm.get_risk_summary().get("weekly_pnl_pct", 0),
                    dd_reason)
            return []

    # Market Context: Positionsgroessen-Multiplikator + Regime Gate
    ctx_multiplier = 1.0
    regime_halt = False
    regime_data = {}
    if mc:
        ctx = mc.get_current_context()
        ctx_multiplier = ctx.get("position_size_multiplier", 1.0)
        if ctx_multiplier < 1.0:
            log.info(f"  Marktkontext: Positionsgroessen x{ctx_multiplier}")

        # Kombinierter Regime-Filter: VIX + Fear&Greed + Brain-Regime
        from app.market_context import check_regime_filter
        buy_allowed, regime_reason, regime_data = check_regime_filter(config)
        if not buy_allowed:
            log.warning(f"  REGIME FILTER: {regime_reason}")
            regime_halt = True
            # v12: Panic-Dip-Buy Override — wenn VIX Term Structure auf akuter
            # Backwardation ist (VIX9D deutlich > VIX > VIX3M) ist das historisch
            # ein Capitulation-Signal. Wir heben den Halt auf und fahren mit
            # reduzierter Position-Multiplier.
            vts = ctx.get("vix_term_structure") or {}
            vts_cfg = config.get("vix_term_structure", {}) or {}
            if vts_cfg.get("panic_dip_override_enabled", True) and vts.get("panic_dip_buy_signal"):
                # R-B11/R6 (07.06.2026): Der Panic-Dip-Override hebt den
                # HAERTESTEN Schutz (Regime-Halt) auf — das darf ein EINZELNES
                # VIX-Term-Structure-Signal nicht, wenn der Bot sonst in eine
                # echte Baer-/Drawdown-Kapitulation hineinkauft. Zusatz-
                # Bestaetigung: Brain-Regime != bear UND kein aktiver Drawdown-Stop.
                brain_regime = (regime_data or {}).get("brain_regime", "unknown")
                dd_ok = True
                if rm:
                    try:
                        dd_ok, _ = rm.check_drawdown_limits()
                    except Exception:
                        dd_ok = True
                if brain_regime != "bear" and dd_ok:
                    log.warning(f"  PANIC-DIP-BUY OVERRIDE: VIX Term "
                                f"ratio={vts.get('ratio_9d_vs_30d')} "
                                f"shape={vts.get('shape')} — Regime Halt aufgehoben")
                    regime_halt = False
                    ctx_multiplier *= vts_cfg.get("panic_dip_position_multiplier", 0.6)
                else:
                    log.warning(f"  PANIC-DIP-OVERRIDE UNTERDRUECKT (R6): "
                                f"brain_regime={brain_regime}, drawdown_ok={dd_ok} "
                                f"-> Regime-Halt bleibt aktiv")

        # R-A48 (28.05.2026 Sprint-Tag-17): Idempotente Notification.
        # Push NUR bei State-Change (halt-on oder halt-off), nicht jeden Cycle.
        # Vor R-A48: alle ~5 Min ein Pushover-Banner waehrend HALT (Cry-Wolf).
        # Plus: Recovery-Alert (alert_regime_resumed) wurde nie gefeuert.
        global _last_regime_halt_state
        if al:
            transition = _classify_regime_state_change(
                current_halt_active=regime_halt,
                previous_halt_active=_last_regime_halt_state,
            )
            if transition == "halt":
                al.alert_regime_halt(regime_reason, regime_data)
                log.info("  R-A48: REGIME-HALT-Alert gefeuert (State-Change off->on)")
            elif transition == "resumed":
                al.alert_regime_resumed()
                log.info("  R-A48: REGIME-HALT-Recovery-Alert gefeuert (State-Change on->off)")
        _last_regime_halt_state = regime_halt

    # Hedging: Bear-Regime Schutz
    hedge_result = {"hedge_needed": False}
    if hdg:
        hedge_result = hdg.check_hedge_needed(regime_data, parsed_positions, config)
        if hedge_result.get("hedge_needed"):
            hedge_mult = hedge_result.get("bear_position_multiplier", 0.5)
            ctx_multiplier *= hedge_mult
            log.info(f"  HEDGING AKTIV: Positionsgroessen x{hedge_mult} "
                     f"(effektiv x{ctx_multiplier:.2f})")

    # Risk Manager: Margin Safety Check
    if rm:
        margin_ok, margin_reason, exposure = rm.check_margin_safety(total_value, parsed_positions, config)
        if not margin_ok:
            log.warning(f"  MARGIN WARNING: {margin_reason}")
            # Auto-Deleverage bei kritischem Margin
            rm.auto_deleverage(client, parsed_positions, total_value, config)

    # v37dg (07.05.2026): instrument_id-Mismatch zwischen Bot-Universum
    # (etoro_id, z.B. 5003 fuer SILVER) und IBKR-Layer (conId, z.B. 1316487
    # fuer SLV). Frueher: existing_ids = {conIds} -> Match mit Scanner's
    # etoro_id schlaegt fehl -> Bot kauft trotz existierender Position
    # (07.05. SLV: 3 Buy-Attempts in 24h, davon 1 filled + 2 rejected).
    # Fix: zusaetzliches existing_symbols-Set mit Symbol-Translation
    # (v37de expand_symbol_for_match — matched bidirektional SILVER<->SLV).
    existing_ids = {p["instrument_id"] for p in parsed_positions}
    try:
        from app.market_scanner import expand_symbol_for_match
        existing_symbols = set()
        for p in parsed_positions:
            sym = p.get("symbol")
            if sym:
                existing_symbols.update(expand_symbol_for_match(sym))
    except Exception as e:
        log.debug(f"existing_symbols-Build skipped: {e}")
        existing_symbols = set()

    log.info(
        f"  Cash: ${credit:,.2f} (deploy-bar ${effective_cash:,.2f}) | "
        f"Positionen: {len(positions)}/{max_positions} | "
        f"max_trade=${max_trade:,.0f}"
    )

    # --- VERKAUFEN: Positionen mit SELL-Signal ---
    sell_candidates = [r for r in scan_results
                       if r["signal"] in ("SELL", "STRONG_SELL")
                       and (r["etoro_id"] in existing_ids
                            or r.get("symbol") in existing_symbols)]  # v37dg

    trades_executed = []
    for candidate in sell_candidates:
        for pos in positions:
            p = EtoroClient.parse_position(pos)
            # R-B1 Phase 3a: symbol-kanonischer Vergleich (akzeptiert alt=Zahl
            # + neu=Symbol). candidate["symbol"] direkt, Position-id reverse.
            if (_canon_instrument_key(p["instrument_id"])
                    == _canon_instrument_key(candidate.get("symbol") or candidate["etoro_id"])
                    and p["invested"] > 0):
                log.info(f"  SCANNER SELL: {candidate['symbol']} "
                         f"(Score={candidate['score']:+.1f}, {candidate['signal']})")

                start_time = time.time()
                result = _close_position_safe(client, p["position_id"], p["instrument_id"], "SCANNER_SELL")
                if _is_skipped_idempotent(result):
                    continue

                if _is_already_closed(result):
                    log.info(f"  SCANNER_SELL skip {candidate['symbol']}: bei IBKR "
                             f"bereits geschlossen (stale Bot-Cache)")
                    continue

                if ex and result:
                    ex.track_execution(None, result, candidate["etoro_id"],
                                       "SCANNER_SELL", p["invested"],
                                       candidate["class"], start_time)

                if result:
                    trade_entry = {
                        "timestamp": datetime.now().isoformat(),
                        "action": "SCANNER_SELL",
                        "symbol": candidate["symbol"],
                        "name": candidate["name"],
                        "instrument_id": candidate["etoro_id"],
                        "asset_class": candidate["class"],
                        "position_id": p["position_id"],
                        "pnl_pct": p["pnl_pct"],
                        "scanner_score": candidate["score"],
                        "signal": candidate["signal"],
                        "status": _trade_status_from_result(result),  # v37dh
                        "ibkr_status_raw": _ibkr_status_raw_from_result(result),
                    }
                    save_trade(_attach_fill_prices(trade_entry, result))
                    trades_executed.append(trade_entry)
                    if al:
                        al.alert_trade_executed(trade_entry)
                else:
                    _log_close_failure("SCANNER_SELL", p, al, extra={
                        "symbol": candidate["symbol"],
                        "scanner_score": candidate["score"],
                        "signal": candidate["signal"],
                    })

    # --- KAUFEN: Top Opportunities mit vollen Safety-Checks ---
    if regime_halt:
        log.info("  BUY-Phase uebersprungen (Regime Halt aktiv)")
        return trades_executed

    # Intraday Timing Filter: Keine Kaeufe in volatilen Marktphasen (Open/Close)
    timing_cfg = config.get("intraday_timing", {})
    if timing_cfg.get("enabled", False):
        now = datetime.now()
        avoid_first = timing_cfg.get("avoid_first_minutes", 30)
        avoid_last = timing_cfg.get("avoid_last_minutes", 30)
        # US Markt: 15:30-22:00 CET
        market_open_h, market_open_m = 15, 30
        market_close_h, market_close_m = 22, 0

        minutes_since_open = (now.hour - market_open_h) * 60 + (now.minute - market_open_m)
        minutes_until_close = (market_close_h - now.hour) * 60 + (market_close_m - now.minute)

        if 0 <= minutes_since_open < avoid_first:
            log.info(f"  INTRADAY TIMING: Erste {avoid_first} Minuten nach Open — "
                     f"keine Kaeufe ({minutes_since_open} Min seit Open)")
            return trades_executed

        if 0 <= minutes_until_close < avoid_last:
            log.info(f"  INTRADAY TIMING: Letzte {avoid_last} Minuten vor Close — "
                     f"keine Kaeufe ({minutes_until_close} Min bis Close)")
            return trades_executed

    # Recovery Mode: Einschraenkungen bei moderatem Drawdown
    recovery_active = False
    recovery_restrictions = {}
    if rm:
        recovery_active, recovery_restrictions = rm.check_recovery_mode(config)
        if recovery_active:
            log.warning(f"  {recovery_restrictions.get('reason', 'RECOVERY MODE')}")
            min_score = max(min_score, recovery_restrictions.get("min_score", 30))

    # v36 — Loop-Cooldown: blockiert Re-Buy desselben Symbols innerhalb
    # cooldown_cycles falls vorheriger Versuch nicht in eine echte Position
    # mündete (broker bestätigt nicht). Fix für ROKU-Fixierungs-Loop am
    # 27.04. (133+ identische Buys).
    cooldown_cycles = dt_config.get("buy_cooldown_cycles", 12)  # 12 × 5min = 1h
    # v37ds (17.06.2026): Post-Close-Cooldown gegen Re-Churn. Der Buy-Cooldown oben
    # greift nur nach KAUFEN; ein gerade (trailing/SL/TP/time-stop) GESCHLOSSENES
    # Symbol war danach sofort wieder kaufbar -> GIII-Churn 16.06. (Motor waehlt
    # GIII -> Trailing schliesst -> sofort re-buy -> ...). Kostet Spread/Slippage +
    # verfaelscht den Soak. Blockt Re-Buy fuer N Stunden nach einem Close (0 = aus).
    post_close_cooldown_hours = dt_config.get("post_close_cooldown_hours", 6)
    cooldown_state = load_json("buy_cooldown.json") or {}
    now_iso = datetime.now().isoformat()
    # Stale-Eintraege bereinigen (>24h)
    # v37h+2 (15.05.2026, Q3-9): _parse_iso_safe statt datetime.fromisoformat
    # damit ISO-Z-Format-Eintraege nicht silent crashen (gleicher Bug der
    # am 14.05. bei pending_closes geknackt wurde).
    def _cooldown_fresh(rec_value):
        """Returns True wenn Eintrag <24h alt + parsbar. False -> entfernen."""
        if not isinstance(rec_value, dict):
            return False
        dt = _parse_iso_safe(rec_value.get("last_attempt"))
        if dt is None:
            return False
        return (datetime.now() - dt).total_seconds() < 86400

    cooldown_state = {k: v for k, v in cooldown_state.items() if _cooldown_fresh(v)}

    # R-B1 Phase 3a: Cooldown-State symbol-kanonisch normalisieren. Alte
    # Zahlen-Keys ("6408") und neue Symbol-Keys ("AAPL") werden beide auf
    # Symbol gemappt → alte persistierte Cooldowns bleiben gueltig, neue
    # Writes nutzen Symbol. Keine Migration noetig.
    _canon_cooldown = {}
    for _k, _v in cooldown_state.items():
        _ck = _canon_instrument_key(_k)
        # Bei Kollision (sollte nicht vorkommen): juengsten Eintrag behalten
        if _ck not in _canon_cooldown:
            _canon_cooldown[_ck] = _v

    def _in_cooldown(ref) -> tuple[bool, str]:
        # ref kann Symbol oder etoro_id sein -> kanonisch nachschlagen
        rec = _canon_cooldown.get(_canon_instrument_key(ref))
        if not rec:
            return False, ""
        dt = _parse_iso_safe(rec.get("last_attempt"))
        if dt is None:
            return False, ""  # defensiv: unparseable -> kein Cooldown-Block
        elapsed_cycles = (datetime.now() - dt).total_seconds() / 300
        if elapsed_cycles < cooldown_cycles:
            return True, f"cooldown {elapsed_cycles:.1f}/{cooldown_cycles} cycles, {rec.get('attempts',1)} prev attempts"
        return False, ""

    # v37ds: Post-Close-Cooldown — True wenn das Symbol innerhalb der letzten
    # post_close_cooldown_hours (trailing/SL/TP/time-stop/tranche) geschlossen
    # wurde. Quelle = trade_history (hat ALLE Closes, kein Instrumentieren noetig).
    _CLOSE_ACTION_KEYS = ("CLOSE", "STOP_LOSS", "TAKE_PROFIT", "TIME_STOP", "TRAILING")
    def _recently_closed(symbol) -> tuple[bool, str]:
        if not symbol or post_close_cooldown_hours <= 0:
            return False, ""
        try:
            hist = load_json("trade_history.json") or []
            threshold_sec = post_close_cooldown_hours * 3600
            canon = _canon_instrument_key(symbol)
            for t in reversed(hist[-400:]):  # juengste zuerst
                if not isinstance(t, dict):
                    continue
                if _canon_instrument_key(t.get("symbol") or "") != canon:
                    continue
                act = str(t.get("action", "")).upper()
                if not any(k in act for k in _CLOSE_ACTION_KEYS):
                    continue
                dt = _parse_iso_safe(t.get("timestamp"))
                if dt is None:
                    return False, ""
                age_sec = (datetime.now() - dt).total_seconds()
                if age_sec < threshold_sec:
                    return True, f"post-close cooldown {age_sec/3600:.1f}/{post_close_cooldown_hours}h"
                return False, ""  # juengster Close ist aelter als das Fenster
            return False, ""
        except Exception:
            return False, ""  # defensiv: nie den Cycle wegen Cooldown-Check killen

    buy_candidates = [r for r in scan_results
                      if r["signal"] in ("BUY", "STRONG_BUY")
                      and r["score"] >= min_score
                      and r["etoro_id"] not in existing_ids
                      and r.get("symbol") not in existing_symbols]  # v37dg

    # Cooldown-Filter
    blocked_by_cooldown = []
    filtered = []
    for c in buy_candidates:
        # R-B1 Phase 3a: symbol-kanonisch (candidate["symbol"] bevorzugt)
        in_cd, reason = _in_cooldown(c.get("symbol") or c["etoro_id"])
        if not in_cd:
            # v37ds: zusaetzlich Post-Close-Cooldown (gerade geschlossen -> nicht
            # sofort zurueck-churnen)
            in_cd, reason = _recently_closed(c.get("symbol"))
        if in_cd:
            blocked_by_cooldown.append((c["symbol"], reason))
        else:
            filtered.append(c)
    if blocked_by_cooldown:
        for sym, r in blocked_by_cooldown:
            log.info(f"  COOLDOWN-SKIP {sym}: {r}")
    buy_candidates = filtered

    # R-A10 (Sprint-Tag-9, 19.05.2026): Symbol-Konzentrations-Hard-Cap.
    # Defense-in-Depth zusaetzlich zu existing_symbols-Filter (Z.1763).
    # Anlass: 18.05.2026 5x SCANNER_BUY OIL trotz existing_symbols-Filter
    # (Cache-Timing zwischen Cycles + Filter nur einmal pre-Loop gebaut).
    # Symbol-Konzentrations-Check greift unabhaengig vom Symbol-Filter und
    # auch innerhalb des Loops (incremental Update von local_same_symbol_count).
    if rm:
        try:
            sc_cfg = (config.get("risk_management", {})
                            .get("symbol_concentration", {}) or {})
            if sc_cfg.get("enabled", True):
                # Lokaler Counter fuer Within-Cycle-Buys (verhindert dass
                # 2 Candidates fuer dasselbe Symbol im selben Cycle beide
                # durchgehen weil parsed_positions noch nicht aktualisiert).
                within_cycle_buys = {}  # symbol -> count
                conc_filtered = []
                # R-B11/R3 (07.06.2026): konservativer Schaetzbetrag statt 0 —
                # sonst ist der %-Cap (max_exposure_per_symbol_pct) wirkungslos
                # (future_exposure = same_symbol_invested + 0). Der echte Betrag
                # steht hier noch nicht fest (Sizing erst nach dem Filter); wir
                # schaetzen eine Position so gross wie der Single-Position-Cap
                # (konservativ -> blockt eher als zu spaet).
                _sc_rm_cfg = config.get("risk_management", {})
                _est_single_pct = _sc_rm_cfg.get("max_single_position_pct", 10)
                est_amount = total_value * (_est_single_pct / 100.0)
                for c in buy_candidates:
                    cand_sym = c.get("symbol")
                    # Simuliere "wie wenn dieser Symbol bereits eine
                    # within-cycle pending Pos haette" durch ergaenzten
                    # existing_positions-Snapshot
                    sim_positions = list(parsed_positions)
                    for sim_sym, sim_count in within_cycle_buys.items():
                        for _ in range(sim_count):
                            sim_positions.append({"symbol": sim_sym, "invested": est_amount})
                    allowed, reason = rm.check_symbol_concentration(
                        cand_sym, est_amount, sim_positions, total_value, config)
                    if not allowed:
                        log.info(f"  CONCENTRATION-BLOCK {cand_sym}: {reason}")
                        count, push_triggered = rm.record_concentration_block(
                            cand_sym, reason, config)
                        if push_triggered:
                            log.warning(
                                f"  CONCENTRATION-PUSHOVER {cand_sym}: "
                                f"{count} Blocks heute (Threshold erreicht)")
                    else:
                        conc_filtered.append(c)
                        within_cycle_buys[cand_sym] = within_cycle_buys.get(cand_sym, 0) + 1
                buy_candidates = conc_filtered
        except Exception as e:
            log.warning(f"Symbol-Concentration-Filter Exception (non-fatal, "
                        f"laesst Buys durch): {e}")

    # v36 — Insider-Signal-Filter (CEOWatcher-Equivalent via Finnhub)
    # Blockt Buys wenn Insider verkaufen (-2 Score). Filter ist via
    # config.scanner.insider_signal_enabled aktiviert. Fix fuer ROKU
    # (Insider-Score -2) das am 27.04. 67x gekauft werden sollte.
    #
    # v37m — Shadow-Mode (Forward-A/B): wenn Filter DEAKTIVIERT ist, wird
    # der Insider-Score trotzdem fuer jedes Candidate berechnet und in
    # data/insider_shadow_log.jsonl persistiert. Ueber 2-4 Wochen Paper-
    # Trading laesst sich so vergleichen ob die "geblockten" Candidates
    # schlechter performt haetten als die durchgelassenen — echtes A/B
    # mit Live-Daten ohne Backtest-Datenbedarf (siehe E5b in Roadmap).
    try:
        from app import insider_signals
        scanner_cfg = config.get("scanner", {}) or {}
        insider_min_score = scanner_cfg.get("insider_min_score", -1)
        quality_filter = scanner_cfg.get("insider_quality_filter", True)
        detect_novelty = scanner_cfg.get("insider_detect_novelty", True)
        shadow_enabled = scanner_cfg.get("insider_shadow_tracking", True)

        if insider_signals.is_enabled(config):
            # AKTIV: filtern + Score-Bonus
            insider_filtered = []
            blocked_by_insider = []
            for c in buy_candidates:
                try:
                    iscore = insider_signals.compute_insider_score(
                        c["symbol"],
                        quality_filter=quality_filter,
                        detect_novelty=detect_novelty,
                    )
                    if iscore < insider_min_score:
                        blocked_by_insider.append((c["symbol"], iscore))
                        continue
                    # Score als Bonus auf Scanner-Score (-2..+5 → bis +10 Bonus)
                    if iscore != 0:
                        original = c["score"]
                        bonus = iscore * 2.0  # Skaliert: -2 Insider → -4 Score
                        c["score"] = round(original + bonus, 1)
                        log.info(f"  INSIDER {c['symbol']}: insider={iscore:+d} -> "
                                 f"score {original:.1f} -> {c['score']:.1f}")
                    insider_filtered.append(c)
                except Exception as ie:
                    log.debug(f"Insider-Score {c['symbol']} fehlgeschlagen: {ie}")
                    insider_filtered.append(c)  # bei Fehler durchlassen

            for sym, sc in blocked_by_insider:
                log.info(f"  INSIDER-BLOCK {sym}: insider_score={sc:+d} "
                         f"(min={insider_min_score})")
            buy_candidates = [c for c in insider_filtered if c["score"] >= min_score]
            buy_candidates.sort(key=lambda x: x["score"], reverse=True)

        elif shadow_enabled and buy_candidates:
            # SHADOW: passiv tracken was geblockt WUERDE — kein Eingriff
            from app.insider_shadow import log_shadow_decision
            shadow_count = 0
            for c in buy_candidates:
                try:
                    iscore = insider_signals.compute_insider_score(
                        c["symbol"],
                        quality_filter=quality_filter,
                        detect_novelty=detect_novelty,
                    )
                    would_block = iscore < insider_min_score
                    log_shadow_decision(
                        symbol=c["symbol"],
                        scanner_score=c.get("score", 0),
                        insider_score=iscore,
                        would_block=would_block,
                        insider_min_score=insider_min_score,
                    )
                    shadow_count += 1
                except Exception as ie:
                    log.debug(f"Shadow-Insider {c['symbol']} fehlgeschlagen: {ie}")
            if shadow_count > 0:
                log.info(f"  INSIDER-SHADOW: {shadow_count} Candidates getrackt "
                         f"(Filter inaktiv — Forward-A/B)")
    except ImportError:
        pass
    except Exception as e:
        log.warning(f"Insider-Filter/Shadow Fehler (non-fatal): {e}", exc_info=True)

    # ML Confidence: multiply scanner score by ML probability if enabled
    use_ml = dt_config.get("use_ml_scoring", False)
    if use_ml:
        try:
            from app.ml_scorer import (
                is_model_trained, predict_score, load_persisted_model,
                get_tuned_threshold,
            )
            if not is_model_trained():
                load_persisted_model()
            if is_model_trained():
                # Option B: Bonus/Malus um den F1-getunten Threshold zentrieren
                # (statt um fixe 0.5), damit der sichere Default des Trainings
                # auch die Buy-Entscheidung kalibriert.
                ml_threshold = get_tuned_threshold()
                for cand in buy_candidates:
                    analysis = cand.get("analysis", {})
                    ml_prob = predict_score({
                        "scanner_score": cand["score"],
                        "rsi": analysis.get("rsi", 50),
                        "macd_hist": analysis.get("macd_histogram", 0),
                        "volume_trend": analysis.get("volume_trend", 1.0),
                        "volatility": analysis.get("volatility", 5.0),
                        "momentum_5d": analysis.get("momentum_5d", 0),
                        "momentum_20d": analysis.get("momentum_20d", 0),
                        "bollinger_pos": analysis.get("bollinger_pos", 0.5),
                    })
                    if ml_prob is not None:
                        original = cand["score"]
                        # Additive Anpassung um den tuned Threshold:
                        # prob == threshold -> 0 Bonus (neutral)
                        # prob == threshold+0.3 -> +15 Bonus
                        # prob == threshold-0.3 -> -15 Malus
                        ml_bonus = round((ml_prob - ml_threshold) * 50, 1)
                        cand["score"] = round(original + ml_bonus, 1)
                        log.info(f"  ML Score: {cand['symbol']} "
                                 f"{original:.1f} + {ml_bonus:+.1f} = {cand['score']:.1f} "
                                 f"(prob={ml_prob:.2f}, t={ml_threshold:.2f})")
                # Re-sort and re-filter after ML adjustment
                buy_candidates = [c for c in buy_candidates if c["score"] >= min_score]
                buy_candidates.sort(key=lambda x: x["score"], reverse=True)
            else:
                log.debug("  ML Scoring: Model nicht trainiert, verwende fixe Scores")
        except Exception as e:
            log.debug(f"  ML Scoring nicht verfuegbar: {e}")

    # R-B11/E4: ein SCANNER_SELL gibt den Slot erst frei, wenn er FILLED ist
    # (executed/partial) — nicht schon bei reiner Submission. Sonst oeffnet der
    # Bot einen neuen Buy auf einem noch nicht freien Slot -> kurzzeitig ueber
    # max_positions, wenn der Sell nicht fuellt.
    freed_slots = [t for t in trades_executed
                   if t.get("action") == "SCANNER_SELL"
                   and t.get("status") in ("executed", "partial")]
    available_slots = max_positions - len(positions) + len(freed_slots)
    if available_slots <= 0 or effective_cash < 100:
        log.info(f"  Keine Slots oder Cash fuer neue Trades")
    else:
        top_buys = buy_candidates[:min(available_slots, 5)]
        if top_buys:
            total_score = sum(max(b["score"], 1) for b in top_buys)
            # v15: Budget nutzt effective_cash (bei aktivem DCA gestaffelt).
            budget = min(effective_cash * 0.7, max_trade * len(top_buys))

            for candidate in top_buys:
                if effective_cash < 100:
                    break

                symbol = candidate["symbol"]
                asset_class = candidate["class"]
                analysis = candidate.get("analysis", {})

                # v37cw: Pre-Submit Market-Hours Guard pro Asset-Klasse.
                # Verhindert Episode 05.05.2026 wo Bot um 04:03 UTC (=06:03 CEST,
                # =00:03 ET, weit vor Pre-Market) AAPL+TSLA Limit-Orders submittete
                # die ueber Nacht pending lagen → MISSED_FILL-Alerts.
                try:
                    from app.asset_classes import is_asset_class_tradeable as _is_tradeable
                    if not _is_tradeable(asset_class):
                        log.info(f"  SKIP {symbol}: Markt geschlossen fuer Klasse '{asset_class}' "
                                 f"— SCANNER_BUY uebersprungen (kein Pre-/Post-Market-Trading)")
                        continue
                except Exception as _e:
                    log.debug(f"Market-Hours-Pre-Check fuer {symbol} fehlgeschlagen (non-fatal): {_e}")

                # Asset Filter Check
                if af:
                    allowed, filter_reasons = af.apply_asset_filters(
                        symbol, asset_class, analysis, config)
                    if not allowed:
                        log.info(f"  SKIP {symbol}: {'; '.join(filter_reasons)}")
                        continue

                # Earnings Blackout Check (Aktien)
                if asset_class == "stocks" and ec:
                    blackout, blackout_reason = ec.is_earnings_blackout(symbol, config)
                    if blackout:
                        log.info(f"  EARNINGS BLACKOUT: {symbol} — {blackout_reason}")
                        continue

                # Sentiment Filter
                mc_config = config.get("market_context", {})
                if sent and mc_config.get("use_sentiment_filter", False):
                    sent_result = sent.get_sentiment(symbol)
                    sent_threshold = mc_config.get("sentiment_block_threshold", -0.5)
                    if sent_result["score"] < sent_threshold:
                        log.info(f"  NEGATIVE SENTIMENT: {symbol} "
                                 f"(Score={sent_result['score']:+.2f}, "
                                 f"Threshold={sent_threshold})")
                        continue

                # Earnings Surprise Score Adjustment
                if asset_class == "stocks" and ec:
                    from app.events_calendar import adjust_score_for_earnings
                    adjusted_score = adjust_score_for_earnings(symbol, candidate["score"])
                    if adjusted_score != candidate["score"]:
                        log.info(f"  Earnings-Anpassung {symbol}: "
                                 f"{candidate['score']:+.1f} -> {adjusted_score:+.1f}")
                        candidate["score"] = adjusted_score
                        if candidate["score"] < min_score:
                            log.info(f"  SKIP {symbol}: Score nach Earnings-Anpassung "
                                     f"unter Minimum ({candidate['score']:.1f} < {min_score})")
                            continue

                # Hedging: Defensive Sektoren bevorzugen
                if hdg and hedge_result.get("hedge_needed"):
                    candidate_sector = analysis.get("sector", "")
                    if candidate_sector and not hdg.is_defensive_sector(candidate_sector, config):
                        # Non-defensive sectors get extra reduction in bear regime
                        log.info(f"  Hedging: {symbol} nicht-defensiver Sektor '{candidate_sector}'")

                # Betrag nach Score-Gewichtung
                weight = max(candidate["score"], 1) / total_score
                # v15: Cap pro Trade via effective_cash (DCA) und max_trade (Prozent-Sizing).
                amount = round(min(budget * weight, max_trade, effective_cash * 0.3), 2)

                # Market Context Multiplikator
                amount = round(amount * ctx_multiplier, 2)

                # Konzentrations-Penalty: Reduziere Positionsgroesse bei hoher Konzentration
                risk_cfg_conc = config.get("risk_management", {})
                if risk_cfg_conc.get("concentration_penalty_enabled", False) and rm:
                    conc_threshold = risk_cfg_conc.get("concentration_threshold", 70)
                    conc_reduction = risk_cfg_conc.get("concentration_size_reduction", 0.7)
                    conc_score = rm.get_portfolio_concentration_score(parsed_positions, config)
                    if conc_score > conc_threshold:
                        amount = round(amount * conc_reduction, 2)
                        log.info(f"  Konzentrations-Penalty: Score {conc_score:.0f} > {conc_threshold} "
                                 f"-> Positionsgroesse x{conc_reduction}")

                # Asset-spezifische Groessen-Anpassung
                if af:
                    amount = round(amount * af.get_position_size_adjustment(symbol, asset_class), 2)

                # Leverage berechnen
                volatility = analysis.get("volatility", 3)
                brain_state = load_json("brain_state.json") or {}
                market_regime = brain_state.get("market_regime", "unknown")

                if lm:
                    leverage = lm.calculate_optimal_leverage(
                        asset_class, symbol, volatility, candidate["score"],
                        market_regime,
                        mc.get_current_context().get("vix_level") if mc else None,
                        config)
                else:
                    leverage = 1
                    if asset_class == "forex":
                        leverage = 2
                    elif asset_class == "indices":
                        leverage = 2

                # Risk Manager: Position Sizing
                if rm:
                    max_size = rm.calculate_leveraged_position_size(
                        total_value, stop_loss_pct, leverage, config)
                    amount = min(amount, max_size)

                # v12: Kelly Sizing (bevorzugt) oder Dynamic Sizing
                if rm and config.get("kelly_sizing", {}).get("enabled", False):
                    kelly_size = rm.calculate_kelly_position_size(
                        total_value, stop_loss_pct, candidate["score"], config)
                    amount = min(amount, kelly_size)
                elif rm and config.get("risk_management", {}).get("dynamic_sizing_enabled", False):
                    dynamic_size = rm.calculate_dynamic_position_size(
                        total_value, stop_loss_pct, candidate["score"], config)
                    amount = min(amount, dynamic_size)

                # Recovery Mode Restrictions
                if recovery_active:
                    amount = round(amount * recovery_restrictions.get("position_size_multiplier", 0.5), 2)
                    max_lev = recovery_restrictions.get("max_leverage", 1)
                    if leverage > max_lev:
                        leverage = max_lev

                # Risk Manager: Pre-Trade Validation
                if rm:
                    # Enrich positions with asset_class + sector for correlation check
                    enriched = []
                    for p in parsed_positions:
                        ep = dict(p)
                        ep["asset_class"] = _lookup_asset_class(p["instrument_id"])
                        ep["sector"] = _lookup_sector(p["instrument_id"])
                        enriched.append(ep)

                    # Sektor des neuen Kandidaten
                    candidate_sector = analysis.get("sector", "") or _lookup_sector(candidate["etoro_id"])

                    allowed, reasons = rm.validate_trade(
                        total_value, amount, leverage, asset_class,
                        enriched, stop_loss_pct, config, sector=candidate_sector)
                    if not allowed:
                        log.info(f"  RISK BLOCK {symbol}: {'; '.join(reasons)}")
                        continue

                if amount < 50:
                    continue

                # Kosten-Filter: Trade muss nach Kosten profitabel sein
                try:
                    from app.optimizer import calculate_min_expected_return, get_asset_class_params
                    min_return = config.get("min_expected_return_pct", 0)
                    if min_return <= 0:
                        min_return = calculate_min_expected_return()
                    tp_check = dt_config.get("take_profit_pct", 5)
                    # Asset-Klassen-spezifische SL/TP
                    ac_params = get_asset_class_params(config)
                    if asset_class in ac_params:
                        ac = ac_params[asset_class]
                        stop_loss_pct = ac.get("sl_pct", stop_loss_pct)
                        tp_check = ac.get("tp_pct", tp_check)
                    # Erwarteter Return = WinRate * TP - (1-WinRate) * |SL|
                    # Vereinfacht: TP muss > min_return sein
                    if tp_check < min_return:
                        log.info(f"  SKIP {symbol}: TP {tp_check}% < min {min_return}% (Kosten-Filter)")
                        continue
                except ImportError:
                    pass

                # Risk/Reward Check
                if lm:
                    entry_price = analysis.get("price", 0)
                    if entry_price > 0:
                        sl_price = entry_price * (1 + stop_loss_pct / 100)
                        tp_price = entry_price * (1 + dt_config.get("take_profit_pct", 5) / 100)
                        rr_ok, rr_ratio, rr_reason = lm.check_risk_reward(
                            entry_price, sl_price, tp_price,
                            config.get("leverage", {}).get("min_risk_reward_ratio", 2.0))
                        if not rr_ok:
                            log.info(f"  SKIP {symbol}: {rr_reason}")
                            continue

                # v12: Meta-Labeling Gate (Shadow- oder Live-Mode)
                ml_decision = None
                try:
                    from app import meta_labeler
                    signal_ctx = {
                        "scanner_score": candidate["score"],
                        "rsi": analysis.get("rsi"),
                        "macd_hist": analysis.get("macd_histogram"),
                        "momentum_5d": analysis.get("momentum_5d"),
                        "momentum_20d": analysis.get("momentum_20d"),
                        "volatility": volatility,
                        "volume_trend": analysis.get("volume_trend"),
                        "market_regime": market_regime,
                        "vix_level": mc.get_current_context().get("vix_level") if mc else None,
                        "fear_greed": mc.get_current_context().get("fear_greed_index") if mc else None,
                        "sector": analysis.get("sector") or _lookup_sector(candidate["etoro_id"]),
                        "asset_class": asset_class,
                    }
                    ml_decision = meta_labeler.meta_predict(signal_ctx, config)
                    if ml_decision["decision"] == "skip":
                        log.info(f"  META-SKIP {symbol}: {ml_decision['reason']}")
                        continue
                    log.info(f"  META: {ml_decision['reason']} -> "
                             f"{ml_decision['decision']}")
                except Exception as e:
                    log.debug(f"Meta-Labeler exception (non-fatal): {e}")

                log.info(f"  SCANNER BUY: {symbol} ({asset_class}) "
                         f"${amount:,.2f} {leverage}x "
                         f"(Score={candidate['score']:+.1f}, {candidate['signal']})")

                start_time = time.time()
                result = client.buy(candidate["etoro_id"], amount, leverage=leverage)

                if ex and result:
                    ex.track_execution(
                        analysis.get("price"), result, candidate["etoro_id"],
                        "SCANNER_BUY", amount, asset_class, start_time)

                if result:
                    order = result.get("orderForOpen", {})
                    # v37q: position_id fuer Meta-Labeler-Match (IBKR conId / eToro orderID)
                    contract_info = result.get("_contract") or {}
                    pid = contract_info.get("conId") or order.get("orderID")
                    trade_entry = {
                        "timestamp": datetime.now().isoformat(),
                        "action": "SCANNER_BUY",
                        "symbol": symbol,
                        "name": candidate["name"],
                        "instrument_id": candidate["etoro_id"],
                        "position_id": str(pid) if pid is not None else None,
                        "asset_class": asset_class,
                        "amount_usd": amount,
                        "leverage": leverage,
                        "order_id": order.get("orderID"),
                        "scanner_score": candidate["score"],
                        "signal": candidate["signal"],
                        "rsi": analysis.get("rsi"),
                        "momentum_5d": analysis.get("momentum_5d"),
                        "volatility": volatility,
                        "market_regime": market_regime,
                        "vix_level": mc.get_current_context().get("vix_level") if mc else None,
                        "ctx_multiplier": ctx_multiplier,
                        "status": _trade_status_from_result(result),  # v37dh
                        "ibkr_status_raw": _ibkr_status_raw_from_result(result),
                    }

                    if lm:
                        trade_entry = lm.log_leverage_trade(trade_entry, total_value)

                    save_trade(_attach_fill_prices(trade_entry, result))
                    trades_executed.append(trade_entry)
                    # R-B11/E5: nur dekrementieren wenn der Buy tatsaechlich Kapital
                    # gebunden hat (executed/partial/submitted), NICHT bei rejected/
                    # cancelled/failed — sonst "verbraucht" der Bot Budget, das real
                    # noch da ist, und unterinvestiert Folge-Kandidaten im Cycle.
                    if trade_entry.get("status") in ("executed", "partial", "submitted"):
                        credit -= amount
                        effective_cash -= amount

                    # v36 — Cooldown-State updaten: jeder Buy wird notiert,
                    # damit das Symbol fuer cooldown_cycles nicht erneut
                    # gekauft wird (auch wenn Broker keine Position bestaetigt).
                    # R-B1 Phase 3a: symbol-kanonischer Key (statt str(etoro_id)).
                    # Discovery-Assets bekommen so eindeutige Cooldowns (vorher
                    # haetten alle etoro_id=-1 denselben Key "-1" geteilt).
                    # _in_cooldown normalisiert alte Zahlen-Keys mit → Kompat.
                    sym_key = _canon_instrument_key(candidate.get("symbol") or candidate["etoro_id"])
                    prev = cooldown_state.get(sym_key, {})
                    cooldown_state[sym_key] = {
                        "symbol": symbol,
                        "last_attempt": datetime.now().isoformat(),
                        "attempts": prev.get("attempts", 0) + 1,
                        "last_amount": amount,
                    }

                    # v12: Meta-Labeler Shadow-Log (matched later via position_id)
                    if ml_decision is not None and ml_decision.get("p_win") is not None:
                        try:
                            from app import meta_labeler
                            # R-A42 (24.05.2026 Sprint-Tag-13): position_id
                            # korrekt aus IBKR-Order erfassen. Vorher las
                            # der Code nur eToro-Field-Namen ("positionID",
                            # "positionId") -> bei IBKR null. Resultat:
                            # alle Live-Shadow-Decisions hatten position_id=
                            # null -> R-A41 outcome-Match konnte nie via
                            # position_id matchen (nur Symbol+Time-Fallback).
                            # Fix: Cascade-Lookup mit IBKR-Fields prioritaer.
                            # Bei IBKR ist orderID einzigartig pro Order
                            # (kein conId-Reuse-Problem wie R-A23).
                            position_id = (
                                order.get("orderID")           # IBKR primary
                                or order.get("orderId")        # IBKR snake_case
                                or order.get("permID")         # IBKR persistent ID
                                or order.get("permId")         # IBKR snake_case
                                or order.get("positionID")     # eToro Legacy
                                or order.get("positionId")
                            )
                            meta_labeler.log_shadow_decision({
                                "timestamp": datetime.now().isoformat(),
                                "position_id": position_id,
                                "order_id": order.get("orderID") or order.get("orderId"),
                                "instrument_id": candidate["etoro_id"],
                                "symbol": symbol,
                                "p_win": ml_decision["p_win"],
                                "threshold": ml_decision.get("threshold"),
                                "decision": ml_decision["decision"],
                                "shadow_mode": ml_decision.get("shadow_mode", True),
                                "scanner_score": candidate["score"],
                                "market_regime": market_regime,
                            })
                        except Exception as e:
                            log.debug(f"Shadow-Log fehlgeschlagen: {e}")

                    if al:
                        al.alert_trade_executed(trade_entry)

    # v15: DCA-Budget konsumieren — einmal pro Scheduler-Zyklus dekrementieren.
    # Der Spent-Betrag summiert alle SCANNER_BUY dieses Zyklus (SELL-Trades
    # geben Cash frei und zaehlen nicht gegen die Staffel).
    if rm and dca_info and dca_info.get("dca_active"):
        spent_this_cycle = sum(
            t.get("amount_usd", 0) for t in trades_executed
            if t.get("action") == "SCANNER_BUY"
        )
        try:
            rm.consume_dca_budget(spent_this_cycle)
        except Exception as e:
            log.warning(f"DCA-Budget Update fehlgeschlagen: {e}", exc_info=True)

    # v36 — Cooldown-State persistieren
    try:
        from app.config_manager import save_json as _save_json
        _save_json("buy_cooldown.json", cooldown_state)
    except Exception as e:
        log.warning(f"Cooldown-State save fehlgeschlagen: {e}")

    log.info(f"\n  Scanner-Trades: {len(trades_executed)} ausgefuehrt")
    return trades_executed


def _resolve_meta_for_id(instrument_id):
    """v36f: Universal-Lookup eToro-ID ODER IBKR-conId -> ASSET_UNIVERSE meta.

    Risk/Sector-Lookup kannte vorher nur eToro-IDs. Mit IBKR-Migration
    haben Positionen jetzt conIds (z.B. 290651477 fuer ROKU). Wir
    konsultieren ibkr_contract_cache.json fuer Reverse-Lookup conId
    -> etoro_id, dann ASSET_UNIVERSE.
    """
    try:
        from app.market_scanner import ASSET_UNIVERSE
    except ImportError:
        return None
    iid = int(instrument_id) if instrument_id is not None else None
    if iid is None:
        return None
    # Direkt: etoro_id matcht
    for symbol, info in ASSET_UNIVERSE.items():
        # R-A46 (25.05.2026 Sprint-Tag-14): defensive gegen etoro_id=None.
        # Python-gotcha: dict.get(key, default) returnt None wenn Key existiert
        # mit Value None — NICHT den Default. R-A45 hat Discovery-Backfill mit
        # etoro_id=None gespeichert (SPOT/MRVL/STX/MU haben keine eToro-ID).
        # Hier int(None) -> TypeError (67 Events in 6h Sentry-Spam).
        # Fix: ".get(key) or -1" Pattern (None/0/Falsy -> -1).
        if int(info.get("etoro_id") or -1) == iid:
            return info
    # IBKR-conId Reverse-Lookup
    try:
        from app.config_manager import load_json
        cache = load_json("ibkr_contract_cache.json") or {}
        for etoro_id_str, entry in cache.items():
            if isinstance(entry, dict) and int(entry.get("conId", -1)) == iid:
                etoro_id = int(etoro_id_str)
                for symbol, info in ASSET_UNIVERSE.items():
                    # R-A46: defensive vs etoro_id=None (siehe oben)
                    if int(info.get("etoro_id") or -1) == etoro_id:
                        return info
    except Exception:
        pass
    return None


def _lookup_asset_class(instrument_id):
    """Finde Asset-Klasse fuer eine Instrument-ID (eToro-ID oder IBKR-conId)."""
    meta = _resolve_meta_for_id(instrument_id)
    return meta.get("class", "stocks") if meta else "stocks"


def _lookup_sector(instrument_id):
    """Finde Sektor fuer eine Instrument-ID (eToro-ID oder IBKR-conId)."""
    meta = _resolve_meta_for_id(instrument_id)
    return meta.get("sector", "") if meta else ""


# ============================================================
# OVERNIGHT / WEEKEND CHECKS
# ============================================================

def check_overnight_positions(client, config):
    """Pruefe und schliesse ggf. Overnight-Risiko-Positionen."""
    rm = _import_risk_manager()
    if not rm:
        return []

    portfolio = client.get_portfolio()
    if not portfolio:
        return []

    positions = portfolio.get("positions", [])
    parsed = []
    for pos in positions:
        p = EtoroClient.parse_position(pos)
        p["asset_class"] = _lookup_asset_class(p["instrument_id"])
        parsed.append(p)

    # Overnight Risk
    to_close = rm.check_overnight_risk(parsed, config)

    # Freitag: Weekend-Gebuehren Check
    if datetime.now().weekday() == 4:
        weekend_close = rm.check_weekend_fee_impact(parsed, config)
        to_close.extend(weekend_close)

    # v37h+2 (Q3-17, 15.05.2026): Market-Hours-Guard analog SL/TP-Loop.
    # Verhindert STOP_LOSS_CLOSE_FAILED-Spam wenn Bot waehrend off-hours
    # eine Position schliessen will (kein Live-Quote -> close_position
    # returnt None -> _log_close_failure -> trade_history mit FAILED).
    try:
        from app.asset_classes import is_asset_class_tradeable as _is_tradeable
    except ImportError:
        _is_tradeable = None
    overnight_skipped_off_hours: set[str] = set()

    closed = []
    for pos in to_close:
        # Q3-17: Market-Hours-Check vor Close-Attempt
        if _is_tradeable is not None:
            ac = pos.get("asset_class") or _lookup_asset_class(pos.get("instrument_id"))
            if not _is_tradeable(ac):
                if ac not in overnight_skipped_off_hours:
                    log.info(
                        f"  OVERNIGHT_CLOSE: Markt geschlossen fuer Klasse '{ac}' — "
                        "Close-Versuche werden uebersprungen (retry naechster RTH-Open)."
                    )
                    overnight_skipped_off_hours.add(ac)
                continue

        result = _close_position_safe(client, pos["position_id"], pos.get("instrument_id"), "OVERNIGHT_CLOSE")
        if _is_skipped_idempotent(result):
            continue
        if _is_already_closed(result):
            log.info(f"  OVERNIGHT_CLOSE skip {pos.get('instrument_id')}: bei IBKR "
                     f"bereits geschlossen (stale Bot-Cache)")
            continue
        if result:
            trade_entry = {
                "timestamp": datetime.now().isoformat(),
                "action": "OVERNIGHT_CLOSE",
                "symbol": pos.get("symbol"),  # Q3-15: fuer Reports/Filter
                "instrument_id": pos["instrument_id"],
                "position_id": pos["position_id"],
                "pnl_pct": pos.get("pnl_pct", 0),
                "reason": pos.get("reason", "Overnight-Risiko"),
                "status": _trade_status_from_result(result),  # v37dh
                "ibkr_status_raw": _ibkr_status_raw_from_result(result),
            }
            save_trade(_attach_fill_prices(trade_entry, result))
            closed.append(trade_entry)
            log.info(f"  Overnight Close: #{pos['instrument_id']} ({pos.get('reason', '')})")
        else:
            _log_close_failure("OVERNIGHT_CLOSE", pos, None, extra={
                "reason": pos.get("reason", "Overnight-Risiko"),
            })

    return closed


# ============================================================
# HAUPTFUNKTION: TRADING-ZYKLUS (v2)
# ============================================================

def run_trading_cycle():
    """Kompletter Trading-Zyklus mit allen Safety-Checks."""
    log.info("=" * 55)
    log.info("InvestPilot Trading-Zyklus startet...")
    log.info(f"Zeit: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    log.info("=" * 55)

    # v37h+2 (15.05.2026, Q3-7): Periodischer pending_closes-Cleanup als
    # Garbage-Collector — laeuft bei jedem Cycle-Start (alle 5 Min),
    # nicht mehr nur bei Close-Attempts. Verhindert dass stale-Eintraege
    # 23h+ unentdeckt bleiben wenn Bot keine Sell-Signale generiert.
    # Idempotent + non-fatal (eigene try/except in der Funktion).
    try:
        _removed = _cleanup_pending_closes()
        if _removed > 0:
            log.info(f"  pending_closes-GC: {_removed} stale-Eintraege entfernt")
    except Exception as e:
        log.debug(f"pending_closes-GC fehlgeschlagen (non-fatal): {e}")

    config = load_config()
    client = get_broker(config)  # broker-agnostic: 'etoro' (default) oder 'ibkr' aus config.json

    if not client.configured:
        log.error("Broker '%s' nicht konfiguriert!", client.broker_name)
        return
    log.info("Trading-Cycle mit Broker '%s'", client.broker_name)

    # Broker-Health-Check mit Telegram-Alert bei Connection-Lost (state-deduped)
    try:
        from app.alerts import check_broker_health
        if not check_broker_health(client, config):
            log.error("Broker-Healthcheck fehlgeschlagen — Cycle wird uebersprungen")
            return
    except Exception as e:
        log.warning("Broker-Healthcheck nicht verfuegbar: %s", e)

    # Module laden
    rm = _import_risk_manager()
    mc = _import_market_context()
    al = _import_alerts()

    # Heartbeat fuer Watchdog
    if al:
        al.update_heartbeat()

    # Telegram Commands pruefen (Kill Switch etc.)
    if al:
        commands = al.check_telegram_commands(config)
        if commands:
            for cmd in commands:
                if cmd["command"] == "killswitch":
                    log.warning("TELEGRAM KILL SWITCH empfangen!")
                    if rm:
                        result = rm.emergency_close_all(client, "Telegram Kill Switch")
                        al.alert_emergency("Telegram Kill Switch", result.get("closed", 0))
                    return
                elif cmd["command"] == "status":
                    portfolio = client.get_portfolio()
                    if portfolio:
                        credit = portfolio.get("credit", 0)
                        pnl = portfolio.get("unrealizedPnL", 0)
                        al.send_alert(f"Portfolio: ${credit + pnl:,.2f}\nP/L: ${pnl:+,.2f}", "INFO")

    # Brain importieren
    try:
        from app.brain import run_brain_cycle
        brain_available = True
    except ImportError:
        brain_available = False

    # Scanner importieren
    try:
        from app.market_scanner import scan_all_assets
        scanner_available = True
    except ImportError:
        scanner_available = False

    # 0. Risk Manager: Drawdown-Check
    if rm:
        dd_ok, dd_reason = rm.check_drawdown_limits()
        if not dd_ok:
            log.warning(f"TRADING PAUSIERT: {dd_reason}")
            if al:
                risk_state = rm.get_risk_summary()
                al.alert_drawdown(
                    risk_state.get("daily_pnl_pct", 0),
                    risk_state.get("weekly_pnl_pct", 0), dd_reason)
            return

    # 1. Status anzeigen
    show_portfolio_status(client)

    # 2. Market Context aktualisieren (1x pro Stunde)
    if mc:
        ctx = mc.get_current_context()
        last_update = ctx.get("last_update", "")
        try:
            last_dt = datetime.fromisoformat(last_update)
            if (datetime.now() - last_dt).total_seconds() > 3600:
                mc.update_full_context(config)
        except (ValueError, TypeError):
            mc.update_full_context(config)

    # 3. Config neu laden
    config = load_config()

    # 4. Market Scan (alle 6 Zyklen)
    scan_results = None
    dt_config = config.get("demo_trading", {})
    scan_interval = dt_config.get("scan_interval_cycles", 6)

    if scanner_available:
        from app.config_manager import load_json as _lj, save_json as _sj
        scan_state = _lj("scanner_state.json") or {"cycle_count": 0, "last_results": []}
        scan_state["cycle_count"] = scan_state.get("cycle_count", 0) + 1

        # v36 — Stale-Cache-Gate: cached scan-results NUR nutzen wenn jung
        # genug. Fix fuer ROKU-Bug am 27.04. (identische RSI/Momentum ueber
        # hunderte Cycles → veraltete Marktdaten generierten dauerhaft den
        # gleichen Buy-Score 58.8 fuer ROKU).
        max_cache_age_min = dt_config.get("scan_max_cache_age_minutes", 15)
        last_scan_iso = scan_state.get("last_scan")
        cache_too_old = True
        if last_scan_iso:
            try:
                age_min = (datetime.now() - datetime.fromisoformat(last_scan_iso)).total_seconds() / 60
                cache_too_old = age_min >= max_cache_age_min
            except Exception:
                cache_too_old = True

        if (scan_state["cycle_count"] >= scan_interval
                or not scan_state.get("last_results")
                or cache_too_old):
            log.info("\n--- Market Scan wird ausgefuehrt ---")
            # v37cv (04.05.2026): Default-Asset-Klassen auf IBKR-handelbar reduziert.
            # Vorher: alle 6 Klassen (Legacy aus eToro-Zeit). Crypto/Forex/Indices
            # auf IBKR-Paper nicht direkt handelbar -> Order-Errors.
            enabled_classes = dt_config.get("enabled_asset_classes",
                                            ["stocks", "etf", "commodities"])
            scan_results = scan_all_assets(enabled_classes=enabled_classes)
            scan_state["cycle_count"] = 0
            scan_state["last_results"] = scan_results
            scan_state["last_scan"] = datetime.now().isoformat()
            _sj("scanner_state.json", scan_state)
        else:
            log.info(f"\n  Scanner: Gespeicherte Ergebnisse "
                     f"(naechster Scan in {scan_interval - scan_state['cycle_count']} Zyklen, "
                     f"cache {age_min:.0f}min alt)")
            scan_results = scan_state.get("last_results", [])

    # 5. Portfolio aufbauen oder Scanner-Trades
    portfolio = client.get_portfolio()
    if portfolio:
        positions = portfolio.get("positions", [])
        if len(positions) == 0 and not scan_results:
            log.info("\nPortfolio leer - baue initiales Portfolio auf...")
            build_initial_portfolio(client, config)
        elif scan_results:
            execute_scanner_trades(client, config, scan_results)
        else:
            rebalance_portfolio(client, config)

    # 6. Stop-Loss / Take-Profit
    check_stop_loss_take_profit(client, config)

    # R-B11/E6: Broker-seitige Catastrophic-Stops aufraeumen (Orphans nach Closes
    # canceln, nach Partial-Close verkleinern). Dark-ship: no-op solange
    # config.risk_management.catastrophic_stop.enabled=false.
    if hasattr(client, "reconcile_protective_stops"):
        try:
            client.reconcile_protective_stops()
        except Exception as e:
            log.debug(f"E6 reconcile_protective_stops (non-fatal): {e}")

    # 6b. Trailing SL State bereinigen (geschlossene Positionen entfernen)
    lm = _import_leverage_manager()
    if lm:
        portfolio_for_cleanup = client.get_portfolio()
        if portfolio_for_cleanup:
            open_ids = [
                EtoroClient.parse_position(pos)["position_id"]
                for pos in portfolio_for_cleanup.get("positions", [])
            ]
            lm.cleanup_trailing_state(open_ids)

    # 7. Overnight Check (abends)
    now = datetime.now()
    if now.hour >= 21:
        check_overnight_positions(client, config)

    # 8. Margin Safety / Auto-Deleverage
    if rm:
        portfolio = client.get_portfolio()
        if portfolio:
            parsed = [EtoroClient.parse_position(pos) for pos in portfolio.get("positions", [])]
            # v37cd: NetLiq als Total (siehe Audit F3). Bei IBKR-Margin-Konten
            # ist credit+invested NICHT NetLiq (Phantom-Cash-Bug). Verwende
            # _equity (NetLiquidation) wenn vorhanden, sonst Legacy-Fallback.
            equity = portfolio.get("_equity")
            total = equity if equity else (
                portfolio.get("credit", 0) + sum(p["invested"] for p in parsed)
            )
            margin_ok, margin_reason, exposure = rm.check_margin_safety(total, parsed, config)
            if not margin_ok:
                log.warning(f"  MARGIN ALERT: {margin_reason}")
                rm.auto_deleverage(client, parsed, total, config)

    # 9. Finaler Status
    log.info("")
    final_portfolio = show_portfolio_status(client)

    # 10. Brain: Lernen & Optimieren
    if brain_available and final_portfolio:
        report = run_brain_cycle(final_portfolio)
        log.info("")
        log.info("=" * 55)
        log.info("BRAIN ZUSAMMENFASSUNG")
        log.info(f"  Lauf:      #{report.get('total_runs', '?')}")
        log.info(f"  Rendite:   {report.get('total_return_pct', 0):+.2f}%")
        log.info(f"  Win-Rate:  {report.get('win_rate', 0):.1f}%")
        log.info(f"  Sharpe:    {report.get('sharpe_estimate', 0):.2f}")
        log.info(f"  Regime:    {report.get('market_regime', '?')}")
        log.info(f"  Regeln:    {report.get('active_rules', 0)} aktiv")
        log.info("=" * 55)

    # 11. Daily Summary (21:00)
    if al and al.should_send_daily_summary():
        if final_portfolio and rm:
            risk = rm.get_risk_summary()
            # v37cd: NetLiq als Total (siehe Audit F4). Daily-Summary Pushover
            # zeigte bisher credit+invested = falscher Wert bei Margin-Konto.
            equity = final_portfolio.get("_equity")
            if equity:
                total = equity
            else:
                total = final_portfolio.get("credit", 0)
                for pos in final_portfolio.get("positions", []):
                    total += EtoroClient.parse_position(pos)["invested"]
            brain_state = load_json("brain_state.json") or {}
            trades_today = risk.get("daily_trades", 0)
            al.send_daily_summary(
                total, risk["daily_pnl_pct"], risk["daily_pnl_usd"],
                trades_today, brain_state.get("market_regime", "?"))

    # Cloud-Backup nach jedem Zyklus
    try:
        from app.persistence import backup_to_cloud
        backup_to_cloud()
    except Exception as e:
        log.warning(f"Cloud-Backup fehlgeschlagen: {e}")

    log.info("")
    log.info("Trading-Zyklus beendet.")
    log.info("=" * 55)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    run_trading_cycle()
